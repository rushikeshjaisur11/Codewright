import textwrap
from pathlib import Path

from codewright import cards, knowledge, stack


def adk_repo(tmp_path: Path, version: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
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
    assert knowledge.fit(by_id["adk.upgrade-to-2"], st)["status"] == "older"  # only for 1.x repos
    assert knowledge.fit(by_id["requests.shared-session"], st)["status"] == "unused"
    assert knowledge.fit(by_id["adk.ref.cancel"], None)["status"] == "unknown"


def test_search_filters_by_library_and_fit(tmp_path):
    st = stack.discover(adk_repo(tmp_path / "v2", "2.9.0"))
    hits = knowledge.search("", cards.load(), st, library="google-adk", only_fitting=True, limit=None)
    assert hits and all(h["library"] == "google-adk" and h["fit"]["status"] == "fits" for h in hits)
    ids = {h["id"] for h in hits}
    assert "adk.workflow" in ids and "adk.ref.cancel" not in ids  # abort_signal is 2.11.0+
    assert any(h["reference"] for h in hits) and any(not h["reference"] for h in hits)
    # On 1.x, the only ADK knowledge that fits is the upgrade.
    old = stack.discover(adk_repo(tmp_path / "v1", "1.39.1"))
    hits = knowledge.search("", cards.load(), old, library="google-adk", only_fitting=True, limit=None)
    assert [h["id"] for h in hits] == ["adk.upgrade-to-2"]


def test_cards_api_returns_fit_for_a_workspace(tmp_path):
    from fastapi.testclient import TestClient

    from codewright.server import create_app

    repo = adk_repo(tmp_path, "2.14.1")
    client = TestClient(create_app([str(repo)]), base_url="http://localhost")
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
    hits = search_knowledge("pause a workflow for human approval", repo=str(adk_repo(tmp_path, "2.14.1")))
    assert hits[0]["id"] == "adk.ref.workflow-human-input"
    assert hits[0]["fit"]["status"] == "fits" and hits[0]["sources"] and hits[0]["snippet"]


def test_chat_adds_relevant_knowledge_and_leaves_code_questions_alone(tmp_path):
    from codewright.service import Service

    w = next(iter(Service([str(Path(__file__).parent / "fixtures" / "sample_app")]).workspaces.values()))
    retry = w.ask("should I retry failed model calls?")
    assert {k["id"] for k in retry["knowledge"]} >= {"adk.gemini-retry-options"}
    assert "\n\n**From the knowledge base**" in retry["answer"] and "[K1]" in retry["answer"]
    assert all(k["sources"] and k["fit"]["status"] == "fits" for k in retry["knowledge"])
    assert w.ask("how are rates fetched?")["knowledge"] == []  # a code question: no cards


def test_extras_card_does_not_fit_when_the_package_is_installed(tmp_path):
    lock = (
        'version = 1\n\n[[package]]\nname = "google-adk"\nversion = "2.14.1"\n\n'
        '[[package]]\nname = "sqlalchemy"\nversion = "2.1.8"\n'
    )
    (tmp_path / "uv.lock").write_text(lock)
    by_id = {c.id: c for c in cards.load()}
    fit = knowledge.fit(by_id["adk.extra-db"], stack.discover(tmp_path))
    assert fit == {"status": "unused", "detail": "sqlalchemy is already installed"}


def test_upgrade_preview_shows_what_an_upgrade_would_change(tmp_path):
    from codewright import graph, watcher

    (tmp_path / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "google-adk"\nversion = "1.39.1"\n'
        '\n[[package]]\nname = "pydantic"\nversion = "1.10.13"\n'
    )
    (tmp_path / "agent.py").write_text(
        "from google.adk.agents import RunConfig, SequentialAgent\n"
        "from pydantic import BaseModel, validator\n\n"
        "root_agent = SequentialAgent(name='p', sub_agents=[])\n"
        "cfg = RunConfig(save_live_audio=True)\n\n"
        "class M(BaseModel):\n    x: int\n\n    @validator('x')\n    def pos(cls, v):\n        return v\n"
    )
    g, st = graph.build(tmp_path), stack.discover(tmp_path)
    adk = watcher.upgrade_preview(g, st, "google-adk", "2.14.1")
    assert {n["card"] for n in adk["new"]} == {"adk.workflow", "adk.save-live-blob"}
    assert next(n for n in adk["new"] if n["card"] == "adk.save-live-blob")["autofix"] is True
    assert [r["card"] for r in adk["resolved"]] == ["adk.upgrade-to-2"]
    pyd = watcher.upgrade_preview(g, st, "pydantic", "2.14.3")
    assert [n["card"] for n in pyd["new"]] == ["pydantic.field-validator"] and pyd["resolved"] == []
    assert watcher.upgrade_preview(g, st, "not-a-dependency", "1.0") == {"new": [], "resolved": []}
