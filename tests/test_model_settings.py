"""The model setup (provider, key, model) and the review progress the web UI shows."""

import json
import stat
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from codewright import llm
from codewright.llm import LLMConfig
from codewright.service import Workspace, stage_progress

AI_ENV = (
    *llm.ENV.values(),
    *(v for spec in llm.PROVIDERS.values() for v in spec.key_env),
    *(v for names in llm._LITELLM_ENV.values() for v in names),
    "CODEWRIGHT_MODEL",
    "CODEWRIGHT_OFFLINE",
)


@pytest.fixture(autouse=True)
def no_ai_env(monkeypatch):
    for name in AI_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fake_api():
    """A local server that answers in each provider's wire format and records what it was sent."""
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
            if self.headers.get("Authorization") == "Bearer wrong":
                out, code = {"error": {"message": "Incorrect API key provided"}}, 401
            elif self.path.endswith("/chat/completions"):
                out, code = {"choices": [{"message": {"content": "ok from openai"}}]}, 200
            elif self.path.endswith("/v1/messages"):
                out, code = {"content": [{"type": "text", "text": "ok from claude"}], "stop_reason": "end_turn"}, 200
            else:
                out, code = {"candidates": [{"content": {"parts": [{"text": "ok from gemini"}]}}]}, 200
            data = json.dumps(out).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", seen
    server.shutdown()


def test_nothing_configured_means_no_model():
    cfg = LLMConfig.load()
    st = llm.status(cfg)
    assert not st["available"] and "Settings" in st["reason"]
    assert llm.complete(cfg, "s", [{"role": "user", "content": "hi"}]) is None
    assert llm.public()["configured"] is False


def test_saved_key_is_private_and_never_returned():
    llm.save_settings({"provider": "anthropic", "api_key": "sk-ant-secret-1234", "model": "claude-haiku-5-5"})
    assert stat.S_IMODE(llm.settings_path().stat().st_mode) == 0o600
    pub = llm.public()
    assert pub["configured"] and pub["has_key"] and pub["key_hint"] == "…1234"
    assert "sk-ant-secret" not in json.dumps(pub)
    llm.save_settings({"model": "claude-sonnet-5-5", "api_key": ""})  # an empty key keeps the stored one
    assert LLMConfig.load().api_key == "sk-ant-secret-1234"
    llm.save_settings({"api_key": None})
    assert not llm.public()["has_key"]


def test_environment_wins_and_is_reported(monkeypatch):
    llm.save_settings({"provider": "anthropic", "api_key": "sk-ant-saved-000000"})
    monkeypatch.setenv("CODEWRIGHT_AI_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    cfg = LLMConfig.load()
    assert (cfg.provider, cfg.model) == ("openai", "gpt-4o-mini")
    assert "provider" in llm.public()["from_environment"]


def test_key_variable_alone_turns_its_provider_on(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    cfg = LLMConfig.load()
    assert (cfg.provider, cfg.api_key, cfg.model) == ("gemini", "g-key", "gemini-2.5-flash")


@pytest.mark.parametrize(
    ("model_id", "provider", "model", "base"),
    [
        ("anthropic/claude-haiku-5-5", "anthropic", "claude-haiku-5-5", ""),
        ("ollama/llama3", "openai", "llama3", llm.OLLAMA_URL),
        ("vertex_ai/gemini-2.5-flash", "litellm", "vertex_ai/gemini-2.5-flash", ""),
    ],
)
def test_older_model_ids_still_work(monkeypatch, tmp_path, model_id, provider, model, base):
    monkeypatch.setenv("CODEWRIGHT_MODEL", model_id)
    cfg = LLMConfig.load()
    assert (cfg.provider, cfg.model, cfg.base_url) == (provider, model, base)
    monkeypatch.delenv("CODEWRIGHT_MODEL")
    (tmp_path / ".codewright").mkdir()
    (tmp_path / ".codewright" / "config.json").write_text(json.dumps({"codewright_model": model_id}))
    assert LLMConfig.load(tmp_path).provider == provider
    assert llm.public(tmp_path)["from_repo"]


def test_offline_switch_keeps_the_rest_of_the_repo_config(tmp_path):
    d = tmp_path / ".codewright"
    d.mkdir()
    (d / "config.json").write_text(json.dumps({"index_url": "https://example.test/simple"}))
    llm.save_settings({"provider": "openai", "base_url": "http://localhost:11434/v1"})
    cfg = LLMConfig.load(tmp_path)
    cfg.offline = True
    cfg.save(tmp_path)
    assert json.loads((d / "config.json").read_text())["index_url"] == "https://example.test/simple"
    assert llm.status(LLMConfig.load(tmp_path))["reason"] == "offline mode is on"


def test_each_wire_format(fake_api, monkeypatch):
    base, seen = fake_api
    msgs = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]

    cfg = LLMConfig("local-model", provider="openai", base_url=base)
    assert llm.generate(cfg, "sys", msgs) == "ok from openai"
    assert seen[-1]["path"] == "/chat/completions" and "authorization" not in seen[-1]["headers"]
    assert seen[-1]["body"]["messages"][0] == {"role": "system", "content": "sys"}

    for pid, spec in llm.PROVIDERS.items():
        if spec.base_url:
            monkeypatch.setitem(llm.PROVIDERS, pid, llm.ProviderSpec(spec.label, spec.model, spec.needs_key, (), base))
    assert (
        llm.generate(LLMConfig("claude-haiku-5-5", provider="anthropic", api_key="k"), "sys", msgs) == "ok from claude"
    )
    assert seen[-1]["path"] == "/v1/messages" and seen[-1]["headers"]["x-api-key"] == "k"
    assert seen[-1]["body"]["system"] == "sys"
    assert llm.generate(LLMConfig("gemini-2.5-flash", provider="gemini", api_key="k"), "sys", msgs) == "ok from gemini"
    assert seen[-1]["path"] == "/models/gemini-2.5-flash:generateContent"
    assert [c["role"] for c in seen[-1]["body"]["contents"]] == ["user", "model", "user"]


def test_a_wrong_key_is_explained_and_chat_falls_back(fake_api):
    base, _ = fake_api
    cfg = LLMConfig("gpt-4o-mini", provider="openai", api_key="wrong", base_url=base)
    result = llm.test(cfg)
    assert not result["ok"] and "401" in result["error"] and "Incorrect API key" in result["error"]
    assert llm.complete(cfg, "s", [{"role": "user", "content": "hi"}]) is None
    unreachable = llm.test(LLMConfig("m", provider="openai", base_url="http://127.0.0.1:9"))
    assert not unreachable["ok"] and "cannot reach" in unreachable["error"]


def test_settings_api(fake_api, tmp_path):
    from fastapi.testclient import TestClient

    from codewright.server import create_app

    base, _ = fake_api
    (tmp_path / "app.py").write_text("def f():\n    return 1\n")
    client = TestClient(create_app([str(tmp_path)]), base_url="http://localhost")
    wid = client.get("/api/workspaces").json()[0]["id"]

    assert client.post("/api/settings/ai/test", json={}).json()["ok"] is False
    assert client.put("/api/settings/ai", json={"provider": "nope"}).status_code == 400
    assert client.put("/api/settings/ai", json={"provider": "openai", "base_url": "ftp://x"}).status_code == 400

    saved = client.put("/api/settings/ai", json={"provider": "openai", "model": "llama3", "base_url": base}).json()
    assert saved["configured"] and saved["provider"] == "openai" and "api_key" not in saved
    tested = client.post("/api/settings/ai/test", json={"workspace": wid}).json()
    assert tested["ok"] and tested["reply"] == "ok from openai"

    s = client.get(f"/api/w/{wid}/settings").json()
    assert s["llm"]["available"] and s["ai"]["model"] == "llama3"
    client.post(f"/api/w/{wid}/settings", json={"offline": True})
    assert "offline" in client.post("/api/settings/ai/test", json={"workspace": wid}).json()["error"]

    off = client.put("/api/settings/ai", json={"provider": "", "model": "", "base_url": "", "clear_key": True}).json()
    assert off["provider"] == "" and not off["configured"]


# --------------------------------------------------------------------------- review progress


def test_stage_progress_moves_forward():
    seq = [("stack", 0, 0), ("graph", 0, 40), ("graph", 20, 40), ("detect", 0, 0), ("check", 0, 0)]
    seq += [("baseline", 0, 0), ("proof", 0, 4), ("proof", 2, 4), ("proof", 4, 4), ("rank", 0, 0)]
    pcts = [stage_progress(*s, proof=True, started=0)["percent"] for s in seq]
    assert pcts == sorted(pcts) and pcts[0] == 0 and pcts[-1] < 100
    quick = stage_progress("rank", 0, 0, proof=False, started=0)
    assert [s["key"] for s in quick["stages"]] == ["stack", "graph", "detect", "check", "rank"]
    assert quick["step"] == quick["steps"] == 5


def test_review_reports_every_stage(tmp_path):
    (tmp_path / "app.py").write_text("import json\n\ndef load(p):\n    return json.loads(open(p).read())\n")
    ws = Workspace(tmp_path)
    seen: list[str] = []
    real = Workspace.__setattr__

    def spy(self, name, value):
        if name == "progress" and value:
            seen.append(value["stage"])
        real(self, name, value)

    Workspace.__setattr__ = spy
    try:
        ws.run_review(proof=False)
    finally:
        Workspace.__setattr__ = real
    assert list(dict.fromkeys(seen)) == ["stack", "graph", "detect", "check", "rank"]
    assert ws.progress is None and not ws.running
