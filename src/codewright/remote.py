"""Shared MCP server: which repositories a caller may reach, and how they get onto the server.

A local MCP server (stdio) reviews any folder on the user's machine. A shared one (Streamable
HTTP, one server for a team) can't see anyone's laptop, so a tool's `repo` argument is one of:

* the name of a folder the server was started with (``--root``), or a path inside one;
* a git URL on an allowed host (``https://github.com/org/repo``, ``...#branch``), cloned shallowly
  into the server's cache and refreshed when older than a few minutes;
* nothing at all: ``review_files`` reviews files the agent sends inline, in a throwaway folder.

Nothing on a shared server runs a repository's code unless the admin allows it (``--allow-proof``):
proof runs the repo's test suite and installs its dependencies.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

DEFAULT_GIT_HOSTS = ("github.com", "gitlab.com", "bitbucket.org")
REFRESH_AFTER = 300  # seconds before a cached clone is fetched again
CLONE_TIMEOUT = 180
MAX_FILES = 500
MAX_BYTES = 3_000_000
REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")


class AccessError(ValueError):
    """The caller asked for something this server doesn't serve; the message says what it does serve."""


@dataclass
class Policy:
    shared: bool = False  # False: a local stdio server, any folder on this machine
    roots: list[Path] = field(default_factory=list)
    git_hosts: tuple[str, ...] = DEFAULT_GIT_HOSTS
    allow_proof: bool = True
    cache: Path = field(default_factory=lambda: _home() / "repos")
    _locks: dict[str, threading.Lock] = field(default_factory=dict)
    _guard: threading.Lock = field(default_factory=threading.Lock)

    def lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    # ------------------------------------------------------------------ resolving `repo`

    def resolve(self, repo: str) -> tuple[Path, bool]:
        """(folder to review, whether it changed since the last call) for a tool's `repo` argument."""
        repo = (repo or "").strip()
        if not repo:
            raise AccessError("pass `repo`: " + self.describe())
        if is_git_url(repo):
            return self.clone(repo)
        if not self.shared:
            return Path(repo).expanduser(), False
        for root in self.roots:
            if repo == root.name:
                return root, False
        p = Path(repo).expanduser()
        if p.is_absolute():
            p = p.resolve()
            for root in self.roots:
                if p == root or p.is_relative_to(root):
                    return p, False
        raise AccessError(f"`{repo}` isn't served here. " + self.describe())

    def describe(self) -> str:
        names = ", ".join(f"`{r.name}`" for r in self.roots)
        parts = []
        if names:
            parts.append(f"a repository this server has ({names})")
        if self.git_hosts:
            parts.append(f"a git URL on {', '.join(self.git_hosts)} (append #branch for a branch)")
        if self.shared:
            parts.append("or call `review_files` with the files themselves")
        else:
            parts.insert(0, "the repository's absolute path on this machine")
        return "Use " + "; ".join(parts) + "."

    # ------------------------------------------------------------------ git

    def clone(self, url: str) -> tuple[Path, bool]:
        clean, ref = parse_git_url(url, self.git_hosts)
        key = hashlib.sha256(f"{clean}#{ref}".encode()).hexdigest()[:12]
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", clean.split("://", 1)[1].removesuffix(".git"))[-80:]
        dest = self.cache / f"{slug}-{key}"
        with self.lock(key):
            marker = dest / ".git" / "codewright-fetched"
            if marker.exists() and time.time() - marker.stat().st_mtime < REFRESH_AFTER:
                return dest, False
            before = _head(dest) if marker.exists() else None
            if marker.exists():
                _git(["fetch", "--depth", "1", "--no-tags", "origin", ref or "HEAD"], cwd=dest)
                _git(["reset", "--hard", "--quiet", "FETCH_HEAD"], cwd=dest)
            else:
                shutil.rmtree(dest, ignore_errors=True)
                dest.parent.mkdir(parents=True, exist_ok=True)
                branch = ["--branch", ref] if ref else []
                _git(["clone", "--depth", "1", "--single-branch", "--no-tags", *branch, "--", clean, str(dest)])
            marker.touch()
            return dest, _head(dest) != before

    def cached_clones(self) -> list[str]:
        if not self.cache.is_dir():
            return []
        out = []
        for d in sorted(self.cache.iterdir()):
            proc = subprocess.run(
                ["git", "-C", str(d), "remote", "get-url", "origin"], capture_output=True, text=True, timeout=10
            )
            if proc.returncode == 0:
                out.append(proc.stdout.strip())
        return out


def is_git_url(s: str) -> bool:
    return s.startswith(("https://", "http://", "git@", "ssh://", "git://", "file://", "ext::"))


def parse_git_url(url: str, hosts: tuple[str, ...]) -> tuple[str, str]:
    """(https URL without credentials, ref or "") for an allowed host; refuses anything else."""
    p = urlsplit(url)
    if p.scheme != "https":
        raise AccessError("only https git URLs are accepted, e.g. https://github.com/org/repo")
    if p.username or p.password:
        raise AccessError("don't put credentials in the URL; the server uses its own git credentials")
    host = (p.hostname or "").lower()
    if host not in hosts:
        raise AccessError(f"git host `{host}` isn't allowed on this server (allowed: {', '.join(hosts) or 'none'})")
    parts = [s for s in PurePosixPath(p.path).parts if s != "/"]
    if len(parts) < 2 or any(s in (".", "..") or s.startswith("-") for s in parts):
        raise AccessError("expected https://host/owner/repo")
    ref = p.fragment
    if ref and (not REF.match(ref) or ".." in ref):
        raise AccessError(f"`{ref}` isn't a branch or tag name")
    port = f":{p.port}" if p.port else ""
    return f"https://{host}{port}/{'/'.join(parts)}", ref


def _git(args: list[str], cwd: Path | None = None) -> None:
    token = os.environ.get("CODEWRIGHT_GIT_TOKEN")
    config = ["-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "core.hooksPath=/dev/null"]
    if token:  # GitHub and GitLab both take a token as the password of basic auth
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        config += ["-c", f"http.extraHeader=Authorization: Basic {basic}"]
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1")
    proc = subprocess.run(
        ["git", *config, *args], cwd=cwd, capture_output=True, text=True, timeout=CLONE_TIMEOUT, env=env
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["failed"]
        msg = detail[0].replace(token, "***") if token else detail[0]
        raise AccessError(f"git {args[0]} failed: {msg[:300]}")


def _head(path: Path) -> str | None:
    proc = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _home() -> Path:
    return Path(os.environ.get("CODEWRIGHT_HOME", "~/.codewright")).expanduser()


# --------------------------------------------------------------------------- files sent inline


def write_files(files: dict[str, str]) -> Path:
    """Write the agent's files into a fresh temporary folder, refusing paths that escape it."""
    if not files:
        raise AccessError("pass at least one file, e.g. {'app/models.py': '...', 'uv.lock': '...'}")
    if len(files) > MAX_FILES:
        raise AccessError(f"at most {MAX_FILES} files per call")
    total = sum(len(v.encode()) for v in files.values())
    if total > MAX_BYTES:
        raise AccessError(f"at most {MAX_BYTES // 1_000_000} MB per call; send the files you're working on")
    root = Path(tempfile.mkdtemp(prefix="codewright-files-"))
    for name, body in files.items():
        rel = PurePosixPath(name.replace("\\", "/"))
        if rel.is_absolute() or ".." in rel.parts or not rel.parts or "\0" in name or rel.parts[0] == ".git":
            shutil.rmtree(root, ignore_errors=True)
            raise AccessError(f"`{name}`: use a path relative to the repository root")
        target = root.joinpath(*rel.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    return root
