"""Which Python runs the repo's tests during proof.

Proof is only meaningful when the tests run with the repo's own dependencies. In order:

1. ``--python`` when given;
2. the repo's own virtualenv (``.venv``/``venv``);
3. an environment built from the repo's lockfile (``uv.lock``, else ``requirements*.txt``)
   with uv, cached under ``~/.codewright/envs`` and reused while the lockfile is unchanged.
   Only third-party dependencies are installed: the repo's own packages are imported from
   the patched copy, so nothing is built or written inside the repo;
4. Codewright's own interpreter, as a last resort, which usually lacks the repo's packages.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import critic
from .index import resolve as resolve_index

BUILD_TIMEOUT = 900
RECIPE = "2"  # bump when the way environments are built changes, so cached ones are rebuilt


@dataclass
class TestEnv:
    python: str
    source: str  # human-readable: where this interpreter came from
    note: str = ""  # why a better option was not used


def _home() -> Path:
    return Path(os.environ.get("CODEWRIGHT_HOME", "~/.codewright")).expanduser()


def _fingerprint(root: Path, files: list[Path]) -> str:
    h = hashlib.sha256(f"{RECIPE}:{root.resolve()}".encode())
    for f in files:
        h.update(f.name.encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:16]


def _python_in(env: Path) -> Path:
    return env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _has_pytest(python: Path) -> bool:
    proc = subprocess.run([str(python), "-c", "import pytest"], capture_output=True, timeout=60)
    return proc.returncode == 0


def _build(root: Path, uv: str) -> tuple[Path | None, str]:
    """Build (or reuse) a cached environment for `root`. Returns (python, error)."""
    uv_lock = root / "uv.lock"
    reqs = sorted(root.glob("requirements*.txt"))
    inputs = [uv_lock, root / "pyproject.toml"] if uv_lock.exists() else reqs
    inputs = [f for f in inputs if f.exists()]
    if not inputs:
        return None, "no uv.lock or requirements*.txt to build an environment from"
    env = _home() / "envs" / _fingerprint(root, inputs)
    python, done = _python_in(env), env / ".codewright-ok"
    if done.exists() and python.exists():
        return python, ""
    shutil.rmtree(env, ignore_errors=True)
    env.parent.mkdir(parents=True, exist_ok=True)
    index = resolve_index(root)
    run_env = dict(os.environ, UV_PROJECT_ENVIRONMENT=str(env), **index.uv_env())
    if uv_lock.exists():
        # Dependencies only (--no-install-workspace): the repo's own packages are imported from
        # the patched copy, and building them could write egg-info or build output into the repo.
        # --all-packages: in a uv workspace the members, not the root, declare the dependencies.
        cmd = [uv, "sync", "--frozen", "--all-packages", "--all-extras", "--all-groups", "--no-install-workspace"]
        cmd += ["--project", str(root)]
        steps = [cmd]
    else:
        steps = [[uv, "venv", "--quiet", str(env)]]
        req_args = [arg for r in reqs for arg in ("-r", str(r))]
        steps.append([uv, "pip", "install", "--quiet", "--python", str(python), *req_args])
    try:
        for step in steps:
            proc = subprocess.run(step, capture_output=True, text=True, timeout=BUILD_TIMEOUT, env=run_env, cwd=root)
            if proc.returncode != 0:
                last = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["failed"]
                return None, f"`uv {step[1]}` failed: {last[0][:200]}"
        if not _has_pytest(python):
            subprocess.run(
                [uv, "pip", "install", "--quiet", "--python", str(python), "pytest"],
                capture_output=True,
                timeout=BUILD_TIMEOUT,
                check=True,
                env=run_env,  # same index as the dependencies: a locked-down network may have no PyPI
            )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"building the environment failed: {exc}"
    done.write_text("ok\n")
    return python, ""


def resolve(root: str | Path, python: str | None = None, *, build: bool = True) -> TestEnv:
    root = Path(root)
    if python:
        return TestEnv(os.path.abspath(python) if os.sep in python else python, "--python")
    own = critic.target_python(root)
    if own != sys.executable:
        return TestEnv(os.path.abspath(own), f"the repo's {Path(own).parent.parent.name}")
    note = ""
    if build and os.environ.get("CODEWRIGHT_BUILD_ENV", "1") != "0":
        uv = shutil.which("uv")
        if uv is None:
            note = "uv isn't installed, so no environment could be built from the lockfile"
        else:
            built, err = _build(root, uv)
            if built is not None:
                lock = "uv.lock" if (root / "uv.lock").exists() else "requirements*.txt"
                return TestEnv(str(built), f"an environment built from {lock} (cached in {built.parent.parent})")
            note = err
    else:
        note = "building an environment is turned off"
    return TestEnv(sys.executable, "Codewright's own interpreter", note)
