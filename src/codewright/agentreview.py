"""Architecture review of the agents: improvements read from how the agents are actually wired.

Each rule looks at the extracted architecture (which endpoint runs which workflow, which step fans out to
which agents, how the orchestrator routes, whether a loop is bounded) and at the code of the functions
involved. An insight is only raised with evidence: the file and line, and the code that shows it. Nothing
here calls a model; ``deep_review`` is the optional, on-request second opinion from the configured one.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

from .graph import text


@dataclass
class Insight:
    id: str
    severity: str  # high | medium | low
    title: str
    why: str
    fix: str
    file: str
    line: int
    evidence: str = ""
    nodes: list[str] = field(default_factory=list)  # architecture node ids it is about
    source: str = "rule"  # rule | ai


def _fn_source(m, file: str, qualname: str) -> str:
    pf = m.g.files.get(file)
    node = pf.nodes.get(qualname) if pf else None
    return text(node) if node is not None else ""


def _line_of(m, file: str, qualname: str, pattern: str, default: int) -> tuple[int, str]:
    """(line, the line's code) of the first line in the function matching `pattern`."""
    pf = m.g.files.get(file)
    fn = next((f for f in pf.module.functions if f.qualname == qualname), None) if pf else None
    if fn is None:
        return default, ""
    for i in range(fn.start_line, fn.end_line + 1):
        ln = pf.lines[i - 1] if i - 1 < len(pf.lines) else ""
        if re.search(pattern, ln):
            return i, ln.strip()
    return default, ""


def review(m) -> list[dict]:
    """Insights for an AiMap whose `architecture()` has been built (it sets the step lookup used here)."""
    if not hasattr(m, "_step_of"):
        m.architecture()
    out: list[Insight] = []
    step_of: dict[tuple[str, str], str] = m._step_of
    graphs = {a.id: a for a in m.agents.values() if a.kind == "graph"}

    # ---- fan-outs written by hand
    fanouts: dict[tuple[str, str], list] = {}
    for r in m.runs:
        if r.parallel:
            fanouts.setdefault((r.file, r.function), []).append(r)
    for (file, fn), runs in fanouts.items():
        src = _fn_source(m, file, fn)
        names = sorted({m.agents[r.agent].name for r in runs if r.agent in m.agents})
        node_id = step_of.get((file, fn), f"run:{file}::{fn}")
        line, code = _line_of(
            m, file, fn, r"gather\(|TaskGroup|create_task\(|as_completed\(|\.submit\(|\.map\(", runs[0].line
        )
        in_graph_step = (file, fn) in step_of and (file, fn) not in getattr(m, "_owner_of", {})
        if in_graph_step:
            out.append(
                Insight(
                    "hand-fanout-in-node",
                    "medium",
                    f"Fan-out to {', '.join(names)} is hand-written inside the `{fn}` step",
                    "All the workers run inside one graph node, so LangGraph sees a single step: it cannot checkpoint, "
                    "retry, stream or trace each worker on its own, one slow or failing worker holds up or fails the "
                    "whole step, and adding a worker means editing this function.",
                    "Make each worker its own node and fan out with the Send API: return "
                    '`[Send(name, {"question": state["question"]}) for name in state["plan"]]` from a conditional '
                    "edge after the orchestrator, and collect results with a reducer field such as "
                    "`results: Annotated[list[str], operator.add]` before the aggregate step.",
                    file,
                    line,
                    code,
                    [node_id],
                )
            )
        elif any(m.agents[r.agent].framework == "Google ADK" for r in runs if r.agent in m.agents):
            out.append(
                Insight(
                    "hand-fanout-adk",
                    "medium",
                    f"`{fn}` runs {', '.join(names)} in parallel by hand",
                    "ADK has a workflow agent for exactly this; hand-written concurrency loses the shared session, "
                    "event stream and tracing the framework gives each sub-agent.",
                    "Use `ParallelAgent(sub_agents=[...])` (followed by an aggregating LlmAgent inside a "
                    "SequentialAgent) instead of gathering the runs yourself.",
                    file,
                    line,
                    code,
                    [node_id],
                )
            )
        if re.search(r"\bgather\(", src) and "return_exceptions" not in src and not re.search(r"\btry\s*:", src):
            ln, code = _line_of(m, file, fn, r"gather\(", line)
            out.append(
                Insight(
                    "fanout-no-isolation",
                    "high",
                    f"One failing worker fails the whole `{fn}` fan-out",
                    "`asyncio.gather` without `return_exceptions=True` raises the first error and drops every other "
                    "worker's result; with no try/except around it, the request fails although most workers succeeded.",
                    "Pass `return_exceptions=True` and keep the results that are not exceptions (log the rest), or "
                    "wrap each worker call so one failure becomes a partial answer.",
                    file,
                    ln,
                    code,
                    [node_id],
                )
            )
        if not re.search(r"wait_for\(|asyncio\.timeout\(|timeout\s*=|anyio\.fail_after", src):
            out.append(
                Insight(
                    "fanout-no-timeout",
                    "medium",
                    f"No time limit on the workers in `{fn}`",
                    "The step waits for the slowest worker; a model or tool that hangs keeps the request open until "
                    "the client gives up.",
                    "Bound each worker: `await asyncio.wait_for(worker.ainvoke(...), timeout=...)`, or "
                    "`async with asyncio.timeout(...)` around the gather, and answer with what finished.",
                    file,
                    line,
                    code,
                    [node_id],
                )
            )

    # ---- routing by reading free text
    for c in m.calls:
        src = _fn_source(m, c.file, c.function)
        if not src:
            continue
        if re.search(
            r"\bin\s+\w+(\.\w+)*\.content\b|\.content\.(lower|strip|split)\(|json\.loads\([^)]*\.content", src
        ):
            ln, code = _line_of(m, c.file, c.function, r"\.content", c.line)
            is_router = (c.file, c.function) in step_of
            out.append(
                Insight(
                    "free-text-routing",
                    "high" if is_router else "medium",
                    f"`{c.function}` decides by searching the model's free-text reply",
                    "Matching names or parsing JSON out of `.content` breaks when the model rephrases, adds a word "
                    "that contains a worker's name, or wraps JSON in prose; the plan then silently picks the wrong "
                    "workers or none.",
                    "Ask for structured output: `fast.with_structured_output(Plan)` where `Plan` is a Pydantic model "
                    "with `workers: list[Literal[...]]`, so the reply is validated against the workers that exist.",
                    c.file,
                    ln,
                    code,
                    [step_of.get((c.file, c.function), f"fn:{c.file}::{c.function}")],
                )
            )

    # ---- graphs: loops, persistence, how the endpoint runs them
    for g in graphs.values():
        steps = {s["name"] for s in g.steps}
        nxt: dict[str, set[str]] = {}
        for a, b, _ in g.step_edges:
            nxt.setdefault(a, set()).add(b)
        # A loop is closed by a conditional edge (the router decides to go round again); plain edges inside the
        # cycle are not themselves the decision.
        back = [
            (a, b, lbl) for a, b, lbl in g.step_edges if lbl.startswith("if") and b in steps and _reaches(nxt, b, a)
        ]
        for a, b, lbl in back:
            router = lbl[3:] if lbl.startswith("if ") else ""
            ref = m._function_ref(m.g.files[g.file], router) if router else None
            rsrc = ""
            if ref is not None:
                rpf = m.g.files[ref[0]]
                fn = next((f for f in rpf.module.functions if f.def_line == ref[1]), None)
                rsrc = _fn_source(m, ref[0], fn.qualname) if fn else ""
            bounded = bool(re.search(r"[<>]=?\s*\d|\d\s*[<>]=?|max_\w+|limit|attempt|round|iteration", rsrc))
            if not bounded:
                out.append(
                    Insight(
                        "unbounded-loop",
                        "high",
                        f"`{a}` can loop back to `{b}` with no limit",
                        "Nothing in the routing counts the rounds, so a model that never says it is done keeps the "
                        "graph cycling until LangGraph's recursion limit raises an error at the user.",
                        "Keep a round counter in the state and route to END after N rounds, and set "
                        '`config={"recursion_limit": ...}` to a deliberate value.',
                        ref[0] if ref else g.file,
                        ref[1] if ref else g.line,
                        "",
                        [f"{g.id}#{a}", f"{g.id}#{b}"],
                    )
                )
        served = [r for r in m.runs if r.agent == g.id and r.kind == "runs"]
        routes = [r for r in served if _is_route(m, r)]
        gsrc = _fn_source(m, g.file, g.scope) if g.scope != "<module>" else m.g.files[g.file].source.decode()
        if routes and ".compile(" in gsrc and "checkpointer" not in gsrc:
            ln, code = _line_of(m, g.file, g.scope, r"\.compile\(", g.line) if g.scope != "<module>" else (g.line, "")
            out.append(
                Insight(
                    "no-checkpointer",
                    "low",
                    f"`{g.name}` keeps no state between requests",
                    "The graph is compiled without a checkpointer, so every call to the endpoint starts from an "
                    "empty state: no conversation memory, no resuming an interrupted run, no human-in-the-loop.",
                    "Compile with a checkpointer (`MemorySaver()` in development, a Postgres or Redis saver in "
                    'production) and pass `config={"configurable": {"thread_id": ...}}` from the endpoint. Skip this '
                    "if each request is meant to be independent.",
                    g.file,
                    ln,
                    code,
                    [g.id],
                )
            )
        heavy = len(steps) >= 3 or any(r.parallel for r in m.runs if (r.file, r.function) in step_of)
        for r in routes:
            if heavy and re.search(r"\.a?invoke$", r.how):
                out.append(
                    Insight(
                        "endpoint-waits-for-everything",
                        "medium",
                        f"The endpoint waits for the whole multi-agent run of `{g.name}`",
                        f"`{r.how}` returns only when every step and worker has finished, so the user sees nothing "
                        "for the length of the slowest path, and a proxy timeout cuts the request off.",
                        "Stream progress: iterate `async for event in workflow.astream_events(...)` (or "
                        '`astream(..., stream_mode="updates")`) and return a `StreamingResponse` / server-sent events.',
                        r.file,
                        r.line,
                        r.how,
                        [f"run:{r.file}::{r.function}", g.id],
                    )
                )

    # ---- agents and how they are built
    for a in m.agents.values():
        if a.kind == "llm" and a.framework in ("LangGraph", "LangChain") and not a.tools and not a.children:
            out.append(
                Insight(
                    "react-agent-without-tools",
                    "low",
                    f"`{a.name}` is a ReAct agent with no tools",
                    "`create_react_agent` adds a tool-calling loop and its prompt overhead; with no tools it can only "
                    "answer once, so it is a slower, costlier single model call.",
                    "Call the model directly (`await model.ainvoke(messages)`) or give the agent the tools it needs.",
                    a.file,
                    a.line,
                    "",
                    [a.id],
                )
            )
    for r in m.runs:
        if r.kind == "builds" and _is_route(m, r):
            out.append(
                Insight(
                    "built-per-request",
                    "medium",
                    f"`{r.how}` builds the agent on every request",
                    "Constructing and compiling agents or graphs inside the endpoint repeats that work (and any client "
                    "setup) for every call.",
                    "Build it once at startup (module level or a FastAPI lifespan) and reuse it across requests.",
                    r.file,
                    r.line,
                    r.how,
                    [r.agent],
                )
            )
    order = {"high": 0, "medium": 1, "low": 2}
    seen, uniq = set(), []
    for i in sorted(out, key=lambda x: (order[x.severity], x.file, x.line)):
        key = (i.id, i.file, i.line)
        if key not in seen:
            seen.add(key)
            uniq.append(asdict(i))
    return uniq


def _reaches(nxt: dict[str, set[str]], start: str, goal: str) -> bool:
    seen, todo = set(), [start]
    while todo:
        x = todo.pop()
        if x == goal:
            return True
        if x in seen:
            continue
        seen.add(x)
        todo.extend(nxt.get(x, ()))
    return False


def _is_route(m, r) -> bool:
    pf = m.g.files.get(r.file)
    fn = next((f for f in pf.module.functions if f.qualname == r.function), None) if pf else None
    return bool(
        fn and any(re.search(r"\.(get|post|put|patch|delete|websocket|api_route|route)\(", d) for d in fn.decorators)
    )


# --------------------------------------------------------------------------- the model's second opinion

DEEP_SYSTEM = (
    "You are reviewing the architecture of a multi-agent application. You get its architecture as a list of "
    "nodes and edges extracted from the code, the source of the key functions, and the issues a rule-based "
    "review already found. Suggest only improvements the code supports: name the file and line, say why it "
    "matters for this system, and give the concrete change. Do not repeat the existing issues. Reply with JSON "
    'only: a list of {"title", "why", "fix", "file", "line", "severity"} (severity: high, medium or low).'
)


def deep_review(m, insights: list[dict], cfg, max_chars: int = 24000) -> dict:
    """Ask the configured model for improvements the rules cannot see. Returns {"insights", "model"} or a reason."""
    from .llm import complete
    from .llm import status as llm_status

    st = llm_status(cfg)
    if not st["available"]:
        return {"insights": [], "error": st["reason"]}
    arch = m.architecture()
    lines = [
        f"- {n['kind']}: {n['label']} ({n['files'][0]['file']}:{n['files'][0]['line']})"
        if n["files"]
        else f"- {n['kind']}: {n['label']}"
        for n in arch["nodes"]
    ]
    label = {n["id"]: n["label"] for n in arch["nodes"]}
    lines += [
        f"- {label.get(e['source'], e['source'])} --{e['label']}--> {label.get(e['target'], e['target'])}"
        for e in arch["edges"]
    ]
    key_fns: list[tuple[str, str]] = list(
        dict.fromkeys(
            [(r.file, r.function) for r in m.runs if r.function != "<module>"]
            + [(c.file, c.function) for c in m.calls]
            + list(m._step_of)
        )
    )
    code, used = [], 0
    for file, fn in key_fns:
        src = _fn_source(m, file, fn)
        if not src or used + len(src) > max_chars:
            continue
        used += len(src)
        code.append(f"# {file} · {fn}\n{src}")
    known = "\n".join(f"- {i['title']} ({i['file']}:{i['line']})" for i in insights) or "- none"
    ask = (
        "Architecture:\n"
        + "\n".join(lines[:400])
        + "\n\nKey functions:\n"
        + "\n\n".join(code)
        + "\n\nAlready found:\n"
        + known
    )
    reply = complete(
        cfg, DEEP_SYSTEM, [{"role": "user", "content": ask}], max_tokens=2500, purpose="architecture review"
    )
    if reply is None:
        from .llm import last_error

        return {"insights": [], "error": last_error() or "the model did not answer"}
    m_ = re.search(r"\[.*\]", reply, re.S)
    try:
        items = json.loads(m_.group(0)) if m_ else []
    except json.JSONDecodeError:
        items = []
    out = []
    for i, it in enumerate(x for x in items if isinstance(x, dict)):
        file = str(it.get("file") or "")
        if file and file not in m.g.files:
            continue  # it named a file that is not in the repo
        out.append(
            asdict(
                Insight(
                    f"ai-{i}",
                    it.get("severity") if it.get("severity") in ("high", "medium", "low") else "medium",
                    str(it.get("title") or "")[:200],
                    str(it.get("why") or "")[:1200],
                    str(it.get("fix") or "")[:1200],
                    file,
                    int(it.get("line") or 1) if str(it.get("line") or "1").isdigit() else 1,
                    source="ai",
                )
            )
        )
    return {"insights": [x for x in out if x["title"]], "model": cfg.model}
