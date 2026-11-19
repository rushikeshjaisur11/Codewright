"""Planned migrations: carry out an upgrade (Pydantic v1 → v2, SQLAlchemy 1.4 → 2.0, sync → async
I/O...) as a sequence of small, reviewable steps instead of one big-bang change.

A plan:

1. finds every place the migration touches, as the code would be reviewed *at the target
   version* (the same re-matching the upgrade preview uses: nothing is installed);
2. orders the files leaf modules first, so each step only depends on steps already done
   (files that import each other in a cycle move together);
3. cuts the order into steps of a few files, each with its findings, a combined patch for
   the ones that can be made mechanically, the tests that cover those files, and a PR body.

Plans are saved in ``.codewright/migrations/<name>.json`` (commit them, so the team shares
progress). ``status`` re-scans and marks a step done when none of its findings remain, so
progress follows the code, not a checkbox.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import cards as card_store
from . import rules as rules_mod
from .graph import CodeGraph, is_test_file
from .models import Finding, Stack


@dataclass
class Migration:
    name: str
    title: str
    library: str | None  # the dependency whose version moves; None for code-only migrations
    target: str  # version the plan is computed at (when the library is used)
    intents: list[str]
    tools: list[str] = field(default_factory=list)  # official codemods worth running first
    notes: str = ""
    blocking_rule: bool = False  # also count blocking calls in async code (from rules.BLOCKING)


CATALOG: dict[str, Migration] = {
    m.name: m
    for m in [
        Migration(
            "pydantic-v2",
            "Pydantic v1 → v2",
            "pydantic",
            "2.0",
            [
                "pydantic-v1-api",
                "pydantic-v1-validator",
                "pydantic-class-config",
                "pydantic-basesettings-import",
                "pydantic-ignored-config",
            ],
            ["bump-pydantic (`pip install bump-pydantic && bump-pydantic <package>`)"],
            "Run the official bump-pydantic codemod over each step's files first; the plan covers what it leaves.",
        ),
        Migration(
            "sqlalchemy-2",
            "SQLAlchemy 1.x → 2.0",
            "sqlalchemy",
            "2.0",
            ["sqlalchemy-legacy-query", "sqlalchemy-ext-declarative"],
            ["SQLALCHEMY_WARN_20=1 with `python -W always::DeprecationWarning` (1.4 only)"],
            "On 1.4, set `future=True` on the engine and session first; each step then works on both versions.",
        ),
        Migration(
            "fastapi-modern",
            "FastAPI: lifespan and Annotated dependencies",
            "fastapi",
            "0.100",
            ["fastapi-on-event", "fastapi-annotated-params"],
        ),
        Migration(
            "async-io",
            "Blocking calls out of async code",
            None,
            "",
            ["blocking-http-in-async", "blocking-sleep-in-async", "connection-per-request"],
            notes="Each step moves the blocking calls of a few modules to their async equivalents.",
            blocking_rule=True,
        ),
        Migration(
            "utc-datetimes",
            "Naive utcnow() → aware datetimes",
            None,
            "",
            ["datetime-utcnow"],
            notes="datetime.utcnow() is deprecated since Python 3.12.",
        ),
        Migration(
            "google-adk",
            "Google ADK: current APIs",
            "google-adk",
            "",
            ["adk-deprecated-api", "adk-upgrade", "adk-sync-run", "adk-missing-extra", "adk-runner-per-request"],
        ),
    ]
}


@dataclass
class Item:
    key: str  # card:file:function, stable across line moves
    finding_id: str
    title: str
    file: str
    line: int
    autofix: bool
    intent: str

    @staticmethod
    def of(f: Finding) -> Item:
        return Item(f"{f.card_id}:{f.file}:{f.function}", f.id, f.title, f.file, f.line, bool(f.patch), f.intent)


@dataclass
class Step:
    n: int
    files: list[str]
    items: list[Item]
    tests: list[str]
    depends_on: list[int]
    done: bool = False
    remaining: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["autofix"] = sum(1 for i in self.items if i.autofix)
        return d


def _target_stack(stack: Stack, m: Migration, version: str | None) -> tuple[Stack, str | None]:
    if not m.library or not stack.has(m.library):
        return stack, None
    want = version or m.target
    if not want:
        return stack, None
    from packaging.version import InvalidVersion, Version

    current = stack.version_of(m.library)
    try:
        if current and Version(current) >= Version(want):
            return stack, current
    except InvalidVersion:
        pass
    st = copy.deepcopy(stack)
    st.dependencies[m.library.lower()].version = want
    return st, want


def findings_for(
    m: Migration, graph: CodeGraph, stack: Stack, version: str | None = None
) -> tuple[list[Finding], str | None]:
    """What the migration touches, reviewed at the target version."""
    from .intents import detect
    from .suggest import make_findings

    st, at = _target_stack(stack, m, version)
    matches = detect(graph, only=set(m.intents))
    found = make_findings(matches, st, graph, card_store.for_repo(graph.root))
    found = [f for f in found if f.intent in m.intents]
    if m.blocking_rule:
        rule = rules_mod.Rule("async-io", "no-blocking-in-async", paths=["**"])
        seen = {(f.file, f.line) for f in found}
        for v in rules_mod.check(graph, [rule]):
            if (v.file, v.line) in seen:
                continue
            fid = hashlib.sha1(f"rule:{v.file}:{v.line}:{v.target}".encode()).hexdigest()[:8]
            found.append(
                Finding(
                    fid,
                    "no-blocking-in-async",
                    v.file,
                    v.line,
                    f"line {v.line}",
                    f"Replace blocking `{v.target}()` in async code",
                    "",
                    v.fix,
                    v.target,
                    v.target,
                    "",
                    1,
                    3,
                    "",
                    "",
                    "",
                    [],
                    v.message,
                    card_id=f"rule:{v.target}",
                )
            )
    return found, at


def _order(graph: CodeGraph, files: set[str]) -> list[list[str]]:
    """Groups of files, leaves first: a group imports only files in earlier groups (or nothing affected).
    Files in an import cycle share a group."""
    from .intents.architecture import _sccs

    resolver = graph.resolver
    deps: dict[str, set[str]] = {}
    for f in files:
        pf = graph.files.get(f)
        imported = resolver.imported_files(pf) if resolver and pf else set()
        deps[f] = {d for d in imported if d in files and d != f}
    comp_of = {f: f for f in files}
    for scc in _sccs(set(files), deps):
        lead = sorted(scc)[0]
        for f in scc:
            comp_of[f] = lead
    groups: dict[str, set[str]] = defaultdict(set)
    for f in files:
        groups[comp_of[f]].add(f)
    gdeps = {g: {comp_of[d] for f in members for d in deps[f]} - {g} for g, members in groups.items()}
    done: set[str] = set()
    out: list[list[str]] = []
    while len(done) < len(groups):
        ready = sorted(g for g in groups if g not in done and gdeps[g] <= done)
        if not ready:  # shouldn't happen after collapsing cycles; keep going rather than loop
            ready = sorted(g for g in groups if g not in done)[:1]
        for g in ready:
            out.append(sorted(groups[g]))
            done.add(g)
    return out


def plan(
    name: str,
    root: str | Path,
    graph: CodeGraph,
    stack: Stack,
    *,
    batch: int = 5,
    version: str | None = None,
) -> dict:
    if name not in CATALOG:
        raise ValueError(f"no migration {name!r}; known: {', '.join(CATALOG)}")
    m = CATALOG[name]
    found, at = findings_for(m, graph, stack, version)
    by_file: dict[str, list[Finding]] = defaultdict(list)
    for f in found:
        by_file[f.file].append(f)
    groups = _order(graph, set(by_file))
    steps: list[Step] = []
    current: list[str] = []
    step_of: dict[str, int] = {}

    def flush():
        if not current:
            return
        n = len(steps) + 1
        items = [Item.of(f) for path in current for f in sorted(by_file[path], key=lambda f: f.line)]
        tests = sorted({t for path in current for t in graph.importers_of(path) if is_test_file(t)})
        steps.append(Step(n, list(current), items, tests, []))
        for path in current:
            step_of[path] = n
        current.clear()

    for group in groups:
        if current and len(current) + len(group) > batch:
            flush()
        current.extend(group)
    flush()
    resolver = graph.resolver
    for s in steps:
        need = set()
        for path in s.files:
            pf = graph.files.get(path)
            for d in resolver.imported_files(pf) if resolver and pf else ():
                if d in step_of and step_of[d] != s.n:
                    need.add(step_of[d])
        s.depends_on = sorted(need)
    data = {
        "name": m.name,
        "title": m.title,
        "library": m.library,
        "installed": stack.version_of(m.library) if m.library else None,
        "target": at,
        "tools": m.tools,
        "notes": m.notes,
        "created": time.time(),
        "batch": batch,
        "steps": [s.to_dict() for s in steps],
        "total": sum(len(s.items) for s in steps),
    }
    return data


def path(root: str | Path, name: str) -> Path:
    return Path(root) / ".codewright" / "migrations" / f"{name}.json"


def save(root: str | Path, data: dict) -> Path:
    from .feedback import ensure_dir

    ensure_dir(root)
    p = path(root, data["name"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))
    return p


def load(root: str | Path, name: str) -> dict | None:
    p = path(root, name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


def saved(root: str | Path) -> list[str]:
    d = Path(root) / ".codewright" / "migrations"
    return sorted(p.stem for p in d.glob("*.json")) if d.is_dir() else []


def status(root: str | Path, name: str, graph: CodeGraph, stack: Stack) -> dict:
    """The saved plan with each step marked done when none of its findings remain in the code."""
    data = load(root, name)
    if data is None:
        raise FileNotFoundError(f"no plan for {name}; run `codewright migrate plan {name}` first")
    m = CATALOG.get(name)
    if m is None:
        raise ValueError(f"unknown migration {name}")
    found, _ = findings_for(m, graph, stack, data.get("target"))
    live = {Item.of(f).key: f for f in found}
    done_items = 0
    for s in data["steps"]:
        left = [i for i in s["items"] if i["key"] in live]
        for i in s["items"]:
            f = live.get(i["key"])
            if f is not None:  # keep ids and lines current for applying the patch
                i["finding_id"], i["line"], i["autofix"] = f.id, f.line, bool(f.patch)
        s["remaining"] = len(left)
        s["done"] = not left
        done_items += len(s["items"]) - len(left)
    planned = {i["key"] for s in data["steps"] for i in s["items"]}
    new = [asdict(Item.of(f)) for k, f in sorted(live.items()) if k not in planned]
    nxt = next((s["n"] for s in data["steps"] if not s["done"]), None)
    data |= {
        "done_items": done_items,
        "progress": round(done_items / data["total"], 3) if data["total"] else 1.0,
        "next_step": nxt,
        "new_since_plan": new,
    }
    return data


def step_findings(root: str | Path, name: str, n: int, graph: CodeGraph, stack: Stack) -> tuple[dict, list[Finding]]:
    data = status(root, name, graph, stack)
    step = next((s for s in data["steps"] if s["n"] == n), None)
    if step is None:
        raise ValueError(f"{name} has no step {n} (it has {len(data['steps'])})")
    m = CATALOG[name]
    found, _ = findings_for(m, graph, stack, data.get("target"))
    keys = {i["key"] for i in step["items"]}
    return step, [f for f in found if Item.of(f).key in keys]


MIGRATE_SYSTEM = (
    "You carry out one step of a code migration. You get one function (or a file's import block) and the "
    "changes the migration needs in it, each with the recommended way to make it. Rewrite it so it follows "
    "that advice and behaves exactly as before for its callers: keep its name, arguments and return values "
    "unless the advice says otherwise, keep comments, and change nothing the advice does not ask for."
)


def agent_task(name: str, graph: CodeGraph, findings: list[Finding]) -> tuple[str, str]:
    """(task, context) for the AI agent: the step's changes no detector could patch, module-level ones too."""
    from .agent import task_for_problems

    problems = [
        {
            "file": f.file,
            "line": f.line,
            "function": f.function,
            "symbol": f.symbol or "",
            "detail": f"{f.title}: {f.suggestion}" + (f" Watch out: {f.risk}" if f.risk else ""),
        }
        for f in findings
        if not f.patch
    ]
    return task_for_problems(f"Migration: {CATALOG[name].title}", problems)


def ai_changes(root: str | Path, name: str, graph: CodeGraph, findings: list[Finding]) -> dict:
    """The model writes the changes no detector could patch, one rewrite per function, each checked to
    compile: {patches: [{id, file, function, patch, explanation, fixes}], skipped, model}."""
    from .upgradesafety import rewrite

    def enclosing(f: Finding) -> str:
        """The function around the finding (some checks name the line, not the function)."""
        pf = graph.files.get(f.file)
        if pf is None:
            return f.function
        if any(fn.qualname == f.function for fn in pf.module.functions):
            return f.function
        inside = [fn for fn in pf.module.functions if fn.start_line <= f.line <= fn.end_line]
        return max(inside, key=lambda fn: fn.start_line).qualname if inside else "<module>"

    manual = [f for f in findings if not f.patch]
    for f in manual:
        f.function = enclosing(f)
    problems = [
        {
            "file": f.file,
            "line": f.line,
            "function": f.function,
            "symbol": f.symbol or "",
            "kind": "call",
            "detail": f"{f.title}: {f.suggestion}" + (f" Watch out: {f.risk}" if f.risk else ""),
            "old_signature": None,
            "new_signature": None,
            "new_doc": "",
        }
        for f in manual
        if f.function != "<module>"
    ]
    out = rewrite(
        root, graph, name, None, "", problems, system=MIGRATE_SYSTEM, heading=f"Migration: {CATALOG[name].title}"
    )
    out["skipped"] += [
        {"file": f.file, "function": f.function, "why": "module-level code: change it by hand"}
        for f in manual
        if f.function == "<module>"
    ]
    return out


def combined_patch(root: str | Path, findings: list[Finding]) -> tuple[str, list[str]]:
    """One diff for every auto-patchable finding of a step. Returns (diff, ids whose patch didn't apply on top
    of the others)."""
    from .refactor import combine

    ordered = sorted((f for f in findings if f.patch), key=lambda f: (f.file, -f.line))  # bottom-up per file
    return combine(root, [(f.id, f.patch) for f in ordered])


def pr_body(data: dict, step: dict, patch: str) -> str:
    lines = [
        f"## {data['title']}: step {step['n']} of {len(data['steps'])}",
        "",
        f"Files: {', '.join(f'`{f}`' for f in step['files'])}",
    ]
    if step.get("depends_on"):
        lines.append(f"Builds on step(s) {', '.join(str(d) for d in step['depends_on'])}.")
    lines += ["", "### Changes", ""]
    for i in step["items"]:
        mark = " (auto-patched)" if i["autofix"] and patch else ""
        lines.append(f"- `{i['file']}:{i['line']}` {i['title']}{mark}")
    lines += ["", "### Verify", ""]
    if step["tests"]:
        lines.append("```")
        lines.append("pytest -q " + " ".join(step["tests"]))
        lines.append("```")
    else:
        lines.append("No test imports these files; add one before merging, or test by hand.")
    if data.get("tools"):
        lines += ["", "Tools: " + "; ".join(data["tools"])]
    lines += ["", "_Planned with `codewright migrate`._"]
    return "\n".join(lines) + "\n"


def overview(graph: CodeGraph, stack: Stack, root: str | Path) -> list[dict]:
    """Every catalog migration with how much of the code it touches here, and saved progress."""
    have = set(saved(root))
    out = []
    for m in CATALOG.values():
        if m.library and not stack.has(m.library):
            continue
        found, at = findings_for(m, graph, stack)
        if not found and m.name not in have:
            continue
        out.append(
            {
                "name": m.name,
                "title": m.title,
                "library": m.library,
                "installed": stack.version_of(m.library) if m.library else None,
                "target": at,
                "places": len(found),
                "files": len({f.file for f in found}),
                "autofix": sum(1 for f in found if f.patch),
                "planned": m.name in have,
            }
        )
    return sorted(out, key=lambda r: (-r["places"], r["name"]))
