"""MCP server: serves Codewright to coding agents (Claude Code, Copilot, Devin...).

Run with ``codewright mcp`` (stdio). Every tool takes the repository path, so
one server can answer for any repo on the machine.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from .report import to_markdown
from .service import Service

INSTRUCTIONS = (
    "Codewright reviews a repository for code that re-implements what an installed library, the standard "
    "library or the platform already provides, and proposes version-checked replacements with proof. "
    "Call `review` before refactoring; prefer findings with proof=verified; respect `when_not`."
)

server = MCPServer("codewright", instructions=INSTRUCTIONS)
svc = Service()


def _ws(path: str):
    return svc.open(path)[1]


@server.tool()
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


@server.tool()
def review_markdown(path: str, base: str = "") -> str:
    """The same review as a Markdown report, ready to paste into a PR or an issue."""
    ws = _ws(path)
    return to_markdown(ws.run_review(proof=False, base=base or None))


@server.tool()
def explain_finding(path: str, finding_id: str) -> dict:
    """Full detail for one finding: evidence, suggested API and version, gain, risk, when-not, patch, sources."""
    f = _ws(path).finding(finding_id)
    if f is None:
        return {"error": f"no finding {finding_id}; run `review` first"}
    return f.to_dict()


@server.tool()
def record_feedback(path: str, finding_id: str, status: str, reason: str = "") -> dict:
    """Mark a finding accepted, rejected or muted (tunes future ranking). status: accepted|rejected|muted|clear."""
    return _ws(path).feedback(finding_id, status, reason)


@server.tool()
def architecture_map(path: str, level: str = "system", target: str = "") -> dict:
    """Architecture diagram data. level: system | module (target = component) | flow (target = entry point id).

    Returns nodes (with files and lines), edges, and Mermaid text.
    """
    m = _ws(path).amap()
    if level == "module":
        view = m.module(target or m.components()[0])
    elif level == "flow":
        eps = m.entry_points()
        if not eps:
            return {"error": "no entry points found"}
        view = m.flow(target or eps[0]["id"])
    else:
        view = m.system()
    return {**view.to_dict(), "mermaid": view.mermaid(), "entry_points": m.entry_points(), "components": m.components()}


@server.tool()
def ask_code(path: str, question: str) -> dict:
    """Answer a question about the code, with citations to files and lines."""
    return _ws(path).ask(question)


@server.tool()
def check_drift(path: str) -> dict:
    """Compare the intended architecture (.codewright/design.json from Design mode) with the code."""
    d = _ws(path).drift()
    return d or {"error": "no intended architecture saved; create one in Design mode"}


@server.tool()
def dependency_releases(path: str) -> list[dict]:
    """Newer releases of the repo's dependencies, and which capability cards an upgrade would unlock."""
    return _ws(path).releases()


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
