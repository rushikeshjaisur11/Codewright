"""An AI agent that makes code changes the way a careful developer does: it searches and reads the repo, looks
things up on the web (release notes, migration guides, API docs), edits files, and runs the tests and small
scripts to check its work, all in a sandbox copy of the repo. The repo itself is untouched: the result is a
patch, with the test run that backs it, for the user to review and apply.

The sandbox is a temporary copy of the repository; commands run with a time limit, and scripts the model
writes run without the API keys and tokens in the environment. It is not a container: code runs as the
user, as the repo's own tests do.
"""

from __future__ import annotations

import difflib
import fnmatch
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import llm

MAX_RESULT = 14000  # characters of one tool result the model sees
MAX_CONTEXT = 360000  # characters of tool results kept in the conversation; older ones are trimmed
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".tox", "dist"}
SECRET = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH", re.I)

SYSTEM = """You are a senior software engineer making a change to a repository. You work in a sandbox copy of it:
nothing you do touches the real repository; your edits become a patch the user reviews.

Work like this:
1. Understand before editing: use search_code and list_files to find the code involved, and read_file to read
   it (and its callers and tests) before you change it.
2. When the change involves a library, an API or a version you are not certain about, look it up: web_search,
   then fetch_url on the official docs, changelog or migration guide. Do not guess APIs.
3. Make the smallest change that does the job, in the repository's existing style. Use edit_file with the
   exact text to replace (copy it from read_file output, without the line numbers); use write_file only for
   new files. Don't touch unrelated code, and don't add files the task doesn't need.
4. Check your work: run_tests on the tests that cover what you changed (or the whole suite when it is small),
   and run_python for quick experiments. If something fails, read the output, fix it and run it again. Never
   claim tests pass unless you ran them and saw them pass.
5. Call finish with a short summary: what you changed and why, what you checked, and anything left for the
   user to do. If the task can't be done, say why in finish rather than making a speculative change."""


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


S = {"type": "string"}
I = {"type": "integer"}  # noqa: E741
B = {"type": "boolean"}

TOOLS = [
    {
        "name": "list_files",
        "description": "List files in the repository, matching a glob such as '**/*.py' or 'src/app/*'.",
        "parameters": _obj({"pattern": {**S, "description": "Glob, relative to the repo root. Default '**/*'."}}),
    },
    {
        "name": "search_code",
        "description": "Search file contents with a regular expression. Returns 'path:line: text' for each match.",
        "parameters": _obj(
            {
                "pattern": {**S, "description": "Python regular expression (or plain text)."},
                "glob": {**S, "description": "Only files matching this glob, e.g. '**/*.py'."},
                "case_sensitive": B,
            },
            ["pattern"],
        ),
    },
    {
        "name": "read_file",
        "description": "Read a file, with line numbers. Long files come in pieces: pass start and end lines.",
        "parameters": _obj({"path": S, "start": I, "end": I}, ["path"]),
    },
    {
        "name": "edit_file",
        "description": "Replace exact text in a file. `old` must match the file exactly (whitespace included) and "
        "occur once, unless replace_all is true. Python files must still compile, or the edit is undone.",
        "parameters": _obj({"path": S, "old": S, "new": S, "replace_all": B}, ["path", "old", "new"]),
    },
    {
        "name": "write_file",
        "description": "Create a file, or replace a whole file, with the given content.",
        "parameters": _obj({"path": S, "content": S}, ["path", "content"]),
    },
    {
        "name": "run_tests",
        "description": "Run the repository's tests (pytest) in the sandbox, with your changes. Pass test files or "
        "node ids to run only those, and/or a -k keyword expression.",
        "parameters": _obj({"paths": {"type": "array", "items": S}, "keyword": S}),
    },
    {
        "name": "run_python",
        "description": "Run a short Python script in the sandbox (the repo is importable) and get its output. For "
        "quick experiments: check an API's signature, call a function. Time limit 90 seconds; no secrets in "
        "the environment.",
        "parameters": _obj({"code": S}, ["code"]),
    },
    {
        "name": "web_search",
        "description": "Search the web: release notes, migration guides, API docs, error messages.",
        "parameters": _obj({"query": S}, ["query"]),
    },
    {
        "name": "fetch_url",
        "description": "Read a public web page as text. Long pages come in pieces: pass offset to read on.",
        "parameters": _obj({"url": S, "offset": I}, ["url"]),
    },
    {
        "name": "finish",
        "description": "End the task: what you changed and why, what you checked (tests run and their result), and "
        "anything left for the user.",
        "parameters": _obj({"summary": S}, ["summary"]),
    },
]


class ToolError(Exception):
    """A tool call that can't be done; the message goes back to the model."""


@dataclass
class Sandbox:
    """A copy of the repo the agent may change, and what it changed."""

    root: Path
    work: Path
    python: str | None = None
    touched: set[str] = field(default_factory=set)
    test_runs: list[dict] = field(default_factory=list)
    edits: int = 0

    # ---------------------------------------------------------------- paths
    def path(self, rel: str) -> Path:
        rel = str(rel or "").strip().lstrip("/").removeprefix("./")
        if not rel:
            raise ToolError("no path given")
        p = (self.work / rel).resolve()
        try:
            inside = p.relative_to(self.work.resolve())
        except ValueError:
            raise ToolError(f"{rel} is outside the repository") from None
        if inside.parts and inside.parts[0] == ".git":
            raise ToolError("the .git folder can't be read or changed")
        return p

    def rel(self, p: Path) -> str:
        return p.resolve().relative_to(self.work.resolve()).as_posix()

    def files(self):
        for dirpath, dirs, names in os.walk(self.work):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info"))
            for n in sorted(names):
                yield Path(dirpath) / n

    # ---------------------------------------------------------------- tools
    def list_files(self, pattern: str = "**/*") -> str:
        pattern = (pattern or "**/*").strip().lstrip("./")
        out = []
        for p in self.files():
            r = self.rel(p)
            if fnmatch.fnmatch(r, pattern) or (pattern.startswith("**/") and fnmatch.fnmatch(r, pattern[3:])):
                out.append(r)
                if len(out) >= 400:
                    return "\n".join(out) + "\n… (more: narrow the pattern)"
        return "\n".join(out) or "no files match"

    def search_code(self, pattern: str, glob: str = "", case_sensitive: bool = False) -> str:
        flags = 0 if case_sensitive else re.I
        try:
            rx = re.compile(pattern, flags)
        except re.error:
            rx = re.compile(re.escape(pattern), flags)
        out, files = [], 0
        for p in self.files():
            r = self.rel(p)
            if glob and not (fnmatch.fnmatch(r, glob) or (glob.startswith("**/") and fnmatch.fnmatch(r, glob[3:]))):
                continue
            try:
                if p.stat().st_size > 1_000_000:
                    continue
                text = p.read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            hit = False
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    out.append(f"{r}:{i}: {line.strip()[:220]}")
                    hit = True
                    if len(out) >= 80:
                        return "\n".join(out) + "\n… (more matches: narrow the pattern or glob)"
            files += hit
        return "\n".join(out) or "no matches"

    def read_file(self, path: str, start: int = 1, end: int | None = None) -> str:
        p = self.path(path)
        if not p.is_file():
            raise ToolError(f"{path} does not exist")
        try:
            lines = p.read_bytes().decode("utf-8").splitlines()
        except UnicodeDecodeError:
            raise ToolError(f"{path} is not a text file") from None
        start = max(1, int(start or 1))
        end = min(len(lines), int(end) if end else start + 399, start + 599)
        body = "\n".join(f"{n:>5}| {lines[n - 1]}" for n in range(start, end + 1))
        more = f"\n… lines {end + 1}-{len(lines)} not shown (read on with start={end + 1})" if end < len(lines) else ""
        return f"{self.rel(p)} ({len(lines)} lines)\n{body}{more}"

    def _write(self, p: Path, text: str, before: str | None) -> str:
        if p.suffix == ".py":
            try:
                compile(text, str(p), "exec")
            except (SyntaxError, ValueError) as exc:
                raise ToolError(
                    f"not saved: the file would not compile ({getattr(exc, 'msg', exc)}, line "
                    f"{getattr(exc, 'lineno', '?')}). Fix the code and try again."
                ) from None
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
        self.touched.add(self.rel(p))
        self.edits += 1
        for pyc in p.parent.glob(f"__pycache__/{p.stem}.*.pyc"):
            pyc.unlink(missing_ok=True)
        if before is None:
            return f"created {self.rel(p)} ({len(text.splitlines())} lines)"
        diff = "".join(difflib.unified_diff(before.splitlines(True), text.splitlines(True), "before", "after", n=1))
        return f"saved {self.rel(p)}:\n{diff[:3000]}"

    def edit_file(self, path: str, old: str, new: str, replace_all: bool = False) -> str:
        p = self.path(path)
        if not p.is_file():
            raise ToolError(f"{path} does not exist (use write_file to create it)")
        text = p.read_bytes().decode("utf-8", "replace")
        if not old:
            raise ToolError("`old` is empty: give the exact text to replace")
        if old not in text and "\r\n" in text and old.replace("\r\n", "\n").replace("\n", "\r\n") in text:
            old, new = old.replace("\r\n", "\n").replace("\n", "\r\n"), new.replace("\r\n", "\n").replace("\n", "\r\n")
        count = text.count(old)
        if count == 0:
            first = old.strip().splitlines()[0] if old.strip() else old
            lines = text.splitlines()
            near = difflib.get_close_matches(first.strip(), [ln.strip() for ln in lines], n=3, cutoff=0.5)
            where = [f"{i + 1}: {ln}" for i, ln in enumerate(lines) if ln.strip() in near][:5]
            raise ToolError(
                f"`old` was not found in {path}. Read the file again and copy the text exactly (indentation "
                "included, no line numbers)." + ("\nClosest lines:\n" + "\n".join(where) if where else "")
            )
        if count > 1 and not replace_all:
            at = [str(text[: m.start()].count("\n") + 1) for m in re.finditer(re.escape(old), text)][:10]
            raise ToolError(
                f"`old` occurs {count} times in {path} (lines {', '.join(at)}): include more surrounding lines "
                "so it is unique, or set replace_all"
            )
        return self._write(p, text.replace(old, new) if replace_all else text.replace(old, new, 1), text)

    def write_file(self, path: str, content: str) -> str:
        p = self.path(path)
        before = p.read_bytes().decode("utf-8", "replace") if p.is_file() else None
        return self._write(p, content, before)

    def _python(self) -> str:
        if self.python is None:
            from . import proof_env

            self.python = proof_env.resolve(self.root, None, build=True).python
        return self.python

    def run_tests(self, paths: list[str] | None = None, keyword: str = "", timeout: int = 900) -> str:
        from . import proof

        if not proof.has_tests(self.work):
            return "this repository has no tests; check the change with run_python instead"
        selection = [str(x) for x in (paths or []) if str(x).strip()]
        for s in selection:
            self.path(s.split("::")[0])
        if keyword:
            selection += ["-k", keyword]
        run = proof.run_tests(self.work, self._python(), timeout, selection=selection, fail_fast=False)
        self.test_runs.append({"ok": run.ok, "selection": selection, "after_edit": self.edits, "at": time.time()})
        head = f"{'PASSED' if run.ok else 'FAILED'} in {run.seconds:.1f}s (exit code {run.code})\n"
        return head + run.output[-9000:]

    def run_python(self, code: str, timeout: int = 90) -> str:
        script = self.work / f".codewright_agent_{os.getpid()}_{threading.get_ident()}.py"
        script.write_text(code)
        env = {k: v for k, v in os.environ.items() if not SECRET.search(k) and not k.startswith("CODEWRIGHT_AI")}
        env["PYTHONPATH"] = os.pathsep.join([str(self.work), str(self.work / "src")])
        env.pop("VIRTUAL_ENV", None)
        try:
            proc = subprocess.run(
                [self._python(), str(script)], cwd=self.work, capture_output=True, text=True, timeout=timeout, env=env
            )
        except subprocess.TimeoutExpired:
            return f"timed out after {timeout}s"
        finally:
            script.unlink(missing_ok=True)
        out = (proc.stdout[-6000:] + ("\n[stderr]\n" + proc.stderr[-4000:] if proc.stderr.strip() else "")).strip()
        return f"exit code {proc.returncode}\n{out or '(no output)'}"

    # ---------------------------------------------------------------- the result
    def patch(self) -> str:
        from .refactor import diff, read_source

        changes = {}
        for rel in sorted(self.touched):
            before = read_source(self.root / rel).splitlines(keepends=True) if (self.root / rel).is_file() else None
            after = read_source(self.work / rel).splitlines(keepends=True) if (self.work / rel).is_file() else []
            changes[rel] = (before, after)
        return diff(changes) or ""


def _describe(name: str, args: dict) -> str:
    """One line for the progress log."""
    if name in ("read_file", "edit_file", "write_file"):
        extra = (
            f" lines {args.get('start')}-{args.get('end') or '…'}" if name == "read_file" and args.get("start") else ""
        )
        return f"{args.get('path', '')}{extra}"
    if name == "search_code":
        return f"/{args.get('pattern', '')}/" + (f" in {args['glob']}" if args.get("glob") else "")
    if name == "run_tests":
        return " ".join((args.get("paths") or []) + ([f"-k {args['keyword']}"] if args.get("keyword") else [])) or "all"
    if name == "run_python":
        first = next((ln for ln in str(args.get("code", "")).splitlines() if ln.strip()), "")
        return first[:100]
    return str(args.get("query") or args.get("url") or args.get("pattern") or "")[:160]


def _trim(turns: list[dict]) -> None:
    """Keep the conversation within MAX_CONTEXT by shortening the oldest tool results."""
    sizes = [sum(len(r["content"]) for r in t["results"]) if t["role"] == "tool" else 0 for t in turns]
    total = sum(sizes)
    for t, size in zip(turns, sizes, strict=True):
        if total <= MAX_CONTEXT:
            return
        if t["role"] != "tool" or size < 400:
            continue
        for r in t["results"]:
            total -= len(r["content"]) - 60
            r["content"] = r["content"][:60] + "… [trimmed to save room; call the tool again if you need it]"


def run(
    root: str | Path,
    task: str,
    *,
    cfg: llm.LLMConfig | None = None,
    context: str = "",
    web: bool = True,
    tests: bool = True,
    max_steps: int = 40,
    progress=None,
    stop: threading.Event | None = None,
    verify: bool = True,
) -> dict:
    """Do `task` in a sandbox copy of the repo. Returns {summary, patch, files, steps, tests, verification,
    applies, model, steps_used, ended}: `ended` is "finished", "answered", "stopped", "step limit" or "error"."""
    from . import proof
    from .proof import patch_error
    from .websearch import SearchError, fetch, search
    from .websearch import status as web_status

    root = Path(root).resolve()
    cfg = cfg or llm.LLMConfig.load(root)
    st = llm.status(cfg)
    if not st["available"]:
        raise RuntimeError(f"no model to run the agent: {st['reason']}. Set one up in Settings")
    tell = progress or (lambda event: None)
    web_ok = web and web_status()["available"]
    tools = [
        t
        for t in TOOLS
        if (web_ok or t["name"] not in ("web_search", "fetch_url"))
        and (tests or t["name"] not in ("run_tests", "run_python"))
    ]
    steps: list[dict] = []
    out: dict = {"summary": "", "patch": "", "files": [], "steps": steps, "model": cfg.model, "ended": "error"}
    with tempfile.TemporaryDirectory(prefix="codewright-agent-") as tmp:
        work = Path(tmp) / "repo"
        tell({"kind": "status", "text": "Copying the repository into a sandbox"})
        shutil.copytree(root, work, ignore=proof.IGNORE, symlinks=True)
        box = Sandbox(root, work)

        def call(name: str, args: dict) -> str:
            if "_invalid_json" in args:
                raise ToolError("the arguments were not valid JSON; call the tool again")
            if name == "list_files":
                return box.list_files(args.get("pattern") or "**/*")
            if name == "search_code":
                return box.search_code(
                    str(args.get("pattern", "")), args.get("glob") or "", bool(args.get("case_sensitive"))
                )
            if name == "read_file":
                return box.read_file(str(args.get("path", "")), args.get("start") or 1, args.get("end"))
            if name == "edit_file":
                return box.edit_file(
                    str(args.get("path", "")),
                    str(args.get("old", "")),
                    str(args.get("new", "")),
                    bool(args.get("replace_all")),
                )
            if name == "write_file":
                return box.write_file(str(args.get("path", "")), str(args.get("content", "")))
            if name == "run_tests" and tests:
                return box.run_tests(args.get("paths") or [], str(args.get("keyword") or ""))
            if name == "run_python" and tests:
                return box.run_python(str(args.get("code", "")))
            if name == "web_search" and web_ok:
                try:
                    rows = search(str(args.get("query", "")))
                except SearchError as exc:
                    raise ToolError(f"search failed: {exc}") from None
                return (
                    "\n\n".join(f"{i + 1}. {r['title']}\n{r['url']}\n{r['snippet']}" for i, r in enumerate(rows))
                    or "no results"
                )
            if name == "fetch_url" and web_ok:
                try:
                    page = fetch(str(args.get("url", "")), offset=int(args.get("offset") or 0))
                except SearchError as exc:
                    raise ToolError(str(exc)) from None
                tail = f"\n… more (read on with offset={page['offset'] + len(page['text'])})" if page["more"] else ""
                return f"{page['title']}\n{page['url']}\n\n{page['text']}{tail}"
            raise ToolError(f"there is no tool called {name}")

        intro = (
            f"Task:\n{task.strip()}"
            + (f"\n\nContext:\n{context.strip()}" if context.strip() else "")
            + "\n\nRepository files (top level): "
            + ", ".join(sorted(p.name for p in work.iterdir() if p.name not in SKIP_DIRS)[:80])
            + (
                ""
                if web_ok
                else "\n\n(Web search is not available: rely on the repository and the installed packages.)"
            )
            + ("" if tests else "\n\n(Running code is turned off for this task: check by reading.)")
        )
        turns: list[dict] = [{"role": "user", "content": intro}]
        finished = None
        try:
            for n in range(max_steps):
                if stop is not None and stop.is_set():
                    out["ended"] = "stopped"
                    break
                tell({"kind": "status", "text": f"Thinking (step {n + 1} of up to {max_steps})"})
                turn = llm.tool_step(cfg, SYSTEM, turns, tools, max_tokens=8000, purpose="agent")
                turns.append(turn)
                if turn["text"]:
                    steps.append({"kind": "text", "text": turn["text"][:4000]})
                    tell(steps[-1])
                if not turn["calls"]:
                    if turn["truncated"]:
                        turns.append(
                            {"role": "user", "content": "Your answer was cut off. Continue, in smaller steps."}
                        )
                        continue
                    finished, out["ended"] = turn["text"], "answered"
                    break
                results = []
                for c in turn["calls"]:
                    if c["name"] == "finish":
                        finished = str(c["args"].get("summary") or turn["text"] or "")
                        results.append({"id": c["id"], "name": c["name"], "content": "ok"})
                        continue
                    t0 = time.perf_counter()
                    try:
                        res, ok = call(c["name"], c["args"]), True
                    except ToolError as exc:
                        res, ok = f"error: {exc}", False
                    except Exception as exc:  # a tool must never end the run
                        res, ok = f"error: {type(exc).__name__}: {exc}", False
                    if len(res) > MAX_RESULT:
                        res = res[: MAX_RESULT - 200] + f"\n… [{len(res) - MAX_RESULT + 200} more characters cut]"
                    step = {
                        "kind": "tool",
                        "tool": c["name"],
                        "detail": _describe(c["name"], c["args"]),
                        "ok": ok,
                        "result": res[:1500],
                        "seconds": round(time.perf_counter() - t0, 1),
                    }
                    steps.append(step)
                    tell(step)
                    results.append({"id": c["id"], "name": c["name"], "content": res})
                if n == max_steps - 4 and finished is None:
                    results[-1]["content"] += "\n\n[3 steps left: wrap up, check your change, then call finish.]"
                turns.append({"role": "tool", "results": results})
                _trim(turns)
                if finished is not None:
                    out["ended"] = "finished"
                    break
            else:
                out["ended"] = "step limit"
        except llm.LLMError as exc:
            out["error"] = str(exc)
            out["ended"] = "error"
            tell({"kind": "status", "text": f"The model call failed: {exc}"})
        out["summary"] = (finished or "").strip() or (
            "The agent stopped before finishing" + (f": {out.get('error')}" if out.get("error") else ".")
        )
        out["patch"] = box.patch()
        out["files"] = sorted(r for r in box.touched if (r in out["patch"]))
        out["steps_used"] = sum(1 for s in steps if s["kind"] == "tool")
        last = box.test_runs[-1] if box.test_runs else None
        out["tests"] = {**last, "after_last_edit": last["after_edit"] == box.edits} if last else None
        if out["patch"]:
            err = patch_error(root, out["patch"], check=True)
            out["applies"], out["apply_error"] = not err, err
            if verify and tests and proof.has_tests(work):
                out["verification"] = _verify(box, root, tell)
        else:
            out["applies"] = False
    return out


def _verify(box: Sandbox, root: Path, tell) -> dict:
    """The repo's test suite on the changed copy; failures are re-run on today's code, so only the ones the
    change caused count."""
    from . import proof

    tell({"kind": "status", "text": "Running the test suite on the change"})
    after = proof.run_tests(box.work, box._python(), 1800, fail_fast=False)
    failed = sorted(set(re.findall(r"^(?:FAILED|ERROR) (\S+)", after.output, re.M)))
    out = {
        "ok": after.ok,
        "seconds": round(after.seconds, 1),
        "failed": failed,
        "new_failures": [],
        "output": after.output[-3000:],
    }
    if after.ok or not failed:
        if not after.ok:
            out["new_failures"] = ["(the test run failed before any test ran: see output)"]
        return out
    tell({"kind": "status", "text": f"{len(failed)} test(s) failed: checking whether they fail on today's code too"})
    with tempfile.TemporaryDirectory(prefix="codewright-agent-base-") as tmp:
        base = Path(tmp) / "repo"
        shutil.copytree(root, base, ignore=proof.IGNORE, symlinks=True)
        before = proof.run_tests(
            base, box._python(), 1800, selection=[f.split(" ")[0] for f in failed], fail_fast=False
        )
    already = set(re.findall(r"^(?:FAILED|ERROR) (\S+)", before.output, re.M))
    out["new_failures"] = [f for f in failed if f not in already]
    out["already_failing"] = sorted(already & set(failed))
    return out


# --------------------------------------------------------------------------- tasks built from Codewright's findings


def task_for_problems(heading: str, problems: list[dict], notes_text: str = "") -> tuple[str, str]:
    """(task, context) for the agent from upgrade or migration problems."""
    by_place: dict[tuple[str, str], list[dict]] = {}
    for p in problems:
        by_place.setdefault((p["file"], p.get("function") or "<module>"), []).append(p)
    lines = []
    for (file, fn), items in by_place.items():
        lines.append(f"- {file}, {'module level' if fn in ('<module>', '<imports>') else f'`{fn}`'}:")
        for p in items:
            lines.append(f"  - line {p.get('line', '?')}: {p['detail']}")
            if p.get("new_signature"):
                lines.append(f"    new signature: {p.get('symbol', '')}{p['new_signature']}")
            if p.get("old_signature"):
                lines.append(f"    old signature: {p.get('symbol', '')}{p['old_signature']}")
    task = (
        f"{heading}. Change the code so it works with this, keeping its behaviour exactly as it is today. "
        "Fix every place listed; look up the library's docs or changelog when the right replacement is not "
        "obvious; run the tests that cover these files."
    )
    context = (
        "Places to change:\n" + "\n".join(lines) + (f"\n\nRelease notes:\n{notes_text[:12000]}" if notes_text else "")
    )
    return task, context


def as_patches(result: dict, fixes: list[str]) -> dict:
    """An agent run in the shape the upgrade and migration dialogs take: {patches, skipped, model, agent}."""
    patches = []
    if result.get("patch") and result.get("applies", True):
        import hashlib

        patches.append(
            {
                "id": hashlib.sha1(result["patch"].encode()).hexdigest()[:10],
                "file": ", ".join(result["files"]) or "(files)",
                "function": "agent",
                "patch": result["patch"],
                "explanation": result["summary"][:600],
                "fixes": fixes,
            }
        )
    skipped = []
    if not patches:
        why = result.get("apply_error") or result.get("error") or result["summary"][:300] or "the agent made no change"
        skipped.append({"file": "", "function": "agent", "why": why})
    return {
        "patches": patches,
        "skipped": skipped,
        "model": result.get("model"),
        "agent": {k: result.get(k) for k in ("summary", "steps", "tests", "verification", "ended", "steps_used")},
    }
