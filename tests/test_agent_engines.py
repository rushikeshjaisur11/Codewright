"""The agent on each engine (built-in, LangGraph, Google ADK): the same tools, sandbox, limits and result,
whichever framework drives the conversation. The model is scripted; the tools, sandbox and tests are real."""

import json
import sys
import threading

import pytest

from codewright import agent, llm, proof_env

APP = "def add(a, b):\n    return a - b\n"
TEST = "from app import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
FIX = [
    [("read_file", {"path": "app.py"})],
    [("edit_file", {"path": "app.py", "old": "a - b", "new": "a + b"})],
    [("run_tests", {"paths": ["tests/test_app.py"]})],
    [("finish", {"summary": "add() subtracted; it adds now and its test passes."})],
]


ENGINES = [
    pytest.param(e, marks=pytest.mark.skipif(not ok["available"], reason=ok["reason"]))
    for e, ok in agent.engine_status().items()
]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "app.py").write_text(APP)
    (root / "tests" / "test_app.py").write_text(TEST)
    monkeypatch.setattr(proof_env, "resolve", lambda r, py=None, build=True: proof_env.TestEnv(sys.executable, "t"))
    monkeypatch.setattr(llm, "status", lambda cfg: {"available": True, "reason": ""})
    return root


def scripted(monkeypatch, plan, final="done"):
    """A model making the calls in `plan`, one turn each, then answering `final`. Every turn carries the
    provider's own message as `raw`, which an engine must hand back unchanged."""
    seen = []

    def step(cfg, system, turns, tools, **k):
        seen.append({"system": system, "turns": json.loads(json.dumps(turns)), "tools": [t["name"] for t in tools]})
        n = sum(1 for t in turns if t["role"] == "assistant")
        if n >= len(plan):
            return {"role": "assistant", "text": final, "calls": [], "raw": {"turn": n}, "truncated": False}
        calls = [{"id": f"call-{n}-{i}", "name": a, "args": b} for i, (a, b) in enumerate(plan[n])]
        return {"role": "assistant", "text": f"Step {n}.", "calls": calls, "raw": {"turn": n}, "truncated": False}

    monkeypatch.setattr(llm, "tool_step", step)
    return seen


@pytest.mark.parametrize("engine", ENGINES)
def test_every_engine_fixes_the_bug_the_same_way(repo, monkeypatch, engine):
    seen = scripted(monkeypatch, FIX)
    events = []
    r = agent.run(repo, "add() is wrong; fix it", engine=engine, web=False, progress=events.append)
    assert (r["engine"], r["ended"]) == (engine, "finished")
    assert r["summary"] == "add() subtracted; it adds now and its test passes."
    assert r["files"] == ["app.py"] and "+    return a + b" in r["patch"] and r["applies"]
    assert r["verification"]["ok"] and r["tests"]["ok"] and r["tests"]["after_last_edit"]
    assert [e["tool"] for e in events if e["kind"] == "tool"] == ["read_file", "edit_file", "run_tests"]
    assert [e["text"] for e in events if e["kind"] == "text"][:2] == ["Step 0.", "Step 1."]
    assert len(seen) == 4 and "senior software engineer" in seen[0]["system"]
    assert "Task:\nadd() is wrong" in seen[0]["turns"][0]["content"]
    last = seen[-1]["turns"]
    assert [t["role"] for t in last] == ["user"] + ["assistant", "tool"] * 3
    assert [t["raw"] for t in last if t["role"] == "assistant"] == [{"turn": 0}, {"turn": 1}, {"turn": 2}]
    assert [t["calls"][0]["id"] for t in last if t["role"] == "assistant"] == ["call-0-0", "call-1-0", "call-2-0"]
    assert [t["results"][0]["id"] for t in last if t["role"] == "tool"] == ["call-0-0", "call-1-0", "call-2-0"]
    assert last[-1]["results"][0]["content"].startswith("PASSED")
    assert (repo / "app.py").read_text() == APP  # only the sandbox changed


@pytest.mark.parametrize("engine", ENGINES)
def test_every_engine_stops_at_the_step_limit(repo, monkeypatch, engine):
    seen = scripted(monkeypatch, [[("list_files", {})]] * 10)
    r = agent.run(repo, "t", engine=engine, max_steps=3, verify=False)
    assert r["ended"] == "step limit" and len(seen) == 3


@pytest.mark.parametrize("engine", ENGINES)
def test_every_engine_can_be_stopped(repo, monkeypatch, engine):
    stop = threading.Event()
    scripted(monkeypatch, [[("list_files", {})]] * 10)
    r = agent.run(repo, "t", engine=engine, stop=stop, progress=lambda e: e.get("kind") == "tool" and stop.set())
    assert r["ended"] == "stopped" and r["steps_used"] == 1


@pytest.mark.parametrize("engine", ENGINES)
def test_an_answer_without_tool_calls_ends_the_run(repo, monkeypatch, engine):
    scripted(monkeypatch, [], final="Nothing to change: add() is already correct.")
    r = agent.run(repo, "t", engine=engine)
    assert r["ended"] == "answered" and r["summary"] == "Nothing to change: add() is already correct."
    assert r["patch"] == "" and not r["applies"]


@pytest.mark.parametrize("engine", ENGINES)
def test_a_failing_model_call_keeps_what_was_done(repo, monkeypatch, engine):
    calls = []

    def step(cfg, system, turns, tools, **k):
        calls.append(1)
        if len(calls) > 1:
            raise llm.LLMError("today's AI budget of 1 model calls is used up")
        c = {"id": "c1", "name": "edit_file", "args": {"path": "app.py", "old": "a - b", "new": "a + b"}}
        return {"role": "assistant", "text": "", "calls": [c], "raw": None, "truncated": False}

    monkeypatch.setattr(llm, "tool_step", step)
    r = agent.run(repo, "t", engine=engine, verify=False)
    assert r["ended"] == "error" and "budget" in r["error"] and "+    return a + b" in r["patch"]


def test_an_engine_that_is_not_installed_says_how_to_get_it(repo, monkeypatch):
    monkeypatch.setattr(
        agent,
        "engine_status",
        lambda: {
            "builtin": {"available": True, "reason": "", "label": "Built-in"},
            "langgraph": {"available": False, "reason": "pip install 'codewright[langgraph]'", "label": "LangGraph"},
            "adk": {"available": False, "reason": "pip install 'codewright[adk]'", "label": "Google ADK"},
        },
    )
    with pytest.raises(RuntimeError, match=r"LangGraph engine is not installed: pip install 'codewright\[langgraph\]'"):
        agent.run(repo, "t", engine="langgraph")
    with pytest.raises(ValueError, match="unknown agent engine"):
        agent.run(repo, "t", engine="crewai")


def test_the_engine_is_chosen_in_settings_and_the_environment_wins(monkeypatch):
    from fastapi.testclient import TestClient

    from codewright.server import create_app

    monkeypatch.delenv("CODEWRIGHT_AGENT_ENGINE", raising=False)
    c = TestClient(create_app([]), base_url="http://localhost")
    got = c.get("/api/settings/agent").json()
    assert got["engine"] == "builtin" and [e["id"] for e in got["engines"]] == ["builtin", "langgraph", "adk"]
    assert c.put("/api/settings/agent", json={"engine": "adk"}).json()["engine"] == "adk"
    assert agent.default_engine() == "adk"
    assert c.put("/api/settings/agent", json={"engine": "nope"}).status_code == 400
    monkeypatch.setenv("CODEWRIGHT_AGENT_ENGINE", "langgraph")
    assert c.get("/api/settings/agent").json() | {"engines": None} == {
        "engine": "langgraph",
        "from_environment": True,
        "engines": None,
    }
