import textwrap
from pathlib import Path

import pytest

from codewright import cards, graph, stack
from codewright.intents import detect
from codewright.suggest import make_findings


def write_repo(tmp_path: Path, files: dict[str, str], adk: str = "2.14.1", locked: tuple[str, ...] = ()) -> Path:
    lock = f'version = 1\n\n[[package]]\nname = "google-adk"\nversion = "{adk}"\n'
    lock += "".join(f'\n[[package]]\nname = "{name}"\nversion = "1.0.0"\n' for name in locked)
    for name, body in {"uv.lock": lock, **files}.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body).lstrip())
    return tmp_path


def findings(tmp_path: Path) -> list:
    g = graph.build(tmp_path)
    return make_findings(detect(g), stack.discover(tmp_path), g, cards.load())


def card_ids(tmp_path: Path) -> set[str]:
    return {f.card_id for f in findings(tmp_path)}


# ---------------------------------------------------------------- output, state, tools


def test_parsing_the_reply_as_json_suggests_output_schema(tmp_path):
    write_repo(
        tmp_path,
        {
            "app.py": """
            import json
            from google.adk.runners import Runner

            async def triage(runner, msg):
                async for event in runner.run_async(user_id="u", session_id="s", new_message=msg):
                    if event.is_final_response():
                        return json.loads(event.content.parts[0].text)

            def unrelated(path):
                return json.loads(open(path).read())
            """
        },
    )
    fs = [f for f in findings(tmp_path) if f.card_id == "adk.output-schema"]
    assert [f.function for f in fs] == ["triage"]


def test_direct_state_write_on_a_session_but_not_on_a_context(tmp_path):
    write_repo(
        tmp_path,
        {
            "app.py": """
            from google.adk.tools import ToolContext

            async def set_plan(session_service, uid, sid):
                session = await session_service.get_session(app_name="a", user_id=uid, session_id=sid)
                session.state["user:plan"] = "pro"

            def tool(plan: str, tool_context: ToolContext) -> dict:
                tool_context.state["user:plan"] = plan
                return {}
            """
        },
    )
    fs = [f for f in findings(tmp_path) if f.card_id == "adk.state-delta"]
    assert [f.function for f in fs] == ["set_plan"]


def test_tool_writing_a_module_dict_suggests_scoped_state(tmp_path):
    write_repo(
        tmp_path,
        {
            "agent.py": """
            from google.adk.agents import LlmAgent

            PREFERENCES = {}
            CURRENCIES = {"usd": "$"}

            def remember(user: str, language: str) -> dict:
                PREFERENCES[user] = language
                return {"status": "ok"}

            def symbol(code: str) -> dict:
                return {"symbol": CURRENCIES[code]}

            root_agent = LlmAgent(name="a", tools=[remember, symbol])
            """
        },
    )
    fs = [f for f in findings(tmp_path) if f.card_id == "adk.state-scopes"]
    assert [f.function for f in fs] == ["remember"] and "PREFERENCES" in fs[0].evidence


def test_tool_that_runs_an_agent_suggests_agent_tool(tmp_path):
    write_repo(
        tmp_path,
        {
            "agent.py": """
            from google.adk.agents import LlmAgent
            from google.adk.runners import InMemoryRunner

            summarizer = LlmAgent(name="summarizer")

            async def summarize(text: str) -> dict:
                runner = InMemoryRunner(agent=summarizer)
                return {"summary": "..."}

            root_agent = LlmAgent(name="a", tools=[summarize])
            """
        },
    )
    assert "adk.agent-tool" in card_ids(tmp_path)


@pytest.mark.parametrize("version,card", [("1.20.0", "adk.mcp-toolset"), ("1.10.0", "adk.mcp-toolset-1x")])
def test_hand_written_mcp_client_suggests_the_toolset_for_the_version(tmp_path, version, card):
    write_repo(
        tmp_path,
        {
            "tools.py": """
            from google.adk.agents import LlmAgent
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            async def read_file(path: str) -> dict:
                async with stdio_client(StdioServerParameters(command="server")) as (r, w):
                    async with ClientSession(r, w) as session:
                        return await session.call_tool("read_file", {"path": path})
            """
        },
        adk=version,
        locked=("mcp",),
    )
    assert card in card_ids(tmp_path)


@pytest.mark.parametrize("version,card", [("1.20.0", "adk.events-compaction"), ("1.15.1", "adk.context-filter")])
def test_slicing_history_suggests_compaction_or_a_filter(tmp_path, version, card):
    write_repo(
        tmp_path,
        {
            "app.py": """
            import google.adk

            def recent(session):
                return session.events[-20:]
            """
        },
        adk=version,
    )
    assert card in card_ids(tmp_path)


def test_runner_built_in_a_route(tmp_path):
    write_repo(
        tmp_path,
        {
            "api.py": """
            from fastapi import FastAPI
            from google.adk.runners import InMemoryRunner
            from agent import root_agent

            app = FastAPI()
            runner = InMemoryRunner(agent=root_agent)  # once: fine

            @app.post("/chat")
            async def chat(body: dict):
                r = InMemoryRunner(agent=root_agent)
                return {}
            """
        },
    )
    fs = [f for f in findings(tmp_path) if f.card_id == "adk.runner-once"]
    assert [f.function for f in fs] == ["chat"]


def test_polling_tool_suggests_a_long_running_tool(tmp_path):
    write_repo(
        tmp_path,
        {
            "agent.py": """
            import time
            from google.adk.agents import LlmAgent

            def render_video(prompt: str) -> dict:
                job = start(prompt)
                while not job.done():
                    time.sleep(5)
                return {"url": job.url}

            root_agent = LlmAgent(name="a", tools=[render_video])
            """
        },
    )
    assert "adk.long-running-tool" in card_ids(tmp_path)


def test_in_memory_artifact_and_memory_services_get_their_own_cards(tmp_path):
    write_repo(
        tmp_path,
        {
            "server.py": """
            from fastapi import FastAPI
            from google.adk.artifacts import InMemoryArtifactService
            from google.adk.memory import InMemoryMemoryService
            from google.adk.sessions import InMemorySessionService

            app = FastAPI()
            sessions, artifacts, memory = InMemorySessionService(), InMemoryArtifactService(), InMemoryMemoryService()
            """
        },
    )
    assert {"adk.persistent-sessions", "adk.gcs-artifacts", "adk.memory-bank"} <= card_ids(tmp_path)


# ---------------------------------------------------------------- deprecations


@pytest.mark.parametrize(
    "src,version,card",
    [
        ('LlmAgent(name="a", global_instruction="Be kind.")', "1.16.0", "adk.global-instruction-plugin"),
        ('LlmAgent(name="a", global_instruction="Be kind.")', "1.15.1", None),  # not deprecated yet
        ("Runner(agent=a, app_name='x', session_service=s, plugins=[p])", "1.14.1", "adk.app-plugins"),
        ("VertexAiCodeExecutor()", "2.12.0", "adk.sandbox-code-executor"),
        ("VertexAiCodeExecutor()", "2.11.0", None),
        ("RunConfig(save_live_audio=True)", "1.19.0", "adk.save-live-blob"),
    ],
)
def test_deprecated_apis_are_flagged_only_once_deprecated(tmp_path, src, version, card):
    write_repo(
        tmp_path,
        {
            "app.py": "from google.adk.agents import LlmAgent, RunConfig\n"
            "from google.adk.runners import Runner\n"
            "from google.adk.code_executors import VertexAiCodeExecutor\n\n"
            f"thing = {src}\n"
        },
        adk=version,
    )
    deprecated = {f.card_id for f in findings(tmp_path) if f.intent == "adk-deprecated-api"}
    assert deprecated == ({card} if card else set())


# ---------------------------------------------------------------- 2.x extras


MCP_IMPORT = "from google.adk.tools.mcp_tool import McpToolset\n"


def test_missing_extra_flagged_from_the_lockfile(tmp_path):
    write_repo(tmp_path, {"agent.py": MCP_IMPORT})
    f = next(f for f in findings(tmp_path) if f.intent == "adk-missing-extra")
    assert f.card_id == "adk.extra-mcp" and "google-adk[mcp]" in f.evidence


@pytest.mark.parametrize(
    "case",
    ["mcp locked", "adk 1.x", "no lockfile", "extra requested"],
)
def test_missing_extra_stays_quiet_without_evidence(tmp_path, case):
    if case == "mcp locked":
        write_repo(tmp_path, {"agent.py": MCP_IMPORT}, locked=("mcp",))
    elif case == "adk 1.x":
        write_repo(tmp_path, {"agent.py": MCP_IMPORT}, adk="1.39.1")
    elif case == "no lockfile":
        (tmp_path / "agent.py").write_text(MCP_IMPORT)
        (tmp_path / "requirements.txt").write_text("google-adk==2.14.1\n")  # direct deps only: not evidence
    else:
        write_repo(tmp_path, {"agent.py": MCP_IMPORT})
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\ndependencies = ["google-adk[mcp]>=2.0"]\n')
    assert not [f for f in findings(tmp_path) if f.intent == "adk-missing-extra"]


def test_reference_cards_never_become_findings(tmp_path):
    write_repo(tmp_path, {"agent.py": "from google.adk import Workflow\nfrom google.adk.agents import LlmAgent\n"})
    assert not [f for f in findings(tmp_path) if f.intent == cards.REFERENCE]
    refs = [c for c in cards.load() if c.intent == cards.REFERENCE]
    assert refs and all(c.topic for c in refs)


# ---------------------------------------------------------------- shapes seen in real ADK apps that must stay quiet


def test_quiet_on_correct_patterns_from_real_adk_apps(tmp_path):
    write_repo(
        tmp_path,
        {
            # structured output through the genai client, then parsing its text: already right
            "outline.py": """
            from google import genai
            from google.genai import types
            import google.adk

            def outline(client, prompt, Schema):
                config = types.GenerateContentConfig(response_mime_type="application/json", response_schema=Schema)
                response = client.models.generate_content(model="m", contents=prompt, config=config)
                return Schema.model_validate_json(response.text)
            """,
            # a plugin flagging the live session of the current run
            "plugin.py": """
            from google.adk.plugins import BasePlugin

            class Judge(BasePlugin):
                async def before_run_callback(self, *, invocation_context):
                    invocation_context.session.state["judged"] = True
            """,
            # a retry loop inside a custom model class around another provider's SDK
            "claude.py": """
            import asyncio
            from google.adk.models.anthropic_llm import Claude

            class RetryingClaude(Claude):
                async def generate_content_async(self, llm_request, stream=False):
                    for wait in (1, 2, None):
                        try:
                            async for r in super().generate_content_async(llm_request, stream):
                                yield r
                            return
                        except Exception:
                            await asyncio.sleep(wait)
            """,
        },
    )
    quiet = {"adk.output-schema", "adk.state-delta", "adk.gemini-retry-options"}
    assert not card_ids(tmp_path) & quiet
