"""Intents for common libraries: Pydantic 2, SQLAlchemy 2, requests, Celery.

Each was checked against the library at the lowest version its card claims and at the latest
release (Pydantic 2.0.3 / 2.14.3, SQLAlchemy 2.0.0 / 2.1.8, requests 2.20.1 / 2.34.2,
Celery 5.0.5 / 5.7.0).
"""

from __future__ import annotations

from tree_sitter import Node

from ..graph import ParsedFile, resolve, text
from ..models import Function, IntentMatch
from .detectors import calls_in, detector, line, module_detector, walk

REQUESTS_CALLS = {f"requests.{m}" for m in ("get", "post", "put", "patch", "delete", "head", "options", "request")}


def _module_fn(node: Node, pf: ParsedFile) -> Function:
    return Function("<module>", "<module>", pf.path, line(node), node.end_point[0] + 1, line(node))


# --------------------------------------------------------------------------- Pydantic 2


@detector("pydantic-v1-validator")
def detect_v1_validator(pf, fn, node, graph):
    """`@validator` / `@root_validator`: Pydantic 1 decorators, deprecated in 2.0."""
    for deco in fn.decorators:
        target = resolve(pf.module, deco.split("(", 1)[0])
        if target in ("pydantic.validator", "pydantic.root_validator", "pydantic.class_validators.validator"):
            kind = target.rsplit(".", 1)[-1]
            return [
                IntentMatch(
                    "pydantic-v1-validator",
                    fn,
                    fn.start_line,
                    f"`@{deco.split('(', 1)[0]}` is a Pydantic 1 decorator",
                    {"decorator": kind},
                )
            ]
    return []


@module_detector("pydantic-class-config")
def detect_class_config(pf: ParsedFile) -> list[IntentMatch]:
    """A Pydantic model configured with a nested `class Config:` (deprecated in 2.0)."""
    if not any(v.startswith("pydantic") for v in pf.module.imports.values()):
        return []
    from .more import _pydantic_models

    models = _pydantic_models(pf)
    out = []
    for cls in walk(pf.root, into_defs=True):
        if cls.type != "class_definition" or text(cls.child_by_field_name("name")) not in models:
            continue
        body = cls.child_by_field_name("body")
        for stmt in body.named_children if body is not None else []:
            if stmt.type == "class_definition" and text(stmt.child_by_field_name("name")) == "Config":
                name = text(cls.child_by_field_name("name"))
                out.append(
                    IntentMatch(
                        "pydantic-class-config",
                        Function(name, name, pf.path, line(cls), cls.end_point[0] + 1, line(cls)),
                        line(stmt),
                        f"model `{name}` is configured with a nested `class Config:`",
                        {},
                    )
                )
    return out


@module_detector("pydantic-basesettings-import")
def detect_basesettings_import(pf: ParsedFile) -> list[IntentMatch]:
    """`from pydantic import BaseSettings`: an import error on Pydantic 2."""
    for child in pf.root.children:
        if child.type == "import_from_statement" and text(child.child_by_field_name("module_name")) == "pydantic":
            names = [text(n.child_by_field_name("name") or n) for n in child.named_children[1:]]
            if "BaseSettings" in names:
                return [
                    IntentMatch(
                        "pydantic-basesettings-import",
                        _module_fn(child, pf),
                        line(child),
                        "`BaseSettings` is imported from `pydantic`, which no longer has it",
                        {},
                    )
                ]
    return []


# --------------------------------------------------------------------------- SQLAlchemy 2


@module_detector("sqlalchemy-ext-declarative")
def detect_ext_declarative(pf: ParsedFile) -> list[IntentMatch]:
    """`from sqlalchemy.ext.declarative import declarative_base`: moved to sqlalchemy.orm in 2.0."""
    for child in pf.root.children:
        if child.type != "import_from_statement":
            continue
        module = child.child_by_field_name("module_name")
        if text(module) != "sqlalchemy.ext.declarative":
            continue
        names = [text(n.child_by_field_name("name") or n) for n in child.named_children[1:]]
        data: dict = {}
        if names == ["declarative_base"]:  # the same function, re-exported from its new home
            data = {"edits": [(module.start_byte, module.end_byte, "sqlalchemy.orm")], "imports": []}
        return [
            IntentMatch(
                "sqlalchemy-ext-declarative",
                _module_fn(child, pf),
                line(child),
                "imports from `sqlalchemy.ext.declarative`, which SQLAlchemy 2.0 marks as moved",
                data,
            )
        ]
    return []


# --------------------------------------------------------------------------- requests without a timeout


@detector("requests-no-timeout", triggers=("requests",))
def detect_requests_no_timeout(pf, fn, node, graph):
    """`requests.get(url)` with no timeout: requests waits forever on a server that stops answering."""
    if (body := node.child_by_field_name("body")) is None:
        return []
    for call, name in calls_in(body, pf):
        if name not in REQUESTS_CALLS:
            continue
        args = call.child_by_field_name("arguments")
        kids = args.named_children if args is not None else []
        if any(k.type == "dictionary_splat" for k in kids):
            continue  # **kwargs may carry a timeout
        if not any(k.type == "keyword_argument" and text(k.child_by_field_name("name")) == "timeout" for k in kids):
            return [
                IntentMatch(
                    "requests-no-timeout",
                    fn,
                    line(call),
                    f"`{name}()` without `timeout=` waits forever if the server stops responding",
                    {},
                )
            ]
    return []


def is_celery_task(fn: Function, pf: ParsedFile) -> bool:
    for deco in fn.decorators:
        head = deco.split("(", 1)[0]
        target = resolve(pf.module, head)
        if target.startswith("celery") and target.endswith(("shared_task", ".task")):
            return True
        if head.endswith(".task") and any(v.startswith("celery") for v in pf.module.imports.values()):
            return True  # @app.task where app = Celery(...)
    return False
