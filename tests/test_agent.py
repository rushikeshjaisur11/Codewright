"""The AI agent: tools for searching and reading the repo, the web, editing and testing in a sandbox copy, the
loop that drives them for every provider, and the result as a patch the user applies."""

import json
import sys
import time

import pytest

from codewright import agent, llm, proof_env, websearch

APP = "def add(a, b):\n    return a - b\n"
TEST = "from app import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "app.py").write_text(APP)
    (root / "tests" / "test_app.py").write_text(TEST)
    monkeypatch.setattr(proof_env, "resolve", lambda r, py=None, build=True: proof_env.TestEnv(sys.executable, "t"))
    monkeypatch.setattr(llm, "status", lambda cfg: {"available": True, "reason": ""})
    return root


def scripted(monkeypatch, plan):
    """A model that makes the calls in `plan` (lists of (tool, args)), one turn each, then answers. It records
    every conversation it was sent."""
    seen = []

    def step(cfg, system, turns, tools, **k):
        seen.append({"turns": json.loads(json.dumps(turns, default=str)), "tools": [t["name"] for t in tools]})
        n = sum(1 for t in turns if t["role"] == "assistant")
        if n >= len(plan):
            return {"role": "assistant", "text": "done", "calls": [], "raw": None, "truncated": False}
        calls = [{"id": f"c{n}{i}", "name": name, "args": args} for i, (name, args) in enumerate(plan[n])]
        return {"role": "assistant", "text": "", "calls": calls, "raw": None, "truncated": False}

    monkeypatch.setattr(llm, "tool_step", step)
    return seen


def results(seen, turn):
    """The tool results the model got back before its turn number `turn` (0-based)."""
    return [r["content"] for r in seen[turn]["turns"][-1]["results"]]


def test_the_agent_finds_fixes_and_tests_a_bug_in_a_sandbox(repo, monkeypatch):
    seen = scripted(
        monkeypatch,
        [
            [("search_code", {"pattern": r"def add"})],
            [("read_file", {"path": "app.py"})],
            [("run_tests", {"paths": ["tests/test_app.py"]})],
            [("edit_file", {"path": "app.py", "old": "return a - b", "new": "return a + b"})],
            [("run_tests", {"paths": ["tests/test_app.py"]})],
            [("finish", {"summary": "add() subtracted; it adds now. tests/test_app.py passes."})],
        ],
    )
    events = []
    r = agent.run(repo, "add() gives the wrong answer; fix it", progress=events.append)
    assert "app.py:1: def add(a, b):" in results(seen, 1)[0]
    assert "    2| " in results(seen, 2)[0]  # numbered lines
    assert results(seen, 3)[0].startswith("FAILED") and results(seen, 5)[0].startswith("PASSED")
    assert r["ended"] == "finished" and r["summary"].startswith("add() subtracted")
    assert r["files"] == ["app.py"] and "+    return a + b" in r["patch"] and r["applies"]
    assert r["tests"]["ok"] and r["tests"]["after_last_edit"]
    assert r["verification"]["ok"] and r["verification"]["new_failures"] == []
    assert (repo / "app.py").read_text() == APP  # the repo itself is untouched
    assert [e["tool"] for e in events if e["kind"] == "tool"][:2] == ["search_code", "read_file"]


def test_a_failing_change_is_reported_with_only_the_failures_it_caused(repo, monkeypatch):
    scripted(
        monkeypatch,
        [[("edit_file", {"path": "app.py", "old": "a + b", "new": "a * b"})], [("finish", {"summary": "s"})]],
    )
    (repo / "app.py").write_text(APP.replace("a - b", "a + b"))  # works today; the change breaks it
    (repo / "tests" / "test_old.py").write_text("def test_old():\n    assert False\n")
    r = agent.run(repo, "t")
    v = r["verification"]
    assert not v["ok"] and set(v["failed"]) == {"tests/test_app.py::test_add", "tests/test_old.py::test_old"}
    assert v["new_failures"] == ["tests/test_app.py::test_add"] and v["already_failing"] == [
        "tests/test_old.py::test_old"
    ]


def test_tool_errors_go_back_to_the_model_and_change_nothing(repo, monkeypatch):
    seen = scripted(
        monkeypatch,
        [
            [("edit_file", {"path": "app.py", "old": "return a-b", "new": "x"})],
            [("edit_file", {"path": "app.py", "old": "return a - b", "new": "return (a +"})],
            [("read_file", {"path": "../outside.py"}), ("read_file", {"path": ".git/config"})],
            [("write_file", {"path": "app.py", "content": APP + APP.replace("add", "sub")})],
            [("edit_file", {"path": "app.py", "old": "return a - b", "new": "return 0"})],
            [("no_such_tool", {})],
        ],
    )
    r = agent.run(repo, "t")
    assert "was not found" in results(seen, 1)[0] and "Closest lines:\n2:     return a - b" in results(seen, 1)[0]
    assert "would not compile" in results(seen, 2)[0]
    assert "outside the repository" in results(seen, 3)[0] and ".git folder" in results(seen, 3)[1]
    assert "occurs 2 times in app.py (lines 2, 4)" in results(seen, 5)[0]
    assert "no tool called no_such_tool" in results(seen, 6)[0]
    assert r["ended"] == "answered" and r["files"] == ["app.py"] and "+def sub(a, b):" in r["patch"]


def test_scripts_run_in_the_sandbox_without_secrets(repo, monkeypatch):
    monkeypatch.setenv("SOME_API_KEY", "sekrit")
    monkeypatch.setenv("CODEWRIGHT_AI_KEY", "sekrit2")
    monkeypatch.setenv("HARMLESS", "fine")
    code = "import os, app\nprint(app.add(1, 1), os.environ.get('SOME_API_KEY'), os.environ.get('HARMLESS'))"
    seen = scripted(monkeypatch, [[("run_python", {"code": code})]])
    agent.run(repo, "t")
    assert results(seen, 1)[0] == "exit code 0\n0 None fine"


def test_web_search_and_pages_reach_the_model(repo, monkeypatch):
    monkeypatch.setattr(
        websearch, "search", lambda q, limit=6: [{"title": "httpx 0.28", "url": "https://x.org/c", "snippet": "proxy="}]
    )
    monkeypatch.setattr(
        websearch,
        "fetch",
        lambda url, offset=0, max_chars=12000: {
            "url": url,
            "title": "Changelog",
            "text": "proxies was removed",
            "offset": 0,
            "total": 40,
            "more": True,
        },
    )
    seen = scripted(
        monkeypatch, [[("web_search", {"query": "httpx proxies"})], [("fetch_url", {"url": "https://x.org/c"})]]
    )
    agent.run(repo, "t")
    assert results(seen, 1)[0] == "1. httpx 0.28\nhttps://x.org/c\nproxy="
    assert results(seen, 2)[0].startswith(
        "Changelog\nhttps://x.org/c\n\nproxies was removed\n… more (read on with offset=19)"
    )


def test_web_and_code_running_can_be_turned_off(repo, monkeypatch):
    monkeypatch.setenv("CODEWRIGHT_SEARCH_PROVIDER", "off")
    seen = scripted(monkeypatch, [])
    agent.run(repo, "t", tests=False)
    names = seen[0]["tools"]
    assert "web_search" not in names and "run_tests" not in names and "read_file" in names
    assert "Web search is not available" in seen[0]["turns"][0]["content"]


def test_the_model_failing_ends_the_run_with_what_was_done(repo, monkeypatch):
    calls = []

    def step(cfg, system, turns, tools, **k):
        calls.append(1)
        if len(calls) == 1:
            c = {"id": "1", "name": "edit_file", "args": {"path": "app.py", "old": "a - b", "new": "a + b"}}
            return {"role": "assistant", "text": "", "calls": [c], "raw": None, "truncated": False}
        raise llm.LLMError("today's AI budget of 2 model calls is used up")

    monkeypatch.setattr(llm, "tool_step", step)
    r = agent.run(repo, "t", verify=False)
    assert r["ended"] == "error" and "budget" in r["error"] and "+    return a + b" in r["patch"]
    out = agent.as_patches(r, ["x"])
    assert out["patches"][0]["function"] == "agent" and out["agent"]["ended"] == "error"


def test_a_long_conversation_is_trimmed_oldest_first():
    big = "x" * 200000
    turns = [
        {"role": "user", "content": "t"},
        {"role": "tool", "results": [{"id": "1", "name": "read_file", "content": big}]},
        {"role": "tool", "results": [{"id": "2", "name": "read_file", "content": big}]},
    ]
    agent._trim(turns)
    assert "trimmed" in turns[1]["results"][0]["content"] and turns[2]["results"][0]["content"] == big


# ---------------------------------------------------------------- tool calling, per provider


TOOLS = [
    {
        "name": "read_file",
        "description": "d",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    }
]


@pytest.fixture
def server(monkeypatch):
    sent, answers = [], []

    def post(url, body, headers, who, timeout):
        sent.append(json.loads(json.dumps(body)))
        return answers.pop(0)

    monkeypatch.setattr(llm, "_post", post)
    llm._CAPS.clear()
    return sent, answers


def _round_trip(cfg, answer, server):
    sent, answers = server
    answers += [answer, answer]
    turns = [{"role": "user", "content": "fix it"}]
    turn = llm.tool_step(cfg, "sys", turns, TOOLS)
    assert turn["calls"][0]["name"] == "read_file" and turn["calls"][0]["args"] == {"path": "a.py"}
    turns += [
        turn,
        {"role": "tool", "results": [{"id": turn["calls"][0]["id"], "name": "read_file", "content": "1| x"}]},
    ]
    llm.tool_step(cfg, "sys", turns, TOOLS)
    return sent[1]


def test_claude_tool_calls_round_trip(server):
    answer = {
        "content": [
            {"type": "text", "text": "Reading."},
            {"type": "tool_use", "id": "tu1", "name": "read_file", "input": {"path": "a.py"}},
        ],
        "stop_reason": "tool_use",
    }
    body = _round_trip(llm.LLMConfig(model="m", provider="anthropic", api_key="k"), answer, server)
    assert body["tools"][0]["input_schema"]["properties"] == {"path": {"type": "string"}}
    assert body["messages"][1] == {"role": "assistant", "content": answer["content"]}  # sent back as it came
    assert body["messages"][2]["content"][0] == {"type": "tool_result", "tool_use_id": "tu1", "content": "1| x"}


def test_openai_tool_calls_round_trip(server):
    msg = {
        "content": None,
        "tool_calls": [
            {"id": "call_9", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}
        ],
    }
    body = _round_trip(
        llm.LLMConfig(model="m", provider="openai", api_key="k"),
        {"choices": [{"message": msg, "finish_reason": "tool_calls"}]},
        server,
    )
    assert body["tools"][0] == {"type": "function", "function": TOOLS[0]}
    assert body["messages"][2]["tool_calls"][0]["function"] == {"name": "read_file", "arguments": '{"path": "a.py"}'}
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "call_9", "content": "1| x"}


def test_gemini_tool_calls_round_trip(server):
    parts = [{"functionCall": {"name": "read_file", "args": {"path": "a.py"}}, "thoughtSignature": "sig"}]
    answer = {"candidates": [{"content": {"parts": parts}, "finishReason": "STOP"}]}
    body = _round_trip(llm.LLMConfig(model="m", provider="gemini", api_key="k"), answer, server)
    assert body["tools"][0]["functionDeclarations"][0]["name"] == "read_file"
    assert body["contents"][1] == {"role": "model", "parts": parts}  # the signature goes back too
    assert body["contents"][2]["parts"][0]["functionResponse"] == {"name": "read_file", "response": {"content": "1| x"}}


# ---------------------------------------------------------------- the web


def test_pages_on_internal_addresses_are_refused():
    for url in (
        "http://127.0.0.1:8000/",
        "http://localhost/x",
        "http://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
    ):
        with pytest.raises(websearch.SearchError):
            websearch.fetch(url)


def test_duckduckgo_results_and_html_pages_are_read_as_text():
    html = (
        '<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.python-httpx.org%2F'
        '&amp;rut=1">HTTPX <b>docs</b></a><a class="result__snippet" href="x">A <b>next</b>-gen client</a>'
    )
    assert websearch.parse_duckduckgo(html) == [
        {"title": "HTTPX <b>docs</b>", "url": "https://www.python-httpx.org/", "snippet": "A <b>next</b>-gen client"}
    ]
    page = (
        "<html><head><style>p{}</style></head><body><nav>menu</nav><h2>Changes</h2>"
        "<p>Removed  <code>proxies</code>.</p><pre>a\n  b</pre></body></html>"
    )
    assert websearch.html_to_text(page) == "## Changes\n\nRemoved proxies.\n\na\n  b"


def test_search_settings_are_saved_without_showing_the_key(monkeypatch):
    for k in ("CODEWRIGHT_SEARCH_PROVIDER", "BRAVE_API_KEY", "TAVILY_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert websearch.public()["provider"] == "duckduckgo" and websearch.status()["available"]
    out = websearch.save({"provider": "brave", "api_key": "abcdefghijklmnop"})
    assert out["provider"] == "brave" and out["has_key"] and "abcdefghijklmnop" not in json.dumps(out)
    assert websearch.save({"provider": "searxng", "url": ""})["reason"] == "no SearXNG address"


# ---------------------------------------------------------------- from the app


def test_the_agent_runs_in_the_background_and_its_change_is_applied_on_request(repo, monkeypatch):
    from fastapi.testclient import TestClient

    from codewright.server import create_app

    scripted(
        monkeypatch,
        [[("edit_file", {"path": "app.py", "old": "a - b", "new": "a + b"})], [("finish", {"summary": "fixed"})]],
    )
    c = TestClient(create_app([str(repo)]), base_url="http://localhost")
    wid = c.get("/api/workspaces").json()[0]["id"]
    assert c.post(f"/api/w/{wid}/agent", json={"task": " "}).status_code == 400
    c.post(f"/api/w/{wid}/agent", json={"task": "fix add", "web": False})
    for _ in range(400):
        p = c.get(f"/api/w/{wid}/agent").json()
        if not p["running"]:
            break
        time.sleep(0.05)
    assert p["result"]["summary"] == "fixed" and p["result"]["verification"]["ok"], p
    assert [e["tool"] for e in p["events"] if e["kind"] == "tool"] == ["edit_file"]
    assert c.get(f"/api/w/{wid}/agent?since=1").json()["events"] == []
    assert (repo / "app.py").read_text() == APP
    assert c.post(f"/api/w/{wid}/agent/apply").json()["applied"]
    assert (repo / "app.py").read_text() == APP.replace("a - b", "a + b")
    assert c.post(f"/api/w/{wid}/agent/apply").status_code == 409  # not twice
