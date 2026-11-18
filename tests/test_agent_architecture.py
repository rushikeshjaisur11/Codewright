"""Complex agent systems: an endpoint runs an orchestrator that fans out by hand to several agents."""

from pathlib import Path

from codewright import aimap, graph

FIX = Path(__file__).parent / "fixtures"


def _arch(name):
    d = aimap.build(graph.build(FIX / name))
    assert d["errors"] == []
    label = {n["id"]: n["label"] for n in d["architecture"]["nodes"]}
    edges = {(label[e["source"]], e["label"], label[e["target"]]) for e in d["architecture"]["edges"]}
    return d, edges


def test_langgraph_orchestrator_with_hand_written_fan_out():
    d, edges = _arch("complex_agents")
    # endpoint -> the compiled graph returned by a factory, named as the app names it
    assert ("POST /chat", "runs", "workflow") in edges
    assert ("uvicorn app:app", "starts", "POST /chat") in edges
    # the graph's steps, the router's branches read from its return values (a bounded loop)
    assert ("start", "then", "orchestrator") in edges
    assert ("orchestrator", "then", "fan_out") in edges
    assert ("aggregate", "if needs_more", "orchestrator") in edges and ("aggregate", "if needs_more", "end") in edges
    # the hand-written fan-out picks agents from a registry and runs them in parallel
    assert ("fan_out", "runs in parallel via WORKERS", "researcher") in edges
    assert ("fan_out", "runs in parallel via WORKERS", "analyst") in edges
    assert ("aggregate", "runs", "writer") in edges
    # model variables resolved to the model they are, including one imported into the step's file
    assert ("orchestrator", "prompts", "gpt-4o-mini") in edges
    assert ("researcher", "model", "gpt-4o") in edges and ("writer", "model", "gpt-4o-mini") in edges
    # tools imported from another module link to where they are defined
    researcher = next(a for a in d["agents"] if a["name"] == "researcher")
    assert {(t["label"], t["file"]) for t in researcher["tools"]} == {
        ("search_web", "assistant/tools.py"),
        ("read_docs", "assistant/tools.py"),
    }


def test_langgraph_improvements_are_specific_and_anchored():
    d, _ = _arch("complex_agents")
    ids = {i["id"]: i for i in d["insights"]}
    assert set(ids) == {
        "free-text-routing",
        "fanout-no-isolation",
        "hand-fanout-in-node",
        "fanout-no-timeout",
        "endpoint-waits-for-everything",
        "react-agent-without-tools",
        "no-checkpointer",
    }
    assert "unbounded-loop" not in ids  # the router counts rounds
    assert (
        ids["fanout-no-isolation"]["file"] == "assistant/graph.py"
        and "gather" in ids["fanout-no-isolation"]["evidence"]
    )
    assert "Send" in ids["hand-fanout-in-node"]["fix"]
    assert "with_structured_output" in ids["free-text-routing"]["fix"]


def test_unbounded_loop_is_flagged(tmp_path):
    src = (FIX / "complex_agents").resolve()
    import shutil

    shutil.copytree(src, tmp_path / "r")
    g = tmp_path / "r" / "assistant" / "graph.py"
    g.write_text(g.read_text().replace('state["rounds"] < 3', "True"))
    ids = {i["id"] for i in aimap.build(graph.build(tmp_path / "r"))["insights"]}
    assert "unbounded-loop" in ids


def test_adk_custom_orchestrator_fanning_out_over_a_list():
    d, edges = _arch("complex_agents_adk")
    assert ("POST /ask", "runs", "desk") in edges  # through a module-level Runner(agent=root_agent)
    for name in ("billing", "outages", "accounts"):
        assert ("desk", "delegates", name) in edges  # sub_agents=[*SPECIALISTS, summarizer]
        assert ("desk", "runs in parallel via SPECIALISTS", name) in edges  # inside _run_async_impl
    assert ("desk", "runs", "summarizer") in edges
    ids = {i["id"] for i in d["insights"]}
    assert {"hand-fanout-adk", "fanout-no-isolation", "fanout-no-timeout"} <= ids
    assert "hand-fanout-in-node" not in ids  # LangGraph advice does not apply here


def test_fan_out_from_a_file_without_an_sdk_import(tmp_path):
    """The orchestrator file imports only the repo's own agents module: it is still scanned and its fan-out found."""
    (tmp_path / "agents.py").write_text(
        "from langgraph.prebuilt import create_react_agent\n"
        "a = create_react_agent('openai:gpt-4o', tools=[])\n"
        "b = create_react_agent('openai:gpt-4o', tools=[])\n"
        "WORKERS = [a, b]\n"
    )
    (tmp_path / "orchestrate.py").write_text(
        "import asyncio\nfrom agents import WORKERS\n\n\n"
        "async def ask_all(q):\n    return await asyncio.gather(*(w.ainvoke(q) for w in WORKERS))\n"
    )
    (tmp_path / "unrelated.py").write_text("from agents import WORKERS\nprint(len('x'))\n")
    m = aimap.AiMap(graph.build(tmp_path))
    runs = {(r.function, m.agents[r.agent].name, r.parallel, r.via) for r in m.runs}
    assert ("ask_all", "a", True, "WORKERS") in runs and ("ask_all", "b", True, "WORKERS") in runs
