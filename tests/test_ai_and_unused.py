"""The AI page (files that need a model, agent architecture) and the Map's unused-file filter."""

from pathlib import Path

from codewright import graph
from codewright.aimap import AiMap
from codewright.archmap import ArchMap

AGENT = """
from google.adk.agents import Agent, SequentialAgent
from google.adk.tools import google_search
from google.adk.tools.agent_tool import AgentTool
from google.adk.models.lite_llm import LiteLlm
from app.tools.search import search_docs


def lookup_order(order_id: str) -> dict:
    return {"id": order_id}


TOOLS = [lookup_order]
researcher = Agent(name="researcher", model="gemini-2.5-flash", instruction="Find facts.",
                   tools=[google_search, search_docs])
writer = Agent(name="writer", model=LiteLlm(model="anthropic/claude-haiku-5-5"), instruction=f"Write {1}", tools=TOOLS)
critic = Agent(name="critic", model="gemini-2.5-pro", instruction="Review the draft.")
pipeline = SequentialAgent(name="pipeline", sub_agents=[researcher, writer])
root_agent = Agent(name="root", model="gemini-2.5-flash", sub_agents=[pipeline], tools=[AgentTool(agent=critic)])
"""
MAIN = """
from fastapi import FastAPI
from google.adk.runners import Runner
from app.agent import root_agent
from app.llm import summarize

api = FastAPI()


@api.post("/chat")
async def chat(q: str):
    runner = Runner(agent=root_agent, app_name="x", session_service=None)
    return summarize(q)
"""
LLM = """
import anthropic

client = anthropic.Anthropic()


def summarize(text: str) -> str:
    r = client.messages.create(model="claude-haiku-5-5", max_tokens=200, messages=[])
    return r.content[0].text
"""
FLOW = """
from langgraph.graph import StateGraph, START, END


def plan(state): return state
def act(state): return state
def route(state): return "act"


g = StateGraph(dict)
g.add_node("plan", plan)
g.add_node("act", act)
g.add_edge(START, "plan")
g.add_conditional_edges("plan", route, {"act": "act", "done": END})
g.add_edge("act", "plan")
"""


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    return root


def agent_repo(tmp_path: Path) -> Path:
    return write(
        tmp_path,
        {
            "app/__init__.py": "",
            "app/tools/__init__.py": "",
            "app/tools/search.py": "def search_docs(query: str) -> str:\n    return query\n",
            "app/agent.py": AGENT,
            "app/main.py": MAIN,
            "app/llm.py": LLM,
            "app/flow.py": FLOW,
            "app/old_helpers.py": "def unused():\n    return 1\n",
            "app/only_tested.py": "def helper():\n    return 2\n",
            "tests/test_helper.py": "from app.only_tested import helper\n\ndef test_it():\n    assert helper() == 2\n",
            "scripts/seed.py": "from app.llm import summarize\n\nif __name__ == '__main__':\n    summarize('x')\n",
        },
    )


def test_agents_tools_models_and_who_runs_them(tmp_path):
    m = AiMap(graph.build(agent_repo(tmp_path)))
    agents = {a.name: a for a in m.agents.values()}
    assert {"researcher", "writer", "critic", "pipeline", "root"} <= set(agents)
    root, pipeline = agents["root"], agents["pipeline"]
    assert root.model == "gemini-2.5-flash" and root.framework == "Google ADK"
    assert pipeline.kind == "sequential"
    assert [m.agents[c].name for c in pipeline.children] == ["researcher", "writer"]
    assert [m.agents[c].name for c in root.children] == ["pipeline"]
    assert root.tools[0]["agent"] == agents["critic"].id  # AgentTool(agent=critic)
    assert "anthropic/claude-haiku-5-5" in agents["writer"].model
    assert agents["writer"].instruction.startswith("built at runtime")
    tools = {t["label"]: t for t in agents["researcher"].tools}
    assert tools["search_docs"]["file"] == "app/tools/search.py" and tools["google_search"].get("builtin")
    assert agents["writer"].tools[0]["label"] == "lookup_order"  # through a module-level list variable

    assert [(c.file, c.function, c.provider, c.model) for c in m.calls] == [
        ("app/llm.py", "summarize", "Claude API", "claude-haiku-5-5")
    ]
    assert [(r.file, r.function, m.agents[r.agent].name) for r in m.runs] == [("app/main.py", "chat", "root")]

    flow = next(a for a in m.agents.values() if a.kind == "graph")
    assert [s["name"] for s in flow.steps] == ["plan", "act"]
    assert ["plan", "END", "if route"] in flow.step_edges and ["START", "plan", ""] in flow.step_edges


def test_files_that_need_ai(tmp_path):
    files = {f.file: f for f in AiMap(graph.build(agent_repo(tmp_path))).files()}
    assert files["app/agent.py"].roles == ["agents"]
    assert files["app/llm.py"].roles == ["model calls"]
    assert files["app/tools/search.py"].roles == ["tools"]
    assert files["app/main.py"].roles == ["runs agents", "calls AI code"]
    assert "app/llm.py · summarize" in files["app/main.py"].via
    assert "app/old_helpers.py" not in files and not any(f.startswith("tests/") for f in files)


def test_architecture_graph(tmp_path):
    arch = AiMap(graph.build(agent_repo(tmp_path))).architecture()
    kinds = {n["label"]: n["kind"] for n in arch["nodes"]}
    assert kinds["root"] == "agent" and kinds["pipeline"] == "workflow" and kinds["search_docs"] == "tool"
    assert (
        kinds["gemini-2.5-flash"] == "llm" and kinds["POST /chat"] == "entry"
    )  # a route is named by its method and path
    edges = {(e["source"].rsplit("::", 1)[-1], e["target"].rsplit("::", 1)[-1], e["label"]) for e in arch["edges"]}
    assert ("chat", "root_agent", "runs") in edges
    assert ("root_agent", "pipeline", "delegates") in edges
    assert ("root_agent", "critic", "uses as tool") in edges
    assert ("summarize", "model:claude-haiku-5-5", "prompts") in {
        (e["source"].rsplit("::", 1)[-1], e["target"], e["label"]) for e in arch["edges"]
    }


def test_no_ai_means_empty(tmp_path):
    write(tmp_path, {"a.py": "def f():\n    return 1\n"})
    m = AiMap(graph.build(tmp_path))
    assert m.files() == [] and m.architecture() == {"nodes": [], "edges": []}


def test_map_leaves_out_unused_files(tmp_path):
    m = ArchMap(graph.build(agent_repo(tmp_path)))
    reasons = {u["file"]: u["reason"] for u in m.unused}
    assert reasons == {
        "app/flow.py": "nothing in use imports it",
        "app/old_helpers.py": "nothing in use imports it",
        "app/only_tested.py": "only tests import it",
        "scripts/seed.py": "script, docs or example folder",
    }
    labels = {n.label for n in m.module("app").nodes} | {c for c in m.components()}
    assert not any("old_helpers" in x or "only_tested" in x for x in labels)
    everything = ArchMap(graph.build(tmp_path), include_unused=True)
    assert "scripts" in everything.components() and len(everything.shown) == len(everything.modules)


def test_launch_configs_and_conventions_keep_files_in_use(tmp_path):
    write(
        tmp_path,
        {
            "svc/__init__.py": "",
            "svc/api.py": "import importlib\nfrom fastapi import FastAPI\napp = FastAPI()\n\n"
            "@app.get('/')\ndef home():\n    return importlib.import_module('svc.extra')\n",
            "svc/worker.py": "def run_worker():\n    while True:\n        pass\n",  # started outside the repo
            "svc/agents.py": "from google.adk.agents import Agent\nroot_agent = Agent(name='a')\n",  # adk web loads it
            "svc/beat.py": "def tick():\n    return 1\n",
            "svc/plugins.py": "PLUGINS = ['svc.beat']\n",
            "svc/extra.py": "def hook():\n    return 1\n",
            "Procfile": "beat: python -m svc.beat\n",
        },
    )
    m = ArchMap(graph.build(tmp_path))
    assert {u["file"] for u in m.unused} == {"svc/plugins.py"}  # extra.py is imported by name


def test_a_library_with_no_entry_points_keeps_everything(tmp_path):
    write(tmp_path, {"lib/__init__.py": "", "lib/a.py": "def f():\n    return 1\n", "docs/conf.py": "x = 1\n"})
    m = ArchMap(graph.build(tmp_path))
    assert [u["file"] for u in m.unused] == ["docs/conf.py"]


def test_ai_and_map_endpoints(tmp_path):
    from fastapi.testclient import TestClient

    from codewright.server import create_app

    client = TestClient(create_app([str(agent_repo(tmp_path))]), base_url="http://localhost")
    wid = client.get("/api/workspaces").json()[0]["id"]
    client.post(f"/api/w/{wid}/review", json={"proof": False})
    ai = client.get(f"/api/w/{wid}/ai").json()
    assert ai["summary"]["agents"] == 5 and ai["summary"]["need_model"] == 3
    flow = next(f for f in ai["files"] if f["file"] == "app/flow.py")
    assert flow["unused"] == "nothing in use imports it"
    meta = client.get(f"/api/w/{wid}/map/meta").json()
    assert len(meta["unused"]) == 4
    hidden = client.get(f"/api/w/{wid}/map/system").json()
    shown = client.get(f"/api/w/{wid}/map/system?unused=true").json()
    assert len(shown["nodes"]) > len(hidden["nodes"])


# --------------------------------------------------------------------------- finding categories


def test_findings_are_tagged_by_kind(tmp_path):
    from codewright.engine import review
    from codewright.models import CATEGORIES, categorize
    from codewright.report import to_markdown, to_pr_comment, to_sarif

    assert categorize("arch-import-cycle") == "architecture"
    assert categorize("adk-sync-run") == "agents"
    assert categorize("adk-deprecated-api") == "upgrade"
    assert categorize("blocking-sleep-in-async") == "performance"
    assert categorize("requests-no-timeout") == "reliability"
    assert categorize("memo-cache") == "performance" and categorize("manual-counter") == "library"

    write(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "from pkg import b\n\ndef fa():\n    return b.fb()\n",
            "pkg/b.py": "from pkg import a\n\ndef fb():\n    return a.fa\n",
            "pkg/main.py": "from pkg import a\n\nif __name__ == '__main__':\n    a.fa()\n",
        },
    )
    r = review(tmp_path, run_proof=False, top=None)
    cycle = next(f for f in r.findings if f.intent == "arch-import-cycle")
    assert cycle.category == "architecture" and cycle.to_dict()["category_label"] == "Architecture"
    assert set(CATEGORIES) >= {f.category for f in r.findings}
    assert "[Architecture]" in to_markdown(r) and "| Architecture |" in to_pr_comment(r)
    assert '"architecture"' in to_sarif(r)
    m = ArchMap(r.graph, r.findings)
    assert any(n.arch for n in m.system().nodes + m.module(m.components()[0]).nodes)


# --------------------------------------------------------------------------- node inspector (both maps)


def test_every_box_on_both_maps_has_details(tmp_path):
    from fastapi.testclient import TestClient

    from codewright.server import create_app

    client = TestClient(create_app([str(agent_repo(tmp_path))]), base_url="http://localhost")
    wid = client.get("/api/workspaces").json()[0]["id"]
    client.post(f"/api/w/{wid}/review", json={"proof": False})
    meta = client.get(f"/api/w/{wid}/map/meta").json()
    views = [("system", "")] + [("module", c) for c in meta["components"]]
    views += [("flow", e["id"]) for e in meta["entry_points"]]
    seen = 0
    for level, target in views:
        view = client.get(f"/api/w/{wid}/map/{level}", params={"target": target}).json()
        for n in view["nodes"]:
            d = client.get(f"/api/w/{wid}/inspect", params={"id": n["id"], "level": level, "target": target}).json()
            assert d["kind"] != "unknown" and d["label"] and d["summary"], (level, n["id"])
            assert d["facts"] or any(s["items"] for s in d["sections"]), (level, n["id"])
            seen += 1
    assert seen > 10

    comp = client.get(f"/api/w/{wid}/inspect", params={"id": "c:app.main", "level": "system"}).json()
    sections = {s["title"]: s["items"] for s in comp["sections"]}
    assert {i["label"] for i in sections["Depends on"]} >= {"app.llm", "app.agent"}
    assert {i["flow"] for i in sections["Entry points"]} == {"app/main.py::<module>", "app/main.py::chat"}
    fn = client.get(f"/api/w/{wid}/inspect", params={"id": "app/main.py::chat", "level": "flow"}).json()
    assert fn["code"]["text"].startswith("@api.post") and "summarize" in {
        i["label"] for s in fn["sections"] for i in s["items"]
    }
    ext = client.get(f"/api/w/{wid}/inspect", params={"id": "x:Claude API", "level": "system"}).json()
    assert [i["label"] for s in ext["sections"] if s["title"] == "Imported in" for i in s["items"]] == ["app/llm.py"]

    arch = client.get(f"/api/w/{wid}/ai").json()["architecture"]
    for n in arch["nodes"]:
        d = client.get(f"/api/w/{wid}/ai/node", params={"id": n["id"]}).json()
        assert d["kind"] != "unknown" and d["summary"], n["id"]
    root = client.get(f"/api/w/{wid}/ai/node", params={"id": "app/agent.py::root_agent"}).json()
    facts = {f["label"]: f["value"] for f in root["facts"]}
    assert facts["Model"] == "gemini-2.5-flash" and facts["Run by"] == "POST /chat"
    assert root["code"]["text"].startswith("root_agent = Agent(")
    writer = client.get(f"/api/w/{wid}/ai/node", params={"id": "app/agent.py::writer"}).json()
    assert {f["label"]: f["value"] for f in writer["facts"]}["Model"].startswith("anthropic/claude-haiku-5-5")
    tool = client.get(f"/api/w/{wid}/ai/node", params={"id": "tool:app/tools/search.py:search_docs"}).json()
    assert tool["code"]["file"] == "app/tools/search.py" and "researcher" in str(tool["sections"])
    assert "flowchart LR" in client.get(f"/api/w/{wid}/ai").json()["mermaid"]


# --------------------------------------------------------------------------- the full agent tree from the app


FULL = {
    "app.py": "import streamlit as st\nfrom bot.service import answer\n\nq = st.text_input('Ask')\nif q:\n"
    "    st.write(answer(q))\n",
    "bot/__init__.py": "",
    "bot/service.py": "from google.adk.runners import InMemoryRunner\nfrom bot.agents import root\n\n\n"
    "def answer(q):\n    runner = InMemoryRunner(agent=root.root_agent)\n    return runner.run(new_message=q)\n",
    "bot/agents/__init__.py": "",
    "bot/agents/root.py": "from google.adk.agents import Agent, ParallelAgent\n"
    "from google.adk.tools.agent_tool import AgentTool\nfrom bot.agents.research import build_researcher\n"
    "from bot.agents.custom import Reviewer\nfrom bot.tools.db import query_orders\n\n"
    "researcher = build_researcher('gemini-2.5-flash')\nreviewer = Reviewer(name='reviewer')\n"
    "fanout = ParallelAgent(name='fanout', sub_agents=[researcher, Agent(name='pricing', model='gemini-2.5-flash', "
    "tools=[query_orders])])\n"
    "root_agent = Agent(name='concierge', model='gemini-2.5-pro', sub_agents=[fanout], tools=[AgentTool(agent=reviewer)])\n",  # noqa: E501
    "bot/agents/research.py": "from google.adk.agents import LlmAgent\nfrom google.adk.tools import google_search\n\n\n"
    "def build_researcher(model):\n    helper = LlmAgent(name='summarizer', model=model)\n"
    "    agent = LlmAgent(name='researcher', model=model, tools=[google_search], sub_agents=[helper])\n"
    "    return agent\n",
    "bot/agents/custom.py": "from google.adk.agents import BaseAgent\n\n\nclass Reviewer(BaseAgent):\n"
    "    async def _run_async_impl(self, ctx):\n        yield None\n",
    "bot/tools/__init__.py": "",
    "bot/tools/db.py": "def query_orders(customer_id):\n    return []\n",
}


def test_the_whole_agent_tree_is_traced_from_app_py(tmp_path):
    from codewright.inspect_node import _reachable

    m = AiMap(graph.build(write(tmp_path, FULL)))
    by_name = {a.name: a for a in m.agents.values()}
    assert set(by_name) == {"concierge", "fanout", "pricing", "researcher", "summarizer", "reviewer"}
    assert by_name["reviewer"].kind == "custom"  # a repo subclass of BaseAgent
    assert by_name["researcher"].model == "gemini-2.5-flash"  # passed into the factory
    assert by_name["summarizer"].model == "gemini-2.5-flash"
    names = lambda ids: sorted(m.agents[i].name for i in ids)  # noqa: E731
    assert names(by_name["fanout"].children) == ["pricing", "researcher"]  # factory-built and inline
    assert names(by_name["researcher"].children) == ["summarizer"]  # local to the factory
    assert by_name["concierge"].tools[0]["agent"] == by_name["reviewer"].id
    assert by_name["pricing"].tools[0]["file"] == "bot/tools/db.py"

    arch = m.architecture()
    labels = {n["id"]: n["label"] for n in arch["nodes"]}
    reach = {labels[x] for x in _reachable(arch, "app:app.py")}
    assert {"answer", "concierge", "fanout", "pricing", "researcher", "summarizer", "reviewer"} <= reach
    assert {"query_orders", "google_search", "gemini-2.5-pro", "gemini-2.5-flash"} <= reach
    assert ("app:app.py", "run:bot/service.py::answer", "starts") in {
        (e["source"], e["target"], e["label"]) for e in arch["edges"]
    }


def test_adk_root_agent_and_langgraph_json_are_entry_points(tmp_path):
    write(
        tmp_path,
        {
            "agent_pkg/__init__.py": "",
            "agent_pkg/agent.py": "from google.adk.agents import Agent\nroot_agent = Agent(name='helper', model='gemini-2.5-flash')\n",  # noqa: E501
            "graphs/flow.py": "from langgraph.graph import StateGraph\nfrom langgraph.prebuilt import create_react_agent\n\n"  # noqa: E501
            "worker = create_react_agent('openai:gpt-4o', tools=[])\ng = StateGraph(dict)\ng.add_node('work', worker)\n"
            "app = g.compile()\n",
            "langgraph.json": '{"graphs": {"main": "./graphs/flow.py:app"}}',
        },
    )
    arch = AiMap(graph.build(tmp_path)).architecture()
    edges = {(e["source"], e["target"], e["label"]) for e in arch["edges"]}
    assert ("adk:agent_pkg/agent.py", "agent_pkg/agent.py::root_agent", "runs") in edges
    assert ("lg:main", "graphs/flow.py::g", "runs") in edges
    assert ("graphs/flow.py::g#work", "graphs/flow.py::worker", "runs agent") in edges


def test_one_file_the_ai_scanner_trips_on_is_skipped_not_fatal(tmp_path, monkeypatch):
    from codewright import aimap, graph

    (tmp_path / "a.py").write_text("from google.adk.agents import Agent\nroot_agent = Agent(name='a', model='m')\n")
    (tmp_path / "b.py").write_text("from google.adk.agents import Agent\nb_agent = Agent(name='b', model='m')\n")
    real = aimap.AiMap._scan_file

    def flaky(self, pf):
        if pf.path == "b.py":
            raise TypeError("sequence item 0: expected str instance, NoneType found")
        return real(self, pf)

    monkeypatch.setattr(aimap.AiMap, "_scan_file", flaky)
    out = aimap.build(graph.build(tmp_path))
    assert [a["name"] for a in out["agents"]] == ["a"]
    assert out["errors"][0]["file"] == "b.py" and "expected str" in out["errors"][0]["error"]
