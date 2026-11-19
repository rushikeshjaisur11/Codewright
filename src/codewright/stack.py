"""Stack discovery: which libraries, at which versions, does this repo actually run?

Lockfiles win over declared ranges, because advice must match what is installed.
Order of precedence: uv.lock > poetry.lock > Pipfile.lock > requirements pins > pyproject ranges.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement

from .models import Dependency, Stack, normalise

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", "build", "dist", ".mypy_cache"}


def discover(root: str | Path) -> Stack:
    root = Path(root).resolve()
    stack = Stack(root=str(root))

    # Lowest precedence first; later sources overwrite versions.
    _from_pyproject(root, stack)
    for req in sorted(root.glob("requirements*.txt")):
        _from_requirements(req, root, stack)
    _from_pipfile_lock(root, stack)
    _from_poetry_lock(root, stack)
    _from_uv_lock(root, stack)
    # A concrete version beats a requires-python lower bound: .python-version > Dockerfile > pyproject.
    declared_python, stack.python = stack.python, None
    _from_python_version(root, stack)
    _from_dockerfiles(root, stack)
    stack.python = stack.python or declared_python
    return stack


LOCKFILES = ("uv.lock", "poetry.lock", "Pipfile.lock")


def _add(
    stack: Stack, name: str, version: str | None, specifier: str | None, source: str, extras: set[str] | None = None
) -> None:
    key = normalise(name)
    existing = stack.dependencies.get(key)
    if existing is None:
        stack.dependencies[key] = Dependency(key, version, specifier, source, set(extras or ()))
        return
    existing.extras |= set(extras or ())
    if version:
        existing.version = version
        existing.source = source
    if specifier and not existing.specifier:
        existing.specifier = specifier


def _record(stack: Stack, path: Path, root: Path) -> None:
    stack.sources.append(str(path.relative_to(root)))


def _parse_requirement(line: str, stack: Stack, source: str) -> None:
    line = line.split("#", 1)[0].strip()
    if not line or line.startswith(("-", "git+", "http://", "https://")):
        return
    try:
        req = Requirement(line)
    except InvalidRequirement:
        return
    spec = str(req.specifier) or None
    pinned = None
    for s in req.specifier:
        if s.operator in ("==", "==="):
            pinned = s.version
    _add(stack, req.name, pinned, spec, source, {e.lower() for e in req.extras})


def _from_pyproject(root: Path, stack: Stack) -> None:
    path = root / "pyproject.toml"
    if not path.exists():
        return
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError:
        return
    _record(stack, path, root)
    project = data.get("project", {})
    if rp := project.get("requires-python"):
        stack.python = stack.python or rp
    deps = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        deps.extend(group)
    for group in data.get("dependency-groups", {}).values():
        deps.extend(d for d in group if isinstance(d, str))
    for d in deps:
        _parse_requirement(d, stack, "pyproject.toml")
    poetry = data.get("tool", {}).get("poetry", {})
    for name, spec in poetry.get("dependencies", {}).items():
        if name.lower() == "python":
            stack.python = stack.python or (spec if isinstance(spec, str) else None)
            continue
        if isinstance(spec, dict):
            spec = spec.get("version")
        _add(stack, name, None, spec if isinstance(spec, str) else None, "pyproject.toml")


def _from_requirements(path: Path, root: Path, stack: Stack) -> None:
    _record(stack, path, root)
    for line in path.read_text().splitlines():
        _parse_requirement(line, stack, path.name)


def _from_lock_packages(path: Path, root: Path, stack: Stack) -> None:
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError:
        return
    _record(stack, path, root)
    for pkg in data.get("package", []):
        if "name" in pkg and "version" in pkg:
            _add(stack, pkg["name"], str(pkg["version"]), None, path.name)


def _from_uv_lock(root: Path, stack: Stack) -> None:
    if (path := root / "uv.lock").exists():
        _from_lock_packages(path, root, stack)


def _from_poetry_lock(root: Path, stack: Stack) -> None:
    if (path := root / "poetry.lock").exists():
        _from_lock_packages(path, root, stack)


def _from_pipfile_lock(root: Path, stack: Stack) -> None:
    path = root / "Pipfile.lock"
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return
    _record(stack, path, root)
    for section in ("default", "develop"):
        for name, info in data.get(section, {}).items():
            version = str(info.get("version", "")).lstrip("=") or None
            _add(stack, name, version, None, "Pipfile.lock")


def _from_python_version(root: Path, stack: Stack) -> None:
    path = root / ".python-version"
    if path.exists():
        _record(stack, path, root)
        stack.python = path.read_text().strip().splitlines()[0]


_FROM_RE = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)", re.IGNORECASE | re.MULTILINE)
_PY_IMAGE_RE = re.compile(r"python:(\d+\.\d+(?:\.\d+)?)")


def _from_dockerfiles(root: Path, stack: Stack) -> None:
    for path in sorted(root.rglob("Dockerfile*")):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        _record(stack, path, root)
        for image in _FROM_RE.findall(path.read_text(errors="ignore")):
            stack.docker_images.append(image)
            if (m := _PY_IMAGE_RE.search(image)) and not stack.python:
                stack.python = m.group(1)


MIN_PYTHON = (3, 11)  # the oldest Python whose code Codewright reviews


def unsupported_python(stack: Stack) -> str | None:
    """The repo's Python version when it is older than MIN_PYTHON (its advice assumes 3.11+), else None."""
    m = re.search(r"(\d+)\.(\d+)", stack.python or "")
    if m and (int(m.group(1)), int(m.group(2))) < MIN_PYTHON:
        return f"{m.group(1)}.{m.group(2)}"
    return None


def python_version(stack: Stack) -> str:
    """Best guess at the concrete Python version the repo targets (major.minor)."""
    import sys

    raw = stack.python or ""
    m = re.search(r"(\d+)\.(\d+)", raw)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    return f"{sys.version_info.major}.{sys.version_info.minor}"


PROJECT_FILES = ("pyproject.toml", "setup.cfg", "uv.lock", "poetry.lock", "Pipfile.lock", "requirements.txt")


def for_path(stack: Stack, path: str) -> Stack:
    """The stack a file actually runs on: its nearest project folder's, layered over the repo root's.

    A monorepo of separate projects (one pyproject.toml each, no root lockfile) has an empty root
    stack; a uv workspace has the versions in the root lock and the ranges in each member. Both
    are right here: the project's own files win, the root fills in what they don't say.
    """
    root = Path(stack.root)
    folder = (root / path).parent
    while folder != root and root in folder.parents:
        if not (folder / "__init__.py").exists() and (
            any((folder / n).exists() for n in PROJECT_FILES) or (folder / "setup.py").exists()
        ):
            break
        folder = folder.parent
    else:
        return stack
    cache = stack.__dict__.setdefault("_projects", {})  # not a dataclass field: never serialised
    key = str(folder)
    if key not in cache:
        own = discover(folder)
        merged = Stack(
            root=stack.root,
            python=own.python or stack.python,
            dependencies={
                k: Dependency(d.name, d.version, d.specifier, d.source, set(d.extras))
                for k, d in stack.dependencies.items()
            },
            docker_images=own.docker_images or stack.docker_images,
            sources=[str(Path(folder.relative_to(root), s)) for s in own.sources] + stack.sources,
        )
        for k, d in own.dependencies.items():
            base = merged.dependencies.get(k)
            if base is None or d.version:
                merged.dependencies[k] = d
            else:  # declared in the project, locked at the root (a workspace)
                base.specifier = d.specifier or base.specifier
                base.extras |= d.extras
        cache[key] = merged
    return cache[key]
