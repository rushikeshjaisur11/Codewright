"""MCP server: serves Codewright to coding agents (Claude Code, Cursor, VS Code, Copilot, Devin...).

* ``codewright mcp``: local, over stdio. `repo` is any folder on this machine (or a git URL).
* ``codewright mcp --http``: one shared server for a team, over Streamable HTTP at ``/mcp``,
  behind bearer tokens. `repo` is a folder the server was started with, or a git URL on an
  allowed host; ``review_files`` reviews files sent inline. See ``remote.py`` for the rules.
"""

from __future__ import annotations

import functools
import hmac
import ipaddress
import json
import shutil

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .remote import AccessError, Policy, write_files
from .report import to_markdown
from .service import Service

INSTRUCTIONS = (
    "Codewright reviews a repository for code that re-implements what an installed library, the standard "
    "library or the platform already provides, and proposes version-checked replacements with proof. "
    "Call `review` before refactoring; prefer findings with proof=verified; respect `when_not`. "
    "Call `search_knowledge` before writing code against a library to get the API its installed version offers."
)

server = MCPServer("codewright", instructions=INSTRUCTIONS)
svc = Service()
POLICY = Policy()  # local by default; `main(shared=True)` narrows it


def _ws(repo: str):
    folder, changed = POLICY.resolve(repo)
    try:
        wid, ws = svc.open(folder)
    except FileNotFoundError:
        raise ToolError(f"not a folder: {repo}. " + POLICY.describe()) from None
    if changed:  # a git checkout moved on: forget results computed for the old commit
        svc.close(wid)
        wid, ws = svc.open(folder)
    return ws


def _tool(fn):
    """Register a tool whose anticipated failures reach the agent as a message, not "Error executing tool"."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except AccessError as exc:
            raise ToolError(str(exc)) from None
        except (FileNotFoundError, KeyError, ValueError, RuntimeError) as exc:
            msg = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
            raise ToolError(f"{type(exc).__name__}: {msg}") from None

    return server.tool()(wrapper)


@_tool
def review(repo: str, base: str = "", proof: bool = False, top: int = 10) -> dict:
    """Ranked 'instead of X, use Y' findings, architecture review and convention fixes for a repository.

    repo: the repository's path, a git URL (https://github.com/org/repo, #branch for a branch) or, on a
    shared server, the name of a repository it serves (see `list_repos`).
    base: a git ref; when set, only code changed since that ref is reviewed.
    proof: run the test suite before and after each auto-patch (slower; off on shared servers by default).
    """
    if proof and not POLICY.allow_proof:
        raise ToolError(
            "proof is off on this shared server, because it runs the repository's tests and installs its "
            "dependencies; review without proof, or run Codewright locally (`codewright mcp`) for proof"
        )
    ws = _ws(repo)
    r = ws.run_review(proof=proof, base=base or None)
    return {
        "findings": [f.to_dict() for f in r.findings[:top]],
        "dropped_by_critic": len(r.rejected),
        "files": len(r.graph.files),
    }


@_tool
def review_markdown(repo: str, base: str = "") -> str:
    """The same review as a Markdown report, ready to paste into a PR or an issue."""
    ws = _ws(repo)
    return to_markdown(ws.run_review(proof=False, base=base or None))


@_tool
def explain_finding(repo: str, finding_id: str) -> dict:
    """Full detail for one finding: evidence, suggested API and version, gain, risk, when-not, patch, sources."""
    f = _ws(repo).finding(finding_id)
    if f is None:
        return {"error": f"no finding {finding_id}; run `review` first"}
    return f.to_dict()


@_tool
def record_feedback(repo: str, finding_id: str, status: str, reason: str = "") -> dict:
    """Mark a finding accepted, rejected or muted (tunes future ranking). status: accepted|rejected|muted|clear."""
    return _ws(repo).feedback(finding_id, status, reason)


@_tool
def architecture_map(repo: str, level: str = "system", target: str = "") -> dict:
    """Architecture diagram data. level: system | module (target = component) | flow (target = entry point id).

    Returns nodes (with files and lines), edges, and Mermaid text.
    """
    m = _ws(repo).amap()
    try:
        view = m.view(level, target)
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    return {**view.to_dict(), "mermaid": view.mermaid(), "entry_points": m.entry_points(), "components": m.components()}


@_tool
def ask_code(repo: str, question: str) -> dict:
    """Answer a question about the code, with citations to files and lines."""
    return _ws(repo).ask(question)


@_tool
def check_drift(repo: str) -> dict:
    """Compare the intended architecture (.codewright/design.json from Design mode) with the code."""
    d = _ws(repo).drift()
    return d or {"error": "no intended architecture saved; create one in Design mode"}


@_tool
def dependency_releases(repo: str) -> list[dict]:
    """Newer releases of the repo's dependencies: what an upgrade unlocks, and an upgrade preview of the
    findings that would appear (things to fix) or go away in this repo at the latest version."""
    return _ws(repo).releases()


@_tool
def search_knowledge(query: str, repo: str = "", library: str = "", limit: int = 8) -> list[dict]:
    """Search Codewright's knowledge base: what each library version offers, with when-not-to and sources.

    Use it before writing code against a library (e.g. query "persist sessions", library "google-adk")
    to get the API that fits the installed version. With `repo`, each result says whether it fits that
    repo's locked versions ("fits"), needs an upgrade ("upgrade"), or the library isn't used ("unused").
    """
    from . import cards as card_store
    from . import knowledge
    from . import stack as stack_mod

    st = stack_mod.discover(POLICY.resolve(repo)[0]) if repo else None
    keep = ("id", "title", "symbol", "library", "versions", "topic", "suggestion", "gain", "risk", "when_not")
    out = []
    for c in knowledge.search(query, card_store.load(), st, library=library, limit=limit):
        out.append({k: c[k] for k in keep} | {"snippet": c["snippet"], "sources": c["sources"], "fit": c["fit"]})
    return out


@_tool
def review_files(files: dict[str, str], top: int = 10) -> dict:
    """Review files sent inline, for when the server can't reach the repository (a shared server).

    files: {relative path: content}. Send the files you are working on plus the dependency files
    that pin versions (uv.lock, poetry.lock, requirements*.txt, pyproject.toml), so suggestions
    match the installed versions. At most 500 files / 3 MB. Nothing is kept or run; no proof.
    """
    from .engine import review as run_review

    root = write_files(files)
    try:
        r = run_review(root, run_proof=False, top=None, use_feedback=False, build_env=False)
        return {
            "findings": [f.to_dict() for f in r.findings[:top]],
            "dropped_by_critic": len(r.rejected),
            "files": len(r.graph.files),
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


@_tool
def list_repos() -> dict:
    """What `repo` can be on this server: the repositories it serves, cached clones and allowed git hosts."""
    return {
        "shared": POLICY.shared,
        "repos": [{"name": r.name} if POLICY.shared else {"name": r.name, "path": str(r)} for r in POLICY.roots],
        "git_hosts": list(POLICY.git_hosts),
        "cached_clones": POLICY.cached_clones(),
        "proof": POLICY.allow_proof,
        "how": POLICY.describe(),
    }


# --------------------------------------------------------------------------- shared server over HTTP


class BearerAuth:
    """Every request to the shared server needs `Authorization: Bearer <one of the tokens>`."""

    def __init__(self, app, tokens: list[str], open_paths: tuple[str, ...] = ("/healthz",)):
        self.app, self.tokens, self.open_paths = app, [t.encode() for t in tokens], open_paths

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] not in self.open_paths:
            given = dict(scope["headers"]).get(b"authorization", b"")
            token = given[7:] if given[:7].lower() == b"bearer " else b""
            if not any(hmac.compare_digest(token, t) for t in self.tokens):
                body = json.dumps({"error": "missing or wrong bearer token"}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"www-authenticate", b'Bearer realm="codewright"'),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def http_app(
    tokens: list[str],
    host: str = "127.0.0.1",
    public_hosts: list[str] | None = None,
    sso=None,
    public_url: str = "",
    required_scopes: list[str] | None = None,
):
    """The Streamable HTTP app (at /mcp, plus /healthz), behind bearer tokens when any are given.

    With `sso` (an SSOVerifier), sign-in goes through your identity provider: the MCP SDK guards /mcp
    with the verifier and publishes the OAuth protected-resource metadata at `public_url`, which
    MCP clients use to find where to sign in. Static tokens are then checked by the verifier too.
    """
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.responses import JSONResponse

    if not getattr(server, "_codewright_health", False):

        @server.custom_route("/healthz", methods=["GET"])
        async def healthz(request):  # for load balancers and container health checks
            return JSONResponse({"ok": True})

        server._codewright_health = True

    if public_hosts:  # behind a domain: only those Host headers (and any port on them)
        allowed = [h for name in public_hosts for h in (name, f"{name}:*")]
        security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=allowed)
    elif is_loopback(host):  # a page in the user's browser must not reach a local server (DNS rebinding)
        local = ["localhost", "127.0.0.1", "[::1]"]
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[h for name in local for h in (name, f"{name}:*")],
        )
    else:  # a team server reached by IP or many names: the tokens are the guard
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    if sso is not None:
        from mcp.server.auth.settings import AuthSettings

        auth = AuthSettings(
            issuer_url=sso.issuer,
            resource_server_url=public_url,
            required_scopes=required_scopes or None,
            validate_token_resource=False,  # the verifier checks the audience itself
        )
        return server._lowlevel_server.streamable_http_app(
            transport_security=security,
            host=host,
            auth=auth,
            token_verifier=sso,
            custom_starlette_routes=server._custom_starlette_routes,
        )
    app = server.streamable_http_app(transport_security=security, host=host)
    return BearerAuth(app, tokens) if tokens else app


def main(
    http: bool = False,
    host: str = "127.0.0.1",
    port: int = 8765,
    tokens: list[str] | None = None,
    policy: Policy | None = None,
    public_hosts: list[str] | None = None,
    sso=None,
    public_url: str = "",
    required_scopes: list[str] | None = None,
) -> None:
    global POLICY
    if policy is not None:
        POLICY = policy
    if not http:
        server.run("stdio")
        return
    import uvicorn

    app = http_app(tokens or [], host, public_hosts, sso, public_url, required_scopes)
    uvicorn.run(app, host=host, port=port, log_level="warning", proxy_headers=True)


if __name__ == "__main__":
    main()
