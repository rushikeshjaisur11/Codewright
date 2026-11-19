"""Branches for fixes, from the Review page: see where you are, switch, start a fix branch, commit fixes.

Nothing here pushes, rebases or discards work. Switching branch is refused while there are uncommitted
changes (they would follow you or block the switch), and a commit only takes the files you choose.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def _git(root: Path, *args: str, check: bool = True) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git failed: {exc}") from None
    if check and proc.returncode != 0:
        raise GitError(
            (proc.stderr or proc.stdout).strip().removeprefix("fatal: ").removeprefix("error: ") or "git failed"
        )
    return proc.stdout


def is_repo(root: Path) -> bool:
    try:
        return _git(root, "rev-parse", "--is-inside-work-tree", check=False).strip() == "true"
    except GitError:
        return False


def changed_files(root: Path) -> list[dict]:
    """Uncommitted changes: [{"file", "status"}] (status as git shows it: M, A, D, ??)."""
    out = []
    for line in _git(root, "status", "--porcelain=v1", "--untracked-files=all").splitlines():
        if len(line) < 4:
            continue
        status, path = line[:2].strip() or "M", line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        out.append({"file": path.strip('"'), "status": status})
    return out


def status(root: Path) -> dict:
    root = Path(root)
    if not is_repo(root):
        return {"git": False}
    branch = _git(root, "branch", "--show-current").strip()
    head = _git(root, "rev-parse", "--short", "HEAD", check=False).strip()
    raw = _git(root, "for-each-ref", "--sort=-committerdate", "--format=%(refname:short)", "refs/heads").splitlines()
    default = ""
    ref = _git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False).strip()
    if ref:
        default = ref.removeprefix("origin/")
    elif "main" in raw:
        default = "main"
    elif "master" in raw:
        default = "master"
    return {
        "git": True,
        "branch": branch or None,
        "detached": not branch,
        "head": head,
        "default": default,
        "branches": raw[:100],
        "changed": changed_files(root),
    }


def _valid_branch(root: Path, name: str) -> str:
    name = name.strip()
    if not name or name.startswith("-"):
        raise GitError("give the branch a name")
    try:
        _git(root, "check-ref-format", "--branch", name)
    except GitError:
        raise GitError(f"{name!r} is not a valid branch name") from None
    return name


def switch(root: Path, branch: str) -> dict:
    root = Path(root)
    branch = _valid_branch(root, branch)
    changed = changed_files(root)
    if changed:
        names = ", ".join(c["file"] for c in changed[:5]) + ("…" if len(changed) > 5 else "")
        raise GitError(f"commit or stash your {len(changed)} uncommitted change(s) first ({names})")
    if branch not in _git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads").split():
        raise GitError(f"no local branch {branch!r}")
    _git(root, "switch", branch)
    return status(root)


def create(root: Path, name: str, start: str = "") -> dict:
    """Create `name` (from `start`, else from where you are) and switch to it. Uncommitted changes come along,
    so fixes already applied can be put on their own branch."""
    root = Path(root)
    name = _valid_branch(root, name)
    if start:
        start = _valid_branch(root, start)
    _git(root, "switch", "-c", name, *([start] if start else []))
    return status(root)


def suggest_name(titles: list[str]) -> str:
    import time

    word = re.sub(r"[^a-z0-9]+", "-", (titles[0] if titles else "fixes").lower()).strip("-")[:40] or "fixes"
    return f"codewright/{word}-{time.strftime('%m%d')}"


def commit(root: Path, message: str, files: list[str]) -> dict:
    """Commit exactly `files` (each must be an uncommitted change) with `message`."""
    root = Path(root)
    message = message.strip()
    if not message:
        raise GitError("write a commit message")
    if not files:
        raise GitError("choose the files to commit")
    changed = {c["file"] for c in changed_files(root)}
    unknown = [f for f in files if f not in changed]
    if unknown:
        raise GitError(f"not an uncommitted change: {', '.join(unknown[:3])}")
    for f in files:
        p = (root / f).resolve()
        if root.resolve() not in p.parents:
            raise GitError(f"outside the repository: {f}")
    _git(root, "add", "--", *files)
    _git(root, "commit", "-m", message, "--", *files)
    return {**status(root), "committed": _git(root, "rev-parse", "--short", "HEAD").strip()}
