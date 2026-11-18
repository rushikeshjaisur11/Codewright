"""Conventions the libraries themselves now recommend, rewritten only where it is provably the same.

* FastAPI (>= 0.95): `q: str = Query(None, max_length=5)` -> `q: Annotated[str, Query(max_length=5)] = None`.
  "Prefer to use the Annotated version if possible" (FastAPI tutorial). FastAPI reads both forms
  into the same parameter, so the OpenAPI schema and request handling are unchanged.
* Pydantic 2: a required field written `x: int = Field(gt=0)` or `Field(...)` -> `x: Annotated[int, Field(gt=0)]`.
  The assignment form "might trick users into thinking f has a default value"; the ellipsis
  form is "discouraged". Fields with a default, default_factory or alias keep the assignment
  form, as the Pydantic docs say (type checkers read those from it).
* Pydantic 2: the same type and constraints repeated on several fields -> one reusable
  `Annotated` alias. Reported only: naming the type is a design choice.
"""

from __future__ import annotations

import re
from collections import defaultdict

from tree_sitter import Node

from ..graph import CodeGraph, ParsedFile, resolve, text
from ..models import Function, IntentMatch
from .detectors import graph_detector, is_test_file, line, module_detector, nodes

FASTAPI_PARAMS = {"Depends", "Security", "Query", "Path", "Body", "Header", "Cookie", "Form", "File"}
FASTAPI_MODULES = ("fastapi", "fastapi.params", "fastapi.param_functions")
NO_DEFAULT = {"Depends", "Security"}  # their first argument is the dependency, not a default
# Field() arguments type checkers read from the assignment form (Pydantic docs), plus dataclass-only ones.
KEEP_ASSIGNED = {"default", "default_factory", "alias", "init", "init_var", "kw_only"}
# Constraints that define a reusable type; per-field text (description, title, examples) does not.
CONSTRAINTS = {
    "gt",
    "ge",
    "lt",
    "le",
    "multiple_of",
    "min_length",
    "max_length",
    "pattern",
    "strict",
    "max_digits",
    "decimal_places",
    "allow_inf_nan",
}
PER_FIELD = {"description", "title", "examples", "json_schema_extra", "deprecated", "repr", "exclude"}


def annotated_name(pf: ParsedFile) -> tuple[str, list[str]]:
    """How to spell Annotated in this file, and the import to add if it isn't imported yet."""
    for alias, target in pf.module.imports.items():
        if target in ("typing.Annotated", "typing_extensions.Annotated"):
            return alias, []
    for alias, target in pf.module.imports.items():
        if target in ("typing", "typing_extensions") and alias == target:
            return f"{alias}.Annotated", []
    return "Annotated", ["from typing import Annotated"]


def _is_annotated(type_text: str, ann: str) -> bool:
    return type_text.startswith((f"{ann}[", "Annotated[", "typing.Annotated[", "typing_extensions.Annotated["))


def _args(call: Node) -> tuple[list[Node], dict[str, Node]] | None:
    """(positional args, keyword args) of a call, or None when the call has *args, **kwargs or comments."""
    args = call.child_by_field_name("arguments")
    if args is None or any(c.type == "comment" for c in args.children):
        return None
    pos, kws = [], {}
    for a in args.named_children:
        if a.type in ("list_splat", "dictionary_splat"):
            return None
        if a.type == "keyword_argument":
            kws[text(a.child_by_field_name("name"))] = a
        else:
            pos.append(a)
    return pos, kws


# --------------------------------------------------------------------------- FastAPI


def _fastapi_kind(pf: ParsedFile, value: Node | None) -> str | None:
    if value is None or value.type != "call":
        return None
    head, _, name = resolve(pf.module, text(value.child_by_field_name("function"))).rpartition(".")
    return name if head in FASTAPI_MODULES and name in FASTAPI_PARAMS else None


def _fastapi_param(pf: ParsedFile, p: Node, ann: str) -> tuple[str, bool] | None:
    """`name: T = Query(d, k=v)` -> (`name: Annotated[T, Query(k=v)] = d`, has_default), or None if unsafe."""
    kind = _fastapi_kind(pf, p.child_by_field_name("value"))
    typ = p.child_by_field_name("type")
    if kind is None or typ is None or _is_annotated(text(typ), ann):
        return None
    call = p.child_by_field_name("value")
    parsed = _args(call)
    if parsed is None:
        return None
    pos, kws = parsed
    name = text(p.child_by_field_name("name"))
    if kind in NO_DEFAULT:
        return f"{name}: {ann}[{text(typ)}, {text(call)}]", False
    if "default_factory" in kws or len(pos) > 1 or (pos and "default" in kws):
        return None
    node = pos[0] if pos else (kws["default"].child_by_field_name("value") if "default" in kws else None)
    if node is not None and _mutable(node):
        return None  # `= []` as a real default is the shared-mutable-default trap; FastAPI copies it, Python doesn't
    default = text(node) if node is not None else None
    if default in ("...", "Ellipsis"):
        default = None  # required, written the old way
    rest = [text(a) for k, a in kws.items() if k != "default"]
    new = f"{name}: {ann}[{text(typ)}, {text(call.child_by_field_name('function'))}({', '.join(rest)})]"
    return (new + f" = {default}", True) if default is not None else (new, False)


def _mutable(node: Node) -> bool:
    if node.type in (
        "list",
        "dictionary",
        "set",
        "list_comprehension",
        "dictionary_comprehension",
        "set_comprehension",
    ):
        return True
    return node.type == "call" and text(node.child_by_field_name("function")) in ("list", "dict", "set")


def _signature_ok(params: Node, converted: dict[int, bool]) -> bool:
    """After the rewrite, no positional parameter without a default may follow one with a default."""
    seen_default, keyword_only = False, False
    for p in params.named_children:
        if p.type in ("keyword_separator", "list_splat_pattern", "dictionary_splat_pattern"):
            keyword_only = True
            continue
        if p.type == "positional_separator":
            continue
        has_default = converted.get(p.id, p.type in ("default_parameter", "typed_default_parameter"))
        if not keyword_only:
            if seen_default and not has_default:
                return False
            seen_default |= has_default
    return True


def _dep_alias(dep_call: str) -> str:
    """`Depends(get_session)` -> `SessionDep`, as in FastAPI's own examples."""
    m = re.match(r"\w+\(\s*([\w.]+)", dep_call)
    base = (m.group(1).rsplit(".", 1)[-1] if m else "value").removeprefix("get_")
    return "".join(w.capitalize() for w in base.split("_") if w) + "Dep"


def _shared_dependencies(graph: CodeGraph) -> dict[tuple[str, str], int]:
    """(type, Depends(...) text) -> number of functions declaring it, across the repo. Computed once."""
    if "fastapi_deps" not in graph.__dict__:
        counts: dict[tuple[str, str], int] = defaultdict(int)
        for pf in graph.files.values():
            if b"Depends" not in pf.source or is_test_file(pf.path):
                continue
            for n in nodes(pf, "typed_default_parameter"):
                if n.type == "typed_default_parameter" and _fastapi_kind(pf, n.child_by_field_name("value")) in (
                    "Depends",
                    "Security",
                ):
                    counts[(text(n.child_by_field_name("type")), text(n.child_by_field_name("value")))] += 1
        graph.__dict__["fastapi_deps"] = counts
    return graph.__dict__["fastapi_deps"]


ROUTE_SUFFIXES = (".get", ".post", ".put", ".patch", ".delete", ".head", ".options", ".api_route", ".websocket")


def _rewrite_function(pf: ParsedFile, fn: Function, node: Node, ann: str, called: bool):
    """(FastAPI-default params, edits, reason left unpatched) for one function."""
    params = node.child_by_field_name("parameters")
    if params is None:
        return [], [], ""
    old, converted, edits = [], {}, []
    for p in params.named_children:
        if p.type in ("typed_default_parameter", "default_parameter") and _fastapi_kind(
            pf, p.child_by_field_name("value")
        ):
            old.append(p)
            rewritten = _fastapi_param(pf, p, ann)
            if rewritten is not None:
                converted[p.id] = rewritten[1]
                edits.append((p.start_byte, p.end_byte, rewritten[0]))
    if not old or _signature_ok(params, converted):
        return old, edits, ""
    # A parameter that loses its default (Depends, a required Query) now follows one with a default.
    # FastAPI calls endpoints by keyword, so a leading `*` keeps every name and the order (FastAPI's
    # own "order the parameters as you need" trick); only for routes nothing in the repo calls directly.
    is_route = any(d.split("(", 1)[0].endswith(ROUTE_SUFFIXES) for d in fn.decorators)
    first = params.named_children[0] if params.named_children else None
    if is_route and not called and first is not None and first.type != "identifier":
        line_start = pf.source.rfind(b"\n", 0, first.start_byte) + 1
        indent = pf.source[line_start : first.start_byte]
        star = f"*,\n{indent.decode()}" if not indent.strip() and line_start > params.start_byte else "*, "
        return old, [(first.start_byte, first.start_byte, star), *edits], ""
    reason = (
        "it is called directly in the repo"
        if is_route
        else "it isn't a route, so its callers may pass arguments by position"
    )
    return (
        old,
        [],
        f"`{fn.qualname}` is left as is: a parameter without a default would follow one with a default, and {reason}",
    )


@graph_detector("fastapi-annotated-params")
def detect_fastapi_annotated(graph: CodeGraph) -> list[IntentMatch]:
    """Parameters declared as `= Depends(...)` / `= Query(...)`: one rewrite per file, one import."""
    called = {c for callees in graph.edges.values() for c in callees}
    shared = _shared_dependencies(graph)
    out = []
    for pf in graph.files.values():
        if b"fastapi" not in pf.source or is_test_file(pf.path):
            continue
        ann, imports = annotated_name(pf)
        functions, all_old, edits, notes = [], [], [], []
        for fn in pf.module.functions:
            node = pf.nodes.get(fn.qualname)
            if node is None:
                continue
            old, fn_edits, note = _rewrite_function(pf, fn, node, ann, graph.key(fn) in called)
            if old:
                functions.append(fn)
                all_old += old
                edits += fn_edits
            if note:
                notes.append(note)
        if not functions:
            continue
        reuse = sorted(
            {
                (shared.get(key, 0), *key)
                for p in all_old
                if p.child_by_field_name("type") is not None
                for key in [(text(p.child_by_field_name("type")), text(p.child_by_field_name("value")))]
                if shared.get(key, 0) >= 3
            },
            reverse=True,
        )
        names = ", ".join(f"`{f.qualname}`" for f in functions[:4]) + (
            f" and {len(functions) - 4} more" if len(functions) > 4 else ""
        )
        evidence = f"{len(all_old)} parameters in {names} use the default-value form, e.g. `{text(all_old[0])}`"
        if notes:
            evidence += "; " + "; ".join(notes[:2])
        data: dict = {"edits": edits, "imports": imports if edits else [], "params": len(all_old)}
        if reuse:
            n, typ, dep = reuse[0]
            data["advice"] = (
                f"Here `{dep}` with type `{typ}` is declared in {n} functions: after this change, define "
                f"`{_dep_alias(dep)} = Annotated[{typ}, {dep}]` once and use `{_dep_alias(dep)}` as the type in each."
            )
        out.append(IntentMatch("fastapi-annotated-params", functions[0], line(all_old[0]), evidence, data))
    return out


# --------------------------------------------------------------------------- Pydantic


def _field_call(pf: ParsedFile, value: Node | None) -> bool:
    if value is None or value.type != "call":
        return False
    target = resolve(pf.module, text(value.child_by_field_name("function")))
    return target in ("pydantic.Field", "pydantic.fields.Field")


def _model_fields(pf: ParsedFile, cls: Node):
    """(assignment, name, type, Field call) for each `name: T = Field(...)` in a class body."""
    body = cls.child_by_field_name("body")
    for stmt in body.named_children if body is not None else ():
        if stmt.type != "expression_statement" or not stmt.named_children:
            continue
        a = stmt.named_children[0]
        if a.type != "assignment":
            continue
        name, typ, value = (a.child_by_field_name(k) for k in ("left", "type", "right"))
        if typ is not None and name is not None and name.type == "identifier" and _field_call(pf, value):
            yield a, text(name), text(typ), value


@module_detector("pydantic-annotated-field")
def detect_pydantic_annotated(pf: ParsedFile) -> list[IntentMatch]:
    """Required model fields written `x: T = Field(...)`: they read as if they had a default."""
    from .more import _pydantic_models

    models = _pydantic_models(pf)
    if not models:
        return []
    ann, imports = annotated_name(pf)
    edits, names, classes, uses_annotated = [], [], [], False
    for cls in nodes(pf, "class_definition"):
        if cls.type != "class_definition" or text(cls.child_by_field_name("name")) not in models:
            continue
        cls_name = text(cls.child_by_field_name("name"))
        before = len(edits)
        for a, name, typ, call in _model_fields(pf, cls):
            parsed = _args(call)
            if parsed is None or _is_annotated(typ, ann):
                continue
            pos, kws = parsed
            ellipsis = (pos and text(pos[0]) in ("...", "Ellipsis")) or (
                "default" in kws and text(kws["default"].child_by_field_name("value")) in ("...", "Ellipsis")
            )
            has_default = (pos and not ellipsis) or ("default" in kws and not ellipsis) or "default_factory" in kws
            if has_default or len(pos) > 1:
                continue  # a field with a default keeps the assignment form
            rest = [text(v) for k, v in kws.items() if k != "default"]
            fn_name = text(call.child_by_field_name("function"))
            if KEEP_ASSIGNED & set(kws) - {"default"}:
                if not ellipsis:
                    continue  # `Field(alias="x")`: already the recommended spelling for an alias
                new = f"{name}: {typ} = {fn_name}({', '.join(rest)})"  # only drop the discouraged `...`
            elif rest:
                new = f"{name}: {ann}[{typ}, {fn_name}({', '.join(rest)})]"
                uses_annotated = True
            else:
                new = f"{name}: {typ}"  # `= Field(...)` alone: a plain required field
            edits.append((a.start_byte, a.end_byte, new))
            names.append(f"{cls_name}.{name}")
        if len(edits) > before:
            classes.append(cls)
    if not edits:
        return []
    first = classes[0]
    cls_name = text(first.child_by_field_name("name"))
    shown = ", ".join(f"`{n}`" for n in names[:4]) + (f" and {len(names) - 4} more" if len(names) > 4 else "")
    return [
        IntentMatch(
            "pydantic-annotated-field",
            Function(cls_name, cls_name, pf.path, line(first), first.end_point[0] + 1, line(first)),
            line(first),
            f"{len(names)} required fields are written as if they had a default: {shown}",
            {"edits": edits, "imports": imports if uses_annotated else [], "fields": names},
        )
    ]


def _constraint_key(typ: str, call: Node) -> tuple[str, str] | None:
    parsed = _args(call)
    if parsed is None:
        return None
    pos, kws = parsed
    if KEEP_ASSIGNED & set(kws) - {"default"} or len(pos) > 1:
        return None  # an alias/factory field: the constraints may still repeat, but the type can't carry it
    constraints = {k: text(v.child_by_field_name("value")) for k, v in kws.items() if k in CONSTRAINTS}
    if not constraints or set(kws) - CONSTRAINTS - PER_FIELD - {"default"}:
        return None
    return typ, ", ".join(f"{k}={v}" for k, v in sorted(constraints.items()))


@graph_detector("pydantic-repeated-constraints")
def detect_repeated_constraints(graph: CodeGraph) -> list[IntentMatch]:
    """The same type and Field constraints on three or more fields: a reusable Annotated type."""
    from .more import _pydantic_models

    groups: dict[tuple[str, str], list[tuple[ParsedFile, str, str, Node]]] = defaultdict(list)
    for pf in graph.files.values():
        if b"Field" not in pf.source or is_test_file(pf.path):
            continue
        models = _pydantic_models(pf)
        for cls in nodes(pf, "class_definition"):
            if cls.type != "class_definition" or text(cls.child_by_field_name("name")) not in models:
                continue
            for a, name, typ, call in _model_fields(pf, cls):
                key = _constraint_key(typ, call)
                if key:
                    groups[key].append((pf, text(cls.child_by_field_name("name")), name, a))
    out = []
    for (typ, constraints), uses in sorted(groups.items()):
        if len(uses) < 3:
            continue
        pf, cls_name, name, a = uses[0]
        common = max({u[2] for u in uses}, key=lambda n: (sum(u[2] == n for u in uses), n))
        alias = "".join(w.capitalize() for w in common.split("_") if w)
        where = ", ".join(f"`{u[1]}.{u[2]}`" for u in uses[:4])
        where += f" and {len(uses) - 4} more" if len(uses) > 4 else ""
        ann, _ = annotated_name(pf)
        out.append(
            IntentMatch(
                "pydantic-repeated-constraints",
                Function(cls_name, cls_name, pf.path, line(a), line(a), line(a)),
                line(a),
                f"`{typ}` with `Field({constraints})` is declared on {len(uses)} fields: {where}",
                {
                    "uses": [f"{u[0].path}:{line(u[3])}" for u in uses],
                    "advice": f"Here define `{alias} = {ann}[{typ}, Field({constraints})]` once, then declare each "
                    f"field as `{common}: {alias}` (keeping its own default or description in its `Field()`).",
                },
            )
        )
    return out
