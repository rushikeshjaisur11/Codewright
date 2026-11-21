"""Upgrade one dependency with the code changes it needs, from the Dependencies page.

`plan` shows, before anything changes:

- the requirement edits (pyproject.toml, requirements*.txt), keeping each file's formatting;
- how the lockfile is brought along (`uv lock --upgrade-package`, or the command to run);
- the code changes the new version calls for: findings that appear when the code is reviewed at the new
  version, split into those with a patch (applied) and those to do by hand;
- what the release notes say changed, and (on request) the model's reading of them against this repo's
  own uses of the library.

`apply` carries out the chosen parts, on a new branch when asked; nothing is committed or pushed.
"""

from __future__ import annotations

import copy
import difflib
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from .models import Finding, Stack, normalise

# --------------------------------------------------------------------------- requirement text


def _next_cap(v: Version) -> str:
    return f"{v.major + 1}" if v.major >= 1 else f"0.{v.minor + 1}"


def new_spec(spec: str, version: str) -> str:
    """The specifier moved to allow `version`: pins and lower bounds move to it, too-low caps are raised."""
    v = Version(version)
    out = []
    for clause in [c.strip() for c in spec.split(",") if c.strip()]:
        m = re.match(r"(===|==|~=|>=|<=|!=|>|<|\^|~)\s*(\S+)", clause)
        if not m:
            out.append(clause)
            continue
        op, val = m.groups()
        try:
            old = Version(val.rstrip(".*"))
        except InvalidVersion:
            out.append(clause)
            continue
        if op in ("==", "===") or op in (">=", "~=", "^", "~"):
            out.append(f"{op}{version}")
        elif op == ">":
            out.append(f">={version}")
        elif op in ("<", "<="):
            out.append(clause if (old > v if op == "<" else old >= v) else f"<{_next_cap(v)}")
        else:  # != stays
            out.append(clause)
    return ",".join(out)


_REQ = re.compile(r"^(\s*[A-Za-z0-9][\w.\-]*(?:\s*\[[^\]]*\])?\s*)([^;]*?)(\s*(?:;.*)?)$")


def _rewrite(raw: str, version: str) -> str | None:
    """`raw` (a requirement as written) with its version specifier moved to allow `version`, or None."""
    m = _REQ.match(raw)
    if not m or not m.group(2).strip():
        return None
    written = m.group(2).strip()
    spec = new_spec(written, version)
    if spec == written or not _admits(spec, version):
        return None
    return f"{m.group(1)}{m.group(2).replace(written, spec)}{m.group(3)}"


def _admits(spec: str, version: str) -> bool:
    if any(c in spec for c in "^~") and "~=" not in spec:
        return True  # poetry ranges: trust the rewrite
    try:
        return Version(version) in SpecifierSet(spec)
    except Exception:
        return True


def requirement_edits(root: str | Path, name: str, version: str) -> list[dict]:
    """[{"file", "line", "old", "new", "diff"}] for every place the dependency is declared with a version."""
    root = Path(root)
    key = normalise(name)
    edits = []

    def edit_file(path: Path, rewrite) -> None:
        try:
            text = path.read_text()
        except OSError:
            return
        new_text, changes = rewrite(text)
        if new_text != text:
            diff = "".join(
                difflib.unified_diff(
                    text.splitlines(keepends=True),
                    new_text.splitlines(keepends=True),
                    f"a/{path.name}",
                    f"b/{path.name}",
                )
            )
            edits.append(
                {"file": path.relative_to(root).as_posix(), "changes": changes, "diff": diff, "text": new_text}
            )

    def pep508(text: str) -> tuple[str, list[dict]]:
        changes = []

        def sub(m: re.Match) -> str:
            raw = m.group(2)
            try:
                req = Requirement(raw)
            except InvalidRequirement:
                return m.group(0)
            if normalise(req.name) != key or not str(req.specifier):
                return m.group(0)
            new_raw = _rewrite(raw, version)
            if new_raw is None:
                return m.group(0)
            changes.append({"old": raw, "new": new_raw})
            return f"{m.group(1)}{new_raw}{m.group(3)}"

        return re.sub(r"""(["'])([A-Za-z0-9][A-Za-z0-9._\-\[\], ]*[<>=!~^][^"']*)(["'])""", sub, text), changes

    pp = root / "pyproject.toml"
    if pp.exists():

        def rewrite_pyproject(text: str):
            text2, changes = pep508(text)

            # poetry: fastapi = "^0.110" or fastapi = { version = "^0.110", ... }
            def poetry(m: re.Match) -> str:
                if normalise(m.group(1)) != key:
                    return m.group(0)
                spec = new_spec(m.group(3), version)
                if spec == m.group(3):
                    return m.group(0)
                changes.append({"old": m.group(3), "new": spec})
                return f"{m.group(1)}{m.group(2)}{spec}{m.group(4)}"

            try:
                tomllib.loads(text)
                has_poetry = "[tool.poetry" in text
            except tomllib.TOMLDecodeError:
                has_poetry = False
            if has_poetry:
                text2 = re.sub(
                    r'(?m)^([A-Za-z0-9][\w.\-]*)(\s*=\s*(?:\{[^}\n]*version\s*=\s*)?")([^"]+)(")', poetry, text2
                )
            return text2, changes

        edit_file(pp, rewrite_pyproject)
    for req_file in sorted(root.glob("requirements*.txt")):

        def rewrite_reqs(text: str):
            changes = []
            lines = text.splitlines(keepends=True)
            for i, line in enumerate(lines):
                body = line.split("#", 1)[0].strip()
                try:
                    req = Requirement(body)
                except InvalidRequirement:
                    continue
                if normalise(req.name) != key or not str(req.specifier):
                    continue
                new_body = _rewrite(body, version)
                if new_body is None:
                    continue
                lines[i] = line.replace(body, new_body, 1)
                changes.append({"old": body, "new": new_body})
            return "".join(lines), changes

        edit_file(req_file, rewrite_reqs)
    return edits


def lock_step(root: str | Path, name: str, version: str) -> dict:
    """How the lockfile follows: {"tool", "command", "available", "note"}."""
    root = Path(root)
    if (root / "uv.lock").exists():
        cmd = ["uv", "lock", "--upgrade-package", f"{name}=={version}"]
        return {
            "tool": "uv",
            "command": cmd,
            "available": shutil.which("uv") is not None,
            "note": "uv.lock is updated for this package only",
        }
    if (root / "poetry.lock").exists():
        cmd = ["poetry", "update", name, "--lock"]
        return {
            "tool": "poetry",
            "command": cmd,
            "available": shutil.which("poetry") is not None,
            "note": "poetry.lock is updated for this package only",
        }
    for p in sorted(root.glob("requirements*.txt")):
        if "pip-compile" in p.read_text(errors="replace")[:2000]:
            return {
                "tool": "pip-compile",
                "command": ["pip-compile", "--upgrade-package", f"{name}=={version}", p.name],
                "available": shutil.which("pip-compile") is not None,
                "note": f"{p.name} is compiled by pip-tools",
            }
    return {"tool": None, "command": [], "available": False, "note": "no lockfile: the requirement edit is the upgrade"}


# --------------------------------------------------------------------------- code changes


def code_changes(root: str | Path, graph, stack: Stack, name: str, version: str) -> dict:
    """Findings that appear and disappear when the code is reviewed at `version` (nothing is installed)."""
    from . import cards as card_store
    from . import detectcache
    from .intents import detect
    from .suggest import make_findings

    key = normalise(name)
    cards = card_store.for_repo(root)
    matches = detect(graph, cache=detectcache.load(root))  # reuses the stored detection: fast on big repos
    target = copy.deepcopy(stack)
    if key in target.dependencies:
        target.dependencies[key].version = version
    now = {(f.card_id, f.file, f.function): f for f in make_findings(matches, stack, graph, cards)}
    then = {(f.card_id, f.file, f.function): f for f in make_findings(matches, target, graph, cards)}
    new = [f for k, f in sorted(then.items()) if k not in now]
    gone = [f for k, f in sorted(now.items()) if k not in then]
    return {"new": new, "resolved": gone}


def uses(graph, name: str, limit: int = 60) -> list[dict]:
    """Where this repo uses the library: file, line, the names it imports from it."""
    from .deps import environment, import_names

    mods = set(import_names(name, environment(None))) | {name.replace("-", "_")}
    out = []
    for path, pf in graph.files.items():
        got = sorted({a for a, t in pf.module.imports.items() if any(t == m or t.startswith(m + ".") for m in mods)})
        if got:
            out.append({"file": path, "names": got[:12]})
        if len(out) >= limit:
            break
    return out


BREAKING_SYSTEM = (
    "You plan a dependency upgrade for one Python repository. You get the release notes between the installed "
    "and the target version, and where the repository uses the library (files and the names they import). "
    "List the changes this repository must make: removed or renamed APIs it uses, changed defaults or behaviour "
    "that affect the names it imports. Only what the notes support and the code uses; an empty list when "
    "nothing applies. `file` is a path from the list."
)

BREAKING_SCHEMA = {
    "type": "object",
    "properties": {
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "change": {"type": "string"},
                    "why": {"type": "string"},
                    "version": {"type": "string"},
                },
                "required": ["file", "change", "why", "version"],
            },
        }
    },
    "required": ["changes"],
}


def breaking_changes(root: str | Path, graph, name: str, installed: str | None, version: str, notes: dict) -> dict:
    from .llm import LLMConfig, complete_json, last_error
    from .llm import status as llm_status

    cfg = LLMConfig.load(root)
    st = llm_status(cfg)
    if not st["available"]:
        raise RuntimeError(f"no model to read the release notes: {st['reason']}. Set one up in Settings")
    used = uses(graph, name)
    text = "\n\n".join(f"## {n['version']}\n{n['body']}" for n in notes.get("notes", []))
    if not text.strip():
        raise RuntimeError("no release notes found for these versions; check the changelog links by hand")
    ask = (
        f"{name}: {installed or 'unknown'} -> {version}\n\nWhere the repo uses it:\n"
        + "\n".join(f"- {u['file']}: {', '.join(u['names'])}" for u in used)
        + f"\n\nRelease notes:\n{text[:30000]}"
    )
    data = complete_json(
        cfg,
        BREAKING_SYSTEM,
        [{"role": "user", "content": ask}],
        BREAKING_SCHEMA,
        max_tokens=3000,
        purpose="plan an upgrade",
    )
    if data is None:
        raise RuntimeError(last_error() or "the model did not answer")
    items = data["changes"]
    files = {u["file"] for u in used}
    return {
        "items": [
            {k: str(i.get(k, ""))[:500] for k in ("file", "change", "why", "version")}
            for i in items
            if isinstance(i, dict) and i.get("change") and (not i.get("file") or i.get("file") in files)
        ][:40],
        "model": cfg.model,
    }


# --------------------------------------------------------------------------- the plan and doing it


def plan(root: str | Path, graph, stack: Stack, name: str, version: str) -> dict:
    from . import migrate

    key = normalise(name)
    dep = stack.dependencies.get(key)
    installed = dep.version if dep else None
    changes = code_changes(root, graph, stack, key, version)
    row = lambda f: {  # noqa: E731
        "id": f.id,
        "title": f.title,
        "file": f.file,
        "line": f.line,
        "patch": f.patch,
        "suggestion": f.suggestion,
        "intent": f.intent,
    }
    migrations = [m for m in migrate.overview(graph, stack, root) if m.get("library") == key]
    return {
        "name": key,
        "installed": installed,
        "version": version,
        "requirements": [{k: e[k] for k in ("file", "changes", "diff")} for e in requirement_edits(root, key, version)],
        "lock": {k: v for k, v in lock_step(root, key, version).items()},
        "patches": [row(f) for f in changes["new"] if f.patch],
        "manual": [row(f) for f in changes["new"] if not f.patch],
        "resolved": [row(f) for f in changes["resolved"]],
        "migrations": [
            {"name": m.get("name"), "title": m.get("title"), "findings": m.get("findings")} for m in migrations
        ],
        "uses": uses(graph, key),
    }


def apply(
    root: str | Path,
    graph,
    stack: Stack,
    name: str,
    version: str,
    *,
    patch_ids: list[str] | None = None,
    branch: str = "",
    run_lock: bool = True,
    extra_patches: list[str] | None = None,
) -> dict:
    """Do the upgrade: optionally on a new branch, edit the requirements, update the lockfile, apply the chosen
    patches. Nothing is committed."""
    from . import gitops
    from .proof import apply_patch, patch_error
    from .refactor import combine

    root = Path(root)
    key = normalise(name)
    out: dict = {"branch": None, "files": [], "lock": None, "applied": [], "skipped": []}
    if branch:
        out["branch"] = gitops.create(root, branch)["branch"]
    for e in requirement_edits(root, key, version):
        (root / e["file"]).write_text(e["text"])
        out["files"].append(e["file"])
    step = lock_step(root, key, version)
    if run_lock and step["command"] and step["available"]:
        try:
            proc = subprocess.run(step["command"], cwd=root, capture_output=True, text=True, timeout=600)
            out["lock"] = {
                "ok": proc.returncode == 0,
                "command": " ".join(step["command"]),
                "output": (proc.stdout + proc.stderr)[-3000:],
            }
        except (OSError, subprocess.TimeoutExpired) as exc:
            out["lock"] = {"ok": False, "command": " ".join(step["command"]), "output": str(exc)}
    elif step["command"]:
        out["lock"] = {
            "ok": False,
            "command": " ".join(step["command"]),
            "output": f"{step['tool']} isn't installed here: run the command yourself",
        }
    found: list[Finding] = code_changes(root, graph, stack, key, version)["new"]
    chosen = [f for f in found if f.patch and (patch_ids is None or f.id in set(patch_ids))]
    if chosen:
        ordered = sorted(chosen, key=lambda f: (f.file, -f.line))
        patch, skipped = combine(root, [(f.id, f.patch) for f in ordered])
        if patch and apply_patch(root, patch):
            out["applied"] = [f.id for f in ordered if f.id not in skipped]
            out["skipped"] = skipped
            out["files"] += sorted({f.file for f in ordered if f.id not in skipped})
        else:
            out["skipped"] = [f.id for f in ordered]
    for patch in extra_patches or []:  # the code changes written for this upgrade (upgradesafety.rewrite)
        files = re.findall(r"^\+\+\+ b/(\S+)", patch, re.M)
        if not (err := patch_error(root, patch)):
            out["files"] += [f for f in files if f not in out["files"]]
            out.setdefault("rewrites_applied", 0)
            out["rewrites_applied"] += 1
        else:
            out.setdefault("rewrites_failed", []).extend(files)
            out.setdefault("rewrite_errors", []).append({"files": files, "why": err[:400]})
    return out
