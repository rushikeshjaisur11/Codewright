import asyncio
from typing import TypedDict

from langgraph.graph import END, StateGraph

from .agents import WORKERS, fast, writer


class State(TypedDict, total=False):
    question: str
    plan: list[str]
    results: list[str]
    answer: str
    rounds: int


async def orchestrator(state: State) -> State:
    """Decide which workers to call."""
    reply = await fast.ainvoke(f"Which of {list(WORKERS)} should answer: {state['question']}?")
    return {"plan": [w for w in WORKERS if w in reply.content], "rounds": state.get("rounds", 0) + 1}


async def fan_out(state: State) -> State:
    """Hand-written fan-out: run the chosen workers in parallel."""
    calls = [WORKERS[name].ainvoke({"messages": [("user", state["question"])]}) for name in state["plan"]]
    outputs = await asyncio.gather(*calls)
    return {"results": [o["messages"][-1].content for o in outputs]}


async def aggregate(state: State) -> State:
    out = await writer.ainvoke({"messages": [("user", "\n".join(state["results"]))]})
    return {"answer": out["messages"][-1].content}


def needs_more(state: State) -> str:
    return "orchestrator" if not state.get("answer") and state["rounds"] < 3 else END


def build_graph():
    g = StateGraph(State)
    g.add_node("orchestrator", orchestrator)
    g.add_node("fan_out", fan_out)
    g.add_node("aggregate", aggregate)
    g.set_entry_point("orchestrator")
    g.add_edge("orchestrator", "fan_out")
    g.add_edge("fan_out", "aggregate")
    g.add_conditional_edges("aggregate", needs_more)
    return g.compile()
