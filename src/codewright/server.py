"""Web app: REST API + the built React UI, served by `codewright serve`."""

from __future__ import annotations

import contextlib
import io
import os
import re
import subprocess
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__, design
from . import cards as card_store
from . import llm as llm_mod
from .feedback import Feedback
from .intents import DETECTORS
from .llm import LLMConfig
from .llm import status as llm_status
from .report import to_json, to_markdown, to_pr_comment
from .service import Service, Workspace, browse, forget, git_branch, load_recent
from .stack import unsupported_python

STATIC = Path(__file__).parent / "web"


def _origin_host(origin: str) -> str:
    host = urlsplit(origin).hostname or ""
    return f"[{host}]" if ":" in host else host


LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1", "[::1]")


def create_app(roots: list[str] | None = None, allowed_hosts: list[str] | None = None) -> FastAPI:
    """The web app. Only requests addressed to `allowed_hosts` (default: loopback names) are served.

    The server can read source files, browse folders and apply patches, so a page on another
    site must not reach it: the Host check stops DNS rebinding (a hostile domain re-pointed at
    127.0.0.1), and the Origin check stops cross-site requests from a page the user has open.
    """
    svc = Service(roots or [])
    app = FastAPI(title="Codewright", version=__version__)
    app.state.svc = svc
    hosts = list(allowed_hosts or LOOPBACK_HOSTS)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and "*" not in hosts and _origin_host(origin) not in hosts:
            return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        return await call_next(request)

    def ws(wid: str) -> Workspace:
        try:
            return svc.get(wid)
        except KeyError:
            raise HTTPException(404, "unknown workspace") from None

    def ws_summary(wid: str, w: Workspace) -> dict:
        return {
            "id": wid,
            "root": str(w.root),
            "name": w.root.name,
            "running": w.running,
            "progress": w.progress if w.running else None,
            "last_run": w.last_run,
            "error": w.error,
            "has_result": w.result is not None,
            "branch": git_branch(w.root),
            "findings": len(w.result.findings) if w.result else None,
            "verified": sum(1 for f in w.result.findings if f.proof.value == "verified") if w.result else None,
            "files": len(w.result.graph.files) if w.result else None,
            "graph_ready": w.graph_ready,
            "watching": w.watching,
            "revision": w.revision,
        }

    # ------------------------------------------------------------------ meta
    @app.get("/api/status")
    def status():
        return {
            "version": __version__,
            "intents": sorted(DETECTORS),
            "workspaces": [ws_summary(k, v) for k, v in svc.workspaces.items()],
        }

    @app.get("/api/activity")
    def activity_feed(since: int = 0, workspace: str = ""):
        """What the server is doing, newest last: poll with `since` set to the last `last` you got."""
        from . import activity

        repo = ws(workspace).name if workspace else None
        return activity.since(since, repo)

    @app.get("/api/workspaces")
    def workspaces():
        return [ws_summary(k, v) for k, v in svc.workspaces.items()]

    @app.post("/api/workspaces")
    def open_workspace(path: Annotated[str, Body(embed=True)]):
        try:
            wid, w = svc.open(path)
        except FileNotFoundError:
            raise HTTPException(400, f"not a directory: {path}") from None
        return ws_summary(wid, w)

    @app.delete("/api/workspaces/{wid}")
    def close_workspace(wid: str):
        svc.close(wid)
        return {"closed": wid}

    @app.get("/api/recent")
    def recent():
        open_roots = {str(w.root) for w in svc.workspaces.values()}
        return [r for r in load_recent() if r["path"] not in open_roots]

    @app.delete("/api/recent")
    def forget_recent(path: str):
        forget(path)
        return {"forgot": path}

    @app.get("/api/fs")
    def list_folders(path: str = ""):
        """Folder picker for the repo switcher. The server binds 127.0.0.1 by default."""
        try:
            return browse(path or None)
        except FileNotFoundError:
            raise HTTPException(404, f"not a folder: {path}") from None

    @app.get("/api/cards")
    def catalog(workspace: str = "", q: str = "", library: str = ""):
        """Every card (or those matching `q`), each with its fit for the workspace's installed versions."""
        from . import knowledge
        from . import stack as stack_mod

        st = stack_mod.discover(ws(workspace).root) if workspace else None
        root = ws(workspace).root if workspace else None
        return knowledge.search(q, card_store.for_repo(root), st, library=library, limit=None)

    # ------------------------------------------------------------------ review
    def result_payload(wid: str, w: Workspace) -> dict:
        base = ws_summary(wid, w)
        if w.result is None:
            saved = w.saved_review()  # the last review, from disk: shown at once while the refresh runs
            if saved is not None:
                return {**base, "result": saved["result"], "from_cache": True, "saved_at": saved.get("at")}
            return {**base, "result": None}
        if w.saved_revision != w.revision:
            out = _result_payload(base, w)
            w.save_review(out["result"])
            return out
        return _result_payload(base, w)

    def _result_payload(base: dict, w: Workspace) -> dict:
        r = w.result
        fb = Feedback.load(w.root)
        findings = [f.to_dict() for f in r.findings]
        from . import aicheck

        rejected, muted = [f.to_dict() for f in r.rejected], [f.to_dict() for f in r.muted]
        aicheck.attach(w.root, findings + rejected + muted)
        from . import verify

        verify.attach(w.root, findings + rejected + muted)
        verified = sum(1 for f in r.findings if f.proof.value == "verified")
        return {
            **base,
            "result": {
                "stack": {
                    "python": r.stack.python,
                    "python_unsupported": unsupported_python(r.stack),
                    "sources": r.stack.sources,
                    "docker_images": r.stack.docker_images,
                    "dependencies": [
                        {"name": d.name, "version": d.version, "specifier": d.specifier, "source": d.source}
                        for d in sorted(r.stack.dependencies.values(), key=lambda d: d.name)
                    ],
                },
                "files": len(r.graph.files),
                "scope": r.scope,
                "proved_files": r.proved_files,
                "reused_proofs": r.reused,
                "functions": sum(1 for _ in r.graph.functions()),
                "entry_points": len(r.graph.entry_points) + len(r.graph.app_entries),
                "intent_matches": r.total_matches,
                "seconds": r.seconds,
                "test_env": r.test_env,
                "base": r.base,
                "findings": findings,
                "rules": r.rules,
                "rejected": rejected,
                "muted": muted,
                "metrics": {
                    "shown": len(findings),
                    "verified": verified,
                    "verified_share": round(verified / len(findings), 3) if findings else None,
                    "with_patch": sum(1 for f in r.findings if f.patch),
                    "hot_path": sum(1 for f in r.findings if f.hot_path),
                    "dropped": len(r.rejected),
                    **fb.stats(),
                },
            },
        }

    @app.post("/api/w/{wid}/review")
    def start_review(
        wid: str,
        proof: Annotated[bool, Body(embed=True)] = True,
        base: Annotated[str, Body(embed=True)] = "",
        bench: Annotated[bool, Body(embed=True)] = False,
        files: Annotated[list[str] | None, Body(embed=True)] = None,
        fresh: Annotated[bool, Body(embed=True)] = False,
    ):
        """Review the repo. `files` limits the test runs to those files; findings whose file and tests are
        unchanged keep their earlier proof unless `fresh`."""
        w = ws(wid)
        if w.running:
            return ws_summary(wid, w)

        def run():
            with contextlib.suppress(Exception):  # the error is recorded on w.error
                w.run_review(proof=proof, base=base or None, bench=bench, files=files, fresh=fresh)

        w.running = True
        threading.Thread(target=run, daemon=True).start()
        return ws_summary(wid, w)

    @app.get("/api/w/{wid}/review/plan")
    def review_plan(wid: str, ai: bool = True):
        """Which files to run the tests on, chosen by the model when one is set up. Only suggests."""
        return ws(wid).plan(use_ai=ai)

    @app.post("/api/w/{wid}/watch")
    def watch(wid: str, on: Annotated[bool, Body(embed=True)]):
        """Turn live re-review on save on or off for this workspace."""
        w = ws(wid)
        w.start_watch() if on else w.stop_watch()
        return ws_summary(wid, w)

    @app.get("/api/w/{wid}/revision")
    def revision(wid: str):
        """Tiny status for polling: has the result changed, and what did the last re-review change?"""
        w = ws(wid)
        return {
            "revision": w.revision,
            "running": w.running,
            "progress": w.progress if w.running else None,
            "watching": w.watching,
            "last_change": w.last_change,
        }

    @app.get("/api/w/{wid}/review")
    def get_review(wid: str):
        return result_payload(wid, ws(wid))

    @app.post("/api/w/{wid}/findings/{fid}/feedback")
    def feedback(
        wid: str, fid: str, status: Annotated[str, Body(embed=True)], reason: Annotated[str, Body(embed=True)] = ""
    ):
        try:
            return ws(wid).feedback(fid, status, reason)
        except KeyError:
            raise HTTPException(404, "unknown finding") from None
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    @app.post("/api/w/{wid}/mute")
    def mute(
        wid: str,
        intent: Annotated[str, Body(embed=True)],
        path: Annotated[str, Body(embed=True)] = "",
        reason: Annotated[str, Body(embed=True)] = "",
        undo: Annotated[bool, Body(embed=True)] = False,
    ):
        return ws(wid).mute(intent, path, reason, undo)

    @app.post("/api/w/{wid}/findings/{fid}/ai-check")
    def ai_check(wid: str, fid: str):
        """Ask the configured model whether this finding is right for this code (what tests could not prove)."""
        from .activity import note

        w = ws(wid)
        try:
            return w.ai_check(fid)
        except KeyError:
            raise HTTPException(404, "unknown finding") from None
        except RuntimeError as e:
            note(w.name, f"AI check failed: {e}", "error")
            raise HTTPException(409, str(e)) from None

    @app.get("/api/w/{wid}/findings/{fid}/verify")
    def verify_steps(wid: str, fid: str):
        """The steps that would prove this finding right or wrong here, with the commands to run."""
        from . import verify

        w = ws(wid)
        f = w.finding(fid)
        if f is None:
            raise HTTPException(404, "unknown finding")
        return {"steps": verify.steps(f, w.result.graph if w.result else None)}

    @app.post("/api/w/{wid}/findings/{fid}/ai-test")
    def ai_write_test(wid: str, fid: str):
        """The model writes a test for the finding's function; it runs before and after the patch, in a copy."""
        from . import verify
        from .activity import Timer, note

        w = ws(wid)
        f = w.finding(fid)
        if f is None:
            raise HTTPException(404, "unknown finding")
        try:
            with Timer(w.name, f"AI test for {f.title} ({f.file}:{f.line})"):
                out = verify.ai_test(w.root, f, w.ensure().graph)
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from None
        note(w.name, f"AI test: {out['verdict']} for {f.title}")
        w.rerank()
        return out

    @app.post("/api/w/{wid}/findings/{fid}/ai-test/save")
    def ai_save_test(wid: str, fid: str):
        """Add the AI's test to the repo (commit it from the branch bar)."""
        from . import verify
        from .activity import note

        w = ws(wid)
        try:
            path = verify.save_test(w.root, fid)
        except FileNotFoundError:
            raise HTTPException(404, "no AI test for this finding yet") from None
        note(w.name, f"Added the AI's test as {path}")
        return {"path": path}

    @app.post("/api/w/{wid}/ai-check/batch")
    def ai_check_batch(wid: str, limit: Annotated[int, Body(embed=True)] = 10):
        """Have the model check the top `limit` unverified findings (at most 50), in the background."""
        return ws(wid).ai_check_batch(limit)

    @app.get("/api/w/{wid}/ai-check/batch")
    def ai_check_batch_progress(wid: str):
        return ws(wid).ai_batch_progress()

    @app.post("/api/w/{wid}/findings/{fid}/apply")
    def apply_patch(wid: str, fid: str):
        """Apply a finding's patch to the working tree. Only on an explicit click; nothing is committed."""
        w = ws(wid)
        f = w.finding(fid)
        if f is None or not f.patch:
            raise HTTPException(404, "no patch for this finding") from None
        proc = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-"], cwd=w.root, input=f.patch, capture_output=True, text=True
        )
        if proc.returncode != 0:
            proc = subprocess.run(
                ["patch", "-p1", "--forward"], cwd=w.root, input=f.patch, capture_output=True, text=True
            )
        if proc.returncode != 0:
            raise HTTPException(409, f"patch did not apply: {proc.stderr.strip() or proc.stdout.strip()}") from None
        w.feedback(fid, "accepted", "applied from the web UI")
        return {"applied": True, "file": f.file}

    @app.post("/api/w/{wid}/apply")
    def apply_many(
        wid: str,
        ids: list[str] | None = Body(None, embed=True),
        verified_only: Annotated[bool, Body(embed=True)] = True,
        dry_run: Annotated[bool, Body(embed=True)] = False,
    ):
        """Apply several findings' patches as one change. Only on an explicit click; nothing is committed."""
        try:
            return ws(wid).apply_findings(ids, verified_only=verified_only, dry_run=dry_run)
        except ValueError as e:
            raise HTTPException(404, str(e)) from None
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from None

    @app.get("/api/w/{wid}/report.md", response_class=PlainTextResponse)
    def report_md(wid: str):
        return to_markdown(ws(wid).ensure())

    @app.get("/api/w/{wid}/pr-comment.md", response_class=PlainTextResponse)
    def report_pr(wid: str):
        return to_pr_comment(ws(wid).ensure())

    @app.get("/api/w/{wid}/report.json")
    def report_json(wid: str):
        return Response(to_json(ws(wid).ensure()), media_type="application/json")

    # ------------------------------------------------------------------ map
    @app.get("/api/w/{wid}/map/meta")
    def map_meta(wid: str):
        from .archmap import changes_since

        w = ws(wid)
        m = w.amap()
        return {
            "components": m.components(),
            "unused": m.unused,
            "entry_points": m.entry_points(),
            "changes": changes_since(w.root, m.system()),
            "drift": w.drift(),
        }

    @app.get("/api/w/{wid}/map/{level}")
    def map_view(wid: str, level: str, target: str = "", unused: bool = False):
        """`unused=true` also draws modules the running system never loads (see ArchMap.unused)."""
        m = ws(wid).amap(include_unused=unused)
        if level not in ("system", "module", "flow"):
            raise HTTPException(400, "level must be system, module or flow")
        try:
            view = m.view(level, target)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        return {**view.to_dict(), "mermaid": view.mermaid()}

    @app.get("/api/w/{wid}/module")
    def module_page(wid: str, path: str):
        m = ws(wid).amap()
        if path not in m.g.files:
            raise HTTPException(404, "unknown file") from None
        return m.module_page(path)

    @app.get("/api/w/{wid}/source")
    def source(wid: str, path: str):
        try:
            return ws(wid).source(path)
        except FileNotFoundError:
            raise HTTPException(404, "file not found") from None

    @app.post("/api/w/{wid}/chat")
    def chat(wid: str, question: Annotated[str, Body(embed=True)], history: list[dict] = Body([], embed=True)):
        return ws(wid).ask(question, history)

    # ------------------------------------------------------------------ branches for fixes
    def _git_call(wid: str, fn, *args):
        from . import gitops
        from .activity import note

        w = ws(wid)
        try:
            out = fn(w.root, *args)
        except gitops.GitError as e:
            raise HTTPException(409, str(e)) from None
        return w, out, note

    @app.get("/api/w/{wid}/git")
    def git_status(wid: str):
        """Current branch, local branches (most recent first), uncommitted changes."""
        from . import gitops

        w = ws(wid)
        out = gitops.status(w.root)
        if out.get("git") and w.result is not None:
            out["suggested_branch"] = gitops.suggest_name([f.title for f in w.result.findings[:1]])
        return out

    @app.post("/api/w/{wid}/git/switch")
    def git_switch(wid: str, branch: Annotated[str, Body(embed=True)]):
        """Switch branch (refused with uncommitted changes); the UI re-scans afterwards."""
        from . import gitops

        w, out, note = _git_call(wid, gitops.switch, branch)
        note(w.name, f"Git: switched to {out['branch']}")
        return out

    @app.post("/api/w/{wid}/git/branch")
    def git_branch_create(
        wid: str, name: Annotated[str, Body(embed=True)], start: Annotated[str, Body(embed=True)] = ""
    ):
        """Create a branch for fixes and switch to it; uncommitted changes come along."""
        from . import gitops

        w, out, note = _git_call(wid, gitops.create, name, start)
        note(w.name, f"Git: created and switched to {out['branch']}")
        return out

    @app.post("/api/w/{wid}/git/fetch")
    def git_fetch(wid: str):
        """Update what the remotes have (nothing local changes), for the ahead/behind counts."""
        from . import gitops

        _, out, _ = _git_call(wid, gitops.fetch)
        return out

    @app.post("/api/w/{wid}/git/pull")
    def git_pull(wid: str):
        """Fast-forward the current branch to its upstream (refused when they have diverged)."""
        from . import gitops

        w, out, note = _git_call(wid, gitops.pull)
        note(w.name, f"Git: pulled {out['pulled']} commit(s) into {out['branch']}")
        return out

    @app.post("/api/w/{wid}/git/sync")
    def git_sync(wid: str):
        """Pull (fast-forward), then push the current branch. Only when the user presses Sync."""
        from . import gitops

        w, out, note = _git_call(wid, gitops.sync)
        note(w.name, f"Git: synced {out['branch']}: pulled {out['pulled']}, pushed {out['pushed']} commit(s)")
        return out

    @app.post("/api/w/{wid}/git/commit")
    def git_commit(wid: str, message: Annotated[str, Body(embed=True)], files: Annotated[list[str], Body(embed=True)]):
        """Commit exactly these changed files. Nothing is pushed."""
        from . import gitops

        w, out, note = _git_call(wid, gitops.commit, message, files)
        note(w.name, f"Git: committed {len(files)} file(s) as {out['committed']} on {out['branch']}")
        return out

    # ------------------------------------------------------------------ scan scope
    @app.get("/api/w/{wid}/scan")
    def scan_scope(wid: str):
        """Include/exclude patterns, and each folder's Python files with how many the patterns keep."""
        from . import scanscope

        return scanscope.summary(ws(wid).root)

    @app.put("/api/w/{wid}/scan")
    def scan_scope_save(
        wid: str,
        include: Annotated[list[str] | None, Body(embed=True)] = None,
        exclude: Annotated[list[str] | None, Body(embed=True)] = None,
    ):
        """Save the patterns to .codewright/config.json (commit it to share). The next scan uses them."""
        from . import scanscope
        from .activity import note

        w = ws(wid)
        scanscope.save(w.root, include or [], exclude or [])
        out = scanscope.summary(w.root)
        note(w.name, f"Scan scope saved: {out['kept']} of {out['total']} Python files are reviewed")
        return out

    # ------------------------------------------------------------------ knowledge: keeping it current
    @app.get("/api/w/{wid}/knowledge/freshness")
    def knowledge_freshness(wid: str, online: bool = True):
        """Each library's cards: the version they were checked against, the newest release, how far behind."""
        from . import knowledgefresh
        from . import stack as stack_mod

        w = ws(wid)
        st = w.result.stack if w.result else stack_mod.discover(w.root)
        return knowledgefresh.freshness(w.root, st, online=online)

    @app.get("/api/w/{wid}/knowledge/notes")
    def knowledge_notes(wid: str, library: str, since: str = "", until: str = ""):
        """What changed in `library` after `since` up to `until`: release notes and changelog links."""
        from . import knowledgefresh
        from .activity import Timer

        w = ws(wid)
        with Timer(w.name, f"Reading the release notes of {library} after {since or 'the start'}"):
            return knowledgefresh.release_notes(library, since or None, until or None)

    @app.post("/api/w/{wid}/knowledge/draft")
    def knowledge_draft(
        wid: str,
        library: Annotated[str, Body(embed=True)],
        since: Annotated[str, Body(embed=True)] = "",
        until: Annotated[str, Body(embed=True)] = "",
        pasted: Annotated[str, Body(embed=True)] = "",
    ):
        """New cards and outdated-card notes drafted by the model from the release notes; nothing is saved."""
        from . import knowledgefresh
        from .activity import Timer

        w = ws(wid)
        notes = knowledgefresh.release_notes(library, since or None, until or None)
        try:
            with Timer(w.name, f"Drafting knowledge updates for {library}"):
                out = knowledgefresh.draft_updates(w.root, library, notes, pasted)
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from None
        return {**out, "notes": notes}

    @app.get("/api/w/{wid}/knowledge/library")
    def knowledge_library_info(wid: str, name: str):
        """What the package index says about a library (newest version, summary, links), and its cards here."""
        from . import knowledgefresh
        from .models import normalise

        w = ws(wid)
        info = knowledgefresh.library_info(normalise(name))
        info.pop("readme", None)
        have = sum(1 for c in card_store.for_repo(w.root) if c.library and normalise(c.library) == normalise(name))
        used = w.result.stack.version_of(name) if w.result else None
        return {**info, "cards": have, "installed": used}

    @app.post("/api/w/{wid}/knowledge/library")
    def knowledge_library_draft(
        wid: str,
        name: Annotated[str, Body(embed=True)],
        focus: Annotated[str, Body(embed=True)] = "",
        pasted: Annotated[str, Body(embed=True)] = "",
        use_repo: Annotated[bool, Body(embed=True)] = True,
        count: Annotated[int, Body(embed=True)] = 8,
    ):
        """Cards for a library drafted by the model: a starter set, or deeper ones beside its existing cards,
        grounded in its README, pasted docs and how this repo uses it. Nothing is saved."""
        from . import knowledgefresh, upgrade
        from .activity import Timer

        w = ws(wid)
        usage = upgrade.uses(w.result.graph, name) if use_repo and w.result is not None else None
        try:
            with Timer(w.name, f"Drafting knowledge cards for {name}"):
                return knowledgefresh.draft_library(w.root, name, focus=focus, pasted=pasted, usage=usage, count=count)
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from None

    @app.post("/api/w/{wid}/knowledge/checked")
    def knowledge_checked(
        wid: str, library: Annotated[str, Body(embed=True)], version: Annotated[str, Body(embed=True)]
    ):
        """Record that the team re-checked this library's cards against `version` (.codewright/knowledge.json)."""
        from . import knowledgefresh
        from .activity import note

        w = ws(wid)
        try:
            knowledgefresh.mark_checked(w.root, library, version)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        note(w.name, f"Knowledge: {library} cards checked against {version}")
        return knowledgefresh.freshness(w.root, w.result.stack if w.result else None, online=False)

    # ------------------------------------------------------------------ upgrading a dependency
    @app.get("/api/w/{wid}/upgrade/plan")
    def upgrade_plan(wid: str, name: str, version: str):
        """What upgrading `name` to `version` changes: requirements, lockfile, code patches, manual work."""
        from . import upgrade
        from .activity import Timer

        w = ws(wid)
        r = w.ensure()
        with Timer(w.name, f"Planning the upgrade of {name} to {version}"):
            return upgrade.plan(w.root, r.graph, r.stack, name, version)

    @app.post("/api/w/{wid}/upgrade/breaking")
    def upgrade_breaking(wid: str, name: Annotated[str, Body(embed=True)], version: Annotated[str, Body(embed=True)]):
        """The model reads the release notes against this repo's uses of the library."""
        from . import knowledgefresh, upgrade

        w = ws(wid)
        r = w.ensure()
        installed = r.stack.version_of(name)
        notes = knowledgefresh.release_notes(name, installed, version)
        try:
            out = upgrade.breaking_changes(w.root, r.graph, name, installed, version, notes)
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from None
        return {**out, "links": notes.get("links", []), "versions": notes.get("versions", [])}

    @app.post("/api/w/{wid}/upgrade/safety")
    def upgrade_safety(
        wid: str,
        step: Annotated[str, Body(embed=True)],
        name: Annotated[str, Body(embed=True)],
        version: Annotated[str, Body(embed=True)],
        patches: Annotated[list[str] | None, Body(embed=True)] = None,
    ):
        """Start a safety step in the background: check, rewrite or trial; poll GET for progress and results."""
        return ws(wid).upgrade_safety(step, name, version, patches)

    @app.get("/api/w/{wid}/upgrade/safety")
    def upgrade_safety_progress(wid: str):
        return ws(wid).upgrade_progress()

    @app.post("/api/w/{wid}/upgrade/apply")
    def upgrade_apply(
        wid: str,
        name: Annotated[str, Body(embed=True)],
        version: Annotated[str, Body(embed=True)],
        patch_ids: Annotated[list[str] | None, Body(embed=True)] = None,
        branch: Annotated[str, Body(embed=True)] = "",
        run_lock: Annotated[bool, Body(embed=True)] = True,
        extra_patches: Annotated[list[str] | None, Body(embed=True)] = None,
    ):
        """Do the upgrade (on a new branch when given); nothing is committed. The UI re-scans afterwards."""
        from . import gitops, upgrade
        from .activity import note

        w = ws(wid)
        r = w.ensure()
        try:
            out = upgrade.apply(
                w.root,
                r.graph,
                r.stack,
                name,
                version,
                patch_ids=patch_ids,
                branch=branch,
                run_lock=run_lock,
                extra_patches=extra_patches,
            )
        except gitops.GitError as e:
            raise HTTPException(409, str(e)) from None
        note(
            w.name,
            f"Upgraded {name} to {version}: {len(out['files'])} file(s) changed, {len(out['applied'])} patch(es)"
            + (f" on {out['branch']}" if out["branch"] else ""),
        )
        return out

    # ------------------------------------------------------------------ knowledge: teach
    @app.post("/api/w/{wid}/cards")
    def teach_card(wid: str, card: Annotated[dict, Body(embed=True)]):
        """Add a card to the repo's knowledge base (.codewright/cards/), from the Knowledge page."""
        from . import teach
        from .activity import note

        w = ws(wid)
        try:
            saved = teach.save(w.root, card)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        note(w.name, f"Knowledge: added card {saved['id']} ({saved['title']})")
        return saved

    @app.delete("/api/w/{wid}/cards/{card_id}")
    def forget_card(wid: str, card_id: str):
        from . import teach

        try:
            teach.delete(ws(wid).root, card_id)
        except FileNotFoundError:
            raise HTTPException(404, "not one of this repo's cards") from None
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return {"deleted": card_id}

    @app.post("/api/w/{wid}/cards/draft")
    def draft_card(
        wid: str,
        topic: Annotated[str, Body(embed=True)] = "",
        library: Annotated[str, Body(embed=True)] = "",
        notes: Annotated[str, Body(embed=True)] = "",
        deepen: Annotated[str, Body(embed=True)] = "",
    ):
        """A card drafted by the configured model from a topic and pasted notes (or deeper than card `deepen`)."""
        from . import stack as stack_mod
        from . import teach
        from .activity import Timer

        w = ws(wid)
        base = next((c for c in card_store.for_repo(w.root) if c.id == deepen), None) if deepen else None
        lib = library or (base.library if base and base.library else "")
        installed = stack_mod.discover(w.root).version_of(lib) if lib else None
        try:
            with Timer(w.name, f"Drafting a knowledge card about {topic or (base.title if base else lib)}"):
                return teach.draft(w.root, topic=topic, library=lib, notes=notes, base=base, installed=installed)
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from None

    @app.get("/api/w/{wid}/ai")
    def ai(wid: str):
        return ws(wid).ai()

    @app.get("/api/w/{wid}/ai/fixes")
    def ai_fixes(wid: str):
        """The team's corrections to the agent architecture (.codewright/agents.yaml)."""
        from . import agentfix

        return {**agentfix.load(ws(wid).root), "labels": list(agentfix.LABELS)}

    @app.put("/api/w/{wid}/ai/fixes")
    def ai_fixes_save(wid: str, fixes: Annotated[dict, Body(embed=True)]):
        from . import agentfix
        from .activity import note

        w = ws(wid)
        out = agentfix.save(w.root, fixes)
        w.forget_ai()
        note(w.name, f"Agent architecture: {len(out['links'])} added link(s), {len(out['agents'])} added agent(s)")
        return {**out, "labels": list(agentfix.LABELS)}

    @app.post("/api/w/{wid}/ai/review")
    def ai_review(wid: str):
        """The configured model's review of the agent architecture, on top of the rule-based insights."""
        from .activity import Timer
        from .agentreview import deep_review
        from .llm import LLMConfig

        w = ws(wid)
        m = w.ai_map()
        with Timer(w.name, "Asking the model to review the agent architecture"):
            return deep_review(m, w.ai().get("insights", []), LLMConfig.load(w.root))

    @app.get("/api/w/{wid}/ai/node")
    def ai_node(wid: str, id: str):
        """Everything known about one box on the agent architecture graph."""
        from .inspect_node import ai_node as details

        return details(ws(wid).ai_map(), id)

    @app.get("/api/w/{wid}/inspect")
    def map_node(wid: str, id: str, level: str = "system", target: str = "", unused: bool = False):
        """Everything known about one box on the Map: what it is, its links, findings and code."""
        from .inspect_node import map_node as details

        if level not in ("system", "module", "flow"):
            raise HTTPException(400, "level must be system, module or flow")
        return details(ws(wid).amap(include_unused=unused), level, target, id)

    @app.get("/api/w/{wid}/drift")
    def drift(wid: str):
        return ws(wid).drift()

    @app.post("/api/w/{wid}/releases/start")
    def releases_start(wid: str):
        """Start a release check in the background; poll /releases/progress for rows as they arrive."""
        return ws(wid).releases_start()

    @app.get("/api/w/{wid}/releases/progress")
    def releases_progress(wid: str):
        return ws(wid).releases_progress()

    @app.get("/api/w/{wid}/releases")
    def releases(wid: str):
        return ws(wid).releases()

    # ------------------------------------------------------------------ rules, dependencies, migrations, plans
    @app.get("/api/w/{wid}/rules")
    def get_rules(wid: str):
        return ws(wid).rules()

    @app.post("/api/w/{wid}/rules/init")
    def init_rules(wid: str, overwrite: Annotated[bool, Body(embed=True)] = False):
        try:
            return ws(wid).rules_init(overwrite)
        except FileExistsError as e:
            raise HTTPException(409, str(e)) from None

    @app.post("/api/w/{wid}/rules/fix")
    def fix_rules(
        wid: str,
        keys: list[str] | None = Body(None, embed=True),
        dry_run: Annotated[bool, Body(embed=True)] = False,
    ):
        """Apply rule violations' fixes. Only on an explicit click; nothing is committed."""
        try:
            return ws(wid).fix_violations(keys, dry_run=dry_run)
        except ValueError as e:
            raise HTTPException(404, str(e)) from None
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from None

    @app.get("/api/w/{wid}/deps")
    def get_deps(wid: str, online: bool = True):
        return ws(wid).deps(online=online)

    @app.get("/api/w/{wid}/migrations")
    def list_migrations(wid: str):
        from .migrate import CATALOG

        return {
            "applicable": ws(wid).migrations(),
            "catalog": [{"name": m.name, "title": m.title, "library": m.library} for m in CATALOG.values()],
        }

    @app.post("/api/w/{wid}/migrations/{name}/plan")
    def plan_migration(wid: str, name: str, batch: Annotated[int, Body(embed=True)] = 5):
        try:
            return ws(wid).migration_plan(name, batch=max(1, min(batch, 50)))
        except ValueError as e:
            raise HTTPException(404, str(e)) from None

    @app.get("/api/w/{wid}/migrations/{name}")
    def migration_status(wid: str, name: str):
        try:
            return ws(wid).migration_status(name)
        except (FileNotFoundError, ValueError) as e:
            raise HTTPException(404, str(e)) from None

    @app.get("/api/w/{wid}/migrations/{name}/steps/{n}")
    def migration_step(wid: str, name: str, n: int):
        try:
            return ws(wid).migration_step(name, n)
        except (FileNotFoundError, ValueError) as e:
            raise HTTPException(404, str(e)) from None

    @app.post("/api/w/{wid}/migrations/{name}/steps/{n}/ai")
    def migration_ai(wid: str, name: str, n: int):
        """AI-written changes for the step's items no detector could patch, as diffs to review."""
        try:
            return ws(wid).migration_ai(name, n)
        except KeyError:
            raise HTTPException(404, f"unknown migration {name!r}") from None
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/w/{wid}/migrations/{name}/steps/{n}/agent")
    def migration_agent(
        wid: str,
        name: str,
        n: int,
        web: Annotated[bool, Body(embed=True)] = True,
        tests: Annotated[bool, Body(embed=True)] = True,
    ):
        """Hand the step's changes to the AI agent (search, read, edit and test in a sandbox); poll /agent."""
        try:
            return ws(wid).migration_agent(name, n, web=web, tests=tests)
        except KeyError:
            raise HTTPException(404, f"unknown migration {name!r}") from None
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from None

    # ------------------------------------------------------------------ the AI agent
    @app.post("/api/w/{wid}/agent")
    def agent_start(
        wid: str,
        task: Annotated[str, Body(embed=True)],
        context: Annotated[str, Body(embed=True)] = "",
        web: Annotated[bool, Body(embed=True)] = True,
        tests: Annotated[bool, Body(embed=True)] = True,
    ):
        """Start the agent on a task in a sandbox copy of the repo; poll GET for its steps and result."""
        try:
            return ws(wid).agent_start(task, context=context, web=web, tests=tests)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/w/{wid}/agent")
    def agent_progress(wid: str, since: int = 0):
        return ws(wid).agent_progress(since)

    @app.post("/api/w/{wid}/agent/stop")
    def agent_stop(wid: str):
        return ws(wid).agent_stop()

    @app.post("/api/w/{wid}/agent/apply")
    def agent_apply(wid: str):
        """Apply the agent's patch to the working tree. Only on an explicit click; nothing is committed."""
        try:
            return ws(wid).agent_apply()
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/w/{wid}/migrations/{name}/steps/{n}/apply")
    def migration_apply(
        wid: str, name: str, n: int, extra_patches: Annotated[list[str] | None, Body(embed=True)] = None
    ):
        """Apply one step's combined patch to the working tree. Only on an explicit click; nothing is committed."""
        try:
            return ws(wid).migration_apply(name, n, extra_patches)
        except (FileNotFoundError, ValueError) as e:
            raise HTTPException(404, str(e)) from None
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from None

    @app.post("/api/w/{wid}/critique")
    def critique(wid: str, text: Annotated[str, Body(embed=True)]):
        if not text.strip():
            raise HTTPException(400, "paste a plan to review")
        return ws(wid).critique(text[:200_000])

    @app.get("/api/w/{wid}/insights")
    def insights(wid: str):
        return ws(wid).insights()

    # ------------------------------------------------------------------ settings
    @app.get("/api/w/{wid}/settings")
    def get_settings(wid: str):
        w = ws(wid)
        cfg = LLMConfig.load(w.root)
        fb = Feedback.load(w.root)
        return {
            "model": cfg.model,
            "offline": cfg.offline,
            "offline_from_environment": bool(os.environ.get("CODEWRIGHT_OFFLINE")),
            "llm": llm_status(cfg),
            "ai": llm_mod.public(w.root),
            "constraints": fb.constraints,
            "mutes": [asdict(m) for m in fb.mutes],
        }

    @app.post("/api/w/{wid}/settings")
    def set_settings(
        wid: str,
        offline: Annotated[bool | None, Body(embed=True)] = None,
        constraints: Annotated[list[str] | None, Body(embed=True)] = None,
    ):
        w = ws(wid)
        if offline is not None:
            cfg = LLMConfig.load(w.root)
            cfg.offline = offline
            cfg.save(w.root)
        if constraints is not None:
            fb = Feedback.load(w.root)
            fb.constraints = [c.strip() for c in constraints if c.strip()]
            fb.save(w.root)
        return get_settings(wid)

    # ------------------------------------------------------------------ model (per machine, not per repo)
    def _root_of(workspace: str) -> Path | None:
        return svc.workspaces[workspace].root if workspace in svc.workspaces else None

    @app.get("/api/settings/ai")
    def ai_settings(workspace: str = ""):
        return llm_mod.public(_root_of(workspace))

    @app.put("/api/settings/ai")
    def ai_settings_save(
        provider: Annotated[str | None, Body(embed=True)] = None,
        api_key: Annotated[str | None, Body(embed=True)] = None,
        model: Annotated[str | None, Body(embed=True)] = None,
        base_url: Annotated[str | None, Body(embed=True)] = None,
        clear_key: Annotated[bool, Body(embed=True)] = False,
        workspace: Annotated[str, Body(embed=True)] = "",
    ):
        """Save the provider, key and model for this machine. The key is stored, never returned."""
        if provider and provider not in llm_mod.PROVIDERS:
            raise HTTPException(400, f"unknown provider {provider}") from None
        if base_url and not re.match(r"^https?://[^\s]+$", base_url.strip()):
            raise HTTPException(400, "the server address must start with http:// or https://") from None
        vals: dict = {
            k: v for k, v in (("provider", provider), ("model", model), ("base_url", base_url)) if v is not None
        }
        if clear_key:
            vals["api_key"] = None
        elif api_key:
            vals["api_key"] = api_key.strip()
        try:
            llm_mod.save_settings(vals)
        except OSError as exc:
            raise HTTPException(500, f"could not write {llm_mod.settings_path()}: {exc}") from None
        return llm_mod.public(_root_of(workspace))

    @app.get("/api/settings/search")
    def search_settings():
        from . import websearch

        return websearch.public()

    @app.put("/api/settings/search")
    def search_settings_save(
        provider: Annotated[str | None, Body(embed=True)] = None,
        api_key: Annotated[str | None, Body(embed=True)] = None,
        url: Annotated[str | None, Body(embed=True)] = None,
        clear_key: Annotated[bool, Body(embed=True)] = False,
    ):
        """Where the agent searches the web. The key is stored on this machine, never returned."""
        from . import websearch

        if provider and provider not in websearch.PROVIDERS:
            raise HTTPException(400, f"unknown search provider {provider}") from None
        if url and not re.match(r"^https?://[^\s]+$", url.strip()):
            raise HTTPException(400, "the address must start with http:// or https://") from None
        vals: dict = {k: v for k, v in (("provider", provider), ("url", url)) if v is not None}
        if clear_key:
            vals["api_key"] = None
        elif api_key:
            vals["api_key"] = api_key.strip()
        try:
            return websearch.save(vals)
        except OSError as exc:
            raise HTTPException(500, f"could not write the settings: {exc}") from None

    @app.post("/api/settings/search/test")
    def search_settings_test(query: Annotated[str, Body(embed=True)] = "python packaging pyproject.toml"):
        from . import websearch

        try:
            rows = websearch.search(query, limit=3)
        except websearch.SearchError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": bool(rows), "results": rows, "error": "" if rows else "no results"}

    @app.get("/api/settings/ai/usage")
    def ai_usage():
        """Model calls and tokens used today on this machine, and the daily call budget."""
        return llm_mod.usage()

    @app.put("/api/settings/ai/budget")
    def ai_budget(daily_calls: Annotated[int, Body(embed=True)]):
        """Most model calls a day on this machine (0: no limit)."""
        return llm_mod.set_budget(daily_calls)

    @app.post("/api/settings/ai/test")
    def ai_settings_test(workspace: Annotated[str, Body(embed=True)] = ""):
        """One tiny request to the configured model. Refused while the repo is in offline mode."""
        cfg = LLMConfig.load(_root_of(workspace))
        if cfg.offline:
            return {"ok": False, "model": cfg.model, "error": "offline mode is on, so no request was sent"}
        return llm_mod.test(cfg)

    # ------------------------------------------------------------------ package index
    def index_state(root: str) -> dict:
        from . import index as idx

        current = idx.resolve(root)
        repo = idx._read_json(idx.repo_config_path(root)).get("index_url")
        machine = idx._read_json(idx.machine_config_path()).get("index_url")
        return {
            "url": current.display,
            "source": current.source,
            "is_pypi": current.is_pypi,
            "repo": idx.redact(repo) if repo else None,
            "machine": idx.redact(machine) if machine else None,
            "env_override": bool(os.environ.get("CODEWRIGHT_INDEX_URL")),
        }

    @app.get("/api/w/{wid}/index")
    def get_index(wid: str):
        return index_state(ws(wid).root)

    @app.post("/api/w/{wid}/index")
    def set_index(
        wid: str,
        url: Annotated[str, Body(embed=True)],
        scope: Annotated[str, Body(embed=True)] = "machine",
    ):
        """Set the index for this repo or this machine; an empty url resets that scope (to PyPI)."""
        from . import index as idx

        if scope not in ("repo", "machine"):
            raise HTTPException(400, "scope is 'repo' or 'machine'") from None
        w = ws(wid)
        try:
            idx.save(url, root=w.root if scope == "repo" else None)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return index_state(w.root)

    @app.post("/api/w/{wid}/index/test")
    def test_index(wid: str, url: Annotated[str, Body(embed=True)] = ""):
        """Look one of the repo's dependencies up on the index (the saved one, or `url` before saving)."""
        from . import index as idx

        w = ws(wid)
        if url.strip():
            if err := idx.validate(url):
                raise HTTPException(400, err) from None
            target = idx.PackageIndex(url.strip(), "unsaved")
        else:
            target = idx.resolve(w.root)
        from .stack import discover

        locked = sorted(d.name for d in discover(w.root).dependencies.values() if d.version)
        return idx.check(target, locked[0] if locked else "pip")

    # ------------------------------------------------------------------ design
    def design_state(s: design.Session) -> dict:
        q = s.next_question()
        return {
            "session": s.to_dict(),
            "done": q is None,
            "question": asdict(q) if q else None,
            "progress": {"answered": sum(1 for x in s.applicable() if x.id in s.answers), "total": len(s.applicable())},
            "questions": [asdict(x) for x in s.applicable()],
        }

    @app.post("/api/design")
    def new_design():
        return design_state(svc.new_design())

    @app.get("/api/design/{sid}")
    def get_design(sid: str):
        try:
            return {**design_state(svc.design_session(sid)), "proposal": svc.proposals.get(sid)}
        except KeyError:
            raise HTTPException(404, "unknown design session") from None

    @app.post("/api/design/{sid}/answer")
    def design_answer(sid: str, qid: Annotated[str, Body(embed=True)], value=Body(..., embed=True)):
        s = svc.design_session(sid)
        try:
            s.answer(qid, value)
        except (ValueError, KeyError) as e:
            raise HTTPException(422, str(e)) from None
        return design_state(s)

    @app.post("/api/design/{sid}/assumptions")
    def design_assumptions(sid: str, overrides: Annotated[dict, Body(embed=True)]):
        s = svc.design_session(sid)
        for k, v in overrides.items():
            if k not in design.ASSUMPTIONS:
                raise HTTPException(422, f"unknown assumption {k}") from None
            if v is None:
                s.overrides.pop(k, None)
            else:
                s.overrides[k] = float(v)
        return svc.propose(sid)

    @app.post("/api/design/{sid}/propose")
    def design_propose(sid: str, workspace: Annotated[str, Body(embed=True)] = ""):
        root = svc.workspaces[workspace].root if workspace in svc.workspaces else None
        return svc.propose(sid, root)

    @app.get("/api/design/{sid}/decisions/{cid}")
    def design_decisions(sid: str, cid: str):
        return design.decisions_for(svc.design_session(sid), _proposal(sid), cid)

    def _proposal(sid: str) -> dict:
        if sid not in svc.proposals:
            svc.propose(sid)
        return svc.proposals[sid]

    @app.get("/api/design/{sid}/design.md", response_class=PlainTextResponse)
    def design_md(sid: str, candidate: str = ""):
        p = _proposal(sid)
        return design.design_doc(svc.design_session(sid), p, candidate or p["recommended"])

    @app.get("/api/design/{sid}/starter.zip")
    def starter(sid: str, candidate: str = ""):
        s = svc.design_session(sid)
        p = _proposal(sid)
        data = design.starter_repo(s, p, candidate or p["recommended"])
        name = str(s.value("name")).replace(" ", "-") or "starter"
        return StreamingResponse(
            io.BytesIO(data),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{name}.zip"'},
        )

    @app.post("/api/design/{sid}/save")
    def design_save(
        sid: str, workspace: Annotated[str, Body(embed=True)], candidate: Annotated[str, Body(embed=True)] = ""
    ):
        w = ws(workspace)
        p = _proposal(sid)
        path = design.save_intended(w.root, design.intended(svc.design_session(sid), p, candidate or p["recommended"]))
        return {"saved": path.relative_to(w.root).as_posix(), "drift": w.drift()}

    # ------------------------------------------------------------------ UI
    if (STATIC / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=STATIC / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith("api/"):
                return JSONResponse({"detail": "not found"}, status_code=404)
            f = STATIC / path
            if path and f.is_file():
                return FileResponse(f)
            return FileResponse(STATIC / "index.html")
    else:

        @app.get("/", include_in_schema=False)
        def no_ui():
            return PlainTextResponse(
                "Codewright API is running. The web UI isn't built: cd web && npm install && npm run build"
            )

    return app
