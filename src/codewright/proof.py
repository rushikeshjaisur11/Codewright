"""The proof runner: run the repo's tests before and after a patch, in a throwaway copy.

v0 runs locally (no cloud sandbox), so code never leaves the machine. A finding
is "verified" only when the suite passes before the change, still passes after it,
and a test actually ran the changed function. Which tests run which functions is
recorded during the baseline run (``sys.monitoring`` on Python 3.12+, a profile
hook on 3.11), so "reached" means executed, not "looks reachable in the call graph".
Anything else (no patch, no tests, a suite that already fails) stays "unverified".

Patched runs go in parallel, each in its own copy of the repo. A run that fails
in parallel is re-run alone before the patch is blamed, so a suite that is not
safe to run concurrently can't get a good patch dropped.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .models import Finding, ProofStatus

IGNORE = shutil.ignore_patterns(".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache")
COLLECTION = "<collection>"  # functions called while pytest imports test modules, outside any test
PLUGIN = "_codewright_trace"

# A pytest plugin, loaded with `-p`, that records which repo functions each test calls.
# It writes {"hits": [[file, qualname, [test ids]]...], "outside": n} when the session ends;
# "outside" counts functions run from the original checkout instead of the copy.
_TRACE_PLUGIN = r"""
import json, os, sys, threading
import pytest

_ROOT = os.path.realpath(os.environ["CODEWRIGHT_TRACE_ROOT"]) + os.sep
_ORIGINAL = os.path.realpath(os.environ["CODEWRIGHT_TRACE_ORIGINAL"]) + os.sep
_OUT = os.environ["CODEWRIGHT_TRACE_OUT"]
_current = ["<collection>"]
_hits = {}
_outside = set()
_real = {}
_stop = []


def _record(code):
    f = code.co_filename
    real = _real.get(f)
    if real is None:
        real = _real[f] = os.path.realpath(f)
    if real.startswith(_ROOT):
        key = (real[len(_ROOT):].replace(os.sep, "/"), code.co_qualname.replace(".<locals>", ""))
        tests = _hits.setdefault(key, set())
        if len(tests) < 50:
            tests.add(_current[0])
    elif real.startswith(_ORIGINAL):
        parts = real[len(_ORIGINAL):].split(os.sep)
        if parts[0] not in (".venv", "venv", ".tox", ".nox") and "site-packages" not in parts:
            _outside.add(real)


if hasattr(sys, "monitoring"):
    _M = sys.monitoring
    _TOOL = next((i for i in (4, 3, 2, 5, 1, 0) if _M.get_tool(i) is None), None)
    if _TOOL is not None:
        def _start(code, offset):
            _record(code)
            return _M.DISABLE  # once per function per test; restart_events() re-arms it

        _M.use_tool_id(_TOOL, "codewright")
        _M.register_callback(_TOOL, _M.events.PY_START, _start)
        _M.set_events(_TOOL, _M.events.PY_START)
        _stop.append(lambda: (_M.set_events(_TOOL, 0), _M.free_tool_id(_TOOL)))
else:
    _seen = set()

    def _profile(frame, event, arg):
        if event == "call":
            k = (frame.f_code, _current[0])
            if k not in _seen:
                _seen.add(k)
                _record(frame.f_code)

    sys.setprofile(_profile)
    threading.setprofile(_profile)
    _stop.append(lambda: (sys.setprofile(None), threading.setprofile(None)))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _current[0] = item.nodeid
    if hasattr(sys, "monitoring"):
        sys.monitoring.restart_events()
    yield
    _current[0] = "<collection>"


def pytest_sessionfinish(session, exitstatus):
    for stop in _stop:
        stop()
    os.makedirs(_OUT, exist_ok=True)
    with open(os.path.join(_OUT, f"{os.getpid()}.json"), "w") as fh:
        json.dump({"hits": [[f, q, sorted(t)] for (f, q), t in _hits.items()], "outside": len(_outside)}, fh)
"""


@dataclass
class TestRun:
    ok: bool
    seconds: float
    output: str


@dataclass
class Trace:
    """Which tests ran which functions during the baseline run."""

    reached: dict[tuple[str, str], list[str]] = field(default_factory=dict)  # (file, qualname) -> test ids
    outside: int = 0  # functions the tests ran from the original checkout rather than the copy

    def tests_for(self, f: Finding) -> list[str]:
        return [t for t in self.reached.get((f.file, f.function), []) if t != COLLECTION]


def has_tests(root: Path) -> bool:
    if (root / "tests").is_dir() or (root / "test").is_dir():
        return True
    if (root / "pytest.ini").exists() or (root / "conftest.py").exists():
        return True
    return any(root.rglob("test_*.py"))


def default_jobs() -> int:
    try:
        return max(1, int(os.environ["CODEWRIGHT_PROOF_JOBS"]))
    except (KeyError, ValueError):
        return min(4, os.cpu_count() or 1)


def _src_dirs(workdir: Path) -> list[str]:
    """`src/` folders of the copy, so its packages shadow an editable install of the original."""
    found = [*workdir.glob("src"), *workdir.glob("*/src"), *workdir.glob("*/*/src")]
    return [str(p) for p in sorted(found) if p.is_dir() and not any(part.startswith(".") for part in p.parts[-3:])]


def run_tests(
    workdir: Path,
    python: str,
    timeout: int,
    *,
    trace_out: Path | None = None,
    original: Path | None = None,
) -> TestRun:
    cmd = [python, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"]
    env = dict(os.environ)
    paths = _src_dirs(workdir)
    if trace_out is not None:
        plugin_dir = trace_out.parent / "plugin"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        (plugin_dir / f"{PLUGIN}.py").write_text(_TRACE_PLUGIN)
        paths.append(str(plugin_dir))
        cmd += ["-p", PLUGIN]
        env |= {
            "CODEWRIGHT_TRACE_ROOT": str(workdir),
            "CODEWRIGHT_TRACE_ORIGINAL": str(original or workdir),
            "CODEWRIGHT_TRACE_OUT": str(trace_out),
        }
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    if paths:
        env["PYTHONPATH"] = os.pathsep.join(paths)
    start = time.perf_counter()
    try:
        proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=timeout, env=env)
        ok, out = proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-1000:]
    except subprocess.TimeoutExpired:
        ok, out = False, f"timed out after {timeout}s"
    except OSError as exc:
        ok, out = False, str(exc)
    return TestRun(ok, time.perf_counter() - start, out)


def read_trace(out: Path) -> Trace | None:
    """Merge the plugin's per-process files; None when the plugin wrote nothing."""
    files = sorted(out.glob("*.json")) if out.is_dir() else []
    if not files:
        return None
    reached: dict[tuple[str, str], set[str]] = {}
    outside = 0
    for p in files:
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        outside += int(data.get("outside", 0))
        for file, qual, tests in data.get("hits", []):
            reached.setdefault((file, qual), set()).update(tests)
    return Trace({k: sorted(v) for k, v in reached.items()}, outside)


def apply_patch(workdir: Path, patch: str, *, reverse: bool = False) -> bool:
    tools = (
        ["git", "apply", "--whitespace=nowarn", *(["-R"] if reverse else []), "-"],
        ["patch", "-p1", "--quiet", *(["-R"] if reverse else ["--forward"])],
    )
    for tool in tools:
        if shutil.which(tool[0]) is None:
            continue
        proc = subprocess.run(tool, cwd=workdir, input=patch, capture_output=True, text=True)
        if proc.returncode == 0:
            return True
    return False


def _drop_bytecode(workdir: Path, file: str) -> None:
    """Remove the cached bytecode of a file a patch touched, so the next run can't import a stale copy."""
    src = workdir / file
    for pyc in src.parent.glob(f"__pycache__/{src.stem}.*.pyc"):
        pyc.unlink(missing_ok=True)


def _reached_detail(f: Finding, trace: Trace | None) -> str:
    tests = trace.tests_for(f) if trace else []
    if trace is None:
        return f"tests that reach `{f.function}` (by the static call graph)"
    shown = ", ".join(f"`{t}`" for t in tests[:2]) + (f" and {len(tests) - 2} more" if len(tests) > 2 else "")
    return f"tests that run `{f.function}` ({shown})"


def prove(
    findings: list[Finding],
    root: str | Path,
    python: str,
    timeout: int = 300,
    jobs: int | None = None,
) -> TestRun | None:
    root = Path(root)
    with_patch = [f for f in findings if f.patch]
    for f in findings:
        if not f.patch:
            f.proof_detail = "no mechanical patch; review the suggested code by hand"
    if not with_patch:
        return None
    if not has_tests(root):
        for f in with_patch:
            f.proof_detail = "repo has no tests to run"
        return None

    with tempfile.TemporaryDirectory(prefix="codewright-") as tmp_name:
        tmp = Path(tmp_name)
        base = tmp / "base"
        shutil.copytree(root, base, ignore=IGNORE, symlinks=True)
        baseline = run_tests(base, python, timeout, trace_out=tmp / "trace", original=root.resolve())
        if not baseline.ok:
            for f in with_patch:
                f.proof_detail = "test suite already fails before the change"
            return baseline

        trace = read_trace(tmp / "trace")
        if trace is not None:  # what actually ran beats what the call graph guessed
            for f in findings:
                f.reached_by = trace.tests_for(f)[:5]
                f.covered = bool(trace.tests_for(f))

        jobs = max(1, min(jobs or default_jobs(), len(with_patch)))
        slots: queue.Queue[Path] = queue.Queue()
        for i in range(jobs):
            work = tmp / f"work{i}"
            shutil.copytree(base, work, symlinks=True)
            slots.put(work)

        def attempt(f: Finding) -> TestRun | None:
            """Run the suite with `f`'s patch in a free copy; None if the patch doesn't apply."""
            work = slots.get()
            try:
                if not apply_patch(work, f.patch):
                    return None
                run = run_tests(work, python, timeout)
                if not apply_patch(work, f.patch, reverse=True):
                    shutil.rmtree(work, ignore_errors=True)
                    shutil.copytree(base, work, symlinks=True)
                _drop_bytecode(work, f.file)
                return run
            finally:
                slots.put(work)

        with ThreadPoolExecutor(max_workers=jobs) as pool:
            runs = dict(zip([f.id for f in with_patch], pool.map(attempt, with_patch), strict=True))
        retried: set[str] = set()
        if jobs > 1:  # a failure under parallel load is only the patch's fault if it also fails alone
            for f in with_patch:
                run = runs[f.id]
                if run is not None and not run.ok:
                    runs[f.id] = attempt(f)
                    retried.add(f.id)

        for f in with_patch:
            after = runs[f.id]
            if after is None:
                f.proof_detail = "patch did not apply cleanly"
            elif after.ok and f.covered:
                f.proof = ProofStatus.VERIFIED
                f.proof_detail = (
                    f"{_reached_detail(f, trace)} pass before ({baseline.seconds:.1f}s) "
                    f"and after ({after.seconds:.1f}s) the change"
                )
            elif after.ok:
                f.proof_detail = (
                    f"the suite still passes after the change, but no test reaches `{f.function}`, "
                    "so this proves only that nothing else broke; add a test to verify it"
                )
                if trace is not None and trace.outside:
                    f.proof_detail += (
                        f". The tests ran code from {root} rather than the patched copy "
                        "(an editable install?), so the patch itself was not exercised"
                    )
            else:
                f.proof = ProofStatus.BROKE_TESTS
                f.proof_detail = "tests fail after the change:\n" + after.output[-800:]
            if f.id in retried and after is not None and after.ok:
                f.proof_detail += " (passed when re-run on its own)"
        return baseline
