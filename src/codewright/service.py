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


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """(mtime, size) of every Python file a review scans; a change means re-review."""
    from .graph import iter_python_files

    out = {}
    for p in iter_python_files(Path(root).resolve()):
        try:
            st = p.stat()
        except OSError:
            continue
        out[str(p)] = (st.st_mtime_ns, st.st_size)
    return out


def diff_findings(before: dict, findings: list) -> tuple[dict, list, list]:
    """(findings by id, new since `before`, resolved since `before`)."""
    now = {f.id: f for f in findings}
    return now, [f for fid, f in now.items() if fid not in before], [f for fid, f in before.items() if fid not in now]


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
    revision: int = 0  # bumps whenever `result` changes, so clients poll something tiny
    watching: bool = False
    last_change: dict | None = None  # what the latest watched re-review found new or resolved
    _watch_stop: threading.Event | None = None

    # --------------------------------------------------------------- review
    def run_review(self, *, proof: bool = True, base: str | None = None, bench: bool = False) -> ReviewResult:
        with self.lock:
            self.running, self.error, self.progress = True, None, "scanning"
            try:
                self.result = review(self.root, run_proof=proof, top=None, base=base, bench=bench and proof)
                self.revision += 1
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

    # --------------------------------------------------------------- live watch
    def start_watch(self, interval: float = 1.0) -> None:
        """Re-review in the background whenever a Python file is saved, added or removed (proof off)."""
        if self.watching:
            return
        stop = threading.Event()
        self._watch_stop, self.watching = stop, True

        def loop() -> None:
            snap = snapshot(self.root)
            while not stop.wait(interval):
                current = snapshot(self.root)
                if current == snap or self.running:
                    continue
                before = {f.id: f for f in self.result.findings} if self.result else {}
                changed = sorted(
                    Path(p).relative_to(self.root).as_posix()
                    for p in (current.keys() ^ snap.keys())
                    | {p for p in current if snap.get(p) not in (None, current[p])}
                )
                snap = current
                try:
                    result = self.run_review(proof=False)
                except Exception:
                    continue  # recorded on self.error; keep watching
                _, new, gone = diff_findings(before, result.findings)
                self.last_change = {
                    "at": time.time(),
                    "files": changed[:20],
                    "new": [{"id": f.id, "title": f.title, "file": f.file, "line": f.line} for f in new],
                    "resolved": [{"id": f.id, "title": f.title, "file": f.file, "line": f.line} for f in gone],
                }

        threading.Thread(target=loop, daemon=True, name=f"codewright-watch-{self.root.name}").start()

    def stop_watch(self) -> None:
        if self._watch_stop is not None:
            self._watch_stop.set()
        self.watching, self._watch_stop = False, None

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
        from . import cards as card_store

        llm = cfg if llm_status(cfg)["available"] else None
        return answer(self.index, question, history, llm, cards=card_store.load(), stack=self.result.stack)

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
        r = self.ensure()
        return watcher.check(r.stack, self.root, graph=r.graph)

    # --------------------------------------------------------------- architecture rules
    def rules(self) -> dict:
        from . import rules as rules_mod

        return rules_mod.summary(self.ensure().graph, self.root)

    def rules_init(self, overwrite: bool = False) -> dict:
        """Write the rules today's code keeps (dependency directions between packages)."""
        from . import rules as rules_mod

        p = rules_mod.rules_path(self.root)
        if p.exists() and not overwrite:
            raise FileExistsError(f"{p.relative_to(self.root)} exists; pass overwrite to replace it")
        inferred = rules_mod.infer(self.ensure().graph)
        rules_mod.save(self.root, inferred)
        out = self.rules()
        if self.result is not None:
            self.result.rules = out
        return out

    # --------------------------------------------------------------- dependencies
    def deps(self, online: bool = True, python: str | None = None) -> dict:
        from . import deps as deps_mod
        from . import proof_env

        r = self.ensure()
        # python="" means: don't run any interpreter (licences and import names then come from the tables).
        # Only the repo's own environment describes its packages; Codewright's own would mislead.
        if python is None:
            env = proof_env.resolve(self.root, None, build=False)
            python = env.python if env.source.startswith("the repo's") else ""
        py = python
        return deps_mod.audit(self.root, r.graph, r.stack, python=py, online=online)

    # --------------------------------------------------------------- migrations
    def migrations(self) -> list[dict]:
        from . import migrate

        r = self.ensure()
        return migrate.overview(r.graph, r.stack, self.root)

    def migration_plan(self, name: str, batch: int = 5, version: str | None = None, save: bool = True) -> dict:
        from . import migrate

        r = self.ensure()
        data = migrate.plan(name, self.root, r.graph, r.stack, batch=batch, version=version)
        if save:
            migrate.save(self.root, data)
            return migrate.status(self.root, name, r.graph, r.stack)
        return data

    def migration_status(self, name: str) -> dict:
        from . import migrate

        r = self.ensure()
        return migrate.status(self.root, name, r.graph, r.stack)

    def migration_step(self, name: str, n: int) -> dict:
        from . import migrate

        r = self.ensure()
        step, found = migrate.step_findings(self.root, name, n, r.graph, r.stack)
        patch, skipped = migrate.combined_patch(self.root, found)
        data = migrate.status(self.root, name, r.graph, r.stack)
        return {
            "step": next(s for s in data["steps"] if s["n"] == n),
            "patch": patch,
            "skipped": skipped,
            "pr_body": migrate.pr_body(data, step, patch),
            "findings": [f.to_dict() for f in found],
        }

    def migration_apply(self, name: str, n: int) -> dict:
        """Apply a step's combined patch to the working tree (nothing is committed), then re-scan."""
        from .proof import apply_patch

        step = self.migration_step(name, n)
        if not step["patch"]:
            raise ValueError(f"step {n} has nothing to patch automatically; make its changes by hand")
        if not apply_patch(self.root, step["patch"]):
            raise RuntimeError("the step's patch did not apply; the files changed since the plan was made")
        self.run_review(proof=False)
        return {"applied": True, "files": step["step"]["files"], "status": self.migration_status(name)}

    # --------------------------------------------------------------- proposal review
    def critique(self, text: str) -> dict:
        from . import critique as critique_mod

        r = self.ensure()
        fb = Feedback.load(self.root)
        return critique_mod.review(text, self.root, r.graph, r.stack, constraints=fb.constraints)

    def insights(self) -> list[dict]:
        r = self.ensure()
        titles = {f.intent: f.title for f in r.findings + r.muted}
        return Feedback.load(self.root).insights(titles)


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
