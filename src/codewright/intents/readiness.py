"""Production readiness: code that works in a demo and fails under real traffic.

Each check names the scale at which it starts to matter (the card's ``threshold``), so a
script that runs once a day isn't held to a web server's standard. Like the other detectors
these are conservative: they fire only on the exact shape they describe.

* ``n-plus-one-query``: a query inside a loop over rows, keyed by the loop variable.
* ``http-no-timeout``: ``urllib.request.urlopen`` without ``timeout=``, or ``timeout=None``
  passed to requests/httpx (which turns httpx's default 5 s off).
* ``cache-on-method``: ``@lru_cache`` / ``@cache`` on a method keeps every ``self`` alive.
* ``unbounded-cache``: ``@cache`` / ``@lru_cache(maxsize=None)`` on a request path grows forever.
* ``post-without-idempotency``: a POST route that writes and triggers an outside side effect
  (a charge, an email, a job) with no idempotency key, so a client retry does it twice.
* ``sqlalchemy-no-pre-ping``: a server-database engine without ``pool_pre_ping``.
"""

from __future__ import annotations

import re

from ..graph import ParsedFile, text
from ..models import Function, IntentMatch
from .architecture import DB_OBJECTS
from .detectors import calls_in, detector, is_test_file, line, module_detector, walk

QUERY_METHODS = {
    "execute",
    "scalar",
    "scalars",
    "query",
    "get",
    "fetch",
    "fetchrow",
    "fetchval",
    "fetchone",
    "find_one",
}
DJANGO_METHODS = {"get", "filter", "exclude", "first", "count", "exists"}


def _hot(graph) -> set[str]:
    if "readiness_hot" not in graph.__dict__:
        graph.__dict__["readiness_hot"] = graph.hot_functions()
    return graph.__dict__["readiness_hot"]


def _names(node) -> set[str]:
    return {text(n) for n in walk(node, into_defs=True) if n.type == "identifier"}


# --------------------------------------------------------------------------- N+1 queries


def _orm(pf: ParsedFile) -> str | None:
    roots = {v.split(".", 1)[0] for v in pf.module.imports.values()}
    for lib in ("sqlalchemy", "sqlmodel", "django", "tortoise", "asyncpg", "psycopg", "psycopg2", "pymongo", "motor"):
        if lib in roots:
            return lib
    return None


@detector("n-plus-one-query", triggers=("for ",))
def detect_n_plus_one(pf, fn, node, graph):
    """`for order in orders: session.get(Customer, order.customer_id)` runs one query per row."""
    if is_test_file(pf.path):
        return []
    body = node.child_by_field_name("body")
    if body is None:
        return []
    orm = _orm(pf)
    passes_db = any(DB_OBJECTS.search(p) for p in fn.params)
    if orm is None and not passes_db:
        return []
    for loop in walk(body):
        if loop.type != "for_statement":
            continue
        left, loop_body = loop.child_by_field_name("left"), loop.child_by_field_name("body")
        if left is None or loop_body is None:
            continue
        loop_vars = {text(n) for n in walk(left) if n.type == "identifier"}
        for call in walk(loop_body):
            if call.type != "call":
                continue
            f = call.child_by_field_name("function")
            if f is None or f.type != "attribute":
                continue
            method = text(f.child_by_field_name("attribute"))
            obj = text(f.child_by_field_name("object"))
            is_db = method in QUERY_METHODS and DB_OBJECTS.search(obj.rsplit(".", 1)[-1])
            is_django = orm == "django" and method in DJANGO_METHODS and obj.endswith(".objects")
            if not (is_db or is_django):
                continue
            args = call.child_by_field_name("arguments")
            if args is None or not (_names(args) & loop_vars):
                continue
            var = sorted(_names(args) & loop_vars)[0]
            if orm == "django":
                advice = (
                    f"Fetch the related rows once before the loop: `select_related(...)` for a foreign key or "
                    f"`prefetch_related(...)` for a reverse/many relation, or `in_bulk([...])` keyed by `{var}`."
                )
            elif orm in ("sqlalchemy", "sqlmodel"):
                advice = (
                    f"Load them in one query before the loop: `select(Model).where(Model.id.in_(ids))` built "
                    f"from every `{var}`, or eager-load the relationship with `selectinload(...)` on the query "
                    "that produced the rows."
                )
            else:
                advice = f"Collect the keys from every `{var}` and fetch them in one query (`WHERE id = ANY($1)`)."
            return [
                IntentMatch(
                    "n-plus-one-query",
                    fn,
                    line(call),
                    f"`{obj}.{method}(...)` runs once per `{var}` in the loop on line {line(loop)}",
                    {"orm": orm, "advice": advice, "hot": graph.key(fn) in _hot(graph)},
                )
            ]
    return []


# --------------------------------------------------------------------------- timeouts


def _kwargs(call) -> dict[str, str]:
    args = call.child_by_field_name("arguments")
    out = {}
    for k in args.named_children if args is not None else []:
        if k.type == "keyword_argument":
            out[text(k.child_by_field_name("name"))] = text(k.child_by_field_name("value"))
        elif k.type == "dictionary_splat":
            out["**"] = text(k)
    return out


def _positional(call) -> int:
    args = call.child_by_field_name("arguments")
    return sum(1 for k in (args.named_children if args is not None else []) if k.type not in ("keyword_argument",))


TIMEOUT_NONE_CALLS = re.compile(
    r"^(requests|httpx)\.(get|post|put|patch|delete|head|options|request|Client|AsyncClient)$"
)


@detector("http-no-timeout", triggers=("urlopen", "timeout"))
def detect_http_no_timeout(pf, fn, node, graph):
    body = node.child_by_field_name("body")
    if body is None or is_test_file(pf.path):
        return []
    for call, name in calls_in(body, pf):
        kw = _kwargs(call)
        if "**" in kw:
            continue
        if name == "urllib.request.urlopen" and "timeout" not in kw and _positional(call) < 3:
            return [
                IntentMatch(
                    "http-no-timeout",
                    fn,
                    line(call),
                    "`urllib.request.urlopen()` without `timeout=` waits forever if the server stops answering",
                    {"client": "urllib"},
                )
            ]
        if TIMEOUT_NONE_CALLS.match(name) and kw.get("timeout") == "None":
            return [
                IntentMatch(
                    "http-no-timeout",
                    fn,
                    line(call),
                    f"`{name}(timeout=None)` turns the timeout off, so a stalled server holds the caller forever",
                    {"client": name.split(".", 1)[0]},
                )
            ]
    return []


# --------------------------------------------------------------------------- caches


def _cache_decorator(pf: ParsedFile, fn: Function) -> tuple[str, str] | None:
    """(resolved decorator, as written) for functools.cache / lru_cache, else None."""
    from ..graph import resolve

    for d in fn.decorators:
        written = d.lstrip("@").strip()
        name = resolve(pf.module, written.split("(", 1)[0])
        if name in ("functools.cache", "functools.lru_cache"):
            return name, written
    return None


@detector("cache-on-method")  # decorators are outside the def node, so no trigger words
def detect_cache_on_method(pf, fn, node, graph):
    """`@lru_cache` on a method: the cache holds `self`, so instances are never freed (ruff B019)."""
    if is_test_file(pf.path) or not fn.params or fn.params[0] != "self" or "." not in fn.qualname:
        return []
    found = _cache_decorator(pf, fn)
    if found is None or any(d.lstrip("@") in ("staticmethod", "classmethod") for d in fn.decorators):
        return []
    no_args = len(fn.params) == 1
    advice = (
        f"`{fn.name}` takes no arguments besides `self`: `@functools.cached_property` caches it per instance "
        "and frees it with the instance."
        if no_args
        else f"Keep the cache on the instance (a dict set in `__init__`), or move `{fn.name}` to a module-level "
        "function of the arguments it really depends on."
    )
    return [
        IntentMatch(
            "cache-on-method",
            fn,
            fn.def_line,
            f"`@{found[1]}` on method `{fn.qualname}` keeps a reference to every `self` it was called with",
            {"no_args": no_args, "advice": advice},
        )
    ]


@detector("unbounded-cache")
def detect_unbounded_cache(pf, fn, node, graph):
    """`@cache` / `@lru_cache(maxsize=None)` on a request path: one entry per distinct argument, forever."""
    if is_test_file(pf.path) or not fn.params or fn.params[0] in ("self", "cls"):
        return []
    found = _cache_decorator(pf, fn)
    if found is None:
        return []
    name, written = found
    unbounded = name == "functools.cache" or re.search(r"maxsize\s*=\s*None", written)
    if not unbounded or graph.key(fn) not in _hot(graph):
        return []
    return [
        IntentMatch(
            "unbounded-cache",
            fn,
            fn.def_line,
            f"`@{written}` on `{fn.name}`, which a request or command reaches: every distinct "
            f"`{', '.join(fn.params)}` stays in memory until the process exits",
            {"advice": f"Bound it: `@functools.lru_cache(maxsize=1024)` on `{fn.name}` (size it to the hot set)."},
        )
    ]


# --------------------------------------------------------------------------- idempotency

SIDE_EFFECT = re.compile(
    r"(^|\.)(stripe|braintree|paypal|twilio|sendgrid|boto3|smtplib)\b|"
    r"\.(delay|apply_async|enqueue|send_task|publish|send_message|send_email|send_mail|charge|create_payment)$|"
    r"^(requests|httpx)\.(post|put|patch)$|\.(post|put|patch)$"
)
WRITE = re.compile(r"\.(add|add_all|commit|insert|insert_one|save|create|bulk_create|execute)$")


@detector("post-without-idempotency")
def detect_post_without_idempotency(pf, fn, node, graph):
    if is_test_file(pf.path):
        return []
    if not any(d.lstrip("@").split("(", 1)[0].endswith((".post", ".route")) for d in fn.decorators):
        return []
    deco = " ".join(fn.decorators)
    if ".route" in deco and "POST" not in deco:
        return []
    src = text(node).lower()
    if "idempot" in src or "request_id" in src or "dedup" in src:
        return []
    names = [name for _, name in calls_in(node, pf, into_defs=False)]
    writes = [n for n in names if WRITE.search(n)]
    effects = [n for n in names if SIDE_EFFECT.search(n) and not n.startswith(("app.", "router."))]
    if not writes or not effects:
        return []
    return [
        IntentMatch(
            "post-without-idempotency",
            fn,
            fn.def_line,
            f"POST `{fn.name}` writes (`{writes[0]}`) and calls `{effects[0]}`; a client retry after a timeout "
            "does both again",
            {
                "effect": effects[0],
                "advice": f"Accept an `Idempotency-Key` header in `{fn.name}`; store the key with the result in the "
                "same transaction as the write, and return the stored result when the key repeats.",
            },
        )
    ]


# --------------------------------------------------------------------------- connection pools


@module_detector("sqlalchemy-no-pre-ping")
def detect_no_pre_ping(pf: ParsedFile) -> list[IntentMatch]:
    if is_test_file(pf.path) or "create_engine" not in pf.source.decode("utf-8", "replace"):
        return []
    from ..graph import resolve

    out = []
    for call in walk(pf.root, into_defs=True):
        if call.type != "call":
            continue
        f = call.child_by_field_name("function")
        if f is None:
            continue
        name = resolve(pf.module, text(f))
        if name not in ("sqlalchemy.create_engine", "sqlalchemy.ext.asyncio.create_async_engine"):
            continue
        kw = _kwargs(call)
        if "pool_pre_ping" in kw or "**" in kw or "poolclass" in kw:
            continue
        args = call.child_by_field_name("arguments")
        first = args.named_children[0] if args is not None and args.named_children else None
        url = text(first).lower() if first is not None else ""
        if "sqlite" in url:
            continue  # no server to drop the connection
        close = args.end_byte - 1  # the ")"
        before = pf.source[args.start_byte + 1 : close].rstrip()
        sep = " " if before.endswith(b",") else (", " if args.named_children else "")
        ln = line(call)
        out.append(
            IntentMatch(
                "sqlalchemy-no-pre-ping",
                Function("<module>", "<module>", pf.path, ln, call.end_point[0] + 1, ln),
                ln,
                f"`{text(f)}(...)` without `pool_pre_ping=True` hands out pooled connections the database has "
                "already closed",
                {"edits": [(close, close, f"{sep}pool_pre_ping=True")], "imports": []},
            )
        )
    return out
