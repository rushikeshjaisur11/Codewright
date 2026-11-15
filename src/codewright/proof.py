"""The proof runner: run the repo's tests before and after a patch, in a throwaway copy.

v0 runs locally (no cloud sandbox), so code never leaves the machine. A finding
is "verified" only when the suite passes before the change and still passes after.
Anything else (no patch, no tests, a suite that already fails) stays "unverified".
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .models import Finding, ProofStatus

IGNORE = shutil.ignore_patterns(".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache")


@dataclass
class TestRun:
    ok: bool
    seconds: float
    output: str


def has_tests(root: Path) -> bool:
    if (root / "tests").is_dir() or (root / "test").is_dir():
        return True
    if (root / "pytest.ini").exists() or (root / "conftest.py").exists():
        return True
    return any(root.rglob("test_*.py"))


def run_tests(workdir: Path, python: str, timeout: int) -> TestRun:
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            [python, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        ok, out = proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-1000:]
    except subprocess.TimeoutExpired:
        ok, out = False, f"timed out after {timeout}s"
    except OSError as exc:
        ok, out = False, str(exc)
    return TestRun(ok, time.perf_counter() - start, out)


def apply_patch(workdir: Path, patch: str) -> bool:
    for tool in (["git", "apply", "--whitespace=nowarn", "-"], ["patch", "-p1", "--forward", "--quiet"]):
        if shutil.which(tool[0]) is None:
            continue
        proc = subprocess.run(tool, cwd=workdir, input=patch, capture_output=True, text=True)
        if proc.returncode == 0:
            return True
    return False


def prove(findings: list[Finding], root: str | Path, python: str, timeout: int = 300) -> TestRun | None:
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

    with tempfile.TemporaryDirectory(prefix="codewright-") as tmp:
        base = Path(tmp) / "base"
        shutil.copytree(root, base, ignore=IGNORE, symlinks=True)
        baseline = run_tests(base, python, timeout)
        if not baseline.ok:
            for f in with_patch:
                f.proof_detail = "test suite already fails before the change"
            return baseline
        for f in with_patch:
            work = Path(tmp) / f.id
            shutil.copytree(base, work, symlinks=True)
            if not apply_patch(work, f.patch):
                f.proof_detail = "patch did not apply cleanly"
                continue
            after = run_tests(work, python, timeout)
            if after.ok and f.covered:
                f.proof = ProofStatus.VERIFIED
                f.proof_detail = (
                    f"tests that reach `{f.function}` pass before ({baseline.seconds:.1f}s) "
                    f"and after ({after.seconds:.1f}s) the change"
                )
            elif after.ok:
                f.proof_detail = (
                    f"the suite still passes after the change, but no test reaches `{f.function}`, "
                    "so this proves only that nothing else broke; add a test to verify it"
                )
            else:
                f.proof = ProofStatus.BROKE_TESTS
                f.proof_detail = "tests fail after the change:\n" + after.output[-800:]
            shutil.rmtree(work, ignore_errors=True)
        return baseline
