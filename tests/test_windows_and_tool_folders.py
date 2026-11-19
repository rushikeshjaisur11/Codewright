"""Regressions from a real repo: MCP toolsets without a literal server, tool folders walked into, path separators."""

import textwrap
import time

from codewright import aimap, archmap, graph, scanscope


def test_mcp_toolset_without_a_literal_server_is_a_tool_not_a_crash(tmp_path):
    (tmp_path / "agent.py").write_text(
        textwrap.dedent("""
        from google.adk.agents import LlmAgent
        from google.adk.tools.mcp_tool import MCPToolset, StdioConnectionParams

        params = StdioConnectionParams(server_params=SERVER)
        execute_sql_agent = LlmAgent(
            name="execute_sql_agent",
            model="gemini-2.5-flash",
            tools=[
                MCPToolset(connection_params=params),
                MCPToolset(connection_params=StdioConnectionParams(server_params=build())),
            ],
        )
    """)
    )
    d = aimap.build(graph.build(tmp_path))
    assert d["errors"] == []
    (agent,) = d["agents"]
    assert [t["label"] for t in agent["tools"]] == ["MCPToolset", "MCPToolset"]


def test_walk_skips_tool_folders_and_any_virtualenv(tmp_path):
    for rel in [
        "app/main.py",
        ".venv/lib/site.py",
        "node_modules/pkg/index.py",
        "env311/lib/x.py",  # a virtualenv under another name
        ".github/workflows/ci.yml",
        "build/out.py",
    ]:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x = 1\n")
    (tmp_path / "env311" / "pyvenv.cfg").write_text("home = /usr\n")
    found = sorted(
        p.relative_to(tmp_path).as_posix() for p in scanscope.walk_files(tmp_path, lambda n: True, (".github",))
    )
    assert found == [".github/workflows/ci.yml", "app/main.py"]  # env311 has a pyvenv.cfg: a virtualenv, skipped whole
    assert sorted(graph.build(tmp_path).files) == ["app/main.py"]


def test_launch_files_are_read_without_entering_node_modules_or_venvs(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "worker.py").write_text("def handle():\n    return 1\n")
    (tmp_path / "app" / "main.py").write_text(
        "from app import worker\n\nif __name__ == '__main__':\n    worker.handle()\n"
    )
    big = tmp_path / "node_modules"
    for i in range(3000):
        d = big / f"p{i}"
        d.mkdir(parents=True)
        (d / "package.json").write_text('{"scripts": {"x": "python -m app.hidden"}}')
    (tmp_path / "docker-compose.yml").write_text("services:\n  w:\n    command: python -m app.worker\n")
    g = graph.build(tmp_path)
    t = time.perf_counter()
    text = archmap.ArchMap(g)._launch_text()
    assert time.perf_counter() - t < 2.0
    assert "app.worker" in text and "app.hidden" not in text


def test_repo_paths_use_forward_slashes(tmp_path):
    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "pkg" / "sub" / "m.py").write_text("def f():\n    return 1\n")
    g = graph.build(tmp_path)
    assert list(g.files) == ["pkg/sub/m.py"] and "\\\\" not in next(iter(g.files))


def test_models_named_by_constants_env_vars_and_settings(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "config.py").write_text(
        textwrap.dedent("""
        import os
        from pydantic_settings import BaseSettings

        VERTEX_MODEL = os.getenv("VERTEX_MODEL", "gemini-2.5-flash")
        PRO_MODEL = "gemini-2.5-pro"
        RUNTIME_MODEL = os.environ["AGENT_MODEL"]


        class Settings(BaseSettings):
            router_model: str = "gemini-2.0-flash"
    """)
    )
    (tmp_path / "app" / "agent.py").write_text(
        textwrap.dedent("""
        from google.adk.agents import LlmAgent
        from google.adk.models import Vertex

        from app.config import PRO_MODEL, RUNTIME_MODEL, VERTEX_MODEL, Settings

        settings = Settings()
        a = LlmAgent(name="a", model=Vertex(model=VERTEX_MODEL))
        b = LlmAgent(name="b", model=PRO_MODEL)
        c = LlmAgent(name="c", model=RUNTIME_MODEL)
        d = LlmAgent(name="d", model=settings.router_model)
        e = LlmAgent(name="e", model=VERTEX_MODEL)
    """)
    )
    out = aimap.build(graph.build(tmp_path))
    models = {a["name"]: a["model"] for a in out["agents"]}
    assert models == {
        "a": "$VERTEX_MODEL (default gemini-2.5-flash)",  # the wrapper's model, one box with `e`
        "b": "gemini-2.5-pro (PRO_MODEL)",
        "c": "$AGENT_MODEL (set at run time)",
        "d": "gemini-2.0-flash (router_model)",
        "e": "$VERTEX_MODEL (default gemini-2.5-flash)",
    }
    assert len(out["summary"]["models"]) == 4
