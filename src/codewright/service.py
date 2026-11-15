"""One facade over the engine, shared by the web API, the MCP server and the CLI.

Holds a per-repo workspace with the latest review, map and design sessions in
memory, so the UI can switch between modes without re-scanning.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import archmap, design, watcher
from .chat import CodeIndex, answer
from .engine import ReviewResult, review
from .feedback import Feedback
from .llm import LLMConfig
from .llm import status as llm_status
from .models import Finding


@dataclass
class Workspace:
    root: Path
    result: ReviewResult | None = None
    index: CodeIndex | None = None
    running: bool = False
    progress: str = ""
    error: str | None = None
    last_run: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    # --------------------------------------------------------------- review
    def run_review(self, *, proof: bool = True, base: str | None = None) -> ReviewResult:
        with self.lock:
            self.running, self.error, self.progress = True, None, "scanning"
            try:
                self.result = review(self.root, run_proof=proof, top=None, base=base)
                self.index = CodeIndex(self.result.graph)
                self.last_run = time.time()
                with contextlib.suppress(OSError):
                    archmap.snapshot(self.root, archmap.ArchMap(self.result.graph, self.result.findings).system())
                return self.result
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                self.running, self.progress = False, ""

    def ensure(self) -> ReviewResult:
        if self.result is None:
            self.run_review(proof=False)
        return self.result

    def finding(self, fid: str) -> Finding | None:
        r = self.ensure()
        return next((f for f in r.findings + r.rejected + r.muted if f.id == fid), None)

    def feedback(self, fid: str, status: str, reason: str = "") -> dict:
        f = self.finding(fid)
        if f is None:
            raise KeyError(fid)
        fb = Feedback.load(self.root)
        if status == "clear":
            fb.clear(fid)
            f.status = None
        else:
            fb.record(f, status, reason)
            f.status = status
        fb.save(self.root)
        if self.result:
            self.result.feedback = fb
        return fb.stats()

    def mute(self, intent: str, path: str = "", reason: str = "", undo: bool = False) -> dict:
        fb = Feedback.load(self.root)
        (fb.unmute(intent, path) if undo else fb.mute(intent, path, reason))
        fb.save(self.root)
        return fb.stats()

    # --------------------------------------------------------------- map
    def amap(self) -> archmap.ArchMap:
        r = self.ensure()
        return archmap.ArchMap(r.graph, r.findings)

    def ask(self, question: str, history: list[dict] | None = None) -> dict:
        self.ensure()
        cfg = LLMConfig.load(self.root)
        return answer(self.index, question, history, cfg if llm_status(cfg)["available"] else None)

    def source(self, path: str) -> dict:
        p = (self.root / path).resolve()
        if self.root not in p.parents or not p.is_file():
            raise FileNotFoundError(path)
        return {"file": path, "text": p.read_text(errors="replace")}

    # --------------------------------------------------------------- design / drift
    def drift(self) -> dict | None:
        intent = design.load_intended(self.root)
        if intent is None:
            return None
        r = self.ensure()
        return design.drift(intent, self.amap().system(), r.graph)

    def releases(self) -> list[dict]:
        return watcher.check(self.ensure().stack, self.root)


# --------------------------------------------------------------------------- recent repos and folder browsing


def _home() -> Path:
    return Path(os.environ.get("CODEWRIGHT_HOME", "~/.codewright")).expanduser()


def load_recent() -> list[dict]:
    """Repos opened before, newest first, that still exist on disk."""
    p = _home() / "recent.json"
    try:
        items = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return [i for i in items if isinstance(i, dict) and Path(i.get("path", "")).is_dir()]


def _save_recent(items: list[dict]) -> None:
    with contextlib.suppress(OSError):
        d = _home()
        d.mkdir(parents=True, exist_ok=True)
        (d / "recent.json").write_text(json.dumps(items[:15], indent=2))


def remember(path: Path) -> None:
    items = [i for i in load_recent() if i.get("path") != str(path)]
    _save_recent([{"path": str(path), "name": path.name, "opened": time.time()}, *items])


def forget(path: str) -> None:
    _save_recent([i for i in load_recent() if i.get("path") != path])


def git_branch(root: Path) -> str | None:
    head = root / ".git" / "HEAD"
    try:
        ref = head.read_text().strip()
    except OSError:
        return None
    return ref.removeprefix("ref: refs/heads/") if ref.startswith("ref: ") else ref[:8]


def browse(path: str | None) -> dict:
    """Subfolders of `path` (default: home), marking which look like Python repos."""
    p = Path(path).expanduser().resolve() if path else Path.home()
    if not p.is_dir():
        raise FileNotFoundError(str(path))
    dirs = []
    try:
        entries = sorted(p.iterdir(), key=lambda e: e.name.lower())
    except PermissionError:
        entries = []
    for e in entries:
        if not e.is_dir() or e.name.startswith(".") or e.name in ("node_modules", "__pycache__"):
            continue
        try:
            names = {c.name for c in e.iterdir()} if os.access(e, os.R_OK) else set()
        except OSError:
            names = set()
        py = bool(names & {"pyproject.toml", "setup.py", "requirements.txt", "uv.lock", "poetry.lock", "Pipfile"})
        dirs.append({"name": e.name, "path": str(e), "git": ".git" in names, "python": py})
        if len(dirs) >= 400:
            break
    parent = str(p.parent) if p.parent != p else None
    return {
        "path": str(p),
        "parent": parent,
        "home": str(Path.home()),
        "dirs": dirs,
        "git": (p / ".git").exists(),
        "python": any((p / n).exists() for n in ("pyproject.toml", "setup.py", "requirements.txt", "uv.lock")),
    }


class Service:
    def __init__(self, roots: list[str | Path] | None = None):
        self.workspaces: dict[str, Workspace] = {}
        self.designs: dict[str, design.Session] = {}
        self.proposals: dict[str, dict] = {}
        for r in roots or []:
            self.open(r)

    def open(self, root: str | Path) -> tuple[str, Workspace]:
        p = Path(root).expanduser().resolve()
        if not p.is_dir():
            raise FileNotFoundError(str(root))
        wid = uuid.uuid5(uuid.NAMESPACE_URL, str(p)).hex[:10]
        if wid not in self.workspaces:
            self.workspaces[wid] = Workspace(p)
        remember(p)
        return wid, self.workspaces[wid]

    def close(self, wid: str) -> None:
        self.workspaces.pop(wid, None)

    def get(self, wid: str) -> Workspace:
        if wid not in self.workspaces:
            raise KeyError(wid)
        return self.workspaces[wid]

    def new_design(self) -> design.Session:
        s = design.Session(uuid.uuid4().hex[:10])
        self.designs[s.id] = s
        return s

    def design_session(self, sid: str) -> design.Session:
        if sid not in self.designs:
            raise KeyError(sid)
        return self.designs[sid]

    def propose(self, sid: str, root: str | Path | None = None) -> dict:
        s = self.design_session(sid)
        cfg = LLMConfig.load(root)
        p = design.propose(s, cfg if llm_status(cfg)["available"] else None, root)
        self.proposals[sid] = p
        return p
