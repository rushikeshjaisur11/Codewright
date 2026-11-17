"""Architecture rules: decisions the code must keep, checked on every review and in CI.

Rules live in ``.codewright/rules.yaml`` (committed, so the team shares them). The intended
design saved from Design mode (``.codewright/design.json``) adds its own. Four kinds:

``forbid-import``
    Files under ``in`` may not import ``import`` (dotted modules such as ``sqlalchemy`` or
    ``app.db``, or repo paths such as ``app/db/**``). "api/ may not import db/ directly."
``only-in``
    ``import`` may be imported only from files under ``in``. "Only billing/ calls Stripe."
``layers``
    Ordered layers, top first. A layer may import the layers below it, never one above.
``no-blocking-in-async``
    No blocking call (``time.sleep``, ``requests.*``, ``subprocess.run``...) inside ``async def``
    under ``in``.

Each violation names the file, line, rule and why, and a fix that keeps behaviour: the module
to go through instead, where to move the call, or the async API to use. ``infer`` writes the
rules today's code already keeps ("freeze the dependency direction"), so drift fails CI from
then on. Rules with ``severity: warn`` are reported but never fail ``codewright check``.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from .graph import CodeGraph, is_test_file, record_imports, text
from .models import Module

KINDS = ("forbid-import", "only-in", "layers", "no-blocking-in-async")
RULES_FILE = "rules.yaml"

# Blocking call -> what to use inside `async def` instead.
BLOCKING = {
    "time.sleep": "await asyncio.sleep(...)",
    "requests.get": "a shared httpx.AsyncClient (await client.get(...))",
    "requests.post": "a shared httpx.AsyncClient (await client.post(...))",
    "requests.put": "a shared httpx.AsyncClient (await client.put(...))",
    "requests.patch": "a shared httpx.AsyncClient (await client.patch(...))",
    "requests.delete": "a shared httpx.AsyncClient (await client.delete(...))",
    "requests.request": "a shared httpx.AsyncClient (await client.request(...))",
    "urllib.request.urlopen": "a shared httpx.AsyncClient",
    "subprocess.run": "await asyncio.create_subprocess_exec(...)",
    "subprocess.call": "await asyncio.create_subprocess_exec(...)",
    "subprocess.check_call": "await asyncio.create_subprocess_exec(...)",
    "subprocess.check_output": "await asyncio.create_subprocess_exec(..., stdout=PIPE)",
    "os.system": "await asyncio.create_subprocess_shell(...)",
    "socket.create_connection": "await asyncio.open_connection(...)",
    "sqlite3.connect": "aiosqlite, or await asyncio.to_thread(...) around the query",
    "psycopg2.connect": "psycopg's AsyncConnection or asyncpg",
    "input": "a non-interactive value (input() blocks the event loop)",
}


@dataclass
class Rule:
    id: str
    kind: str
    reason: str = ""
    severity: str = "error"  # error | warn
    paths: list[str] = field(default_factory=list)  # `in`
    imports: list[str] = field(default_factory=list)  # `import`
    exclude: list[str] = field(default_factory=list)  # `except`
    layers: list[dict] = field(default_factory=list)  # [{name, paths}]
    calls: list[str] = field(default_factory=list)  # extra blocking calls
    source: str = RULES_FILE

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Violation:
    rule: str
    kind: str
    severity: str
    file: str
    line: int
    message: str
    reason: str
    fix: str
    target: str = ""  # what was imported or called

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- loading


def _as_list(v) -> list[str]:
    if v is None:
        return []
    return [str(x) for x in v] if isinstance(v, list | tuple) else [str(v)]


def parse(raw: dict, source: str = RULES_FILE, n: int = 0) -> Rule:
    kind = str(raw.get("kind", "")).strip()
    if kind not in KINDS:
        raise ValueError(
            f"{source}: rule {raw.get('id') or n + 1} has kind {kind!r}; expected one of {', '.join(KINDS)}"
        )
    rule = Rule(
        id=str(raw.get("id") or f"{kind}-{n + 1}"),
        kind=kind,
        reason=str(raw.get("reason", "")),
        severity="warn" if str(raw.get("severity", "error")).lower() in ("warn", "warning") else "error",
        paths=_as_list(raw.get("in")) or (["**"] if kind in ("forbid-import", "no-blocking-in-async") else []),
        imports=_as_list(raw.get("import")),
        exclude=_as_list(raw.get("except")),
        layers=[{"name": str(x.get("name", "")), "paths": _as_list(x.get("paths"))} for x in raw.get("layers") or []],
        calls=_as_list(raw.get("calls")),
        source=source,
    )
    if kind in ("forbid-import", "only-in") and not rule.imports:
        raise ValueError(f"{source}: rule {rule.id} needs `import`")
    if kind == "only-in" and not rule.paths:
        raise ValueError(f"{source}: rule {rule.id} needs `in` (where the import is allowed)")
    if kind == "layers" and len(rule.layers) < 2:
        raise ValueError(f"{source}: rule {rule.id} needs at least two `layers`")
    return rule


def rules_path(root: str | Path) -> Path:
    return Path(root) / ".codewright" / RULES_FILE


def load(root: str | Path) -> list[Rule]:
    """Rules from .codewright/rules.yaml plus the saved design's `forbid_import` decisions."""
    out: list[Rule] = []
    p = rules_path(root)
    if p.exists():
        data = yaml.safe_load(p.read_text()) or {}
        items = data.get("rules", []) if isinstance(data, dict) else data
        for i, raw in enumerate(items or []):
            out.append(parse(raw, RULES_FILE, i))
    from .design import load_intended

    intent = load_intended(root) or {}
    for i, raw in enumerate(intent.get("rules", [])):
        if raw.get("forbid_import"):
            out.append(
                Rule(
                    id=f"design-forbid-{raw['forbid_import']}",
                    kind="forbid-import",
                    reason=raw.get("reason", ""),
                    paths=["**"],
                    imports=[raw["forbid_import"]],
                    source="design.json",
                )
            )
        elif raw.get("kind") in KINDS:
            out.append(parse(raw, "design.json", i))
    return out


def save(root: str | Path, rules: list[Rule]) -> Path:
    from .feedback import ensure_dir

    ensure_dir(root)
    items = []
    for r in rules:
        d: dict = {"id": r.id, "kind": r.kind}
        if r.kind == "layers":
            d["layers"] = r.layers
        else:
            if r.paths and r.paths != ["**"]:
                d["in"] = r.paths
            if r.imports:
                d["import"] = r.imports
        if r.exclude:
            d["except"] = r.exclude
        if r.calls:
            d["calls"] = r.calls
        if r.reason:
            d["reason"] = r.reason
        if r.severity != "error":
            d["severity"] = r.severity
        items.append(d)
    p = rules_path(root)
    header = (
        "# Architecture rules Codewright checks on every review and in `codewright check` (CI).\n"
        "# Kinds: forbid-import, only-in, layers, no-blocking-in-async. See `codewright rules --help`.\n"
    )
    p.write_text(header + yaml.safe_dump({"rules": items}, sort_keys=False, width=110))
    return p


# --------------------------------------------------------------------------- matching


_GLOB_CACHE: dict[str, re.Pattern] = {}


def _glob(pattern: str) -> re.Pattern:
    """`**` crosses folders, `*` doesn't; a bare folder (``app/api``) means everything under it."""
    if pattern not in _GLOB_CACHE:
        p = pattern.strip().strip("/")
        if not any(c in p for c in "*?[") and not p.endswith(".py"):
            p += "/**"
        out, i = "", 0
        while i < len(p):
            if p.startswith("**/", i):
                out, i = out + "(?:.*/)?", i + 3
            elif p.startswith("**", i):
                out, i = out + ".*", i + 2
            elif p[i] == "*":
                out, i = out + "[^/]*", i + 1
            elif p[i] == "?":
                out, i = out + "[^/]", i + 1
            else:
                out, i = out + re.escape(p[i]), i + 1
        _GLOB_CACHE[pattern] = re.compile(f"^{out}$")
    return _GLOB_CACHE[pattern]


def path_matches(path: str, patterns: list[str]) -> bool:
    return any(_glob(p).match(path) for p in patterns)


def _module_matches(dotted: str, internal_file: str | None, pattern: str) -> bool:
    """`pattern` is a dotted module prefix (``sqlalchemy``, ``app.db``) or a repo path glob (``app/db/**``)."""
    if "/" in pattern or pattern.endswith(".py") or "*" in pattern:
        return internal_file is not None and path_matches(internal_file, [pattern])
    return dotted == pattern or dotted.startswith(pattern + ".")


@dataclass
class ImportSite:
    file: str
    line: int
    target: str  # absolute dotted name
    internal: str | None  # repo file it resolves to, if any


def import_sites(graph: CodeGraph) -> list[ImportSite]:
    """Every import statement in non-test files, with its line and the repo file it points to."""
    from .archmap import ArchMap, resolve_relative
    from .intents.detectors import walk

    if "rule_import_sites" in graph.__dict__:
        return graph.__dict__["rule_import_sites"]
    amap = ArchMap(graph)
    out: list[ImportSite] = []
    for path, pf in sorted(graph.files.items()):
        if is_test_file(path):
            continue
        for node in walk(pf.root, into_defs=True):
            if node.type not in ("import_statement", "import_from_statement"):
                continue
            tmp = Module(path)
            record_imports(node, tmp)
            ln = node.start_point[0] + 1
            for target in set(tmp.imports.values()):
                full = resolve_relative(target, path)
                mod = amap._internal_target(full)
                internal = amap.modules.get(mod) if mod else None
                if internal == path:
                    continue
                out.append(ImportSite(path, ln, full, internal))
    graph.__dict__["rule_import_sites"] = out
    return out


def _dedupe(sites: list[ImportSite]) -> list[ImportSite]:
    """`import a.b` records both `a` and `a.b`: keep the most specific per file and line."""
    by_line: dict[tuple[str, int], list[ImportSite]] = defaultdict(list)
    for s in sites:
        by_line[(s.file, s.line)].append(s)
    out = []
    for group in by_line.values():
        for s in group:
            if not any(o is not s and o.target.startswith(s.target + ".") for o in group):
                out.append(s)
    return out


# --------------------------------------------------------------------------- checking


def check(graph: CodeGraph, rules: list[Rule]) -> list[Violation]:
    if not rules:
        return []
    sites = import_sites(graph)
    out: list[Violation] = []
    for rule in rules:
        if rule.kind == "forbid-import":
            out += _forbid(rule, sites)
        elif rule.kind == "only-in":
            out += _only_in(rule, sites)
        elif rule.kind == "layers":
            out += _layers(rule, sites)
        else:
            out += _blocking(rule, graph)
    seen, unique = set(), []
    for v in out:
        key = (v.rule, v.file, v.line, v.target)
        if key not in seen:
            seen.add(key)
            unique.append(v)
    return sorted(unique, key=lambda v: (v.severity != "error", v.file, v.line, v.rule))


def _hits(rule: Rule, s: ImportSite) -> str | None:
    return next((p for p in rule.imports if _module_matches(s.target, s.internal, p)), None)


def _go_through(rule: Rule, s: ImportSite, sites: list[ImportSite], pattern: str) -> str:
    """An allowed module that already imports what `s` wants: the way round the rule."""
    allowed = sorted(
        {
            o.file
            for o in sites
            if o.file != s.file
            and _module_matches(o.target, o.internal, pattern)
            and not (path_matches(o.file, rule.paths) and not path_matches(o.file, rule.exclude))
        }
    )
    used_by_me = {o.internal for o in sites if o.file == s.file and o.internal}
    preferred = [a for a in allowed if a in used_by_me] or allowed
    if preferred:
        return (
            f"Go through `{preferred[0]}`, which already uses `{pattern}`: call a function there instead of "
            "importing it here (add one that takes the same arguments if none fits)."
        )
    return f"Move the code that needs `{pattern}` out of `{s.file}` into a module the rule allows, and call that."


def _forbid(rule: Rule, sites: list[ImportSite]) -> list[Violation]:
    out = []
    for s in _dedupe(sites):
        if not path_matches(s.file, rule.paths) or path_matches(s.file, rule.exclude):
            continue
        pattern = _hits(rule, s)
        if pattern is None:
            continue
        out.append(
            Violation(
                rule.id,
                rule.kind,
                rule.severity,
                s.file,
                s.line,
                f"`{s.file}` imports `{s.target}`, which rule `{rule.id}` forbids here",
                rule.reason,
                _go_through(rule, s, sites, pattern),
                s.target,
            )
        )
    return out


def _only_in(rule: Rule, sites: list[ImportSite]) -> list[Violation]:
    out = []
    owners = sorted({s.file for s in sites if path_matches(s.file, rule.paths) and _hits(rule, s)})
    for s in _dedupe(sites):
        pattern = _hits(rule, s)
        if pattern is None or path_matches(s.file, rule.paths) or path_matches(s.file, rule.exclude):
            continue
        where = ", ".join(f"`{p}`" for p in rule.paths)
        fix = (
            f"Call it through `{owners[0]}`, which owns `{pattern}`, instead of importing it here."
            if owners
            else f"Move this use of `{pattern}` into {where} and call that from here."
        )
        out.append(
            Violation(
                rule.id,
                rule.kind,
                rule.severity,
                s.file,
                s.line,
                f"`{s.target}` may only be imported from {where}",
                rule.reason,
                fix,
                s.target,
            )
        )
    return out


def _layer_of(rule: Rule, path: str) -> int | None:
    for i, layer in enumerate(rule.layers):
        if path_matches(path, layer["paths"]):
            return i
    return None


def _layers(rule: Rule, sites: list[ImportSite]) -> list[Violation]:
    out = []
    for s in _dedupe(sites):
        if s.internal is None or path_matches(s.file, rule.exclude):
            continue
        mine, theirs = _layer_of(rule, s.file), _layer_of(rule, s.internal)
        if mine is None or theirs is None or theirs >= mine:
            continue
        lower, upper = rule.layers[mine]["name"], rule.layers[theirs]["name"]
        out.append(
            Violation(
                rule.id,
                rule.kind,
                rule.severity,
                s.file,
                s.line,
                f"layer `{lower}` imports `{s.target}` from layer `{upper}` above it",
                rule.reason or f"layers, top first: {' > '.join(x['name'] for x in rule.layers)}",
                f"Move what `{s.file}` needs from `{s.internal}` down into `{lower}` (or a layer below it), "
                f"or have `{upper}` pass it in as an argument.",
                s.target,
            )
        )
    return out


def _blocking(rule: Rule, graph: CodeGraph) -> list[Violation]:
    from .intents.detectors import calls_in

    extra = {c: "an async equivalent" for c in rule.calls}
    table = {**BLOCKING, **extra}
    out = []
    for pf, fn in graph.functions():
        if not fn.is_async or is_test_file(pf.path):
            continue
        if not path_matches(pf.path, rule.paths) or path_matches(pf.path, rule.exclude):
            continue
        node = pf.nodes.get(fn.qualname)
        if node is None:
            continue
        for call, name in calls_in(node, pf):
            if name not in table:
                continue
            parent = call.parent
            if parent is not None and parent.type == "await":
                continue
            if _inside_to_thread(call):
                continue
            out.append(
                Violation(
                    rule.id,
                    rule.kind,
                    rule.severity,
                    pf.path,
                    call.start_point[0] + 1,
                    f"`async def {fn.name}` calls `{name}()`, which blocks the event loop",
                    rule.reason or "every request on this worker waits while it runs",
                    f"Use {table[name]}; for code with no async version, `await asyncio.to_thread({name}, ...)`.",
                    name,
                )
            )
    return out


def _inside_to_thread(node) -> bool:
    p = node.parent
    while p is not None and p.type not in ("function_definition",):
        if p.type == "call":
            f = p.child_by_field_name("function")
            if f is not None and text(f).endswith(("to_thread", "run_in_executor", "run_sync")):
                return True
        p = p.parent
    return False


# --------------------------------------------------------------------------- inference


def _package(path: str) -> str:
    parts = path.split("/")
    return "/".join(parts[:-1]) if len(parts) > 1 else ""


def infer(graph: CodeGraph, *, min_files: int = 1) -> list[Rule]:
    """Rules today's code already keeps: for packages A and B where A imports B and B never imports A,
    "B may not import A". Freezes the dependency direction so a new back-edge (a cycle) fails CI."""
    sites = [s for s in import_sites(graph) if s.internal]
    files: dict[str, int] = defaultdict(int)
    for p in graph.files:
        if not is_test_file(p):
            files[_package(p)] += 1
    edges: dict[tuple[str, str], int] = defaultdict(int)
    for s in sites:
        a, b = _package(s.file), _package(s.internal)
        if a and b and a != b and not a.startswith(b + "/") and not b.startswith(a + "/"):
            edges[(a, b)] += 1
    rules = []
    for (a, b), n in sorted(edges.items()):
        if (b, a) in edges or files[a] < min_files or files[b] < min_files:
            continue
        rules.append(
            Rule(
                id=f"{b.replace('/', '.')}-not-{a.replace('/', '.')}",
                kind="forbid-import",
                reason=f"`{a}` depends on `{b}` ({n} import(s)); the reverse would make a cycle",
                paths=[f"{b}/*.py"],
                imports=[f"{a}/*.py"],
                source=RULES_FILE,
            )
        )
    return rules


def summary(graph: CodeGraph, root: str | Path) -> dict:
    """Rules and their violations, for the web app, MCP and reports."""
    try:
        rules = load(root)
        error = None
    except (ValueError, yaml.YAMLError) as exc:
        rules, error = [], str(exc)
    violations = check(graph, rules)
    return {
        "rules": [r.to_dict() for r in rules],
        "violations": [v.to_dict() for v in violations],
        "errors": sum(1 for v in violations if v.severity == "error"),
        "warnings": sum(1 for v in violations if v.severity == "warn"),
        "ok": not any(v.severity == "error" for v in violations) and error is None,
        "error": error,
        "file": str(rules_path(root).relative_to(root)) if rules_path(root).exists() else None,
    }


def to_json(rules: list[Rule]) -> str:
    return json.dumps([r.to_dict() for r in rules], indent=2)
