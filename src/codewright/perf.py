"""Performance proof: measure a patch's gain instead of claiming it.

After the tests prove a patch keeps behaviour, the tests that run the changed function run
again, before and after the patch, alternating, ``rounds`` times each. A small pytest plugin
times only the target function (outermost calls, per thread; a generator or coroutine counts
only while it runs, not while it waits), and the best round on each side is compared, which
is how ``timeit`` keeps noise out. The wall time of the selected tests is recorded too.

The verdict is "faster" or "slower" only beyond ``NOISE`` (10 %) and when the function ran
for at least ``MIN_MS``; anything else is "no measurable change", which is also an answer:
the patch is about correctness or readability, not speed.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .models import Finding, ProofStatus

PLUGIN = "_codewright_perf"
NOISE = 0.10
MIN_MS = 0.2

_PERF_PLUGIN = r"""
import json, os, sys, threading, time

_ROOT = os.path.realpath(os.environ["CODEWRIGHT_PERF_ROOT"]) + os.sep
_OUT = os.environ["CODEWRIGHT_PERF_OUT"]
_TARGETS = {(f, q) for f, q in json.loads(os.environ["CODEWRIGHT_PERF_TARGETS"])}
_is_target = {}
_totals = {}
_calls = {}
_local = threading.local()
_clock = time.perf_counter_ns


def _match(code):
    hit = _is_target.get(code)
    if hit is None:
        real = os.path.realpath(code.co_filename)
        key = None
        if real.startswith(_ROOT):
            q = getattr(code, "co_qualname", code.co_name).replace(".<locals>", "")
            cand = (real[len(_ROOT):].replace(os.sep, "/"), q)
            if cand in _TARGETS:
                key = cand
        hit = _is_target[code] = key or False
    return hit


def _profile(frame, event, arg):
    if event == "call":
        key = _match(frame.f_code)
        if key:
            stack = getattr(_local, "stack", None)
            if stack is None:
                stack = _local.stack = []
            stack.append((frame, key, _clock()))
    elif event == "return":
        stack = getattr(_local, "stack", None)
        if stack and stack[-1][0] is frame:
            _, key, start = stack.pop()
            if not any(k == key for _, k, _ in stack):  # outermost only: recursion counts once
                _totals[key] = _totals.get(key, 0) + (_clock() - start)
                _calls[key] = _calls.get(key, 0) + 1


sys.setprofile(_profile)
threading.setprofile(_profile)


def pytest_sessionfinish(session, exitstatus):
    sys.setprofile(None)
    threading.setprofile(None)
    with open(_OUT, "w") as fh:
        json.dump([[f, q, _totals.get((f, q), 0), _calls.get((f, q), 0)] for f, q in _TARGETS], fh)
"""


@dataclass
class Measurement:
    verdict: str  # faster | slower | no-change | too-small | failed
    before_ms: float = 0.0
    after_ms: float = 0.0
    calls_before: int = 0
    calls_after: int = 0
    tests_before_s: float = 0.0
    tests_after_s: float = 0.0
    rounds: int = 0
    speedup: float = 1.0  # before / after
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self, function: str) -> str:
        if self.verdict == "failed":
            return f"not measured: {self.note}"
        if self.verdict == "too-small":
            return (
                f"not measured: the tests spend only {self.before_ms:.2f} ms in `{function}`, too little to time "
                "reliably"
            )
        after = f"{self.after_ms:.1f}" if self.after_ms >= 0.1 else f"{self.after_ms:.2f}"
        inside = f"{self.before_ms:.1f} ms → {after} ms in `{function}`"
        calls = ""
        if self.calls_after < self.calls_before:
            calls = (
                f", {self.calls_before} → {self.calls_after} calls (calls answered before reaching it, by a cache "
                "for example, aren't timed: the test time below includes them)"
            )
        elif self.calls_after > self.calls_before:
            calls = f", {self.calls_before} → {self.calls_after} calls"
        tests = f"; the tests took {self.tests_before_s:.2f} s → {self.tests_after_s:.2f} s"
        best = f" (best of {self.rounds})"
        if self.verdict == "faster":
            return f"measured {self.speedup:.1f}× faster: {inside}{calls}{tests}{best}"
        if self.verdict == "slower":
            return f"measured {1 / self.speedup:.1f}× slower: {inside}{calls}{tests}{best}"
        return f"no measurable speed change: {inside}{calls}{tests}{best}"


def judge(before_ns: int, after_ns: int) -> tuple[str, float]:
    before_ms, after_ms = before_ns / 1e6, after_ns / 1e6
    if before_ms < MIN_MS and after_ms < MIN_MS:
        return "too-small", 1.0
    speedup = before_ms / after_ms if after_ms > 0 else math.inf
    if speedup >= 1 + NOISE:
        return "faster", speedup
    if speedup <= 1 / (1 + NOISE):
        return "slower", speedup
    return "no-change", speedup


def _write_plugin(where: Path) -> Path:
    where.mkdir(parents=True, exist_ok=True)
    (where / f"{PLUGIN}.py").write_text(_PERF_PLUGIN)
    return where


def timed_run(workdir: Path, python: str, f: Finding, selection: list[str], plugin_dir: Path, timeout: int):
    """(ns inside the function, calls, seconds for the tests) for one run, or None if the run failed."""
    from .proof import run_tests

    out = plugin_dir / f"result-{time.monotonic_ns()}.json"
    env = {
        "CODEWRIGHT_PERF_ROOT": str(workdir),
        "CODEWRIGHT_PERF_OUT": str(out),
        "CODEWRIGHT_PERF_TARGETS": json.dumps([[f.file, f.function]]),
    }
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        run = run_tests(workdir, python, timeout, selection=selection, plugins=[(plugin_dir, PLUGIN)])
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    if not run.ok or not out.exists():
        return None
    try:
        rows = json.loads(out.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    ns = sum(r[2] for r in rows)
    calls = sum(r[3] for r in rows)
    return ns, calls, run.seconds


def measure(
    findings: list[Finding],
    base: Path,
    scratch: Path,
    python: str,
    selections: dict[str, list[str]],
    *,
    rounds: int = 3,
    timeout: int = 300,
    limit: int = 10,
) -> None:
    """Time each verified, test-reached finding before and after its patch; fills `f.perf` and adds the
    result to `f.proof_detail`. Runs one at a time, alternating sides, so the two compete for nothing."""
    from .proof import _drop_bytecode, apply_patch

    plugin_dir = _write_plugin(scratch / "perf-plugin")
    todo = [f for f in findings if f.patch and f.proof == ProofStatus.VERIFIED and f.reached_by and f.function]
    for f in todo[:limit]:
        work = scratch / f"bench-{f.id}"
        shutil.copytree(base, work, symlinks=True)
        before_runs, after_runs = [], []
        try:
            if apply_patch(work, f.patch):
                _drop_bytecode(work, f.file)
                for _ in range(rounds):  # alternate sides so drift (CPU heat, caches) hits both
                    b = timed_run(base, python, f, selections.get(f.id, []), plugin_dir, timeout)
                    a = timed_run(work, python, f, selections.get(f.id, []), plugin_dir, timeout) if b else None
                    if b is None or a is None:
                        break
                    before_runs.append(b)
                    after_runs.append(a)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if not before_runs or not after_runs:
            m = Measurement("failed", note="a timed test run failed")
        else:
            bn, bc, _ = min(before_runs)
            an, ac, _ = min(after_runs)
            verdict, speedup = judge(bn, an)
            m = Measurement(
                verdict,
                round(bn / 1e6, 3),
                round(an / 1e6, 3),
                bc,
                ac,
                round(min(r[2] for r in before_runs), 3),
                round(min(r[2] for r in after_runs), 3),
                len(after_runs),
                round(speedup, 2) if math.isfinite(speedup) else 99.0,
            )
        f.perf = m.to_dict()
        f.proof_detail += f". Speed: {m.summary(f.function)}"
    for f in todo[limit:]:
        f.proof_detail += f". Speed: not measured (only the first {limit} verified findings are timed)"
