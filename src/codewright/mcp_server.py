"""MCP server: serves Codewright to coding agents (Claude Code, Copilot, Devin...).

Run with ``codewright mcp`` (stdio). Every tool takes the repository path, so
one server can answer for any repo on the machine.
"""

from __future__ import annotations

import functools

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

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


def _ws(path: str):
    try:
        return svc.open(path)[1]
    except FileNotFoundError:
        raise ToolError(f"not a folder: {path}. Pass the repository's absolute path.") from None


def _tool(fn):
    """Register a tool whose anticipated failures reach the agent as a message, not "Error executing tool"."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except (FileNotFoundError, KeyError, ValueError, RuntimeError) as exc:
            msg = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
            raise ToolError(f"{type(exc).__name__}: {msg}") from None

    return server.tool()(wrapper)


@_tool
def review(path: str, base: str = "", proof: bool = False, top: int = 10) -> dict:
    """Ranked 'instead of X, use Y' findings for the repo at `path`.

    base: a git ref; when set, only code changed since that ref is reviewed.
    proof: run the test suite before and after each auto-patch (slower).
    """
    ws = _ws(path)
    r = ws.run_review(proof=proof, base=base or None)
    return {
        "findings": [f.to_dict() for f in r.findings[:top]],
        "dropped_by_critic": len(r.rejected),
        "files": len(r.graph.files),
    }


@_tool
def review_markdown(path: str, base: str = "") -> str:
    """The same review as a Markdown report, ready to paste into a PR or an issue."""
    ws = _ws(path)
    return to_markdown(ws.run_review(proof=False, base=base or None))


@_tool
def explain_finding(path: str, finding_id: str) -> dict:
    """Full detail for one finding: evidence, suggested API and version, gain, risk, when-not, patch, sources."""
    f = _ws(path).finding(finding_id)
    if f is None:
        return {"error": f"no finding {finding_id}; run `review` first"}
    return f.to_dict()


@_tool
def record_feedback(path: str, finding_id: str, status: str, reason: str = "") -> dict:
    """Mark a finding accepted, rejected or muted (tunes future ranking). status: accepted|rejected|muted|clear."""
    return _ws(path).feedback(finding_id, status, reason)


@_tool
def architecture_map(path: str, level: str = "system", target: str = "") -> dict:
    """Architecture diagram data. level: system | module (target = component) | flow (target = entry point id).

    Returns nodes (with files and lines), edges, and Mermaid text.
    """
    m = _ws(path).amap()
    try:
        view = m.view(level, target)
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    return {**view.to_dict(), "mermaid": view.mermaid(), "entry_points": m.entry_points(), "components": m.components()}


@_tool
def ask_code(path: str, question: str) -> dict:
    """Answer a question about the code, with citations to files and lines."""
    return _ws(path).ask(question)


@_tool
def check_drift(path: str) -> dict:
    """Compare the intended architecture (.codewright/design.json from Design mode) with the code."""
    d = _ws(path).drift()
    return d or {"error": "no intended architecture saved; create one in Design mode"}


@_tool
def dependency_releases(path: str) -> list[dict]:
    """Newer releases of the repo's dependencies, and which capability cards an upgrade would unlock."""
    return _ws(path).releases()


@_tool
def search_knowledge(query: str, path: str = "", library: str = "", limit: int = 8) -> list[dict]:
    """Search Codewright's knowledge base: what each library version offers, with when-not-to and sources.

    Use it before writing code against a library (e.g. query "persist sessions", library "google-adk")
    to get the API that fits the installed version. With `path`, each result says whether it fits that
    repo's locked versions ("fits"), needs an upgrade ("upgrade"), or the library isn't used ("unused").
    """
    from . import cards as card_store
    from . import knowledge
    from . import stack as stack_mod

    st = stack_mod.discover(path) if path else None
    keep = ("id", "title", "symbol", "library", "versions", "topic", "suggestion", "gain", "risk", "when_not")
    out = []
    for c in knowledge.search(query, card_store.load(), st, library=library, limit=limit):
        out.append({k: c[k] for k in keep} | {"snippet": c["snippet"], "sources": c["sources"], "fit": c["fit"]})
    return out


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
