"""Make sure a dependency upgrade does not break the repo, before it is applied.

Three checks, each with evidence:

1. `check_uses`: every place the repo uses the library (calls with the keyword arguments it passes, imports,
   base classes) is checked against the target version *installed in a throwaway environment*: does the name
   still exist, does it still accept those keywords, is it marked deprecated. The same names are inspected in
   the repo's current environment, so a changed signature shows old and new side by side.
2. `rewrite`: for every broken use, the model rewrites the whole function (or the import block), grounded in
   the target version's real signatures and docstrings and the release notes. The model answers in a JSON
   schema ({explanation, code}); the code is spliced into the file as it is on disk, must compile, still
   define the function and apply cleanly, or it goes back to the model with the reason. It is returned as a
   patch for review.
3. `trial`: the tests run on today's code, then in a sandbox copy with the upgrade (requirement, lockfile, and
   the chosen patches) applied and an environment built from it. The result lists what fails only after.

Nothing touches the repo; the Upgrade dialog applies what the user picks.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
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
    "behaves exactly as before: change only what the upgrade requires, keep names, arguments, return values, "
    "decorators and comments."
)

# What the code writer must answer: the whole replacement for the code it was given, as a JSON string.
REWRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "explanation": {"type": "string", "description": "One sentence: what changed and why."},
        "code": {
            "type": "string",
            "description": "The complete rewritten code that replaces the code given, from its first line (any "
            "decorators) to its last, with the same indentation. Plain code: no ``` fences, no placeholders "
            "such as '...' or '# rest unchanged'.",
        },
    },
    "required": ["explanation", "code"],
    "additionalProperties": False,
}

_FENCE = re.compile(r"(?:```|~~~)[ \t]*([\w+.-]*)[^\n]*\n(.*?)(?:```|~~~)", re.S)
_ELIDED = re.compile(
    r"^[ \t]*(?:\.\.\.|#[ \t]*\.\.\..*|#.*\b(?:rest|remainder) of the (?:code|function|method|body|file|class)\b.*"
    r"|#.*\b(?:existing|original|unchanged) code\b.*)[ \t]*$",
    re.I | re.M,
)


def code_from_text(reply: str) -> dict | None:
    """{explanation, code} from a reply that ignored the schema: the longest Python code block (```python,
    ```py, ~~~ or unlabelled), and the first line before it as the explanation."""
    blocks = [m for m in _FENCE.finditer(reply) if m.group(1).lower() in ("", "python", "py", "python3")]
    if not blocks:
        return None
    m = max(blocks, key=lambda b: len(b.group(2)))
    before = reply[: m.start()].strip().splitlines()
    return {"explanation": before[0] if before else "", "code": m.group(2)}


def _unfence(code: str) -> str:
    """The code itself, when the model put fences inside the JSON string anyway."""
    m = re.fullmatch(r"\s*(?:```|~~~)[ \t]*[\w+.-]*[^\n]*\n(.*?)(?:```|~~~)\s*", code, re.S)
    return m.group(1) if m else code


def _candidates(new: str, indent: str) -> list[str]:
    """Ways to fit the model's code at the original indentation: as written, dedented and re-indented, or with
    only the first line indented (code copied out of a class often loses just that one)."""
    out = [new]
    if indent:
        body = textwrap.dedent(new)
        out.append("".join(indent + ln if ln.strip() else ln for ln in body.splitlines(keepends=True)))
        first, _, rest = new.partition("\n")
        if not first.startswith((" ", "\t")):
            out.append(indent + first + "\n" + rest)
    if not new.startswith(indent):  # most likely right first
        out = out[1:] + out[:1]
    return list(dict.fromkeys(out))


def splice(
    before: str, old: str, new: str, file: str, defines: str | None = None, at: int | None = None
) -> tuple[str | None, str]:
    """`before` with `old` (at offset `at`, or found once in the file) replaced by the model's `new` code, fitted
    to the original indentation and line endings. Returns (the new file text, "") or (None, why it can't be
    used)."""
    new = _unfence(new)
    if not new.strip():
        return None, "the code is empty"
    if {m.group(0).strip() for m in _ELIDED.finditer(new)} - {m.group(0).strip() for m in _ELIDED.finditer(old)}:
        return None, "the code leaves parts out ('...' or '# rest of the code'): give all of it"
    if at is None or before[at : at + len(old)] != old:
        at = before.find(old)
        if at < 0 or before.find(old, at + 1) >= 0:
            return None, "the code to replace is no longer in the file as it was scanned; run the review again"
    newline = "\r\n" if "\r\n" in old else "\n"
    new = new.replace("\r\n", "\n")
    if old.endswith(("\n", "\r\n")) and not new.endswith("\n"):
        new += "\n"
    if not old.endswith("\n") and new.endswith("\n"):
        new = new.rstrip("\n")
    if defines and not re.search(rf"^[ \t]*(?:async[ \t]+)?def[ \t]+{re.escape(defines)}[ \t]*\(", new, re.M):
        return None, f"the code no longer defines `{defines}`: give the whole function, from its `def` line"
    indent = re.match(r"[ \t]*", old).group(0)
    first_error = ""
    for cand in _candidates(new, indent):
        after = before[:at] + cand.replace("\n", newline) + before[at + len(old) :]
        try:
            compile(after, file, "exec")
        except SyntaxError as exc:
            first_error = first_error or f"the file would not compile ({exc.msg}, line {exc.lineno})"
            continue
        except ValueError as exc:  # null bytes and the like
            first_error = first_error or f"the file would not compile ({exc})"
            continue
        return after, ""
    return None, first_error


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
    system: str | None = None,
    heading: str | None = None,
) -> dict:
    """One patch per affected function (or import block), written by the model as structured output
    ({explanation, code}). Each is spliced into the file as it is on disk now, must compile and still define
    the function, and must apply to the working tree; an answer that fails any of these goes back to the model
    with the reason, once. What still fails is listed in `skipped` with the reason."""
    from .llm import LLMConfig, complete_json, last_error
    from .llm import status as llm_status
    from .proof import patch_error
    from .refactor import read_source, unified

    root = Path(root)
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
        is_imports = fn in ("<imports>", "<module>")
        span = (_import_span(pf) if is_imports else _function_span(pf, fn)) if pf else None
        if pf is None or span is None:
            skipped.append({"file": file, "function": fn, "why": "could not find the code to rewrite"})
            continue
        try:
            before = read_source(root / file)
        except OSError as exc:
            skipped.append({"file": file, "function": fn, "why": f"could not read the file: {exc}"})
            continue
        old = "".join(pf.lines[span[0] - 1 : span[1]])
        at = len("".join(pf.lines[: span[0] - 1])) if before == "".join(pf.lines) else None
        if at is None and before.find(old) < 0:
            skipped.append(
                {"file": file, "function": fn, "why": "the file changed since the scan; run the review again"}
            )
            continue
        issues = "\n".join(
            f"- line {p['line']}: {p['detail']}"
            + (f"\n  new signature: {p['symbol']}{p['new_signature']}" if p.get("new_signature") else "")
            + (f"\n  old signature: {p['symbol']}{p['old_signature']}" if p.get("old_signature") else "")
            + (f"\n  docs: {p['new_doc'][:400]}" if p.get("new_doc") else "")
            for p in items
        )
        ask = (
            (heading or f"{name} {installed or '?'} -> {version}")
            + f". File {file}, "
            + ("its import block" if is_imports else f"function `{fn}`")
            + (".\n\nWhat to change:\n" if heading else ".\n\nWhat breaks or is deprecated:\n")
            + f"{issues}\n\nCode now (lines {span[0]}-{span[1]}):\n```python\n{old}```"
            + (f"\n\nRelease notes:\n{notes_text}" if notes_text else "")
        )
        defines = None if is_imports else fn.rsplit(".", 1)[-1]
        result: dict = {}

        def check(value: dict, before=before, old=old, file=file, defines=defines, at=at, result=result) -> str | None:
            after, why = splice(before, old, str(value.get("code") or ""), file, defines, at)
            if after is None:
                return why
            if after == before:
                result["patch"] = ""
                return None
            patch = unified(before.splitlines(keepends=True), after.splitlines(keepends=True), f"a/{file}", f"b/{file}")
            if err := patch_error(root, patch, check=True):
                return f"the change does not apply to the file ({err.splitlines()[0][:200]})"
            result["patch"] = patch
            return None

        value = complete_json(
            cfg,
            system or REWRITE_SYSTEM,
            [{"role": "user", "content": ask}],
            REWRITE_SCHEMA,
            # room for the whole function written out again, as a JSON string, plus the explanation
            max_tokens=min(16000, max(2000, len(old) // 2 + 1200)),
            purpose=f"upgrade {file}",
            check=check,
            fallback=code_from_text,
        )
        if value is None:
            skipped.append({"file": file, "function": fn, "why": last_error() or "the model did not answer"})
            continue
        patch = result.get("patch", "")
        if not patch:
            continue  # the model changed nothing
        pid = hashlib.sha1(f"{file}:{fn}:{patch}".encode()).hexdigest()[:10]
        patches.append(
            {
                "id": pid,
                "file": file,
                "function": fn,
                "patch": patch,
                "explanation": str(value.get("explanation") or "").strip().splitlines()[0][:300]
                if str(value.get("explanation") or "").strip()
                else "",
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


# --------------------------------------------------------------------------- behaviour, without the repo's tests

IMPORT_SCRIPT = r"""
import importlib, json, sys, traceback
out = {}
for mod in json.loads(sys.argv[1]):
    try:
        importlib.import_module(mod)
        out[mod] = None
    except BaseException as exc:
        out[mod] = f"{type(exc).__name__}: {exc}"[:400]
print("@@CODEWRIGHT@@" + json.dumps(out))
"""

# Runs a probe written by the model: each `record(name, fn)` calls fn and keeps what came back (or what it
# raised) in a form that compares across versions: no memory addresses, sets sorted, objects by type and fields.
PROBE_HARNESS = r"""
import asyncio, inspect, json, re, sys
_OUT = {}
_ADDR = re.compile(r" at 0x[0-9a-fA-F]+")


def _describe(v, depth=0):
    if depth > 6:
        return "..."
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return round(v, 9)
    if isinstance(v, bytes):
        return {"bytes": v[:400].decode("utf-8", "replace")}
    if isinstance(v, dict):
        return {str(k): _describe(x, depth + 1) for k, x in list(v.items())[:100]}
    if isinstance(v, (list, tuple)):
        return [_describe(x, depth + 1) for x in list(v)[:100]]
    if isinstance(v, (set, frozenset)):
        return sorted((_describe(x, depth + 1) for x in v), key=repr)[:100]
    for dump in ("model_dump", "dict", "_asdict"):
        f = getattr(v, dump, None)
        if callable(f):
            try:
                return {"type": type(v).__name__, "value": _describe(f(), depth + 1)}
            except Exception:
                pass
    if hasattr(v, "__dict__") and not inspect.isroutine(v) and not inspect.isclass(v):
        fields = {k: x for k, x in vars(v).items() if not k.startswith("_")}
        if fields:
            return {"type": type(v).__name__, "fields": _describe(fields, depth + 1)}
    return {"type": type(v).__name__, "repr": _ADDR.sub("", repr(v))[:300]}


def record(name, fn):
    try:
        v = fn()
        if inspect.isawaitable(v):

            async def _wait(x=v):
                return await x

            v = asyncio.run(_wait())
        _OUT[name] = {"returned": _describe(v)}
    except BaseException as exc:
        _OUT[name] = {"raised": type(exc).__name__, "message": _ADDR.sub("", str(exc))[:300]}


def _done():
    print("@@CODEWRIGHT@@" + json.dumps(_OUT, default=str))
"""

PROBE_SYSTEM = (
    "You write a short Python probe that exercises functions in a repository so their behaviour can be compared "
    "before and after a library upgrade. The probe calls `record(name, lambda: <call>)` once per scenario "
    "(`record` is already defined; it runs the call, awaits coroutines and keeps the result or the exception). "
    "Use 3 to 6 scenarios per function with realistic inputs covering the normal path, edge cases and errors, "
    "chosen so the library's code actually runs. Make it deterministic and offline: patch network clients, "
    "clocks, randomness, environment and files outside the working directory with unittest.mock (start the "
    "patches with `.start()`, not a `with` block around `record`), but do NOT mock the upgraded library itself "
    "unless it would reach the network: its behaviour is what is being compared. Never assert anything. Import "
    "the functions from their modules."
)

CODE_SCHEMA = {
    "type": "object",
    "properties": {"code": {"type": "string", "description": "The complete Python code, without ``` fences."}},
    "required": ["code"],
    "additionalProperties": False,
}


def compiles(name: str):
    """A `check` for complete_json: the answer's `code` must be valid Python."""

    def check(value: dict) -> str | None:
        code = _unfence(str(value.get("code") or ""))
        if not code.strip():
            return "the code is empty"
        try:
            compile(code, name, "exec")
        except (SyntaxError, ValueError) as exc:
            return f"the code is not valid Python ({getattr(exc, 'msg', exc)}, line {getattr(exc, 'lineno', '?')})"
        return None

    return check


def _module_name(path: str) -> str:
    parts = path[:-3].split("/")
    if parts[0] == "src" and len(parts) > 1:
        parts = parts[1:]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _run_script(work: Path, python: str, code: str, args: list[str], timeout: int) -> tuple[dict | None, str]:
    """Run `code` with the repo copy importable; returns (the JSON it printed, the output tail)."""
    script = work / "_codewright_probe.py"
    script.write_text(code)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(work), str(work / "src")]), "PYTHONHASHSEED": "0"}
    env.pop("VIRTUAL_ENV", None)
    try:
        proc = subprocess.run(
            [python, str(script), *args], cwd=work, capture_output=True, text=True, timeout=timeout, env=env
        )
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout}s"
    except OSError as exc:
        return None, str(exc)
    finally:
        with contextlib.suppress(OSError):
            script.unlink()
    out = proc.stdout + proc.stderr
    mark = proc.stdout.rfind("@@CODEWRIGHT@@")
    if mark < 0:
        return None, out[-2000:]
    try:
        return json.loads(proc.stdout[mark + len("@@CODEWRIGHT@@") :].splitlines()[0]), out[-2000:]
    except (ValueError, IndexError):
        return None, out[-2000:]


def import_modules(graph: CodeGraph, name: str) -> list[str]:
    """The repo's modules that import the library (tests left out)."""
    from .graph import is_test_file

    mods = library_modules(name)
    return sorted(
        {
            _module_name(path)
            for path, pf in graph.files.items()
            if not is_test_file(path)
            and any(t == m or t.startswith(m + ".") for t in pf.module.imports.values() for m in mods)
        }
    )


def probe_targets(graph: CodeGraph, name: str, problems: list[dict] | None = None, limit: int = 8) -> list[dict]:
    """The functions whose behaviour the upgrade could change, most at risk first: those with a problem from
    the check, then those that use the library most. [{file, function, uses}]"""
    from .graph import is_test_file

    count: dict[tuple[str, str], int] = {}
    for s in usage_sites(graph, name):
        if s["kind"] == "call" and s["function"] not in ("<module>",) and not is_test_file(s["file"]):
            count[(s["file"], s["function"])] = count.get((s["file"], s["function"]), 0) + 1
    risky = {(p["file"], p["function"]) for p in problems or []}
    ranked = sorted(count, key=lambda k: (k not in risky, -count[k], k))
    return [{"file": f, "function": fn, "uses": count[(f, fn)]} for f, fn in ranked[:limit]]


def write_probes(root: str | Path, graph: CodeGraph, name: str, targets: list[dict]) -> dict:
    """The model writes one probe per function: inputs only, no expected values; today's code is the
    reference. {probes: [{file, function, code}], skipped: [{file, function, why}], model}"""
    from .llm import LLMConfig, complete_json, last_error
    from .llm import status as llm_status

    cfg = LLMConfig.load(root)
    st = llm_status(cfg)
    if not st["available"]:
        return {"probes": [], "skipped": [], "model": None, "why": f"no model to write the probes: {st['reason']}"}
    probes, skipped = [], []
    for t in targets:
        pf = graph.files.get(t["file"])
        span = _function_span(pf, t["function"]) if pf else None
        if span is None:
            skipped.append({**t, "why": "could not find the function"})
            continue
        code = "".join(pf.lines[span[0] - 1 : span[1]])
        mod = _module_name(t["file"])
        top = t["function"].split(".")[0]
        ask = (
            f"Function `{t['function']}` in `{t['file']}` (import with `from {mod} import {top}`); it uses {name}.\n"
            f"```python\n{code[:8000]}```\n\nThe module's imports:\n```python\n"
            + "".join(pf.lines[: min(len(pf.lines), 40)])
            + "```\nName each scenario '"
            + t["function"]
            + ": <what it tries>'."
        )
        value = complete_json(
            cfg,
            PROBE_SYSTEM,
            [{"role": "user", "content": ask}],
            CODE_SCHEMA,
            max_tokens=2500,
            purpose="upgrade probe",
            check=compiles("probe.py"),
            fallback=code_from_text,
        )
        if value is None:
            skipped.append({**t, "why": last_error() or "the model did not answer"})
            continue
        body = _unfence(str(value["code"])).strip() + "\n"
        probes.append({"file": t["file"], "function": t["function"], "code": body})
    return {"probes": probes, "skipped": skipped, "model": cfg.model}


def _probe_program(probes: list[dict]) -> str:
    parts = [PROBE_HARNESS]
    for i, p in enumerate(probes):  # each probe on its own: one that fails to import doesn't hide the rest
        body = textwrap.indent(p["code"], "    ")
        parts.append(
            f"def _probe_{i}():\n{body}\n"
            f"try:\n    _probe_{i}()\nexcept BaseException as exc:\n"
            f"    _OUT[{p['function']!r} + ': (probe setup)'] = "
            f"{{'raised': type(exc).__name__, 'message': str(exc)[:300]}}\n"
            f"try:\n    from unittest import mock as _m\n    _m.patch.stopall()\nexcept Exception:\n    pass\n"
        )
    parts.append("_done()\n")
    return "\n".join(parts)


def _compare_probes(before: dict, after: dict) -> list[dict]:
    diffs = []
    for k in sorted(set(before) | set(after)):
        if before.get(k) != after.get(k):
            diffs.append({"scenario": k, "before": before.get(k), "after": after.get(k)})
    return diffs


def behaviour_problems(graph: CodeGraph, name: str, version: str, trial_out: dict, probes: list[dict]) -> list[dict]:
    """Behaviour the trial saw change, as problems `rewrite` can fix: keep what the code does today."""
    by_fn = {p["function"]: p["file"] for p in probes or []}
    out = []
    for c in ((trial_out or {}).get("behaviour") or {}).get("changed") or []:
        fn = c["scenario"].split(":", 1)[0].strip()
        file = by_fn.get(fn)
        pf = graph.files.get(file) if file else None
        span = _function_span(pf, fn) if pf else None
        if span is None:
            continue
        out.append(
            {
                "file": file,
                "line": span[0],
                "function": fn,
                "symbol": name,
                "kind": "call",
                "kwargs": [],
                "issue": "behaviour",
                "detail": f"`{c['scenario']}` gave {json.dumps(c['before'])[:400]} today and "
                f"{json.dumps(c['after'])[:400]} with {name} {version}: change the code so it gives today's result",
                "old_signature": None,
                "new_signature": None,
                "new_doc": "",
            }
        )
    return out


# --------------------------------------------------------------------------- before and after, in a sandbox


def trial(
    root: str | Path,
    name: str,
    version: str,
    patches: list[str],
    *,
    graph: CodeGraph | None = None,
    probes: list[dict] | None = None,
    progress=None,
    timeout: int = 1800,
) -> dict:
    """Today's code against a sandbox copy with the upgrade applied, three ways, none needing the repo's tests
    to be complete: every module that imports the library is imported in both; the probes (the functions that
    use it, called with the same inputs) must return or raise the same in both; and the repo's tests, if any,
    must not fail only after. Nothing in the repo changes."""
    from . import proof, proof_env, upgrade

    root = Path(root).resolve()
    tell = progress or (lambda msg: None)
    out: dict = {
        "before": None,
        "after": None,
        "lock": None,
        "patches_applied": 0,
        "patches_failed": 0,
        "imports": None,
        "behaviour": None,
        "tests_ran": False,
    }
    modules = import_modules(graph, name) if graph is not None else []
    with tempfile.TemporaryDirectory(prefix="codewright-upgrade-") as tmp:
        tmp = Path(tmp)
        now = proof_env.resolve(root, None, build=True)
        base = tmp / "before"
        shutil.copytree(root, base, ignore=proof.IGNORE, symlinks=True)
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
        tell(f"Building an environment with {name} {version}")
        env = proof_env.resolve(work, None, build=True)
        if env.python == now.python:
            out["error"] = (
                "could not build an environment with the new version (needs uv and a uv.lock or requirements file): "
                + (env.note or "")
            )
            return out

        if modules:  # 1. does every module that uses it still import?
            tell(f"Importing the {len(modules)} module(s) that use {name}, before and after")
            args = [json.dumps(modules)]
            b, b_out = _run_script(base, now.python, IMPORT_SCRIPT, args, 300)
            a, a_out = _run_script(work, env.python, IMPORT_SCRIPT, args, 300)
            b, a = b or {}, a or {}
            out["imports"] = {
                "modules": len(modules),
                "broken": [{"module": m, "error": a[m]} for m in modules if b.get(m) is None and a.get(m)],
                "already_failing": [m for m in modules if b.get(m)],
                "error": None if (b or not modules) and a else (a_out or b_out)[-600:],
            }

        if probes:  # 2. do the functions that use it behave the same?
            tell(f"Calling {len(probes)} function(s) with the same inputs, before and after")
            program = _probe_program(probes)
            b, b_out = _run_script(base, now.python, program, [], 600)
            a, a_out = _run_script(work, env.python, program, [], 600)
            if b is None or a is None:
                out["behaviour"] = {"error": "the probes did not run: " + ((b_out if b is None else a_out)[-800:])}
            else:
                ran = {k: v for k, v in b.items() if not k.endswith("(probe setup)")}
                out["behaviour"] = {
                    "functions": len(probes),
                    "scenarios": len(ran),
                    "changed": _compare_probes(b, a),
                    "probe_errors": [k for k in b if k.endswith("(probe setup)")],
                }

        if proof.has_tests(root):  # 3. the repo's own tests, if it has any
            tell("Running the tests on today's code")
            out["before"] = _summary(proof.run_tests(base, now.python, timeout, fail_fast=False))
            tell(f"Today: {out['before']['passed']} passed, {len(out['before']['failed'])} failing")
            tell(f"Running the tests with {name} {version}")
            out["after"] = _summary(proof.run_tests(work, env.python, timeout, fail_fast=False))
            out["tests_ran"] = True

    reasons = []
    if out["tests_ran"]:
        before, after = set(out["before"]["failed"]), set(out["after"]["failed"])
        out["new_failures"] = sorted(after - before)
        out["fixed"] = sorted(before - after)
        if out["new_failures"]:
            reasons.append(f"{len(out['new_failures'])} test(s) fail only after")
        elif out["after"]["passed"] < out["before"]["passed"] * 0.98:
            reasons.append("fewer tests pass after")
    else:
        out["new_failures"], out["fixed"] = [], []
    if out["imports"] and out["imports"]["broken"]:
        reasons.append(f"{len(out['imports']['broken'])} module(s) no longer import")
    if out["behaviour"] and out["behaviour"].get("changed"):
        reasons.append(f"{len(out['behaviour']['changed'])} call(s) behave differently")
    evidence = bool(out["tests_ran"] or (out["imports"] and out["imports"]["modules"]) or out["behaviour"])
    out["safe"] = evidence and not reasons
    out["reasons"] = reasons
    out["evidence"] = [
        x
        for x in (
            out["imports"] and f"{out['imports']['modules']} module(s) imported",
            out["behaviour"]
            and "scenarios" in out["behaviour"]
            and f"{out['behaviour']['scenarios']} call(s) in {out['behaviour']['functions']} function(s) compared",
            out["tests_ran"] and f"{out['after']['passed']} test(s) passed after",
        )
        if x
    ]
    if not evidence:
        out["error"] = out.get("error") or "nothing could be compared: no module imports it and no probes or tests ran"
    tell(
        ("Safe: " if out["safe"] else "Not safe yet: ")
        + ("; ".join(reasons) if reasons else ", ".join(out["evidence"]) or "no evidence")
    )
    return out
