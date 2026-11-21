import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from codewright import cards, critic, graph, stack
from codewright.intents import detect
from codewright.models import Finding
from codewright.suggest import make_findings


def write_repo(tmp_path: Path, files: dict[str, str], adk: str = "2.14.1") -> Path:
    files = {"uv.lock": f'version = 1\n\n[[package]]\nname = "google-adk"\nversion = "{adk}"\n', **files}
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body).lstrip())
    return tmp_path


def findings(tmp_path: Path) -> dict:
    g = graph.build(tmp_path)
    return {f.intent: f for f in make_findings(detect(g), stack.discover(tmp_path), g, cards.load())}


def patched(tmp_path: Path, f) -> str:
    subprocess.run(["git", "apply", "-"], cwd=tmp_path, input=f.patch, text=True, check=True)
    src = (tmp_path / f.file).read_text()
    compile(src, f.file, "exec")
    return src


def test_every_adk_card_cites_sources_and_a_when_not():
    adk = [c for c in cards.load() if c.library == "google-adk"]
    assert {c.id for c in adk} >= {
        "adk.workflow",
        "adk.upgrade-to-2",
        "adk.run-async",
        "adk.persistent-sessions",
        "adk.instruction-provider",
        "adk.fast-api-app",
        "adk.gemini-retry-options",
    }
    assert len({c.id for c in adk}) == len(adk), "duplicate card ids"
    for c in adk:
        assert c.sources and c.when_not and c.topic, c.id
        # Extras cards name the extra: the symbol they'd check is exactly what fails to import.
        assert critic.is_dotted_symbol(c.symbol) or (c.extra and c.symbol == f"google-adk[{c.extra}]"), c.id
        if c.snippet:
            compile(c.snippet, c.id, "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)  # snippets may be fragments
    # Knowledge covers 2.x only: every card applies from 2.0 on, except the upgrade card for 1.x repos.
    for c in adk:
        if c.id != "adk.upgrade-to-2":
            assert cards.version_matches("2.0.0", c.versions) or c.versions.startswith(">=2."), c.id
        assert not cards.version_matches("1.39.1", c.versions) or c.id == "adk.upgrade-to-2", c.id
    topics = {c.topic for c in adk}
    assert len(topics) >= 8 and len([c for c in adk if c.intent == "reference"]) >= 35


def test_every_adk_intent_has_a_detector():
    from codewright.intents.detectors import DETECTORS, GRAPH_DETECTORS, MODULE_DETECTORS

    intents = {c.intent for c in cards.load() if c.library == "google-adk"} - {cards.REFERENCE}
    assert intents <= set(DETECTORS) | set(MODULE_DETECTORS) | set(GRAPH_DETECTORS)


# ---------------------------------------------------------------- Runner.run in async code


def test_sync_run_in_async_is_patched_to_run_async(tmp_path):
    write_repo(
        tmp_path,
        {
            "app.py": """
            from google.adk.runners import Runner

            async def reply(runner: Runner, uid, sid, msg):
                out = []
                for event in runner.run(user_id=uid, session_id=sid, new_message=msg):
                    out.append(event)
                return out

            def script(runner, uid, sid, msg):
                return list(runner.run(user_id=uid, session_id=sid, new_message=msg))
            """
        },
    )
    f = findings(tmp_path)["adk-sync-run"]
    assert f.card_id == "adk.run-async" and f.function == "reply"
    assert "async for event in runner.run_async(user_id=uid" in patched(tmp_path, f)
    assert "list(runner.run(" in (tmp_path / "app.py").read_text()  # the sync function is left alone


def test_sync_run_outside_a_for_loop_is_flagged_without_a_patch(tmp_path):
    write_repo(
        tmp_path,
        {
            "app.py": """
            from google.adk.runners import InMemoryRunner

            async def reply(self, msg):
                return list(self.runner.run(user_id="u", session_id="s", new_message=msg))
            """
        },
    )
    f = findings(tmp_path)["adk-sync-run"]
    assert f.patch is None


# ---------------------------------------------------------------- sessions, servers, retries


def test_in_memory_sessions_flagged_only_in_a_server(tmp_path):
    server = """
    from fastapi import FastAPI
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    app = FastAPI()
    runner = Runner(agent=None, app_name="a", session_service=InMemorySessionService())
    """
    write_repo(
        tmp_path,
        {"server.py": server, "demo.py": "from google.adk.runners import InMemoryRunner\nr = InMemoryRunner()\n"},
    )
    f = findings(tmp_path)["adk-in-memory-services"]
    assert f.file == "server.py" and f.card_id == "adk.persistent-sessions"
    assert "sqlite+aiosqlite" in f.risk + f.suggestion


@pytest.mark.parametrize("version,expected", [("2.14.1", "adk.fast-api-app"), ("1.39.1", None)])
def test_hand_written_route_suggests_get_fast_api_app(tmp_path, version, expected):
    write_repo(
        tmp_path,
        {
            "api.py": """
            from fastapi import FastAPI
            from google.adk.runners import Runner

            app = FastAPI()

            @app.post("/chat")
            async def chat(body: dict):
                events = []
                async for e in app.state.runner.run_async(user_id="u", session_id="s", new_message=body):
                    events.append(e)
                return events
            """
        },
        adk=version,
    )
    f = findings(tmp_path).get("adk-hand-server")
    assert (f.card_id if f else None) == expected


@pytest.mark.parametrize("version,expected", [("2.14.1", "adk.gemini-retry-options"), ("1.39.1", "tenacity.retry")])
def test_retrying_an_agent_run_suggests_model_retry_options(tmp_path, version, expected):
    write_repo(
        tmp_path,
        {
            "app.py": """
            import asyncio
            from google.adk.runners import Runner

            async def ask(runner, msg):
                for attempt in range(5):
                    try:
                        return [e async for e in runner.run_async(user_id="u", session_id="s", new_message=msg)]
                    except Exception:
                        await asyncio.sleep(2 ** attempt)
            """
        },
        adk=version,
    )
    assert findings(tmp_path)["retry"].card_id == expected


def test_plain_retry_loop_in_an_adk_repo_keeps_the_generic_card(tmp_path):
    write_repo(
        tmp_path,
        {
            "app.py": """
            import time
            import google.adk

            def fetch(client):
                for attempt in range(3):
                    try:
                        return client.download()
                    except OSError:
                        time.sleep(1)
            """
        },
    )
    assert findings(tmp_path)["retry"].card_id == "tenacity.retry"


# ---------------------------------------------------------------- instructions frozen at import


def test_frozen_instruction_becomes_a_provider(tmp_path):
    write_repo(
        tmp_path,
        {
            "agent.py": """
            from datetime import date

            from google.adk.agents import LlmAgent

            root_agent = LlmAgent(
                name="assistant",
                instruction=f"Today is {date.today()}. Be brief.",
            )
            """
        },
    )
    f = findings(tmp_path)["adk-frozen-instruction"]
    assert f.card_id == "adk.instruction-provider" and "date.today()" in f.evidence
    assert 'instruction=lambda ctx: f"Today is {date.today()}' in patched(tmp_path, f)


@pytest.mark.parametrize(
    "src,flagged,patchable",
    [
        ('NAME = "Ada"\nroot_agent = Agent(name="a", instruction=f"You are {NAME}.")\n', False, False),
        ('root_agent = Agent(name="a", instruction=f"Hi {{user}}, it is {now()}.")\n', True, False),
        ('def build():\n    return Agent(name="a", instruction=f"It is {now()}.")\n', False, False),
    ],
)
def test_frozen_instruction_edge_cases(tmp_path, src, flagged, patchable):
    write_repo(tmp_path, {"agent.py": "from google.adk import Agent\n" + src})
    f = findings(tmp_path).get("adk-frozen-instruction")
    assert bool(f) is flagged
    assert bool(f and f.patch) is patchable


# ---------------------------------------------------------------- critic


def test_symbol_needing_a_missing_extra_is_unchecked_not_a_crash(tmp_path, monkeypatch):
    """`google.adk.sessions.DatabaseSessionService` raises ImportError without SQLAlchemy installed."""
    (tmp_path / "lazy.py").write_text(
        "def __getattr__(name):  # like google.adk.sessions: only the name that needs the extra raises\n"
        "    if name == 'Service':\n"
        "        raise ImportError('pip install lazy[db]')\n"
        "    raise AttributeError(name)\n\n"
        "REAL = 1\n"
    )
    monkeypatch.chdir(tmp_path)

    def finding(fid, symbol):
        return Finding(fid, "i", "f.py", 1, "fn", "t", "", "", symbol, "", "", 1, 1, "", "", "", [], "")

    lazy, real = finding("a", "lazy.Service"), finding("b", "lazy.REAL")
    critic.check_symbols([lazy, real], sys.executable, {f.id: f.symbol for f in (lazy, real)})
    assert (lazy.symbol_check, real.symbol_check) == ("unchecked", "ok")


def test_reading_one_runs_events_is_not_orchestration(tmp_path):
    write_repo(
        tmp_path,
        {
            "app.py": """
            from google.adk.runners import Runner

            async def reply(runner, msg):
                async for event in runner.run_async(user_id="u", session_id="s", new_message=msg):
                    print(event)

            async def pipeline(runners, msg):
                for r in runners:
                    async for event in r.run_async(user_id="u", session_id="s", new_message=msg):
                        msg = event
            """
        },
    )
    g = graph.build(tmp_path)
    orchestrators = [m.function.qualname for m in detect(g, only={"agent-orchestrator"})]
    assert orchestrators == ["pipeline"]


def test_symbol_in_a_module_whose_own_imports_fail_is_unchecked(tmp_path, monkeypatch):
    """`google.adk.models.lite_llm.LiteLlm` without litellm installed exists; it is not a hallucination."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "needs_extra.py").write_text("import not_installed_dependency_xyz\nclass Thing: ...\n")
    (pkg / "custom_error.py").write_text("raise ImportError('pip install pkg[extra]')\n")
    monkeypatch.chdir(tmp_path)

    def finding(fid, symbol):
        return Finding(fid, "i", "f.py", 1, "fn", "t", "", "", symbol, "", "", 1, 1, "", "", "", [], "")

    fs = [
        finding("a", "pkg.needs_extra.Thing"),
        finding("b", "pkg.custom_error.Thing"),
        finding("c", "pkg.no_such_module.Thing"),
        finding("d", "functools.lru_cache"),
    ]
    critic.check_symbols(fs, sys.executable, {f.id: f.symbol for f in fs})
    assert [f.symbol_check for f in fs] == ["unchecked", "unchecked", "missing", "ok"]


def test_symbol_of_a_library_missing_from_the_checking_env_is_unchecked(tmp_path, monkeypatch):
    """Another package in the `google` namespace is installed, the card's library is not: that's no evidence.
    (A made-up distribution, so the test holds whether or not google-adk is installed here.)"""
    ns = tmp_path / "google" / "other"
    ns.mkdir(parents=True)
    (ns / "__init__.py").write_text("")
    monkeypatch.chdir(tmp_path)

    def finding(fid, symbol):
        return Finding(fid, "i", "f.py", 1, "fn", "t", "", "", symbol, "", "", 1, 1, "", "", "", [], "")

    adk = finding("a", "google.notinstalled.Workflow")
    ghost = finding("b", "google.other.Ghost")
    critic.check_symbols(
        [adk, ghost], sys.executable, {"a": adk.symbol, "b": ghost.symbol}, {adk.symbol: "google-notinstalled"}
    )
    assert (adk.symbol_check, ghost.symbol_check) == ("unchecked", "missing")
