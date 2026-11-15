"""Further intent detectors: async hygiene, deprecated APIs, stdlib reinventions.

Detectors that can be fixed mechanically put the exact byte spans to rewrite in
``IntentMatch.data["edits"]`` as ``(start_byte, end_byte, replacement)`` tuples,
plus any ``data["imports"]`` the rewrite needs. ``suggest.py`` turns those into a diff.
"""

from __future__ import annotations

from tree_sitter import Node

from ..graph import resolve, text
from ..models import IntentMatch
from .detectors import calls_in, detector, line, walk

BLOCKING_HTTP = {
    "requests.get", "requests.post", "requests.put", "requests.patch", "requests.delete",
    "requests.request", "requests.head", "urllib.request.urlopen",
}


def _body(node: Node) -> Node | None:
    return node.child_by_field_name("body")


def _callee(call: Node) -> Node | None:
    return call.child_by_field_name("function")


def _in_await(call: Node) -> bool:
    return call.parent is not None and call.parent.type == "await"


# --------------------------------------------------------------------------- async hygiene


@detector("blocking-sleep-in-async")
def detect_sync_sleep_in_async(pf, fn, node, graph):
    if not fn.is_async or (body := _body(node)) is None:
        return []
    edits, first = [], None
    for call, name in calls_in(body, pf):
        if name == "time.sleep":
            args = call.child_by_field_name("arguments")
            edits.append((call.start_byte, call.end_byte, f"await asyncio.sleep{text(args)}"))
            first = first or call
    if not edits:
        return []
    imports = [] if "asyncio" in pf.module.imports else ["import asyncio"]
    return [IntentMatch(
        "blocking-sleep-in-async", fn, line(first),
        f"time.sleep() inside `async def {fn.name}` blocks the event loop for every other request",
        {"edits": edits, "imports": imports},
    )]


@detector("blocking-http-in-async")
def detect_blocking_http_in_async(pf, fn, node, graph):
    if not fn.is_async or (body := _body(node)) is None:
        return []
    for call, name in calls_in(body, pf):
        if name in BLOCKING_HTTP:
            return [IntentMatch(
                "blocking-http-in-async", fn, line(call),
                f"blocking {name}() inside `async def {fn.name}` stalls the event loop while it waits",
                {"call": name},
            )]
    return []


# --------------------------------------------------------------------------- timing


@detector("wall-clock-timing")
def detect_wall_clock_timing(pf, fn, node, graph):
    """`start = time.time()` ... `time.time() - start` measures durations with the wall clock."""
    if (body := _body(node)) is None:
        return []
    calls = [c for c, name in calls_in(body, pf) if name == "time.time" and not c.child_by_field_name("arguments").named_children]
    if len(calls) < 2:
        return []
    in_subtraction = any(
        c.parent is not None and c.parent.type == "binary_operator" and "-" in text(c.parent)
        for c in calls
    )
    if not in_subtraction:
        return []
    callee_text = text(_callee(calls[0]))
    if callee_text != "time.time":
        return []  # `from time import time` style: leave to a human
    edits = [(c.start_byte, c.end_byte, "time.perf_counter()") for c in calls]
    return [IntentMatch(
        "wall-clock-timing", fn, line(calls[0]),
        "durations measured with time.time(), which jumps when the system clock is adjusted",
        {"edits": edits, "imports": []},
    )]


# --------------------------------------------------------------------------- deprecated APIs


@detector("datetime-utcnow")
def detect_utcnow(pf, fn, node, graph):
    if (body := _body(node)) is None:
        return []
    edits, first = [], None
    imports: list[str] = []
    for call, name in calls_in(body, pf):
        if name != "datetime.datetime.utcnow":
            continue
        written = text(_callee(call))  # "datetime.utcnow" or "datetime.datetime.utcnow"
        prefix = written[: -len(".utcnow")]
        if prefix == "datetime.datetime":
            repl = "datetime.datetime.now(datetime.timezone.utc)"
        else:
            repl = f"{prefix}.now(timezone.utc)"
            if pf.module.imports.get("timezone") != "datetime.timezone":
                imports = ["from datetime import timezone"]
        edits.append((call.start_byte, call.end_byte, repl))
        first = first or call
    if not edits:
        return []
    return [IntentMatch(
        "datetime-utcnow", fn, line(first),
        "datetime.utcnow() returns a naive datetime and is deprecated since Python 3.12",
        {"edits": edits, "imports": imports},
    )]


PYDANTIC_RENAMES = {"parse_obj": "model_validate", "parse_raw": "model_validate_json", "construct": "model_construct",
                    "update_forward_refs": "model_rebuild"}


@detector("pydantic-v1-api")
def detect_pydantic_v1(pf, fn, node, graph):
    """Pydantic v1 class methods used on a v2 install (they still work but warn and will go)."""
    if not any(v.startswith("pydantic") for v in pf.module.imports.values()):
        return []
    if (body := _body(node)) is None:
        return []
    models = _pydantic_models(pf)
    edits, first, used = [], None, set()
    for call in walk(body):
        if call.type != "call" or (callee := _callee(call)) is None or callee.type != "attribute":
            continue
        obj, attr = callee.child_by_field_name("object"), callee.child_by_field_name("attribute")
        if text(obj) in models and text(attr) in PYDANTIC_RENAMES:
            edits.append((attr.start_byte, attr.end_byte, PYDANTIC_RENAMES[text(attr)]))
            used.add(text(attr))
            first = first or call
    if not edits:
        return []
    return [IntentMatch(
        "pydantic-v1-api", fn, line(first),
        "Pydantic v1 methods " + ", ".join(f"{u}()" for u in sorted(used)) + " used on BaseModel subclasses",
        {"edits": edits, "imports": [], "methods": sorted(used)},
    )]


def _pydantic_models(pf) -> set[str]:
    names = set()
    for n in walk(pf.root, into_defs=True):
        if n.type == "class_definition":
            supers = n.child_by_field_name("superclasses")
            if supers is not None and any(resolve(pf.module, text(s)).startswith("pydantic") or text(s) in names
                                          for s in supers.named_children):
                names.add(text(n.child_by_field_name("name")))
    return names


@detector("fastapi-on-event")
def detect_on_event(pf, fn, node, graph):
    for deco in fn.decorators:
        if ".on_event(" in deco and any(v.startswith("fastapi") for v in pf.module.imports.values()):
            return [IntentMatch(
                "fastapi-on-event", fn, fn.start_line,
                f"`@{deco}` startup/shutdown hook",
                {},
            )]
    return []


@detector("sqlalchemy-legacy-query")
def detect_legacy_query(pf, fn, node, graph):
    if not any(v.startswith("sqlalchemy") for v in pf.module.imports.values()):
        return []
    if (body := _body(node)) is None:
        return []
    for call in walk(body):
        callee = _callee(call) if call.type == "call" else None
        if callee is not None and (text(callee).endswith("session.query") or text(callee) == "db.query"):
            return [IntentMatch(
                "sqlalchemy-legacy-query", fn, line(call),
                "legacy Session.query() API",
                {},
            )]
    return []


# --------------------------------------------------------------------------- stdlib reinventions


@detector("manual-counter")
def detect_manual_counter(pf, fn, node, graph):
    """if k in d: d[k] += 1  else: d[k] = 1   ->  collections.Counter"""
    if (body := _body(node)) is None:
        return []
    for stmt in walk(body):
        if stmt.type != "if_statement":
            continue
        cond = stmt.child_by_field_name("condition")
        alt = stmt.child_by_field_name("alternative")
        if cond is None or alt is None or cond.type != "comparison_operator" or len(cond.named_children) != 2:
            continue
        k, d = (text(c) for c in cond.named_children)
        cons = text(stmt.child_by_field_name("consequence")).replace(" ", "")
        alt_t = text(alt).replace(" ", "")
        if " not in " in text(cond):
            cons, alt_t = alt_t, cons
        elif " in " not in text(cond):
            continue
        if cons == f"{d}[{k}]+=1" and alt_t.endswith(f"{d}[{k}]=1"):
            return [IntentMatch("manual-counter", fn, line(stmt),
                                f"dict `{d}` counts occurrences by hand", {"dict": d})]
    return []


@detector("manual-defaultdict")
def detect_manual_defaultdict(pf, fn, node, graph):
    """if k not in d: d[k] = []  then d[k].append(v)   ->  collections.defaultdict(list)"""
    if (body := _body(node)) is None:
        return []
    for stmt in walk(body):
        if stmt.type != "if_statement" or stmt.child_by_field_name("alternative") is not None:
            continue
        cond = stmt.child_by_field_name("condition")
        if cond is None or " not in " not in text(cond) or len(cond.named_children) != 2:
            continue
        k, d = (text(c) for c in cond.named_children)
        cons = text(stmt.child_by_field_name("consequence")).replace(" ", "")
        if cons in (f"{d}[{k}]=[]", f"{d}[{k}]=list()", f"{d}[{k}]=set()", f"{d}[{k}]={{}}"):
            kind = {"[]": "list", "list()": "list", "set()": "set", "{}": "dict"}[cons.split("=", 1)[1]]
            return [IntentMatch("manual-defaultdict", fn, line(stmt),
                                f"`{d}` initialises missing keys by hand before adding to them",
                                {"dict": d, "factory": kind})]
    return []


@detector("manual-thread-fanout")
def detect_thread_fanout(pf, fn, node, graph):
    if (body := _body(node)) is None:
        return []
    names = [n for _, n in calls_in(body, pf)]
    if "threading.Thread" in names and any(n.endswith(".join") for n in names) and \
            any(n.type == "for_statement" for n in walk(body)):
        call = next(c for c, n in calls_in(body, pf) if n == "threading.Thread")
        return [IntentMatch("manual-thread-fanout", fn, line(call),
                            "threads started in a loop and joined by hand", {})]
    return []


@detector("os-system")
def detect_os_system(pf, fn, node, graph):
    if (body := _body(node)) is None:
        return []
    for call, name in calls_in(body, pf):
        if name in ("os.system", "os.popen"):
            return [IntentMatch("os-system", fn, line(call),
                                f"{name}() runs the command through a shell", {"call": name})]
    return []


@detector("string-concat-loop")
def detect_string_concat_loop(pf, fn, node, graph):
    if (body := _body(node)) is None:
        return []
    empty_strings = set()
    for stmt in body.named_children:
        t = text(stmt).replace(" ", "")
        if stmt.type == "expression_statement" and (t.endswith('=""') or t.endswith("=''")) and "\n" not in t:
            empty_strings.add(t.split("=", 1)[0])
    if not empty_strings:
        return []
    for loop in walk(body):
        if loop.type not in ("for_statement", "while_statement"):
            continue
        for n in walk(loop):
            if n.type == "augmented_assignment" and "+=" in text(n):
                target = text(n.child_by_field_name("left"))
                if target in empty_strings:
                    return [IntentMatch("string-concat-loop", fn, line(n),
                                        f"string `{target}` built with += inside a loop", {"var": target})]
    return []
