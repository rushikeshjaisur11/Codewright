"""Web app: REST API + the built React UI, served by `codewright serve`."""

from __future__ import annotations

import contextlib
import io
import subprocess
import threading
from dataclasses import asdict
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, design
from . import cards as card_store
from .feedback import Feedback
from .intents import DETECTORS
from .llm import LLMConfig
from .llm import status as llm_status
from .report import to_json, to_markdown, to_pr_comment
from .service import Service, Workspace

STATIC = Path(__file__).parent / "web"


def create_app(roots: list[str] | None = None) -> FastAPI:
    svc = Service(roots or [])
    app = FastAPI(title="Codewright", version=__version__)
    app.state.svc = svc

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
            "last_run": w.last_run,
            "error": w.error,
            "has_result": w.result is not None,
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
    def open_workspace(path: str = Body(..., embed=True)):
        try:
            wid, w = svc.open(path)
        except FileNotFoundError:
            raise HTTPException(400, f"not a directory: {path}") from None
        return ws_summary(wid, w)

    @app.get("/api/cards")
    def catalog():
        return [asdict(c) for c in card_store.load()]

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
    def start_review(wid: str, proof: bool = Body(True, embed=True), base: str = Body("", embed=True)):
        w = ws(wid)
        if w.running:
            return ws_summary(wid, w)

        def run():
            with contextlib.suppress(Exception):  # the error is recorded on w.error
                w.run_review(proof=proof, base=base or None)

        w.running = True
        threading.Thread(target=run, daemon=True).start()
        return ws_summary(wid, w)

    @app.get("/api/w/{wid}/review")
    def get_review(wid: str):
        return result_payload(wid, ws(wid))

    @app.post("/api/w/{wid}/findings/{fid}/feedback")
    def feedback(wid: str, fid: str, status: str = Body(..., embed=True), reason: str = Body("", embed=True)):
        try:
            return ws(wid).feedback(fid, status, reason)
        except KeyError:
            raise HTTPException(404, "unknown finding") from None
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    @app.post("/api/w/{wid}/mute")
    def mute(
        wid: str,
        intent: str = Body(..., embed=True),
        path: str = Body("", embed=True),
        reason: str = Body("", embed=True),
        undo: bool = Body(False, embed=True),
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
            "entry_points": m.entry_points(),
            "changes": changes_since(w.root, m.system()),
            "drift": w.drift(),
        }

    @app.get("/api/w/{wid}/map/{level}")
    def map_view(wid: str, level: str, target: str = ""):
        m = ws(wid).amap()
        if level == "system":
            view = m.system()
        elif level == "module":
            comps = m.components()
            if not comps:
                raise HTTPException(404, "no components") from None
            view = m.module(target or comps[0])
        elif level == "flow":
            eps = m.entry_points()
            if not eps:
                raise HTTPException(404, "no entry points found") from None
            view = m.flow(target or eps[0]["id"])
        else:
            raise HTTPException(400, "level must be system, module or flow") from None
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
    def chat(wid: str, question: str = Body(..., embed=True), history: list[dict] = Body([], embed=True)):
        return ws(wid).ask(question, history)

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
            "llm": llm_status(cfg),
            "constraints": fb.constraints,
            "mutes": [asdict(m) for m in fb.mutes],
        }

    @app.post("/api/w/{wid}/settings")
    def set_settings(
        wid: str,
        model: str = Body(None, embed=True),
        offline: bool = Body(None, embed=True),
        constraints: list[str] = Body(None, embed=True),
    ):
        w = ws(wid)
        cfg = LLMConfig.load(w.root)
        if model is not None:
            cfg.model = model.strip() or cfg.model
        if offline is not None:
            cfg.offline = offline
        cfg.save(w.root)
        if constraints is not None:
            fb = Feedback.load(w.root)
            fb.constraints = [c.strip() for c in constraints if c.strip()]
            fb.save(w.root)
        return get_settings(wid)

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
    def design_answer(sid: str, qid: str = Body(..., embed=True), value=Body(..., embed=True)):
        s = svc.design_session(sid)
        try:
            s.answer(qid, value)
        except (ValueError, KeyError) as e:
            raise HTTPException(422, str(e)) from None
        return design_state(s)

    @app.post("/api/design/{sid}/assumptions")
    def design_assumptions(sid: str, overrides: dict = Body(..., embed=True)):
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
    def design_propose(sid: str, workspace: str = Body("", embed=True)):
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
    def design_save(sid: str, workspace: str = Body(..., embed=True), candidate: str = Body("", embed=True)):
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
