"""Google ADK (google-adk) intents: code that hand-builds what ADK already provides.

Every detector here only looks at files that import ``google.adk``. Where the fix is
mechanical (``Runner.run`` -> ``run_async`` in a ``for`` loop, an f-string instruction
-> an instruction provider) the byte edits go in ``data["edits"]`` like the other
auto-patching detectors.
"""

from __future__ import annotations

from tree_sitter import Node

from ..graph import ParsedFile, text
from ..models import Function, IntentMatch
from .detectors import ROUTE_SUFFIXES, calls_in, detector, line, module_detector, walk

AGENT_CLASSES = {
    "google.adk.Agent",
    "google.adk.agents.Agent",
    "google.adk.agents.LlmAgent",
    "google.adk.agents.llm_agent.Agent",
    "google.adk.agents.llm_agent.LlmAgent",
}
IN_MEMORY_SERVICES = {  # constructor -> (class, what it keeps in memory)
    "google.adk.sessions.InMemorySessionService": ("InMemorySessionService", "sessions"),
    "google.adk.sessions.in_memory_session_service.InMemorySessionService": ("InMemorySessionService", "sessions"),
    "google.adk.runners.InMemoryRunner": ("InMemoryRunner", "sessions"),
    "google.adk.artifacts.InMemoryArtifactService": ("InMemoryArtifactService", "artifacts"),
    "google.adk.artifacts.in_memory_artifact_service.InMemoryArtifactService": ("InMemoryArtifactService", "artifacts"),
    "google.adk.memory.InMemoryMemoryService": ("InMemoryMemoryService", "memory"),
    "google.adk.memory.in_memory_memory_service.InMemoryMemoryService": ("InMemoryMemoryService", "memory"),
}
WEB_FRAMEWORKS = ("fastapi", "flask", "starlette", "django", "quart", "aiohttp", "litestar", "sanic")


def imports_adk(pf: ParsedFile) -> bool:
    return any(v.startswith("google.adk") for v in pf.module.imports.values())


def _runner_call(call: Node) -> tuple[Node, Node] | None:
    """(object, attribute) for a `<something runner>.run(...)` / `.run_async(...)` call."""
    callee = call.child_by_field_name("function")
    if callee is None or callee.type != "attribute":
        return None
    obj, attr = callee.child_by_field_name("object"), callee.child_by_field_name("attribute")
    if obj is None or attr is None or text(attr) not in ("run", "run_async"):
        return None
    if "runner" not in text(obj).rsplit(".", 1)[-1].lower():
        return None
    return obj, attr


def _innermost_function(pf: ParsedFile, ln: int) -> Function:
    inside = [f for f in pf.module.functions if f.start_line <= ln <= f.end_line]
    if inside:
        return max(inside, key=lambda f: f.start_line)
    return Function("<module>", "<module>", pf.path, ln, ln, ln)


# --------------------------------------------------------------------------- Runner.run in async code


@detector("adk-sync-run")
def detect_sync_run_in_async(pf, fn, node, graph):
    """`runner.run(...)` inside `async def`: the sync wrapper blocks the event loop for the whole turn."""
    if not fn.is_async or not imports_adk(pf) or (body := node.child_by_field_name("body")) is None:
        return []
    for call in walk(body):
        if call.type != "call" or (hit := _runner_call(call)) is None or text(hit[1]) != "run":
            continue
        data: dict = {}
        loop = call.parent
        if (
            loop is not None
            and loop.type == "for_statement"
            and (right := loop.child_by_field_name("right")) is not None
            and right.start_byte == call.start_byte
            and not text(loop).startswith("async")
        ):
            attr = hit[1]
            data = {
                "edits": [(loop.start_byte, loop.start_byte, "async "), (attr.start_byte, attr.end_byte, "run_async")],
                "imports": [],
            }
        return [
            IntentMatch(
                "adk-sync-run",
                fn,
                line(call),
                f"`{text(call.child_by_field_name('function'))}()` is ADK's sync wrapper, called inside async def",
                data,
            )
        ]
    return []


# --------------------------------------------------------------------------- hand-written API server


@detector("adk-hand-server")
def detect_hand_server(pf, fn, node, graph):
    """A web route that drives an ADK Runner itself."""
    if not imports_adk(pf) or (body := node.child_by_field_name("body")) is None:
        return []
    if not any(d.split("(", 1)[0].endswith(ROUTE_SUFFIXES) for d in fn.decorators):
        return []
    for call in walk(body):
        if call.type == "call" and _runner_call(call) is not None:
            return [
                IntentMatch(
                    "adk-hand-server",
                    fn,
                    line(call),
                    f"route `@{fn.decorators[0]}` runs the agent with a hand-written endpoint",
                    {},
                )
            ]
    return []


# --------------------------------------------------------------------------- in-memory services in a server


def serves_requests(pf: ParsedFile) -> bool:
    return any(v.split(".", 1)[0] in WEB_FRAMEWORKS for v in pf.module.imports.values()) or any(
        d.split("(", 1)[0].endswith(ROUTE_SUFFIXES) for f in pf.module.functions for d in f.decorators
    )


@module_detector("adk-in-memory-services")
def detect_in_memory_services(pf: ParsedFile) -> list[IntentMatch]:
    """In-memory session, artifact or memory services in code that serves requests (one finding per kind)."""
    if not imports_adk(pf) or not serves_requests(pf):
        return []
    out, seen = [], set()
    what = {"sessions": "every session", "artifacts": "every saved file", "memory": "all long-term memory"}
    for call, name in calls_in(pf.root, pf, into_defs=True):
        hit = IN_MEMORY_SERVICES.get(name)
        if hit is None or hit[1] in seen:
            continue
        cls, kind = hit
        seen.add(kind)
        ln = line(call)
        out.append(
            IntentMatch(
                "adk-in-memory-services",
                _innermost_function(pf, ln),
                ln,
                f"{cls} keeps {what[kind]} in this process's memory, in a module that serves requests",
                {"service": cls, "kind": kind},
            )
        )
    return out


# --------------------------------------------------------------------------- instructions frozen at import


@module_detector("adk-frozen-instruction")
def detect_frozen_instruction(pf: ParsedFile) -> list[IntentMatch]:
    """A module-level agent whose f-string instruction calls something (a date, a config read)."""
    if not imports_adk(pf):
        return []
    out = []
    for call, name in calls_in(pf.root, pf):  # module scope only: a factory function re-runs its f-string
        if name not in AGENT_CLASSES:
            continue
        args = call.child_by_field_name("arguments")
        for kw in args.named_children if args is not None else []:
            if kw.type != "keyword_argument" or text(kw.child_by_field_name("name")) != "instruction":
                continue
            value = kw.child_by_field_name("value")
            calls = _interpolated_calls(value)
            if not calls:
                continue
            data: dict = {"calls": calls}
            if "{{" not in text(value):  # `{{key}}` is an ADK state placeholder a provider would not fill
                data |= {"edits": [(value.start_byte, value.start_byte, "lambda ctx: ")], "imports": []}
            ln = line(value)
            out.append(
                IntentMatch(
                    "adk-frozen-instruction",
                    Function("<module>", "<module>", pf.path, line(call), call.end_point[0] + 1, line(call)),
                    ln,
                    f"f-string instruction calls {calls[0]} once, when the module is imported",
                    data,
                )
            )
    return out


def _interpolated_calls(value: Node | None) -> list[str]:
    if value is None or value.type != "string":
        return []
    start = value.children[0] if value.children else None
    if start is None or "f" not in text(start).lower().split("'")[0].split('"')[0]:
        return []
    found = []
    for interp in value.children:
        if interp.type == "interpolation":
            found += [text(n) for n in walk(interp) if n.type == "call"]
    return found
