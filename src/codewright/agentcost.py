"""How many model calls one request to each entry point makes, read from the agent architecture.

Walks what an entry point runs: an agent is one call to its model (plus one per tool round when it has
tools, which the code cannot bound), a step that prompts a model is one call, a fan-out runs every worker,
and a workflow loop multiplies the steps inside it by its round limit (`rounds < 3`) when the router has one.
The result is a floor ("at least N calls"), with what can add more spelled out, per model.
"""

from __future__ import annotations

import re
from collections import Counter

from .graph import text

WALK = ("runs", "starts", "delegates", "uses as tool", "runs agent", "calls over HTTP")


def _rounds(m, g) -> tuple[int | None, set[str]]:
    """(round limit or None when unbounded, step names inside the loop) for a workflow graph `g`; (1, {}) if no loop."""
    steps = {s["name"] for s in g.steps}
    nxt: dict[str, set[str]] = {}
    for a, b, _ in g.step_edges:
        nxt.setdefault(a, set()).add(b)

    def reach(start: str, goal: str) -> set[str] | None:
        """Nodes on some path start -> goal (inclusive), or None."""
        seen, stack, parents = set(), [start], {}
        while stack:
            x = stack.pop()
            if x == goal:
                path, y = {goal}, goal
                while y in parents:
                    y = parents[y]
                    path.add(y)
                return path
            if x in seen:
                continue
            seen.add(x)
            for y in nxt.get(x, ()):
                parents.setdefault(y, x)
                stack.append(y)
        return None

    for a, b, label in g.step_edges:
        if not label.startswith("if") or b not in steps:
            continue
        loop = reach(b, a)
        if loop is None:
            continue
        router = label[3:]
        ref = m._function_ref(m.g.files[g.file], router) if router else None
        src = ""
        if ref is not None:
            rpf = m.g.files[ref[0]]
            fn = next((f for f in rpf.module.functions if f.def_line == ref[1]), None)
            node = rpf.nodes.get(fn.qualname) if fn else None
            src = text(node) if node is not None else ""
        hit = re.search(r"<\s*=?\s*(\d+)|(\d+)\s*>\s*=?", src)
        if hit:
            n = int(hit.group(1) or hit.group(2))
            return (n + 1 if "<=" in hit.group(0) else n), loop & steps
        return None, loop & steps
    return 1, set()


def estimate(m, arch: dict) -> list[dict]:
    nodes = {n["id"]: n for n in arch["nodes"]}
    out_edges: dict[str, list[dict]] = {}
    for e in arch["edges"]:
        out_edges.setdefault(e["source"], []).append(e)
    graphs = {a.id: a for a in m.agents.values() if a.kind == "graph"}
    memo: dict[str, tuple[Counter, set[str], bool]] = {}

    def model_of(nid: str) -> str:
        for e in out_edges.get(nid, ()):
            if e["label"] == "model":
                return nodes[e["target"]]["label"]
        return ""

    def cost(nid: str, stack: frozenset) -> tuple[Counter, set[str], bool]:
        """(calls per model, agents whose tool rounds add more, unbounded loop somewhere)."""
        if nid in memo:
            return memo[nid]
        if nid in stack:
            return Counter(), set(), False
        stack = stack | {nid}
        node = nodes.get(nid, {})
        agent = m.agents.get(nid)
        calls, more, unbounded = Counter(), set(), False

        def take(c: tuple[Counter, set[str], bool], times: int = 1) -> None:
            nonlocal unbounded
            for model, n in c[0].items():
                calls[model] += n * times
            more.update(c[1])
            unbounded |= c[2]

        if node.get("kind") == "agent" and (agent is None or agent.kind != "custom" or model_of(nid)):
            calls[model_of(nid) or "its model"] += 1  # a custom agent class with no model makes no call itself
            if agent is not None and agent.tools:
                more.add(node["label"])
        for e in out_edges.get(nid, ()):
            if e["label"] == "prompts":
                calls[nodes[e["target"]]["label"]] += 1
        if nid in graphs:
            g = graphs[nid]
            rounds, inside = _rounds(m, g)
            unbounded |= rounds is None
            for st in g.steps:
                take(cost(f"{nid}#{st['name']}", stack), (rounds or 1) if st["name"] in inside else 1)
        edges = out_edges.get(nid, ())
        runs = {e["target"] for e in edges if e["label"] in WALK and e["label"] != "delegates"}
        runs |= {e["target"] for e in edges if e["label"].startswith("runs in parallel")}
        delegates = {e["target"] for e in edges if e["label"] == "delegates"} - runs
        for t in sorted(runs):  # each distinct thing it runs, once
            take(cost(t, stack))
        if delegates and not runs:
            kind = agent.kind if agent is not None else ""
            if kind in ("sequential", "parallel", "crew", "custom"):  # workflow agents run every sub-agent
                for t in sorted(delegates):
                    take(cost(t, stack))
            elif kind == "loop":
                for t in sorted(delegates):
                    take(cost(t, stack))
                unbounded |= "max_iterations" not in (agent.options if agent else {})
            else:  # a model that hands off picks one specialist: count the dearest
                best = max((cost(t, stack) for t in delegates), key=lambda c: sum(c[0].values()))
                take(best)
        memo[nid] = (calls, more, unbounded)
        return memo[nid]

    out = []
    for nid, n in nodes.items():
        if n["kind"] != "entry":
            continue
        calls, more, unbounded = cost(nid, frozenset())
        if not calls:
            continue
        total = sum(calls.values())
        notes = []
        if more:
            notes.append(f"plus one call per tool round of {', '.join(sorted(more))}")
        if unbounded:
            notes.append("a workflow loop has no round limit, so a request can make many more")
        out.append(
            {
                "entry": n["label"],
                "node": nid,
                "file": n["files"][0]["file"] if n["files"] else "",
                "line": n["files"][0]["line"] if n["files"] else 1,
                "calls": total,
                "by_model": dict(calls.most_common()),
                "at_least": bool(more) or unbounded,
                "notes": notes,
            }
        )
    # The same work reached from both the app and one of its routes: keep the route, it is what a request hits.
    routes = [o for o in out if re.match(r"(GET|POST|PUT|PATCH|DELETE|WEBSOCKET|ROUTE) ", o["entry"])]
    return sorted(routes or out, key=lambda o: -o["calls"])
