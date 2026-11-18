"""Structural intent detectors.

Each detector looks at one function's tree-sitter node and reports an
``IntentMatch`` when the code hand-implements a known engineering intent
(retry, cache, queue, pool, orchestrator...). Detectors are deliberately
conservative: a missed intent costs nothing, a wrong one costs trust.

v0 ships rule-based detectors. An LLM-backed detector can be registered the
same way later; the critic and proof stages treat both identically.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator

from tree_sitter import Node, Query, QueryCursor

from ..graph import PY, CodeGraph, ParsedFile, is_test_file, resolve, text
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


USES_GRAPH: set[str] = set()  # function detectors that read other files (hot paths): never cached per file
# intent -> fn(match, graph) -> match or None: the part of a detector that needs the whole graph (is it on a hot
# path?), applied after the per-file part, which can then be cached and run while the graph is being built.
GRAPH_FILTERS: dict[str, Callable[[IntentMatch, CodeGraph], IntentMatch | None]] = {}


def graph_filter(intent: str):
    def wrap(fn):
        GRAPH_FILTERS[intent] = fn
        return fn

    return wrap


def detector(intent: str, triggers: tuple[str, ...] = (), uses_graph: bool = False):
    """Register a function-level detector. `triggers` must be necessary: a function whose source
    contains none of them (nor a name imported from something containing them) can't match. Set
    `uses_graph` when the result depends on other files (callers, hot paths): it then runs on every review
    instead of being reused while the file is unchanged."""

    def wrap(fn: Detector) -> Detector:
        DETECTORS[intent] = fn
        if uses_graph:
            USES_GRAPH.add(intent)
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


_QUERIES: dict[tuple[str, ...], Query] = {}


def nodes(pf: ParsedFile, *types: str) -> list[Node]:
    """Every node of these types anywhere in the file (nested definitions included), in the order a
    depth-first walk meets them. One native query per file and type set, cached on the file: several
    detectors scan whole files, and walking every node in Python was most of detection on big repos."""
    key = ("nodes", *types)
    cache = pf.tree_cache  # syntax nodes: kept with the tree, dropped with it
    hit = cache.get(key)
    if hit is None:
        q = _QUERIES.get(types)
        if q is None:
            q = _QUERIES[types] = Query(PY, "[" + " ".join(f"({t})" for t in types) + "] @n")
        found = QueryCursor(q).captures(pf.root).get("n", [])
        hit = cache[key] = sorted(found, key=lambda n: (n.start_byte, -n.end_byte))
    return hit


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
    hit = pf.cache.get("imports_adk")
    if hit is None:
        hit = pf.cache["imports_adk"] = any(v.startswith("google.adk") for v in pf.module.imports.values())
    return hit


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


def file_hash(pf: ParsedFile) -> str:
    h = pf.cache.get("sha1")
    if h is None:
        h = pf.cache["sha1"] = hashlib.sha1(pf.source).hexdigest()
    return h


def detect_file(
    pf: ParsedFile, graph: CodeGraph | None, only: set[str] | None = None, local: bool | None = None
) -> list[IntentMatch]:
    """Function and module detectors for one file. With `local=True` only those that read just this file
    (their result can be reused while its bytes are the same); with `local=False` only those that read the
    graph (USES_GRAPH); with None, all."""
    matches: list[IntentMatch] = []
    triggers = _file_triggers(pf)
    for fn in pf.module.functions:
        node = pf.nodes.get(fn.qualname)
        if node is None:
            continue
        source = pf.source[node.start_byte : node.end_byte]
        for intent, det in DETECTORS.items():
            if only and intent not in only:
                continue
            if local is not None and (intent in USES_GRAPH) == local:
                continue
            words = triggers.get(intent)
            if words is not None and not any(w in source for w in words):
                continue
            matches.extend(det(pf, fn, node, graph))
    if local is not False:
        for intent, mdet in MODULE_DETECTORS.items():
            if not only or intent in only:
                matches.extend(mdet(pf))
    return matches


def fill_cache(cache: dict[str, dict]):
    """A graph-build visitor: detect each new or edited file while its tree is in memory (see graph.on_parse),
    so detection does not need the tree again later."""

    def visit(pf: ParsedFile) -> None:
        if is_test_file(pf.path):
            return
        h = file_hash(pf)
        hit = cache.get(pf.path)
        if hit is None or hit.get("h") != h:
            cache[pf.path] = {"h": h, "m": detect_file(pf, None, local=True)}
            visit.fresh.add(pf.path)

    visit.fresh = set()  # type: ignore[attr-defined]  # files detected now, not reused
    return visit


GRAPH_KEY = "<whole graph>"  # cache entry for the whole-graph detectors


def detect(
    graph: CodeGraph,
    only: set[str] | None = None,
    cache: dict[str, dict] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> list[IntentMatch]:
    """Every match in the repo. With `cache` ({file: {"h": sha1, "m": matches}}), a file whose hash is
    unchanged keeps its matches without being looked at again, and the cache is updated in place; the
    whole-graph detectors always run. `progress(done, total)` is called as files are done."""
    matches: list[IntentMatch] = []
    use_cache = cache is not None and not only
    files = [pf for pf in graph.files.values() if not is_test_file(pf.path)]
    reused = 0
    for n, pf in enumerate(files):
        if progress and n % 50 == 0:
            progress(n, len(files))
        h = file_hash(pf) if use_cache else ""
        hit = cache.get(pf.path) if use_cache else None
        if hit is not None and hit.get("h") == h:
            by_name = {fn.qualname: fn for fn in pf.module.functions}
            for m in hit["m"]:  # point at this graph's Function objects
                m.function = by_name.get(m.function.qualname, m.function)
            matches.extend(hit["m"])
            reused += 1
            continue
        found = detect_file(pf, graph, only, local=True if use_cache else None)
        if use_cache:
            cache[pf.path] = {"h": h, "m": found}
        matches.extend(found)
    if GRAPH_FILTERS:
        kept = []
        for m in matches:
            f = GRAPH_FILTERS.get(m.intent)
            m = f(m, graph) if f is not None else m
            if m is not None:
                kept.append(m)
        matches[:] = kept
    if use_cache and USES_GRAPH:  # these read other files: run every time, on the files that could match
        for pf in files:
            matches.extend(detect_file(pf, graph, only, local=False))
    if not use_cache:
        for intent, gdet in GRAPH_DETECTORS.items():
            if not only or intent in only:
                matches.extend(gdet(graph))
        return matches
    for path in [p for p in cache if p != GRAPH_KEY and p not in graph.files]:
        del cache[path]
    graph.__dict__["detect_reused"] = reused - len(graph.__dict__.get("detect_fresh", ()))
    # Whole-graph detectors (import cycles, layering...) depend on every file, tests included: reuse their
    # matches only when no file at all changed.
    whole = hashlib.sha1("".join(f"{p}:{file_hash(pf)};" for p, pf in sorted(graph.files.items())).encode())
    whole = whole.hexdigest()
    hit = cache.get(GRAPH_KEY)
    if hit is not None and hit.get("h") == whole:
        by_key = {(pf.path, fn.qualname): fn for pf, fn in graph.functions()}
        for m in hit["m"]:
            m.function = by_key.get((m.function.file, m.function.qualname), m.function)
        matches.extend(hit["m"])
        graph.__dict__["detect_graph_reused"] = True
        return matches
    found = [m for gdet in GRAPH_DETECTORS.values() for m in gdet(graph)]
    cache[GRAPH_KEY] = {"h": whole, "m": found}
    matches.extend(found)
    return matches
