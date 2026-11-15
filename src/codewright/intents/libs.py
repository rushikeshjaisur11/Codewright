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
            data: dict = {"decorator": kind}
            if kind == "validator" and (edits := _field_validator_edits(pf, fn, node)) is not None:
                imports = [] if "field_validator" in pf.module.imports else ["from pydantic import field_validator"]
                if deco.startswith("pydantic."):
                    imports = []
                data |= {"edits": edits, "imports": imports}
            return [
                IntentMatch(
                    "pydantic-v1-validator",
                    fn,
                    fn.start_line,
                    f"`@{deco.split('(', 1)[0]}` is a Pydantic 1 decorator",
                    data,
                )
            ]
    return []


def _field_validator_edits(pf: ParsedFile, fn: Function, defn: Node) -> list | None:
    """Edits turning `@validator("x", pre=True)` into `@field_validator("x", mode="before")` + `@classmethod`.

    Only for the form whose behaviour is the same in both: field names as plain strings, options limited
    to pre / allow_reuse / check_fields, and a (cls, value) signature. A validator taking `values`,
    `field` or `config`, or using each_item / always, behaves differently and is left for a human.
    """
    outer = defn.parent
    if outer is None or outer.type != "decorated_definition":
        return None
    deco_node = None
    has_classmethod = False
    for d in outer.children:
        if d.type != "decorator":
            continue
        expr = d.named_children[0] if d.named_children else None
        head = text(expr.child_by_field_name("function")) if expr is not None and expr.type == "call" else text(expr)
        if head == "classmethod":
            has_classmethod = True
        elif resolve(pf.module, head) in ("pydantic.validator", "pydantic.class_validators.validator"):
            deco_node = expr
    if deco_node is None or deco_node.type != "call":
        return None
    fields, options = [], []
    for arg in deco_node.child_by_field_name("arguments").named_children:
        if arg.type == "string" and not any(c.type == "interpolation" for c in arg.children):
            fields.append(text(arg))
        elif arg.type == "keyword_argument":
            name, value = text(arg.child_by_field_name("name")), text(arg.child_by_field_name("value"))
            if name == "pre" and value in ("True", "False"):
                if value == "True":
                    options.append('mode="before"')
            elif name == "check_fields":
                options.append(f"check_fields={value}")
            elif name != "allow_reuse":  # allow_reuse has no meaning in v2
                return None
        else:
            return None
    params = [p for p in defn.child_by_field_name("parameters").named_children if p.type != "comment"]
    if not fields or len(params) != 2 or any(p.type not in ("identifier", "typed_parameter") for p in params):
        return None
    head = text(deco_node.child_by_field_name("function"))
    new_head = head[: -len("validator")] + "field_validator"
    edits = [(deco_node.start_byte, deco_node.end_byte, f"{new_head}({', '.join(fields + options)})")]
    if not has_classmethod:
        indent = " " * defn.start_point[1]
        edits.append((defn.start_byte, defn.start_byte, f"@classmethod\n{indent}"))
    return edits


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
                data: dict = {}
                if (replacement := _config_dict(stmt)) is not None:
                    imports = [] if "ConfigDict" in pf.module.imports else ["from pydantic import ConfigDict"]
                    data = {"edits": [(stmt.start_byte, stmt.end_byte, replacement)], "imports": imports}
                out.append(
                    IntentMatch(
                        "pydantic-class-config",
                        Function(name, name, pf.path, line(cls), cls.end_point[0] + 1, line(cls)),
                        line(stmt),
                        f"model `{name}` is configured with a nested `class Config:`",
                        data,
                    )
                )
    return out


# Options ConfigDict accepts in every Pydantic 2 release (2.0.3 through 2.14.3). A `class Config:`
# made only of these moves to ConfigDict unchanged. The v1 names below are *ignored* by Pydantic 2
# (it only warns), so renaming them changes behaviour: that is reported, never auto-patched.
CONFIG_KEYS = frozenset(
    [
        "alias_generator",
        "allow_inf_nan",
        "arbitrary_types_allowed",
        "extra",
        "from_attributes",
        "frozen",
        "hide_input_in_errors",
        "ignored_types",
        "json_schema_extra",
        "loc_by_alias",
        "populate_by_name",
        "protected_namespaces",
        "revalidate_instances",
        "ser_json_bytes",
        "ser_json_timedelta",
        "str_max_length",
        "str_min_length",
        "str_strip_whitespace",
        "str_to_lower",
        "str_to_upper",
        "strict",
        "title",
        "use_enum_values",
        "validate_assignment",
        "validate_default",
        "validate_return",
    ]
)
CONFIG_RENAMES = {
    "orm_mode": "from_attributes",
    "allow_population_by_field_name": "populate_by_name",
    "anystr_strip_whitespace": "str_strip_whitespace",
    "anystr_lower": "str_to_lower",
    "anystr_upper": "str_to_upper",
    "max_anystr_length": "str_max_length",
    "min_anystr_length": "str_min_length",
    "schema_extra": "json_schema_extra",
    "validate_all": "validate_default",
    "keep_untouched": "ignored_types",
}


def _v1_config_keys(config: Node) -> list[str]:
    """Pydantic 1 option names in a `class Config:` body; Pydantic 2 only warns about them and ignores them."""
    body = config.child_by_field_name("body")
    found = []
    for stmt in body.named_children if body is not None else []:
        expr = stmt.named_children[0] if stmt.type == "expression_statement" and stmt.named_children else None
        if expr is not None and expr.type == "assignment":
            key = text(expr.child_by_field_name("left"))
            if key in CONFIG_RENAMES:
                found.append(key)
    return found


def _config_dict(config: Node) -> str | None:
    """`model_config = ConfigDict(...)` for a `class Config:` made only of known options, else None."""
    body = config.child_by_field_name("body")
    items: list[tuple[str, str]] = []
    for stmt in body.named_children if body is not None else []:
        if stmt.type == "comment":
            continue
        expr = stmt.named_children[0] if stmt.type == "expression_statement" and stmt.named_children else None
        if expr is not None and expr.type == "string" and not items:
            continue  # a docstring
        if expr is None or expr.type != "assignment":
            return None
        left, right = expr.child_by_field_name("left"), expr.child_by_field_name("right")
        if left is None or left.type != "identifier" or right is None or expr.child_by_field_name("type") is not None:
            return None
        key = text(left)
        if key not in CONFIG_KEYS:  # includes v1 names: Pydantic 2 ignores them, so renaming changes behaviour
            return None
        items.append((key, text(right)))
    if not items or len({k for k, _ in items}) != len(items):
        return None
    indent = " " * config.start_point[1]
    one_line = f"model_config = ConfigDict({', '.join(f'{k}={v}' for k, v in items)})"
    if len(indent) + len(one_line) <= 100 and "\n" not in one_line:
        return one_line
    inner = "".join(f"{indent}    {k}={v},\n" for k, v in items)
    return f"model_config = ConfigDict(\n{inner}{indent})"


def _model_configs(pf: ParsedFile):
    """(model class node, its nested `class Config:` node) for each Pydantic model in the file."""
    if not any(v.startswith("pydantic") for v in pf.module.imports.values()):
        return
    from .more import _pydantic_models

    models = _pydantic_models(pf)
    for cls in walk(pf.root, into_defs=True):
        if cls.type != "class_definition" or text(cls.child_by_field_name("name")) not in models:
            continue
        body = cls.child_by_field_name("body")
        for stmt in body.named_children if body is not None else []:
            if stmt.type == "class_definition" and text(stmt.child_by_field_name("name")) == "Config":
                yield cls, stmt


@module_detector("pydantic-ignored-config")
def detect_ignored_config(pf: ParsedFile) -> list[IntentMatch]:
    """Pydantic 1 option names in a model's `class Config:`: Pydantic 2 warns and ignores them."""
    out = []
    for cls, config in _model_configs(pf):
        ignored = _v1_config_keys(config)
        if ignored:
            name = text(cls.child_by_field_name("name"))
            renames = ", ".join(f"`{k}` (now `{CONFIG_RENAMES[k]}`)" for k in ignored)
            out.append(
                IntentMatch(
                    "pydantic-ignored-config",
                    Function(name, name, pf.path, line(cls), cls.end_point[0] + 1, line(cls)),
                    line(config),
                    f"model `{name}` sets {renames}: Pydantic 2 ignores these names",
                    {"keys": ignored},
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
