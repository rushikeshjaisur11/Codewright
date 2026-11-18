"""AI checks on findings, the deeper architecture review, and teaching the knowledge base from the UI."""

import json
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codewright import aicheck, cards, llm, teach
from codewright.server import create_app

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def model(monkeypatch):
    """A configured model whose replies the test sets."""
    replies = []
    monkeypatch.setattr(llm, "status", lambda cfg: {"available": True, "reason": "", "model": "m", "provider": "p"})
    for mod in (aicheck, teach):
        monkeypatch.setattr(mod, "llm_status", llm.status, raising=False)

    def complete(cfg, system, messages, max_tokens=1200):
        replies.append(messages[-1]["content"])
        return model.reply

    model.reply = ""
    monkeypatch.setattr(llm, "complete", complete)
    monkeypatch.setattr(aicheck, "complete", complete)
    model.asked = replies
    return model


def _client(repo):
    c = TestClient(create_app([str(repo)]), base_url="http://localhost")
    wid = c.get("/api/workspaces").json()[0]["id"]
    c.post(f"/api/w/{wid}/review", json={"proof": False})
    for _ in range(300):
        d = c.get(f"/api/w/{wid}/review").json()
        if d["has_result"] and not d["running"]:
            return c, wid, d
        time.sleep(0.05)
    raise AssertionError("review did not finish")


@pytest.fixture
def repo(tmp_path):
    dest = tmp_path / "app"
    shutil.copytree(FIX / "sample_app", dest, ignore=shutil.ignore_patterns(".codewright", "__pycache__"))
    return dest


def test_ai_check_is_stored_and_shown_until_the_code_changes(repo, model):
    c, wid, d = _client(repo)
    f = d["result"]["findings"][0]
    model.reply = (
        'Here: {"verdict": "confirmed", "confidence": 0.8, "reason": "the API exists", "risks": ["none"], "test": "t"}'
    )
    out = c.post(f"/api/w/{wid}/findings/{f['id']}/ai-check").json()
    assert out["verdict"] == "confirmed" and out["confidence"] == 0.8
    assert f["title"] in model.asked[-1] and f["code_today"][:30] in model.asked[-1]
    shown = next(x for x in c.get(f"/api/w/{wid}/review").json()["result"]["findings"] if x["id"] == f["id"])
    assert shown["ai_check"]["verdict"] == "confirmed"
    # the verdict is for this code and patch: a different patch is a different question
    store = json.loads((repo / ".codewright" / "ai-checks.json").read_text())
    assert aicheck.key({**f, "patch": "changed"}) != store[f["id"]]["key"]


def test_ai_check_without_a_model_says_how_to_get_one(repo):
    c, wid, d = _client(repo)
    r = c.post(f"/api/w/{wid}/findings/{d['result']['findings'][0]['id']}/ai-check")
    assert r.status_code == 409 and "Settings" in r.json()["detail"]


def test_deeper_architecture_review_keeps_only_real_files(model, tmp_path):
    from codewright import agentreview, aimap, graph

    m = aimap.AiMap(graph.build(FIX / "complex_agents"))
    model.reply = json.dumps(
        [
            {
                "title": "Cache the planner",
                "why": "w",
                "fix": "f",
                "file": "assistant/graph.py",
                "line": 17,
                "severity": "low",
            },
            {"title": "Invented", "why": "w", "fix": "f", "file": "nope.py", "line": 1},
        ]
    )
    out = agentreview.deep_review(m, [], llm.LLMConfig())
    assert [i["title"] for i in out["insights"]] == ["Cache the planner"] and out["insights"][0]["source"] == "ai"
    assert "fan_out" in model.asked[-1]  # the key functions' code is sent


def test_teach_a_card_from_the_ui_and_use_it(repo):
    c, wid, _ = _client(repo)
    card = {
        "title": "Fan out with Send",
        "library": "langgraph",
        "versions": ">=0.2",
        "suggestion": "Use Send.",
        "sources": "https://example.com/send\nhttps://example.com/b",
    }
    saved = c.post(f"/api/w/{wid}/cards", json={"card": card}).json()
    assert saved["id"] == "team.langgraph-fan-out-with-send" and saved["intent"] == "reference"
    assert (repo / ".codewright" / "cards" / f"{saved['id']}.yaml").is_file()
    found = c.get(f"/api/cards?workspace={wid}&q=fan out send").json()
    mine = next(x for x in found if x["id"] == saved["id"])  # ranked with the built-in cards (and by fit)
    assert mine["origin"] == "repo"
    assert c.post(f"/api/w/{wid}/cards", json={"card": {"title": "x"}}).status_code == 400  # needs a suggestion
    builtin = cards.load()[0].id
    assert c.post(f"/api/w/{wid}/cards", json={"card": {**card, "id": builtin}}).status_code == 400
    assert c.delete(f"/api/w/{wid}/cards/{saved['id']}").json() == {"deleted": saved["id"]}
    assert c.delete(f"/api/w/{wid}/cards/{saved['id']}").status_code == 404


def test_a_broken_team_card_is_skipped_not_fatal(repo):
    d = cards.repo_dir(repo)
    d.mkdir(parents=True)
    (d / "bad.yaml").write_text("- id: x\n  title: [unclosed\n")
    (d / "partial.yaml").write_text("- id: y\n  title: only a title\n")
    assert len(cards.for_repo(repo)) == len(cards.load())


def test_draft_and_deepen_with_the_model(repo, model):
    c, wid, _ = _client(repo)
    model.reply = json.dumps(
        {
            "title": "Bound fan-out with a semaphore",
            "library": "",
            "suggestion": "Use a Semaphore.",
            "sources": ["https://docs.python.org/3/library/asyncio-sync.html"],
        }
    )
    d = c.post(
        f"/api/w/{wid}/cards/draft", json={"topic": "limit concurrency", "library": "httpx", "notes": "docs here"}
    ).json()
    assert d["title"] == "Bound fan-out with a semaphore" and d["library"] == "httpx" and "model" in d
    assert "docs here" in model.asked[-1]
    base = cards.load()[0]
    c.post(f"/api/w/{wid}/cards/draft", json={"deepen": base.id})
    assert "Go deeper than this existing card" in model.asked[-1] and base.title in model.asked[-1]
