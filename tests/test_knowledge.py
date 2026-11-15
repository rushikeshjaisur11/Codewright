import textwrap
from pathlib import Path

from codewright import cards, knowledge, stack


def adk_repo(tmp_path: Path, version: str) -> Path:
    (tmp_path / "uv.lock").write_text(
        textwrap.dedent(f"""
        version = 1

        [[package]]
        name = "google-adk"
        version = "{version}"
        """)
    )
    return tmp_path


def test_search_ranks_relevant_cards_and_reports_fit(tmp_path):
    st = stack.discover(adk_repo(tmp_path, "2.9.0"))
    hits = knowledge.search("persist sessions across restarts", cards.load(), st, limit=3)
    assert {"adk.persistent-sessions", "adk.ref.sqlite-sessions"} & {h["id"] for h in hits}
    assert all(h["fit"]["status"] == "fits" for h in hits)


def test_fit_says_what_needs_an_upgrade_and_what_is_unused(tmp_path):
    st = stack.discover(adk_repo(tmp_path, "2.9.0"))
    by_id = {c.id: c for c in cards.load()}
    assert knowledge.fit(by_id["adk.ref.cancel"], st)["status"] == "upgrade"  # abort_signal is 2.11.0+
    assert "needs >=2.11.0" in knowledge.fit(by_id["adk.ref.cancel"], st)["detail"]
    assert knowledge.fit(by_id["adk.sequential-agent"], st)["status"] == "older"  # a 1.x card
    assert knowledge.fit(by_id["requests.shared-session"], st)["status"] == "unused"
    assert knowledge.fit(by_id["adk.ref.cancel"], None)["status"] == "unknown"


def test_search_filters_by_library_and_fit(tmp_path):
    st = stack.discover(adk_repo(tmp_path, "1.10.0"))
    hits = knowledge.search("", cards.load(), st, library="google-adk", only_fitting=True, limit=None)
    assert hits and all(h["library"] == "google-adk" and h["fit"]["status"] == "fits" for h in hits)
    assert "adk.workflow" not in {h["id"] for h in hits}  # Workflow is 2.0+
    assert any(h["reference"] for h in hits) and any(not h["reference"] for h in hits)


def test_cards_api_returns_fit_for_a_workspace(tmp_path):
    from fastapi.testclient import TestClient

    from codewright.server import create_app

    repo = adk_repo(tmp_path, "2.14.1")
    client = TestClient(create_app([str(repo)]))
    wid = client.get("/api/workspaces").json()[0]["id"]
    rows = client.get("/api/cards", params={"workspace": wid}).json()
    by_id = {r["id"]: r for r in rows}
    assert by_id["adk.ref.cancel"]["fit"]["status"] == "fits"
    assert by_id["adk.ref.cancel"]["reference"] is True and by_id["adk.ref.cancel"]["topic"]
    assert len(rows) == len(cards.load())
    assert all(r["fit"]["status"] == "unknown" for r in client.get("/api/cards").json())


def test_search_knowledge_mcp_tool(tmp_path):
    import asyncio

    from codewright.mcp_server import search_knowledge, server

    assert "search_knowledge" in {t.name for t in asyncio.run(server.list_tools())}
    hits = search_knowledge("pause a workflow for human approval", path=str(adk_repo(tmp_path, "2.14.1")))
    assert hits[0]["id"] == "adk.ref.workflow-human-input"
    assert hits[0]["fit"]["status"] == "fits" and hits[0]["sources"] and hits[0]["snippet"]
