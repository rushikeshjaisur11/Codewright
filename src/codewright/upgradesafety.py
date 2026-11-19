"""Make sure a dependency upgrade does not break the repo, before it is applied.

Three checks, each with evidence:

1. `check_uses`: every place the repo uses the library (calls with the keyword arguments it passes, imports,
   base classes) is checked against the target version *installed in a throwaway environment*: does the name
   still exist, does it still accept those keywords, is it marked deprecated. The same names are inspected in
   the repo's current environment, so a changed signature shows old and new side by side.
2. `rewrite`: for every broken use, the model rewrites the whole function (or the import block), grounded in
   the target version's real signatures and docstrings and the release notes. Each rewrite must compile; it
   is returned as a patch for review.
3. `trial`: the tests run on today's code, then in a sandbox copy with the upgrade (requirement, lockfile, and
   the chosen patches) applied and an environment built from it. The result lists what fails only after.

Nothing touches the repo; the Upgrade dialog applies what the user picks.
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .graph import CodeGraph, text
from .models import Stack, normalise

INSPECT_SCRIPT = r"""
import importlib, inspect, json, re, sys, warnings
out = {}
for sym in json.loads(sys.argv[1]):
    parts = sym.split(".")
    rec = {"exists": False}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        obj = None
        for i in range(len(parts), 0, -1):
            try:
                obj = importlib.import_module(".".join(parts[:i]))
            except Exception:
                continue
            try:
                for attr in parts[i:]:
                    obj = getattr(obj, attr)
                rec["exists"] = True
            except AttributeError:
                obj = None
            break
    if rec["exists"]:
        rec["kind"] = type(obj).__name__
        target = obj.__init__ if inspect.isclass(obj) else obj
        try:
            sig = inspect.signature(obj)
            rec["signature"] = str(sig)[:400]
            rec["params"] = [p.name for p in sig.parameters.values()
                             if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)]
            rec["var_kw"] = any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values())
        except (TypeError, ValueError):
            pass
        doc = inspect.getdoc(obj) or ""
        rec["doc"] = doc[:700]
        dep = getattr(obj, "__deprecated__", None)  # PEP 702 @deprecated
        if dep or re.search(r"(?im)^\s*(\.\. deprecated::|deprecated[:.])", doc):
            rec["deprecated"] = str(dep or "marked deprecated in its docstring")[:300]
    kinds = (DeprecationWarning, FutureWarning, PendingDeprecationWarning)
    msgs = [str(w.message) for w in caught if issubclass(w.category, kinds)]
    if msgs and not rec.get("deprecated"):
        rec["deprecated"] = msgs[0][:300]
    out[sym] = rec
print(json.dumps(out))
"""


# --------------------------------------------------------------------------- where the repo uses the library


def library_modules(name: str) -> list[str]:
    from .deps import environment, import_names

    return sorted(set(import_names(normalise(name), environment(None))) | {normalise(name).replace("-", "_")})


def usage_sites(graph: CodeGraph, name: str, limit: int = 400) -> list[dict]:
    """Every use of the library: {file, line, function, symbol (dotted, resolved), kind, kwargs}."""
    from .graph import import_statements, resolve
    from .intents.detectors import calls_in

    mods = library_modules(name)

    def ours(dotted: str) -> bool:
        return any(dotted == m or dotted.startswith(m + ".") for m in mods)

    out: list[dict] = []
    for path, pf in sorted(graph.files.items()):
        if not any(ours(t) for t in pf.module.imports.values()):
            continue
        for ln, _deferred, _kind, imported in import_statements(pf):
            for _alias, target in imported.items():
                if ours(target):
                    out.append(
                        {
                            "file": path,
                            "line": ln,
                            "function": "<module>",
                            "symbol": target,
                            "kind": "import",
                            "kwargs": [],
                        }
                    )
        for cls, bases in pf.classes.items():
            for b in bases:
                full = resolve(pf.module, b)
                if ours(full):
                    out.append({"file": path, "line": 1, "function": cls, "symbol": full, "kind": "base", "kwargs": []})
        fns = sorted(pf.module.functions, key=lambda f: f.start_line)
        for call, full in calls_in(pf.root, pf, into_defs=True):
            if not ours(full):
                continue
            line = call.start_point[0] + 1
            inside = [f for f in fns if f.start_line <= line <= f.end_line]
            fn = max(inside, key=lambda f: f.start_line).qualname if inside else "<module>"
            args = call.child_by_field_name("arguments")
            kwargs = sorted(
                {
                    text(a.child_by_field_name("name"))
                    for a in (args.named_children if args is not None else [])
                    if a.type == "keyword_argument"
                }
            )
            out.append({"file": path, "line": line, "function": fn, "symbol": full, "kind": "call", "kwargs": kwargs})
        if len(out) >= limit:
            break
    return out[:limit]


# --------------------------------------------------------------------------- the target version, installed aside


def _home() -> Path:
    return Path(os.environ.get("CODEWRIGHT_HOME", "~/.codewright")).expanduser()


def target_env(name: str, version: str, extras: list[str] | None = None, index_env: dict | None = None) -> Path:
    """A cached virtual environment with only `name==version` (and its own dependencies) installed."""
    spec = f"{name}[{','.join(sorted(extras))}]=={version}" if extras else f"{name}=={version}"
    env = _home() / "upgrade-envs" / hashlib.sha1(f"{spec}:{sys.version_info[:2]}".encode()).hexdigest()[:16]
    python = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if (env / ".codewright-ok").exists() and python.exists():
        return python
    shutil.rmtree(env, ignore_errors=True)
    env.parent.mkdir(parents=True, exist_ok=True)
    run_env = dict(os.environ, **(index_env or {}))
    uv = shutil.which("uv")
    steps = (
        [[uv, "venv", "--quiet", str(env)], [uv, "pip", "install", "--quiet", "--python", str(python), spec]]
        if uv
        else [[sys.executable, "-m", "venv", str(env)], [str(python), "-m", "pip", "install", "--quiet", spec]]
    )
    for step in steps:
        proc = subprocess.run(step, capture_output=True, text=True, timeout=900, env=run_env)
        if proc.returncode != 0:
            last = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["failed"]
            raise RuntimeError(f"could not install {spec} to check against: {last[0][:300]}")
    (env / ".codewright-ok").write_text("ok\n")
    return python


def inspect_symbols(python: str | Path, symbols: list[str], timeout: int = 180) -> dict[str, dict]:
    if not symbols:
        return {}
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(INSPECT_SCRIPT)
        script = fh.name
    try:
        proc = subprocess.run(
            [str(python), script, json.dumps(sorted(set(symbols)))], capture_output=True, text=True, timeout=timeout
        )
        return json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 and proc.stdout.strip() else {}
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        return {}
    finally:
        with contextlib.suppress(OSError):
            os.unlink(script)


def check_uses(root: str | Path, graph: CodeGraph, stack: Stack, name: str, version: str) -> dict:
    """Each use, checked against the target version: {sites, problems, summary, current_python, target_python}."""
    from . import proof_env
    from .index import resolve as resolve_index

    key = normalise(name)
    sites = usage_sites(graph, key)
    symbols = sorted({s["symbol"] for s in sites})
    dep = stack.dependencies.get(key)
    py_target = target_env(
        key, version, sorted(dep.extras) if dep and dep.extras else None, resolve_index(root).uv_env()
    )
    now_py = proof_env.resolve(root, None, build=True).python
    new = inspect_symbols(py_target, symbols)
    old = inspect_symbols(now_py, symbols)
    problems = []
    for s in sites:
        n, o = new.get(s["symbol"], {}), old.get(s["symbol"], {})
        issue = None
        if not n.get("exists"):
            issue = ("removed", f"`{s['symbol']}` does not exist in {key} {version}")
        elif s["kwargs"] and n.get("params") is not None and not n.get("var_kw"):
            gone = [k for k in s["kwargs"] if k not in n["params"]]
            if gone:
                issue = ("keyword", f"`{s['symbol']}` no longer accepts {', '.join(f'`{k}=`' for k in gone)}")
        if issue is None and n.get("deprecated"):
            issue = ("deprecated", f"`{s['symbol']}` is deprecated in {version}: {n['deprecated']}")
        if issue is None and o.get("signature") and n.get("signature") and o["signature"] != n["signature"]:
            issue = ("changed", f"`{s['symbol']}` changed its signature")
        if issue:
            problems.append(
                {
                    **s,
                    "issue": issue[0],
                    "detail": issue[1],
                    "old_signature": o.get("signature"),
                    "new_signature": n.get("signature"),
                    "new_doc": n.get("doc", "")[:600],
                }
            )
    order = {"removed": 0, "keyword": 1, "deprecated": 2, "changed": 3}
    problems.sort(key=lambda p: (order[p["issue"]], p["file"], p["line"]))
    breaking = sum(1 for p in problems if p["issue"] in ("removed", "keyword"))
    return {
        "name": key,
        "version": version,
        "sites": len(sites),
        "symbols": len(symbols),
        "files": len({s["file"] for s in sites}),
        "problems": problems,
        "breaking": breaking,
        "summary": (
            f"{len(sites)} use(s) of {len(symbols)} name(s) in {len({s['file'] for s in sites})} file(s): "
            + (f"{breaking} will break" if breaking else "none will break")
            + (f", {len(problems) - breaking} to look at" if len(problems) > breaking else "")
        ),
        "current_python": str(now_py),
        "target_python": str(py_target),
    }


# --------------------------------------------------------------------------- the code changes


REWRITE_SYSTEM = (
    "You upgrade code to a new version of a Python library. You get one function (or a file's import block) "
    "that uses the library, the uses that break or are deprecated in the new version with the new version's "
    "real signatures and docstrings, and its release notes. Rewrite it so it works with the new version and "
    "behaves exactly as before: change only what the upgrade requires, keep names, arguments, return values and "
    "comments. Reply with a one-line explanation, then the complete rewritten code in one ```python block."
)


def _function_span(pf, qualname: str) -> tuple[int, int] | None:
    fn = next((f for f in pf.module.functions if f.qualname == qualname), None)
    return (fn.start_line, fn.end_line) if fn else None


def _import_span(pf) -> tuple[int, int] | None:
    """First and last line of the file's top-level imports (a parenthesised import runs to its `)`)."""
    from .graph import import_statements

    lines = [ln for ln, deferred, _k, _i in import_statements(pf) if not deferred]
    if not lines:
        return None
    start, end = min(lines), max(lines)
    src = pf.lines
    while end < len(src) and "".join(src[end - 1 : end]).count("(") > "".join(src[end - 1 : end]).count(")"):
        end += 1  # `from x import (` on the last import line
        while end < len(src) and ")" not in src[end - 1]:
            end += 1
    return start, end


def rewrite(
    root: str | Path,
    graph: CodeGraph,
    name: str,
    installed: str | None,
    version: str,
    problems: list[dict],
    notes: dict | None = None,
    max_groups: int = 30,
) -> dict:
    """One patch per affected function (or import block), written by the model, each checked to compile."""
    from .llm import LLMConfig, complete, last_error
    from .llm import status as llm_status

    cfg = LLMConfig.load(root)
    st = llm_status(cfg)
    if not st["available"]:
        raise RuntimeError(f"no model to write the changes: {st['reason']}. Set one up in Settings")
    groups: dict[tuple[str, str], list[dict]] = {}
    for p in problems:
        groups.setdefault((p["file"], p["function"] if p["kind"] != "import" else "<imports>"), []).append(p)
    notes_text = "\n\n".join(f"## {n['version']}\n{n['body']}" for n in (notes or {}).get("notes", []))[:12000]
    patches, skipped = [], []
    for (file, fn), items in list(groups.items())[:max_groups]:
        pf = graph.files.get(file)
        span = _import_span(pf) if fn in ("<imports>", "<module>") else _function_span(pf, fn) if pf else None
        if pf is None or span is None:
            skipped.append({"file": file, "function": fn, "why": "could not find the code to rewrite"})
            continue
        lines = pf.lines
        old = "".join(lines[span[0] - 1 : span[1]])
        issues = "\n".join(
            f"- line {p['line']}: {p['detail']}"
            + (f"\n  new signature: {p['symbol']}{p['new_signature']}" if p.get("new_signature") else "")
            + (f"\n  old signature: {p['symbol']}{p['old_signature']}" if p.get("old_signature") else "")
            + (f"\n  docs: {p['new_doc'][:400]}" if p.get("new_doc") else "")
            for p in items
        )
        ask = (
            f"{name} {installed or '?'} -> {version}. File {file}, "
            + ("its import block" if fn in ("<imports>", "<module>") else f"function `{fn}`")
            + f".\n\nWhat breaks or is deprecated:\n{issues}\n\nCode now:\n```python\n{old}```"
            + (f"\n\nRelease notes:\n{notes_text}" if notes_text else "")
        )
        reply = complete(
            cfg, REWRITE_SYSTEM, [{"role": "user", "content": ask}], max_tokens=3000, purpose=f"upgrade {file}"
        )
        if reply is None:
            skipped.append({"file": file, "function": fn, "why": last_error() or "the model did not answer"})
            continue
        m = re.search(r"```(?:python)?\s*\n(.*?)```", reply, re.S)
        if not m:
            skipped.append({"file": file, "function": fn, "why": "the model's answer had no code"})
            continue
        new = m.group(1)
        indent = re.match(r"[ \t]*", old).group(0)
        if indent and not new.startswith(indent):  # a method: keep the class's indentation
            new = "".join(indent + ln if ln.strip() else ln for ln in new.splitlines(keepends=True))
        if not new.endswith("\n"):
            new += "\n"
        before = "".join(lines)
        after = "".join(lines[: span[0] - 1]) + new + "".join(lines[span[1] :])
        try:
            compile(after, file, "exec")
        except SyntaxError as exc:
            skipped.append(
                {"file": file, "function": fn, "why": f"the rewrite does not compile ({exc.msg}, line {exc.lineno})"}
            )
            continue
        if after == before:
            continue
        patch = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True), after.splitlines(keepends=True), f"a/{file}", f"b/{file}"
            )
        )
        explain = reply[: m.start()].strip().splitlines()[0][:300] if reply[: m.start()].strip() else ""
        pid = hashlib.sha1(f"{file}:{fn}:{patch}".encode()).hexdigest()[:10]
        patches.append(
            {
                "id": pid,
                "file": file,
                "function": fn,
                "patch": patch,
                "explanation": explain,
                "fixes": [p["detail"] for p in items],
            }
        )
    return {"patches": patches, "skipped": skipped, "model": cfg.model}


# --------------------------------------------------------------------------- the tests, before and after


_FAILED = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.M)
_COUNTS = re.compile(r"(\d+) (passed|failed|errors?|skipped)")


def _summary(run) -> dict:
    counts = {k if not k.startswith("error") else "errors": int(n) for n, k in _COUNTS.findall(run.output)}
    return {
        "ok": run.ok,
        "seconds": round(run.seconds, 1),
        "passed": counts.get("passed", 0),
        "failed": sorted(set(_FAILED.findall(run.output))),
        "counts": counts,
        "output": run.output[-3000:],
    }


def trial(root: str | Path, name: str, version: str, patches: list[str], *, progress=None, timeout: int = 1800) -> dict:
    """Run the tests on today's code and on a copy with the upgrade applied; what fails only after is the risk."""
    from . import proof, proof_env, upgrade

    root = Path(root).resolve()
    tell = progress or (lambda msg: None)
    out: dict = {"before": None, "after": None, "lock": None, "patches_applied": 0, "patches_failed": 0}
    if not proof.has_tests(root):
        out["error"] = "this repo has no tests to run; the checks above are the evidence"
        return out
    with tempfile.TemporaryDirectory(prefix="codewright-upgrade-") as tmp:
        tmp = Path(tmp)
        tell("Running the tests on today's code")
        now = proof_env.resolve(root, None, build=True)
        base = tmp / "before"
        shutil.copytree(root, base, ignore=proof.IGNORE, symlinks=True)
        out["before"] = _summary(proof.run_tests(base, now.python, timeout, fail_fast=False))
        tell(f"Today: {out['before']['passed']} passed, {len(out['before']['failed'])} failing")

        work = tmp / "after"
        shutil.copytree(root, work, ignore=proof.IGNORE, symlinks=True)
        for e in upgrade.requirement_edits(work, name, version):
            (work / e["file"]).write_text(e["text"])
        step = upgrade.lock_step(work, name, version)
        if step["command"] and step["available"]:
            tell(f"Updating the lockfile in the copy ({' '.join(step['command'])})")
            proc = subprocess.run(step["command"], cwd=work, capture_output=True, text=True, timeout=900)
            out["lock"] = {"ok": proc.returncode == 0, "output": (proc.stdout + proc.stderr)[-1500:]}
            if proc.returncode != 0:
                out["error"] = "the lockfile could not be updated for this version; see lock.output"
                return out
        for patch in patches:
            if proof.apply_patch(work, patch):
                out["patches_applied"] += 1
            else:
                out["patches_failed"] += 1
        tell(f"Building an environment with {name} {version} and running the tests")
        env = proof_env.resolve(work, None, build=True)
        if env.python == now.python:
            out["error"] = (
                "could not build an environment with the new version (needs uv and a uv.lock or requirements file): "
                + (env.note or "")
            )
            return out
        out["after"] = _summary(proof.run_tests(work, env.python, timeout, fail_fast=False))
    before, after = set(out["before"]["failed"]), set(out["after"]["failed"])
    out["new_failures"] = sorted(after - before)
    out["fixed"] = sorted(before - after)
    out["safe"] = not out["new_failures"] and out["after"]["passed"] >= out["before"]["passed"] * 0.98
    tell(
        f"After the upgrade: {out['after']['passed']} passed, {len(after)} failing"
        + (f"; {len(out['new_failures'])} newly failing" if out["new_failures"] else "; nothing newly failing")
    )
    return out
