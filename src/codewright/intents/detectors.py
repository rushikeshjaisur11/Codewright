"""Structural intent detectors.

Each detector looks at one function's tree-sitter node and reports an
``IntentMatch`` when the code hand-implements a known engineering intent
(retry, cache, queue, pool, orchestrator...). Detectors are deliberately
conservative: a missed intent costs nothing, a wrong one costs trust.

v0 ships rule-based detectors. An LLM-backed detector can be registered the
same way later; the critic and proof stages treat both identically.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

from tree_sitter import Node

from ..graph import CodeGraph, ParsedFile, is_test_file, resolve, text
from ..models import Function, IntentMatch

Detector = Callable[[ParsedFile, Function, Node, CodeGraph], list[IntentMatch]]
ModuleDetector = Callable[[ParsedFile], list[IntentMatch]]
DETECTORS: dict[str, Detector] = {}
# Words a function's source must contain for a detector to possibly match (any one of them).
# Functions without them are skipped before any tree walking: most functions never mention
# sleep, utcnow or os.system, and on large repos this skips the large majority of the work.
TRIGGERS: dict[str, tuple[bytes, ...]] = {}
MODULE_DETECTORS: dict[str, ModuleDetector] = {}  # run once per file, for code at module scope
GraphDetector = Callable[[CodeGraph], list[IntentMatch]]
GRAPH_DETECTORS: dict[str, GraphDetector] = {}  # run once per review, over the whole code graph

SLEEP_CALLS = {"time.sleep", "asyncio.sleep", "trio.sleep", "anyio.sleep"}
CONNECT_CALLS = {
    "sqlite3.connect": "sqlite3",
    "psycopg.connect": "psycopg",
    "psycopg.AsyncConnection.connect": "psycopg",
    "psycopg2.connect": "psycopg2",
    "asyncpg.connect": "asyncpg",
    "pymysql.connect": "pymysql",
    "sqlalchemy.create_engine": "sqlalchemy",
    "sqlalchemy.ext.asyncio.create_async_engine": "sqlalchemy",
    "redis.Redis": "redis",
    "redis.asyncio.Redis": "redis",
    "httpx.Client": "httpx",
    "httpx.AsyncClient": "httpx",
    "requests.Session": "requests",
}
ROUTE_SUFFIXES = (".get", ".post", ".put", ".patch", ".delete", ".api_route", ".route", ".websocket")
ADK_DEPRECATED = ("SequentialAgent", "ParallelAgent", "LoopAgent")


def detector(intent: str, triggers: tuple[str, ...] = ()):
    """Register a function-level detector. `triggers` must be necessary: a function whose source
    contains none of them (nor a name imported from something containing them) can't match."""

    def wrap(fn: Detector) -> Detector:
        DETECTORS[intent] = fn
        if triggers:
            TRIGGERS[intent] = tuple(t.encode() for t in triggers)
        return fn

    return wrap


def module_detector(intent: str):
    def wrap(fn: ModuleDetector) -> ModuleDetector:
        MODULE_DETECTORS[intent] = fn
        return fn

    return wrap


def graph_detector(intent: str):
    def wrap(fn: GraphDetector) -> GraphDetector:
        GRAPH_DETECTORS[intent] = fn
        return fn

    return wrap


def all_intents() -> set[str]:
    return set(DETECTORS) | set(MODULE_DETECTORS) | set(GRAPH_DETECTORS)


def walk(node: Node, *, into_defs: bool = False) -> Iterator[Node]:
    """Depth-first walk that, by default, does not descend into nested defs/classes."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        for child in reversed(n.children):
            if not into_defs and child.type in ("function_definition", "class_definition", "decorated_definition"):
                continue
            stack.append(child)


def calls_in(node: Node, pf: ParsedFile, *, into_defs: bool = False) -> Iterator[tuple[Node, str]]:
    for n in walk(node, into_defs=into_defs):
        if n.type == "call":
            f = n.child_by_field_name("function")
            if f is not None and f.type in ("identifier", "attribute"):
                yield n, resolve(pf.module, text(f))


def line(node: Node) -> int:
    return node.start_point[0] + 1


def snippet(pf: ParsedFile, start: int, end: int, limit: int = 12) -> str:
    lines = pf.lines[start - 1 : end]
    if len(lines) > limit:
        lines = [*lines[:limit], "    ...\n"]
    return "".join(lines).rstrip("\n")


def module_level_names(pf: ParsedFile) -> set[str]:
    """Names assigned at module scope. Computed once per file: detectors call it for every function."""
    if "module_level_names" not in pf.cache:
        pf.cache["module_level_names"] = _module_level_names(pf)
    return pf.cache["module_level_names"]  # type: ignore[return-value]


def _module_level_names(pf: ParsedFile) -> set[str]:
    names = set()
    for child in pf.root.children:
        if child.type == "expression_statement" and child.named_children:
            expr = child.named_children[0]
            if expr.type == "assignment":
                left = expr.child_by_field_name("left")
                if left is not None and left.type == "identifier":
                    names.add(text(left))
    return names


# --------------------------------------------------------------------------- retry


@detector("retry", triggers=("sleep",))
def detect_retry(pf, fn, node, graph):
    body = node.child_by_field_name("body")
    if body is None:
        return []
    for loop in walk(body):
        if loop.type not in ("for_statement", "while_statement"):
            continue
        has_try = any(n.type == "try_statement" for n in walk(loop))
        names = [name for _, name in calls_in(loop, pf)]
        sleeps = [name for name in names if name in SLEEP_CALLS]
        if has_try and sleeps:
            # Retrying a whole agent turn through a Runner: the Gemini client can retry just the request.
            # (A loop inside a custom model class, around another provider's SDK, is not that.)
            model_call = _imports_adk(pf) and any(
                n.endswith((".run_async", ".run")) and "runner" in n.rsplit(".", 2)[-2].lower()
                for n in names
                if n.count(".") >= 1
            )
            return [
                IntentMatch(
                    "retry",
                    fn,
                    line(loop),
                    f"loop with try/except and {sleeps[0]}() re-runs a failing call",
                    {
                        "sleep": sleeps[0],
                        "async": fn.is_async,
                        **({"adk": True} if model_call else {}),
                        **({"celery": True} if is_celery_task(fn, pf) else {}),
                    },
                )
            ]
    return []


# --------------------------------------------------------------------------- memo cache


@detector("memo-cache", triggers=(" in ",))
def detect_memo_cache(pf, fn, node, graph):
    """`if key in CACHE: return CACHE[key]` ... `CACHE[key] = value` on a module-level dict."""
    body = node.child_by_field_name("body")
    if body is None:
        return []
    globals_ = module_level_names(pf)
    for stmt in body.named_children:
        if stmt.type != "if_statement" or stmt.child_by_field_name("alternative") is not None:
            continue
        cond = stmt.child_by_field_name("condition")
        if cond is None or cond.type != "comparison_operator" or " in " not in f" {text(cond)} ":
            continue
        operands = cond.named_children
        if len(operands) != 2 or text(operands[1]) not in globals_:
            continue
        key, cache = text(operands[0]), text(operands[1])
        consequence = stmt.child_by_field_name("consequence")
        if consequence is None or len(consequence.named_children) != 1:
            continue
        ret = consequence.named_children[0]
        if ret.type != "return_statement" or text(ret).replace(" ", "") != f"return{cache}[{key}]":
            continue
        store = None
        for s in body.named_children:
            t = text(s).replace(" ", "")
            if s.type == "expression_statement" and t.startswith(f"{cache}[{key}]=") and "\n" not in t:
                store = s
        if store is None:
            continue
        return [
            IntentMatch(
                "memo-cache",
                fn,
                line(stmt),
                f"module-level dict `{cache}` used as an unbounded memo cache keyed on `{key}`",
                {
                    "cache": cache,
                    "key": key,
                    "check_lines": (line(stmt), stmt.end_point[0] + 1),
                    "store_lines": (line(store), store.end_point[0] + 1),
                    "store_value": text(store).split("=", 1)[1].strip(),
                    "params": fn.params,
                },
            )
        ]
    return []


# --------------------------------------------------------------------------- polling queue


@detector("poll-queue", triggers=("sleep",))
def detect_poll_queue(pf, fn, node, graph):
    body = node.child_by_field_name("body")
    if body is None:
        return []
    for loop in walk(body):
        if loop.type != "while_statement":
            continue
        cond = text(loop.child_by_field_name("condition"))
        if cond not in ("True", "1", "not stop", "running", "self.running"):
            continue
        names = [n for _, n in calls_in(loop, pf)]
        sleeps = [n for n in names if n in SLEEP_CALLS]
        queries = [n for n in names if n.endswith((".execute", ".fetch", ".fetchrow", ".fetchall", ".fetchone"))]
        sql = text(loop).upper()
        if sleeps and queries and "SELECT" in sql:
            return [
                IntentMatch(
                    "poll-queue",
                    fn,
                    line(loop),
                    f"`while {cond}` loop polls the database with SELECT and {sleeps[0]}()",
                    {"skip_locked": "SKIP LOCKED" in sql},
                )
            ]
    return []


# --------------------------------------------------------------------------- connection per request


@detector("connection-per-request")
def detect_connection_per_request(pf, fn, node, graph):
    is_route = any(d.split("(", 1)[0].endswith(ROUTE_SUFFIXES) for d in fn.decorators)
    if not is_route:
        return []
    body = node.child_by_field_name("body")
    if body is None:
        return []
    for call, name in calls_in(body, pf):
        if name in CONNECT_CALLS:
            return [
                IntentMatch(
                    "connection-per-request",
                    fn,
                    line(call),
                    f"request handler opens a new client/connection with {name}() on every request",
                    {"call": name, "library": CONNECT_CALLS[name]},
                )
            ]
    return []


# --------------------------------------------------------------------------- ADK orchestration


def is_celery_task(fn: Function, pf: ParsedFile) -> bool:
    for deco in fn.decorators:
        head = deco.split("(", 1)[0]
        target = resolve(pf.module, head)
        if target.startswith("celery") and target.endswith(("shared_task", ".task")):
            return True
        if head.endswith(".task") and any(v.startswith("celery") for v in pf.module.imports.values()):
            return True  # @app.task where app = Celery(...)
    return False


def _imports_adk(pf: ParsedFile) -> bool:
    return any(v.startswith("google.adk") for v in pf.module.imports.values())


@detector("agent-orchestrator")
def detect_hand_orchestrator(pf, fn, node, graph):
    """A function that drives several ADK agents itself instead of using a workflow agent."""
    if not _imports_adk(pf):
        return []
    body = node.child_by_field_name("body")
    if body is None:
        return []
    runs = [c for c, name in calls_in(body, pf) if name.endswith(".run_async") or name.endswith(".run")]
    in_loop = any(_in_loop_body(c, body) for c in runs)
    if len(runs) >= 2 or (runs and in_loop):
        return [
            IntentMatch(
                "agent-orchestrator",
                fn,
                line(runs[0]),
                "hand-written loop drives agents one after another"
                if in_loop
                else f"{len(runs)} agent runs chained by hand",
                {"runs": len(runs), "loop": in_loop},
            )
        ]
    return []


def _in_loop_body(call: Node, stop: Node) -> bool:
    """True when `call` runs once per iteration of a loop, not when it is the loop's own iterable.

    `for event in runner.run_async(...)` reads one run's events; `for step in steps: await step.run(...)`
    drives several runs.
    """
    for p in _ancestors(call, stop):
        if p.type in ("for_statement", "while_statement"):
            body = p.child_by_field_name("body")
            if body is not None and body.start_byte <= call.start_byte < body.end_byte:
                return True
    return False


def _ancestors(node: Node, stop: Node) -> Iterator[Node]:
    p = node.parent
    while p is not None and p != stop:
        yield p
        p = p.parent


@module_detector("agent-orchestrator")
def detect_module_level(pf: ParsedFile) -> list[IntentMatch]:
    """Module-scope intents (e.g. a deprecated ADK agent class assigned to root_agent)."""
    out: list[IntentMatch] = []
    deprecated = {
        alias: target
        for alias, target in pf.module.imports.items()
        if target.startswith("google.adk") and target.rsplit(".", 1)[-1] in ADK_DEPRECATED
    }
    if not deprecated:
        return out
    seen: set[str] = set()
    for call, name in calls_in(pf.root, pf):
        cls = name.rsplit(".", 1)[-1]
        if cls in ADK_DEPRECATED and name.startswith("google.adk") and cls not in seen:
            seen.add(cls)
            ln = line(call)
            fn = Function("<module>", "<module>", pf.path, ln, call.end_point[0] + 1, ln)
            out.append(
                IntentMatch(
                    "agent-orchestrator",
                    fn,
                    ln,
                    f"uses {cls}, a fixed workflow agent class",
                    {"deprecated_class": cls},
                )
            )
    return out


def _file_triggers(pf: ParsedFile) -> dict[str, tuple[bytes, ...]]:
    """Each detector's trigger words, plus local names this file imports from something matching them
    (`from time import sleep as pause` makes `pause` a trigger for the sleep detectors)."""
    out = {}
    for intent, words in TRIGGERS.items():
        aliases = tuple(
            alias.encode()
            for alias, target in pf.module.imports.items()
            if any(w.decode() in target for w in words) and alias.encode() not in words
        )
        out[intent] = words + aliases
    return out


def detect(graph: CodeGraph, only: set[str] | None = None) -> list[IntentMatch]:
    matches: list[IntentMatch] = []
    file_triggers: dict[str, dict[str, tuple[bytes, ...]]] = {}
    for pf, fn in graph.functions():
        if is_test_file(pf.path):
            continue
        node = pf.nodes.get(fn.qualname)
        if node is None:
            continue
        if pf.path not in file_triggers:
            file_triggers[pf.path] = _file_triggers(pf)
        triggers = file_triggers[pf.path]
        source = pf.source[node.start_byte : node.end_byte]
        for intent, det in DETECTORS.items():
            if only and intent not in only:
                continue
            words = triggers.get(intent)
            if words is not None and not any(w in source for w in words):
                continue
            matches.extend(det(pf, fn, node, graph))
    for pf in graph.files.values():
        if is_test_file(pf.path):
            continue
        for intent, mdet in MODULE_DETECTORS.items():
            if not only or intent in only:
                matches.extend(mdet(pf))
    for intent, gdet in GRAPH_DETECTORS.items():
        if not only or intent in only:
            matches.extend(gdet(graph))
    return matches
