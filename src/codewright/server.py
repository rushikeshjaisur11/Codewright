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
        return knowledge.search(q, card_store.load(), st, library=library, limit=None)

    # ------------------------------------------------------------------ review
    def result_payload(wid: str, w: Workspace) -> dict:
        base = ws_summary(wid, w)
        if w.result is None:
            return {**base, "result": None}
        r = w.result
        fb = Feedback.load(w.root)
        findings = [f.to_dict() for f in r.findings]
        verified = sum(1 for f in r.findings if f.proof.value == "verified")
        return {
            **base,
            "result": {
                "stack": {
                    "python": r.stack.python,
                    "sources": r.stack.sources,
                    "docker_images": r.stack.docker_images,
                    "dependencies": [
                        {"name": d.name, "version": d.version, "specifier": d.specifier, "source": d.source}
                        for d in sorted(r.stack.dependencies.values(), key=lambda d: d.name)
                    ],
                },
                "files": len(r.graph.files),
                "functions": sum(1 for _ in r.graph.functions()),
                "entry_points": len(r.graph.entry_points),
                "intent_matches": r.total_matches,
                "seconds": r.seconds,
                "test_env": r.test_env,
                "base": r.base,
                "findings": findings,
                "rejected": [f.to_dict() for f in r.rejected],
                "muted": [f.to_dict() for f in r.muted],
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
        wid: str, proof: Annotated[bool, Body(embed=True)] = True, base: Annotated[str, Body(embed=True)] = ""
    ):
        w = ws(wid)
        if w.running:
            return ws_summary(wid, w)

        def run():
            with contextlib.suppress(Exception):  # the error is recorded on w.error
                w.run_review(proof=proof, base=base or None)

        w.running = True
        threading.Thread(target=run, daemon=True).start()
        return ws_summary(wid, w)

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

    @app.get("/api/w/{wid}/ai")
    def ai(wid: str):
        return ws(wid).ai()

    @app.get("/api/w/{wid}/drift")
    def drift(wid: str):
        return ws(wid).drift()

    @app.get("/api/w/{wid}/releases")
    def releases(wid: str):
        return ws(wid).releases()

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
        return {"saved": str(path.relative_to(w.root)), "drift": w.drift()}

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
