"""More Google ADK intents: state, tools, MCP, history, per-request runners, deprecations, 2.x extras.

Like ``adk.py``, every detector here only looks at files that import ``google.adk``.
"Tool functions" are the plain functions a file passes in an agent's ``tools=[...]``
list, plus any function that takes a ``tool_context`` parameter.
"""

from __future__ import annotations

from tree_sitter import Node

from ..graph import ParsedFile, text
from ..models import Function, IntentMatch
from .adk import AGENT_CLASSES, imports_adk
from .detectors import ROUTE_SUFFIXES, SLEEP_CALLS, calls_in, detector, line, module_detector, walk

RUNNER_CLASSES = {"google.adk.Runner", "google.adk.runners.Runner", "google.adk.runners.InMemoryRunner"}
JSON_PARSERS = {"json.loads", "orjson.loads", "ujson.loads"}
MCP_CLIENTS = {
    "mcp.ClientSession",
    "mcp.client.session.ClientSession",
    "mcp.client.stdio.stdio_client",
    "mcp.client.sse.sse_client",
    "mcp.client.streamable_http.streamablehttp_client",
}
CONTEXT_NAMES = ("tool_context", "callback_context", "ctx", "context", "readonly_context", "invocation_context")

# Deprecated ADK APIs: (resolved call name suffix or keyword, replacement label).
DEPRECATED_CALLS = {
    "VertexAiCodeExecutor": "vertex_code_executor",
    "ApiRegistry": "api_registry",
}
# ADK features whose dependency is only installed by an extra: import prefix -> (extra, package).
# mcp, sqlalchemy and google-cloud-storage left the base install in 2.0; the others were always extras.
EXTRA_FOR_IMPORT = {
    "google.adk.tools.mcp_tool": ("mcp", "mcp"),
    "google.adk.sessions.DatabaseSessionService": ("db", "sqlalchemy"),
    "google.adk.sessions.database_session_service": ("db", "sqlalchemy"),
    "google.adk.artifacts.GcsArtifactService": ("gcp", "google-cloud-storage"),
    "google.adk.artifacts.gcs_artifact_service": ("gcp", "google-cloud-storage"),
    "google.adk.models.lite_llm": ("extensions", "litellm"),
    "google.adk.models.LiteLlm": ("extensions", "litellm"),
    "google.adk.models.anthropic_llm": ("extensions", "anthropic"),
    "google.adk.a2a": ("a2a", "a2a-sdk"),
    "google.adk.agents.remote_a2a_agent": ("a2a", "a2a-sdk"),
    "google.adk.agents.RemoteA2aAgent": ("a2a", "a2a-sdk"),
}


def _kwargs(call: Node) -> dict[str, Node]:
    args = call.child_by_field_name("arguments")
    out = {}
    for kw in args.named_children if args is not None else []:
        if kw.type == "keyword_argument" and (value := kw.child_by_field_name("value")) is not None:
            out[text(kw.child_by_field_name("name"))] = value
    return out


def _written(n: Node, methods: tuple[str, ...]) -> Node | None:
    """The object `n` writes into: `obj[k] = v`, `obj[k] += v` or `obj.<method>(...)` for a mutating method."""
    if n.type in ("assignment", "augmented_assignment"):
        left = n.child_by_field_name("left")
        return left.child_by_field_name("value") if left is not None and left.type == "subscript" else None
    f = n.child_by_field_name("function") if n.type == "call" else None
    if f is not None and f.type == "attribute" and text(f.child_by_field_name("attribute")) in methods:
        return f.child_by_field_name("object")
    return None


def _runner_run(call: Node) -> bool:
    from .adk import _runner_call

    return _runner_call(call) is not None


def tool_functions(pf: ParsedFile) -> set[str]:
    """Qualnames of functions this file hands to an agent as tools."""
    if "adk_tool_functions" in pf.cache:
        return pf.cache["adk_tool_functions"]  # type: ignore[return-value]
    names = set()
    for call, name in calls_in(pf.root, pf, into_defs=True):
        if name in AGENT_CLASSES and (tools := _kwargs(call).get("tools")) is not None and tools.type == "list":
            names.update(text(n) for n in tools.named_children if n.type in ("identifier", "attribute"))
    found = {fn.qualname for fn in pf.module.functions if fn.name in names or "tool_context" in fn.params}
    pf.cache["adk_tool_functions"] = found
    return found


def _module_fn(pf: ParsedFile, node: Node) -> Function:
    return Function("<module>", "<module>", pf.path, line(node), node.end_point[0] + 1, line(node))


# --------------------------------------------------------------------------- parsing the model's JSON by hand


@detector("adk-parse-model-json")
def detect_parse_model_json(pf, fn, node, graph):
    """`json.loads(event.content.parts[0].text)`: parsing the model's reply instead of using output_schema."""
    if not imports_adk(pf) or (body := node.child_by_field_name("body")) is None:
        return []
    # Only replies read from ADK events in a function that runs the agent: a direct genai call
    # with response_schema is already structured output, and parsing its text is right.
    if not any(n.type == "call" and _runner_run(n) for n in walk(body)):
        return []
    for call, name in calls_in(body, pf):
        if name not in JSON_PARSERS and not name.endswith(".model_validate_json"):
            continue
        args = call.child_by_field_name("arguments")
        arg = text(args) if args is not None else ""
        if "content.parts" in arg and ".text" in arg:
            return [
                IntentMatch(
                    "adk-parse-model-json",
                    fn,
                    line(call),
                    f"`{name}()` parses the agent's text reply into data by hand",
                    {},
                )
            ]
    return []


# --------------------------------------------------------------------------- session.state written directly


@detector("adk-direct-state-write")
def detect_direct_state_write(pf, fn, node, graph):
    """`session.state[k] = v` on a session object: ADK never records the change, so it is lost."""
    if not imports_adk(pf) or (body := node.child_by_field_name("body")) is None:
        return []
    for n in walk(body):
        target = _written(n, ("update", "setdefault", "pop", "clear"))
        if target is None or target.type != "attribute" or text(target.child_by_field_name("attribute")) != "state":
            continue
        owner = text(target.child_by_field_name("object"))
        segments = owner.split(".")
        # `invocation_context.session.state` inside a plugin or callback is the live session of the
        # current run; flag only sessions fetched from a session service.
        if "session" in segments[-1].lower() and not any(seg in CONTEXT_NAMES for seg in segments):
            return [
                IntentMatch(
                    "adk-direct-state-write",
                    fn,
                    line(n),
                    f"`{owner}.state` changed directly on a session object, outside any event",
                    {},
                )
            ]
    return []


# --------------------------------------------------------------------------- module dict used as memory by tools


@module_detector("adk-module-dict-state")
def detect_module_dict_state(pf: ParsedFile) -> list[IntentMatch]:
    """A tool that stores data in a module-level dict: lost on restart, shared by every user."""
    if not imports_adk(pf):
        return []
    tools = tool_functions(pf)
    if not tools:
        return []
    dicts = set()
    for child in pf.root.children:
        expr = child.named_children[0] if child.type == "expression_statement" and child.named_children else None
        if expr is not None and expr.type == "assignment":
            left, right = expr.child_by_field_name("left"), expr.child_by_field_name("right")
            is_dict = right is not None and (
                right.type == "dictionary" or text(right) in ("dict()", "defaultdict(list)", "defaultdict(dict)")
            )
            if left is not None and left.type == "identifier" and is_dict:
                dicts.add(text(left))
    if not dicts:
        return []
    for fn in pf.module.functions:
        node = pf.nodes.get(fn.qualname)
        if fn.qualname not in tools or node is None:
            continue
        for n in walk(node):
            obj = _written(n, ("update", "setdefault", "append", "extend"))
            written = text(obj).split("[", 1)[0] if obj is not None else None
            if written in dicts:
                return [
                    IntentMatch(
                        "adk-module-dict-state",
                        fn,
                        line(n),
                        f"tool `{fn.name}` keeps data in the module-level dict `{written}`",
                        {"dict": written},
                    )
                ]
    return []


# --------------------------------------------------------------------------- a tool that runs another agent


@module_detector("adk-tool-runs-agent")
def detect_tool_runs_agent(pf: ParsedFile) -> list[IntentMatch]:
    """A tool function that builds a Runner or runs an agent itself."""
    if not imports_adk(pf):
        return []
    out = []
    for fn in pf.module.functions:
        node = pf.nodes.get(fn.qualname)
        if fn.qualname not in tool_functions(pf) or node is None:
            continue
        for call, name in calls_in(node, pf):
            if name in RUNNER_CLASSES or (name.endswith((".run_async", ".run")) and "runner" in name.lower()):
                out.append(
                    IntentMatch("adk-tool-runs-agent", fn, line(call), f"tool `{fn.name}` runs an agent itself", {})
                )
                break
    return out


# --------------------------------------------------------------------------- hand-written MCP client


@detector("adk-hand-mcp-client")
def detect_hand_mcp_client(pf, fn, node, graph):
    """An MCP client session opened by hand in an ADK app."""
    if not imports_adk(pf) or (body := node.child_by_field_name("body")) is None:
        return []
    for call, name in calls_in(body, pf):
        if name in MCP_CLIENTS:
            return [
                IntentMatch(
                    "adk-hand-mcp-client",
                    fn,
                    line(call),
                    f"opens an MCP connection by hand with `{name.rsplit('.', 1)[-1]}`",
                    {},
                )
            ]
    return []


# --------------------------------------------------------------------------- trimming history by hand


@detector("adk-history-slice")
def detect_history_slice(pf, fn, node, graph):
    """`session.events[-20:]`: trimming conversation history by hand."""
    if not imports_adk(pf) or (body := node.child_by_field_name("body")) is None:
        return []
    for n in walk(body):
        if n.type != "subscript":
            continue
        value = n.child_by_field_name("value")
        sub = n.child_by_field_name("subscript")
        if (
            value is not None
            and value.type == "attribute"
            and text(value.child_by_field_name("attribute")) == "events"
            and sub is not None
            and sub.type == "slice"
        ):
            return [
                IntentMatch(
                    "adk-history-slice",
                    fn,
                    line(n),
                    f"`{text(n)}` trims the conversation history by hand",
                    {},
                )
            ]
    return []


# --------------------------------------------------------------------------- runner built per request


@detector("adk-runner-per-request")
def detect_runner_per_request(pf, fn, node, graph):
    """A web route that builds the Runner (or the agent) on every request."""
    if not imports_adk(pf) or (body := node.child_by_field_name("body")) is None:
        return []
    if not any(d.split("(", 1)[0].endswith(ROUTE_SUFFIXES) for d in fn.decorators):
        return []
    for call, name in calls_in(body, pf):
        if name in RUNNER_CLASSES or name in AGENT_CLASSES:
            cls = name.rsplit(".", 1)[-1]
            return [
                IntentMatch(
                    "adk-runner-per-request",
                    fn,
                    line(call),
                    f"route `@{fn.decorators[0]}` builds a new {cls} on every request",
                    {"cls": cls},
                )
            ]
    return []


# --------------------------------------------------------------------------- a tool that polls


@module_detector("adk-polling-tool")
def detect_polling_tool(pf: ParsedFile) -> list[IntentMatch]:
    """A tool that waits for a job with a sleep loop, holding the whole turn open."""
    if not imports_adk(pf):
        return []
    out = []
    for fn in pf.module.functions:
        node = pf.nodes.get(fn.qualname)
        if fn.qualname not in tool_functions(pf) or node is None:
            continue
        for loop in walk(node):
            if loop.type in ("for_statement", "while_statement") and any(
                name in SLEEP_CALLS for _, name in calls_in(loop, pf)
            ):
                out.append(
                    IntentMatch(
                        "adk-polling-tool",
                        fn,
                        line(loop),
                        f"tool `{fn.name}` polls in a sleep loop until a job finishes",
                        {},
                    )
                )
                break
    return out


# --------------------------------------------------------------------------- deprecated APIs


@module_detector("adk-deprecated-api")
def detect_deprecated_api(pf: ParsedFile) -> list[IntentMatch]:
    """Calls ADK has deprecated, each mapped to a card by data["api"]."""
    if not imports_adk(pf):
        return []
    out, seen = [], set()

    def hit(api: str, node: Node, evidence: str) -> None:
        if api not in seen:
            seen.add(api)
            ln = line(node)
            fn = next(
                (
                    f
                    for f in sorted(pf.module.functions, key=lambda f: -f.start_line)
                    if f.start_line <= ln <= f.end_line
                ),
                _module_fn(pf, node),
            )
            out.append(IntentMatch("adk-deprecated-api", fn, ln, evidence, {"api": api}))

    for call, name in calls_in(pf.root, pf, into_defs=True):
        if not name.startswith("google.adk"):
            continue
        cls = name.rsplit(".", 1)[-1]
        kwargs = _kwargs(call)
        if name in AGENT_CLASSES and "global_instruction" in kwargs:
            hit("global_instruction", kwargs["global_instruction"], "`global_instruction=` on an agent is deprecated")
        if name in RUNNER_CLASSES and "plugins" in kwargs:
            hit("runner_plugins", kwargs["plugins"], "`plugins=` on the Runner is deprecated")
        if cls == "RunConfig" and "save_live_audio" in kwargs:
            hit("save_live_audio", kwargs["save_live_audio"], "`RunConfig(save_live_audio=...)` is deprecated")
            name = kwargs["save_live_audio"].parent.child_by_field_name("name")  # the keyword itself
            out[-1].data |= {"edits": [(name.start_byte, name.end_byte, "save_live_blob")], "imports": []}
        if cls in DEPRECATED_CALLS:
            hit(DEPRECATED_CALLS[cls], call, f"`{cls}` is deprecated")
    return out


# --------------------------------------------------------------------------- google-adk 2.x extras


@module_detector("adk-missing-extra")
def detect_missing_extra(pf: ParsedFile) -> list[IntentMatch]:
    """Imports of ADK features whose dependencies google-adk 2.x only installs with an extra.

    The match fires on the import alone; the card decides whether the lockfile lacks the package.
    """
    out, seen = [], set()
    for child in pf.root.children:
        if child.type not in ("import_statement", "import_from_statement"):
            continue
        for target in _import_targets(child):
            hit = next(
                (v for prefix, v in EXTRA_FOR_IMPORT.items() if target == prefix or target.startswith(prefix + ".")),
                None,
            )
            if hit and hit[1] not in seen:
                extra, package = hit
                seen.add(package)
                out.append(
                    IntentMatch(
                        "adk-missing-extra",
                        _module_fn(pf, child),
                        line(child),
                        f"imports `{target}`, which needs {package} (`google-adk[{extra}]`)",
                        {"extra": extra, "package": package},
                    )
                )
    return out


def _import_targets(node: Node) -> list[str]:
    if node.type == "import_statement":
        return [text(c.child_by_field_name("name") or c) for c in node.named_children]
    module = node.child_by_field_name("module_name")
    base = text(module)
    names = [
        text(c.child_by_field_name("name") or c)
        for c in node.named_children
        if c.type in ("dotted_name", "aliased_import") and (module is None or c.start_byte != module.start_byte)
    ]
    return [f"{base}.{n}" for n in names] or [base]


# --------------------------------------------------------------------------- upgrading from 1.x


@module_detector("adk-upgrade")
def detect_adk_app(pf: ParsedFile) -> list[IntentMatch]:
    """A module that defines an ADK `root_agent`: the place to report a google-adk 1.x install.

    The match fires on any ADK app; the card's version range (<2.0) decides whether it is shown.
    """
    if not imports_adk(pf):
        return []
    for child in pf.root.children:
        expr = child.named_children[0] if child.type == "expression_statement" and child.named_children else None
        if expr is not None and expr.type == "assignment" and text(expr.child_by_field_name("left")) == "root_agent":
            return [IntentMatch("adk-upgrade", _module_fn(pf, child), line(child), "defines an ADK root_agent", {})]
    return []


# --------------------------------------------------------------------------- 2.x pitfalls


@detector("adk-session-events-append")
def detect_session_events_append(pf, fn, node, graph):
    """`ctx.session.events.append(event)`: appending events by hand bypasses the 2.x graph engine."""
    if not imports_adk(pf) or (body := node.child_by_field_name("body")) is None:
        return []
    for call, _ in calls_in(body, pf):
        f = call.child_by_field_name("function")
        if f is None or f.type != "attribute" or text(f.child_by_field_name("attribute")) not in ("append", "extend"):
            continue
        obj = f.child_by_field_name("object")
        if obj is not None and obj.type == "attribute" and text(obj.child_by_field_name("attribute")) == "events":
            owner = text(obj.child_by_field_name("object"))
            if "session" in owner.rsplit(".", 1)[-1].lower():
                return [
                    IntentMatch(
                        "adk-session-events-append",
                        fn,
                        line(call),
                        f"`{owner}.events.{text(f.child_by_field_name('attribute'))}()` adds events by hand",
                        {},
                    )
                ]
    return []


@module_detector("adk-tool-catches-baseexception")
def detect_tool_catches_baseexception(pf: ParsedFile) -> list[IntentMatch]:
    """A tool whose `except BaseException` / bare `except:` swallows the error instead of re-raising it."""
    if not imports_adk(pf):
        return []
    out = []
    for fn in pf.module.functions:
        node = pf.nodes.get(fn.qualname)
        if fn.qualname not in tool_functions(pf) or node is None:
            continue
        for clause in walk(node):
            if clause.type != "except_clause":
                continue
            caught = [c for c in clause.named_children if c.type != "block"]
            names = text(caught[0]) if caught else ""
            broad = not caught or "BaseException" in names
            reraises = any(n.type == "raise_statement" for n in walk(clause))
            if broad and not reraises:
                what = "a bare `except:`" if not caught else "`except BaseException`"
                out.append(
                    IntentMatch(
                        "adk-tool-catches-baseexception",
                        fn,
                        line(clause),
                        f"tool `{fn.name}` catches {what} without re-raising",
                        {},
                    )
                )
                break
    return out
