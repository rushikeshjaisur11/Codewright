"""One facade over the engine, shared by the web API, the MCP server and the CLI.

Holds a per-repo workspace with the latest review, map and design sessions in
memory, so the UI can switch between modes without re-scanning.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import archmap, design, watcher
from .activity import Timer, note
from .chat import CodeIndex, answer
from .engine import ReviewResult, review
from .feedback import Feedback
from .llm import LLMConfig
from .llm import status as llm_status
from .models import Finding

# Review stages in order: key, what the UI says, and the share of the bar each takes (with proof, without).
STAGES = [
    ("stack", "Reading the stack and lockfiles", 3, 5),
    ("graph", "Parsing files into the code graph", 17, 50),
    ("detect", "Matching code against capability cards", 10, 30),
    ("check", "Checking the suggested APIs exist in your versions", 10, 10),
    ("baseline", "Running your tests once, before any change", 25, 0),
    ("proof", "Running your tests against each patch", 32, 0),
    ("rank", "Ranking findings", 3, 5),
]


def stage_progress(stage: str, done: int, total: int, *, proof: bool, started: float) -> dict:
    """Where a review is, for the progress bar: the stage, its count and an overall percentage."""
    steps = [s for s in STAGES if proof or s[3]]
    keys = [s[0] for s in steps]
    i = keys.index(stage) if stage in keys else 0
    weights = [s[2] if proof else s[3] for s in steps]
    before = sum(weights[:i])
    inside = weights[i] * (done / total) if total else 0
    return {
        "stage": keys[i],
        "label": steps[i][1],
        "step": i + 1,
        "steps": len(steps),
        "stages": [{"key": k, "label": s[1]} for k, s in zip(keys, steps, strict=True)],
        "done": done,
        "total": total,
        "percent": min(99, round(before + inside)),
        "started": started,
        "proof": proof,
    }


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
class Snapshot:
    """What is known before a review finishes: the parsed code and the stack, with no findings yet.
    Enough for the map, the file lists, the dependency audit and the rules."""

    graph: object
    stack: object
    findings: list = field(default_factory=list)


@dataclass
class Workspace:
    root: Path
    result: ReviewResult | None = None
    index: CodeIndex | None = None
    running: bool = False
    progress: dict | None = None  # stage_progress() of the review that is running
    error: str | None = None
    last_run: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    revision: int = 0  # bumps whenever `result` changes, so clients poll something tiny
    watching: bool = False
    last_change: dict | None = None  # what the latest watched re-review found new or resolved
    _watch_stop: threading.Event | None = None
    saved_revision: int = -1  # revision of `result` last written to .codewright/last-review.json
    _saved: dict | None = None
    early: Snapshot | None = None  # the graph of the review in progress, before findings exist
    _upgrade_job: dict | None = None  # the running (or last) upgrade safety step and its results
    _ai_batch: dict | None = None  # progress of the running (or last) batch of AI checks
    _releases: dict | None = None  # progress and rows of the running (or last) release check
    _cache_lock: threading.RLock = field(default_factory=threading.RLock)
    _aimap_graph: tuple | None = None  # (graph, AiMap) of the last scan, reused when only findings changed
    _cache: dict = field(default_factory=dict)  # maps and the AI page, built once per revision of `result`

    # --------------------------------------------------------------- review
    def run_review(
        self,
        *,
        proof: bool = True,
        base: str | None = None,
        bench: bool = False,
        files: list[str] | None = None,
        fresh: bool = False,
    ) -> ReviewResult:
        with self.lock:
            started = time.time()
            last = {"stage": None, "at": 0.0}
            what = "with tests" if proof else "quick scan, no tests"
            if files:
                what += f", tests on {len(files)} chosen file(s)"
            note(self.name, f"Review started ({what}{', since ' + base if base else ''})")

            def tell(stage: str, done: int, total: int) -> None:
                self.progress = p = stage_progress(stage, done, total, proof=proof, started=started)
                now = time.time()
                if stage != last["stage"]:
                    last.update(stage=stage, at=now)
                    note(self.name, f"{p['label']}" + (f" ({total})" if total else ""))
                elif total and (now - last["at"] > 2 or done == total):  # a heartbeat, not a flood
                    last["at"] = now
                    note(self.name, f"  {p['label']}: {done}/{total}")

            self.running, self.error = True, None
            tell("stack", 0, 0)
            try:
                self.result = review(
                    self.root,
                    run_proof=proof,
                    top=None,
                    base=base,
                    progress=tell,
                    bench=bench and proof,
                    files=set(files) if files else None,
                    fresh=fresh,
                    on_graph=self._graph_ready,
                )
                self.revision += 1
                self._cache.clear()
                r = self.result
                reused = r.graph.__dict__.get("detect_reused")
                note(
                    self.name,
                    f"Review done in {r.seconds:.1f}s: {len(r.findings)} findings in {len(r.graph.files)} files"
                    + (f"; {reused} unchanged files reused from the last run" if reused else "")
                    + (f"; {r.reused} proofs kept" if r.reused else "")
                    + (f"; tests ran on {len(r.proved_files)} file(s)" if r.proved_files else ""),
                )
                self.index = CodeIndex(self.result.graph)
                self.last_run = time.time()
                with contextlib.suppress(OSError):
                    archmap.snapshot(self.root, archmap.ArchMap(self.result.graph, self.result.findings).system())
                return self.result
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                note(self.name, f"Review failed: {self.error}", "error")
                raise
            finally:
                self.running, self.progress, self.early = False, None, None

    # --------------------------------------------------------------- the last review, kept on disk
    def _saved_path(self) -> Path:
        return self.root / ".codewright" / "last-review.json"

    def save_review(self, payload: dict) -> None:
        """Keep what the web app shows, so reopening the repo shows it at once instead of an empty page."""
        from .feedback import ensure_dir

        self.saved_revision = self.revision
        try:
            ensure_dir(self.root)
            tmp = self._saved_path().with_suffix(".tmp")
            tmp.write_text(json.dumps({"at": time.time(), "result": payload}))
            tmp.replace(self._saved_path())
        except (OSError, TypeError, ValueError):
            pass

    def saved_review(self) -> dict | None:
        if self._saved is None:
            try:
                data = json.loads(self._saved_path().read_text())
                self._saved = data if isinstance(data.get("result"), dict) else {}
            except (OSError, ValueError):
                self._saved = {}
        return self._saved or None

    @property
    def name(self) -> str:
        return self.root.name

    def _graph_ready(self, g, st) -> None:
        self.early = Snapshot(g, st)
        note(self.name, f"Code parsed: {len(g.files)} files; the map, AI and dependency pages can load now")

    @property
    def graph_ready(self) -> bool:
        return self.result is not None or self.early is not None

    def view(self):
        """The newest thing with a graph and a stack: the finished review, else the one in progress.
        While the first parse is running it waits for the graph (not for the findings)."""
        deadline = time.time() + 600
        while self.result is None and self.early is None and self.running and time.time() < deadline:
            time.sleep(0.05)
        if self.result is not None:
            return self.result
        if self.early is not None:
            return self.early
        return self.ensure()

    # --------------------------------------------------------------- live watch
    def start_watch(self, interval: float = 1.0) -> None:
        """Re-review in the background whenever a Python file in the scan scope is saved, added or removed
        (proof off). Uses the operating system's file events (watchfiles), so an idle repo costs nothing;
        without them it polls, less often the longer a scan of the repo takes."""
        if self.watching:
            return
        stop = threading.Event()
        self._watch_stop, self.watching = stop, True

        def rereview(changed: list[str]) -> None:
            before = {f.id: f for f in self.result.findings} if self.result else {}
            note(
                self.name,
                f"Live: {len(changed)} file(s) changed ({', '.join(changed[:3])}{'…' if len(changed) > 3 else ''})",
            )
            try:
                result = self.run_review(proof=False)
            except Exception:
                return  # recorded on self.error; keep watching
            _, new, gone = diff_findings(before, result.findings)
            self.last_change = {
                "at": time.time(),
                "files": changed[:20],
                "new": [{"id": f.id, "title": f.title, "file": f.file, "line": f.line} for f in new],
                "resolved": [{"id": f.id, "title": f.title, "file": f.file, "line": f.line} for f in gone],
            }

        def events() -> None:
            import watchfiles

            from . import scanscope
            from .stack import SKIP_DIRS

            def wanted(_change, path: str) -> bool:
                p = Path(path)
                if p.suffix != ".py":
                    return False
                try:
                    rel = p.resolve().relative_to(self.root)
                except ValueError:
                    return False
                if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
                    return False
                return scanscope.included(rel.as_posix(), scanscope.load(self.root))

            note(self.name, "Live: watching for saved files (file system events)")
            for batch in watchfiles.watch(self.root, watch_filter=wanted, stop_event=stop, debounce=600, step=100):
                while self.running and not stop.is_set():  # finish the review in flight, then take these
                    stop.wait(0.2)
                if stop.is_set():
                    return
                changed = sorted({Path(p).resolve().relative_to(self.root).as_posix() for _, p in batch})
                rereview(changed)

        def poll() -> None:
            t0 = time.perf_counter()
            snap = snapshot(self.root)
            cost = time.perf_counter() - t0
            wait = max(interval, min(30.0, cost * 20))  # a scan never takes more than ~5% of the time
            note(self.name, f"Live: checking for saved files every {wait:.0f}s (no file events available)")
            while not stop.wait(wait):
                if self.running:
                    continue
                current = snapshot(self.root)
                if current == snap:
                    continue
                changed = sorted(
                    Path(p).relative_to(self.root).as_posix()
                    for p in (current.keys() ^ snap.keys())
                    | {p for p in current if snap.get(p) not in (None, current[p])}
                )
                snap = current
                rereview(changed)

        def loop() -> None:
            try:
                events()
            except Exception as exc:  # watchfiles missing, or no inotify/FSEvents here: poll instead
                if stop.is_set():
                    return
                note(self.name, f"Live: file events unavailable ({type(exc).__name__}); polling instead", "warn")
                poll()

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
    def _cached(self, key, build):
        """Build once per revision of the review: the maps are pure functions of it, and the UI asks for them
        on every page visit and every click on a node."""
        r = self.view()
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit is None or hit[0] is not r:
                label = {"aimap": "Building the AI map", "ai": "Listing AI files and agents"}.get(
                    key if isinstance(key, str) else key[0], "Building the code map"
                )
                with Timer(self.name, label):
                    hit = self._cache[key] = (r, build(r))
        return hit[1]

    def amap(self, include_unused: bool = False) -> archmap.ArchMap:
        return self._cached(
            ("amap", include_unused), lambda r: archmap.ArchMap(r.graph, r.findings, include_unused=include_unused)
        )

    def ai_map(self):
        from . import aimap

        def build(r):
            # The scan reads only the code; findings are just counted. When the review that finishes has the
            # same parsed code the page was drawn from, reuse the scan instead of repeating it.
            prev = self._aimap_graph
            if prev is not None and prev[0] is r.graph:
                prev[1].findings = r.findings
                return prev[1]
            m = aimap.AiMap(r.graph, r.findings)
            self._aimap_graph = (r.graph, m)
            return m

        return self._cached("aimap", build)

    def ai(self) -> dict:
        """Files that need a model and the agent architecture (see aimap)."""

        def build(r):
            out = self.ai_map().to_dict()
            unused = {u["file"]: u["reason"] for u in self.amap().unused}
            for f in out["files"]:
                f["unused"] = unused.get(f["file"], "")
            return out

        return self._cached("ai", build)

    # --------------------------------------------------------------- AI checks
    def rerank(self) -> None:
        """Order the findings again with the latest feedback and AI verdicts, without re-reading the code."""
        from . import aicheck
        from .rank import rank

        r = self.result
        if r is None:
            return
        fb = Feedback.load(self.root)
        ai = aicheck.weights(self.root, r.findings)
        r.findings = rank(r.findings, None, {f.id: fb.weight_for(f) * ai.get(f.id, 1.0) for f in r.findings})
        self.revision += 1

    def ai_check(self, fid: str) -> dict:
        from . import aicheck

        f = self.finding(fid)
        if f is None:
            raise KeyError(fid)
        note(self.name, f"Asking the model to check: {f.title} ({f.file}:{f.line})")
        out = aicheck.check(self.root, f)
        note(self.name, f"AI check: {out['verdict']} ({round(out['confidence'] * 100)}%) for {f.title}")
        self.rerank()
        return out

    def ai_check_batch(self, limit: int = 10) -> dict:
        """Check the highest-ranked unverified findings that have no current verdict, in the background."""
        from . import aicheck

        cur = self._ai_batch
        if cur and cur["running"]:
            return self.ai_batch_progress()
        r = self.ensure()
        store = aicheck.load(self.root)
        todo = [
            f
            for f in r.findings
            if f.proof.value != "verified" and (store.get(f.id) or {}).get("key") != aicheck.key(f)
        ][: max(1, min(50, limit))]
        state = {"running": True, "total": len(todo), "done": 0, "verdicts": {}, "error": None}
        self._ai_batch = state

        def run() -> None:
            note(self.name, f"AI check: {len(todo)} unverified finding(s), best first")
            try:
                for f in todo:
                    out = aicheck.check(self.root, f)
                    state["verdicts"][out["verdict"]] = state["verdicts"].get(out["verdict"], 0) + 1
                    state["done"] += 1
                    note(self.name, f"  {state['done']}/{len(todo)} {out['verdict']}: {f.title} ({f.file}:{f.line})")
            except RuntimeError as exc:  # no model, the budget is spent, or it stopped answering
                state["error"] = str(exc)
                note(self.name, f"AI check stopped: {exc}", "warn")
            finally:
                state["running"] = False
                self.rerank()

        threading.Thread(target=run, daemon=True, name=f"codewright-aicheck-{self.root.name}").start()
        return self.ai_batch_progress()

    def ai_batch_progress(self) -> dict:
        s = self._ai_batch or {"running": False, "total": 0, "done": 0, "verdicts": {}, "error": None}
        return {k: s[k] for k in ("running", "total", "done", "verdicts", "error")}

    # --------------------------------------------------------------- upgrade safety
    def upgrade_safety(self, step: str, name: str, version: str, patches: list[str] | None = None) -> dict:
        """Run one safety step in the background: "check" (every use against the version), "rewrite" (the code
        changes for the problems found), "trial" (tests before and after, with the chosen patches)."""
        from . import knowledgefresh, upgradesafety

        cur = self._upgrade_job
        if cur and cur["running"]:
            return self.upgrade_progress()
        key = f"{name}=={version}"
        if not cur or cur.get("key") != key:
            cur = {"key": key, "check": None, "rewrite": None, "trial": None, "probes": None}
        state = {**cur, "running": True, "step": step, "log": [], "error": None}
        self._upgrade_job = state

        def tell(msg: str) -> None:
            state["log"].append(msg)
            note(self.name, f"Upgrade {key}: {msg}")

        def run() -> None:
            r = self.ensure()
            try:
                if step == "check":
                    tell(f"Installing {key} on its own and checking every use against it")
                    state["check"] = upgradesafety.check_uses(self.root, r.graph, r.stack, name, version)
                    tell(state["check"]["summary"])
                elif step == "rewrite":
                    problems = [p for p in (state.get("check") or {}).get("problems") or [] if p["issue"] != "changed"]
                    problems += upgradesafety.behaviour_problems(
                        r.graph, name, version, state.get("trial"), (state.get("probes") or {}).get("probes")
                    )
                    if not problems:
                        raise RuntimeError(
                            "nothing to rewrite yet: check every use, or run before and after, to find what changes"
                        )
                    installed = r.stack.version_of(name)
                    tell("Reading the release notes")
                    notes = knowledgefresh.release_notes(name, installed, version)
                    tell(f"Writing the code changes for {len(problems)} use(s)")
                    state["rewrite"] = upgradesafety.rewrite(
                        self.root, r.graph, name, installed, version, problems, notes
                    )
                    tell(
                        f"{len(state['rewrite']['patches'])} change(s) written, "
                        f"{len(state['rewrite']['skipped'])} left by hand"
                    )
                elif step == "trial":
                    problems = (state.get("check") or {}).get("problems") or []
                    targets = upgradesafety.probe_targets(r.graph, name, problems)
                    probes = None
                    if targets:
                        tell(f"Writing calls for {len(targets)} function(s) that use {name} (inputs only)")
                        written = upgradesafety.write_probes(self.root, r.graph, name, targets)
                        state["probes"] = written
                        probes = written["probes"]
                        if written.get("why"):
                            tell(written["why"] + "; comparing imports and tests only")
                    state["trial"] = upgradesafety.trial(
                        self.root, name, version, patches or [], graph=r.graph, probes=probes, progress=tell
                    )
                else:
                    raise RuntimeError(f"unknown step {step!r}")
            except Exception as exc:
                state["error"] = str(exc)
                tell(f"stopped: {exc}")
            finally:
                state["running"] = False

        threading.Thread(target=run, daemon=True, name=f"codewright-upgrade-{self.root.name}").start()
        return self.upgrade_progress()

    def upgrade_progress(self) -> dict:
        s = self._upgrade_job or {"running": False, "step": None, "log": [], "error": None}
        return {k: s.get(k) for k in ("key", "running", "step", "log", "error", "check", "rewrite", "trial", "probes")}

    def forget_ai(self) -> None:
        """Rebuild the AI page next time (its corrections file changed); the scan itself is kept."""
        with self._cache_lock:
            self._cache.pop("ai", None)

    # --------------------------------------------------------------- which files to verify
    def plan(self, use_ai: bool = True) -> dict:
        """Files worth running the tests on, suggested by the model when one is set up (else by impact).
        Nothing runs until the user confirms a list."""
        from . import scope

        r = self.ensure()
        return scope.plan(r.findings, r.scope.get("changed", []), self.root, use_ai=use_ai)

    def ask(self, question: str, history: list[dict] | None = None) -> dict:
        self.ensure()
        cfg = LLMConfig.load(self.root)
        from . import cards as card_store

        llm = cfg if llm_status(cfg)["available"] else None
        return answer(self.index, question, history, llm, cards=card_store.for_repo(self.root), stack=self.result.stack)

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
        r = self.view()
        return design.drift(intent, self.amap().system(), r.graph)

    def releases(self) -> list[dict]:
        r = self.view()
        return watcher.check(r.stack, self.root, graph=r.graph)

    def releases_start(self) -> dict:
        """Check releases in the background and keep the rows found so far, so the page fills in as they arrive."""
        cur = self._releases
        if cur and cur["running"]:
            return self.releases_progress()
        r = self.view()
        state = {"running": True, "total": len(r.stack.dependencies), "rows": [], "error": None, "started": time.time()}
        self._releases = state

        def run() -> None:
            def got(row: dict) -> None:
                state["rows"].append(row)
                latest = row.get("latest") or "no answer"
                note(self.name, f"  {row['name']}: {row.get('installed') or '?'} -> {latest} ({row['bump']})")

            note(self.name, f"Checking {state['total']} dependencies for new releases")
            try:
                state["rows"] = []
                watcher.check(r.stack, self.root, graph=r.graph, on_row=got)
                note(self.name, f"Release check done: {len(state['rows'])} dependencies")
            except Exception as exc:
                state["error"] = f"{type(exc).__name__}: {exc}"
                note(self.name, f"Release check failed: {state['error']}", "error")
            finally:
                state["running"] = False

        threading.Thread(target=run, daemon=True, name=f"codewright-releases-{self.root.name}").start()
        return self.releases_progress()

    def releases_progress(self) -> dict:
        s = self._releases or {"running": False, "total": 0, "rows": [], "error": None}
        order = {"major": 0, "minor": 1, "patch": 2, "unknown": 3, "current": 4}
        rows = sorted(list(s["rows"]), key=lambda r: (order[r["bump"]], r["name"]))
        return {"running": s["running"], "total": s["total"], "done": len(rows), "rows": rows, "error": s["error"]}

    # --------------------------------------------------------------- architecture rules
    def rules(self) -> dict:
        from . import rules as rules_mod

        return rules_mod.summary(self.view().graph, self.root)

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

    # --------------------------------------------------------------- applying patches
    def _apply(self, patch: str) -> None:
        from .proof import apply_patch

        if not apply_patch(self.root, patch):
            raise RuntimeError("the patch did not apply; the files changed since the review. Run the review again")

    def apply_findings(
        self, ids: list[str] | None = None, *, verified_only: bool = True, dry_run: bool = False
    ) -> dict:
        """Apply several findings' patches as one change (nothing is committed), then re-review.

        Without `ids`: every shown finding with a patch that isn't rejected; with `verified_only`, only those
        the tests proved."""
        from .refactor import combine

        r = self.ensure()
        pool = r.findings if ids is None else [f for f in r.findings + r.rejected + r.muted if f.id in set(ids)]
        chosen = [
            f
            for f in pool
            if f.patch
            and (ids is not None or f.status != "rejected")
            and (not verified_only or f.proof.value == "verified")
        ]
        if not chosen:
            raise ValueError(
                "no patch to apply" + (" among verified findings; run the review with proof" if verified_only else "")
            )
        ordered = sorted(chosen, key=lambda f: (f.file, -f.line))  # bottom-up within a file
        patch, skipped = combine(self.root, [(f.id, f.patch) for f in ordered])
        applied = [f.id for f in ordered if f.id not in skipped]
        out = {"patch": patch, "applied": applied, "skipped": skipped, "files": _patched_files(patch)}
        if dry_run or not patch:
            return out
        self._apply(patch)
        fb = Feedback.load(self.root)
        for f in ordered:
            if f.id in applied:
                fb.record(f, "accepted", "applied from Codewright")
        fb.save(self.root)
        self.run_review(proof=False)
        return out

    def fix_violations(self, keys: list[str] | None = None, *, dry_run: bool = False) -> dict:
        """Apply the fixes of rule violations that have one (all, or those whose `file:line:rule` key is given)."""
        from . import rules as rules_mod
        from .refactor import combine

        r = self.ensure()
        vs = rules_mod.check(r.graph, rules_mod.load(self.root))
        chosen = [v for v in vs if v.patch and (keys is None or f"{v.file}:{v.line}:{v.rule}" in set(keys))]
        if not chosen:
            raise ValueError("no violation with an automatic fix")
        ordered = sorted(chosen, key=lambda v: (v.file, -v.line))
        patch, skipped = combine(self.root, [(f"{v.file}:{v.line}:{v.rule}", v.patch) for v in ordered])
        applied = [f"{v.file}:{v.line}:{v.rule}" for v in ordered if f"{v.file}:{v.line}:{v.rule}" not in skipped]
        out = {"patch": patch, "applied": applied, "skipped": skipped, "files": _patched_files(patch)}
        if dry_run or not patch:
            return out
        self._apply(patch)
        self.run_review(proof=False)
        out["rules"] = self.rules()
        return out

    # --------------------------------------------------------------- dependencies
    def deps(self, online: bool = True, python: str | None = None) -> dict:
        from . import deps as deps_mod
        from . import proof_env

        r = self.view()
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

        r = self.view()
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

    def migration_ai(self, name: str, n: int) -> dict:
        """The model writes the step's changes that have no automatic patch."""
        from . import migrate

        r = self.ensure()
        _step, found = migrate.step_findings(self.root, name, n, r.graph, r.stack)
        note(self.name, f"Migration {name} step {n}: AI writing {sum(1 for f in found if not f.patch)} change(s)")
        return migrate.ai_changes(self.root, name, r.graph, found)

    def migration_apply(self, name: str, n: int, extra_patches: list[str] | None = None) -> dict:
        """Apply a step's combined patch and the chosen AI-written changes to the working tree (nothing is
        committed), then re-scan."""
        from .proof import apply_patch

        step = self.migration_step(name, n)
        if not step["patch"] and not extra_patches:
            raise ValueError(f"step {n} has nothing to patch automatically: write its changes with AI or by hand")
        if step["patch"] and not apply_patch(self.root, step["patch"]):
            raise RuntimeError("the step's patch did not apply; the files changed since the plan was made")
        failed = []
        for patch in extra_patches or []:
            if not apply_patch(self.root, patch):
                failed += re.findall(r"^\+\+\+ b/(\S+)", patch, re.M)
        self.run_review(proof=False)
        return {
            "applied": True,
            "files": step["step"]["files"],
            "failed": failed,
            "status": self.migration_status(name),
        }

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


def _patched_files(patch: str) -> list[str]:
    import re

    return sorted(set(re.findall(r"^\+\+\+ b/(.+)$", patch, re.M)))


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
