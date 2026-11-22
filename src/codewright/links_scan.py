"""Per-file facts for hidden links, gathered once while the syntax tree is in memory (stored in `pf.cache`).

What a file says about state and settings other files share with it: environment variables it reads, upper-case
constants it defines, module globals its functions reassign (`global X`), globals that start as None and are used
without a check, names it imports at module level, and state of other modules it sets or mutates.
"""

from __future__ import annotations

import re

from tree_sitter import Node

from .graph import ParsedFile, on_parse, text

VERSION = 3
KEY = "links"

_CONST = re.compile(r"^[A-Z][A-Z0-9_]*$")
_LITERALS = {"string", "integer", "float", "true", "false"}
_ENV_CALLS = {"os.getenv", "getenv", "os.environ.get", "environ.get"}
_MUTATORS = {"append", "extend", "update", "add", "setdefault", "insert", "pop", "clear", "remove", "discard"}


def _literal(node: Node | None) -> str | None:
    """Normalised text of a literal (`"x"`, `30`, `-1.5`, `True`), else None."""
    if node is None:
        return None
    if node.type == "unary_operator" and node.named_children and node.named_children[0].type in ("integer", "float"):
        return text(node).replace(" ", "")
    if node.type not in _LITERALS:
        return None
    if node.type == "string":
        if any(c.type == "interpolation" for c in node.named_children):
            return None
        return repr("".join(text(c) for c in node.named_children if c.type == "string_content"))
    return text(node).replace("_", "")


def _string_value(node: Node | None) -> str | None:
    if node is None or node.type != "string" or any(c.type == "interpolation" for c in node.named_children):
        return None
    return "".join(text(c) for c in node.named_children if c.type == "string_content")


def scan(pf: ParsedFile) -> dict:
    envs: list[list] = []  # [name, default or None, line, owner]
    consts: list[list] = []  # [NAME, value, line]
    top_imports: list[list] = []  # [bound name, line]: module-level `from m import X`
    module_values: dict[str, list[bool]] = {}  # module-level name -> [assigned None?] per assignment
    rebinds: list[list] = []  # [name, function, line]: `global X` and an assignment to X in a function
    object_uses: list[list] = []  # [name, function, line]: X.attr / X(...) / X[...] in a function
    outside: list[list] = []  # [object, attr or "", how, line, owner]: m.X = v / m.X[...] = v / X.append(...)
    checked: dict[str, set[str]] = {}  # function -> globals it tests against None
    bound = {a for a in pf.module.imports if "." not in a and "#" not in a and not a.startswith("*")}

    src = pf.source
    has_global = b"global " in src
    deep = has_global or b"environ" in src or b"getenv" in src or _touches(src, bound)

    def walk(node: Node, owner: str, globals_: set[str]) -> None:
        stack = [(node, owner, globals_)]
        while stack:
            n, own, gl = stack.pop()
            t = n.type
            if t in ("function_definition", "class_definition") and not deep:
                continue  # nothing in the file needs its bodies: module-level facts only
            if t in ("function_definition", "class_definition"):
                name = text(n.child_by_field_name("name"))
                qual = f"{own}.{name}" if own else name
                body = n.child_by_field_name("body")
                if body is not None:
                    inner: set[str] = set()
                    if t == "function_definition" and has_global:
                        for g in _globals(body):
                            inner.add(g)
                        checked[qual] = _none_checked(text(body))
                    stack.append((body, qual, inner))
                for c in n.children:
                    if c.type in ("parameters", "argument_list", "decorator"):
                        stack.append((c, own, gl))
                continue
            if t == "import_from_statement" and not own:
                mod = n.child_by_field_name("module_name")
                for c in n.named_children:
                    if c == mod:
                        continue
                    if c.type == "dotted_name":
                        top_imports.append([text(c), n.start_point[0] + 1])
                    elif c.type == "aliased_import":
                        top_imports.append([text(c.child_by_field_name("alias")), n.start_point[0] + 1])
                continue
            if t in ("assignment", "augmented_assignment"):
                left, right = n.child_by_field_name("left"), n.child_by_field_name("right")
                line = n.start_point[0] + 1
                if left is not None and left.type == "identifier":
                    name = text(left)
                    if not own:
                        module_values.setdefault(name, []).append(right is not None and right.type == "none")
                        value = _literal(right)
                        if value is not None and _CONST.match(name) and t == "assignment":
                            consts.append([name, value, line])
                    elif name in gl:
                        rebinds.append([name, own, line])
                elif left is not None and left.type == "attribute":
                    obj = left.child_by_field_name("object")
                    if obj is not None and obj.type == "identifier" and text(obj) in bound:
                        outside.append([text(obj), text(left.child_by_field_name("attribute")), "set", line, own])
                elif left is not None and left.type == "subscript":
                    target = left.child_by_field_name("value")
                    if target is not None and target.type in ("identifier", "attribute"):
                        head = text(target).split(".")[0]
                        if head in bound:
                            obj, _, attr = text(target).partition(".")
                            outside.append([obj, attr, "item", line, own])
            elif t == "call":
                fn = n.child_by_field_name("function")
                args = n.child_by_field_name("arguments")
                name = text(fn) if fn is not None else ""
                if name in _ENV_CALLS and args is not None and args.named_children:
                    key = _string_value(args.named_children[0])
                    if key and re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", key):
                        default = args.named_children[1] if len(args.named_children) > 1 else None
                        envs.append([key, text(default) if default is not None else None, n.start_point[0] + 1, own])
                if fn is not None and fn.type == "attribute":
                    obj = fn.child_by_field_name("object")
                    method = text(fn.child_by_field_name("attribute"))
                    if method in _MUTATORS and obj is not None and obj.type in ("identifier", "attribute"):
                        head = text(obj).split(".")[0]
                        if head in bound and own:
                            o, _, attr = text(obj).partition(".")
                            outside.append([o, attr, method, n.start_point[0] + 1, own])
                    if obj is not None and obj.type == "identifier" and own:
                        object_uses.append([text(obj), own, n.start_point[0] + 1])
                elif fn is not None and fn.type == "identifier" and own:
                    object_uses.append([name, own, n.start_point[0] + 1])
            elif t == "subscript" and own:
                v = n.child_by_field_name("value")
                if v is not None and v.type == "identifier":
                    object_uses.append([text(v), own, n.start_point[0] + 1])
            elif t == "attribute" and own:
                obj = n.child_by_field_name("object")
                if obj is not None and obj.type == "identifier":
                    object_uses.append([text(obj), own, n.start_point[0] + 1])
            if t == "subscript":
                v = n.child_by_field_name("value")
                written = (
                    n.parent is not None
                    and n.parent.type == "assignment"
                    and (n.parent.child_by_field_name("left") == n)
                )
                if v is not None and text(v) in ("os.environ", "environ") and not written:
                    key = _string_value(n.child_by_field_name("subscript"))
                    if key and re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", key):
                        envs.append([key, None, n.start_point[0] + 1, own])
            stack.extend((c, own, gl) for c in n.children)

    walk(pf.root, "", set())
    none_globals = sorted(n for n, vs in module_values.items() if vs and all(vs))
    ng = set(none_globals)
    return {
        "v": VERSION,
        "envs": envs,
        "consts": consts,
        "top_imports": top_imports,
        "rebinds": rebinds,
        "none_globals": none_globals,
        "unchecked": [u for u in object_uses if u[0] in ng and u[0] not in checked.get(u[1], set())],
        "outside": outside,
    }


def _touches(src: bytes, bound: set[str]) -> bool:
    """Whether the file may set or mutate something it imported: `name.attr = `, `name[...] = `, `name.append(`."""
    if not bound:
        return False
    names = "|".join(re.escape(b) for b in sorted(bound))
    pat = re.compile(
        rf"\b(?:{names})(?:(?:\.\w+)+\s*=[^=]|(?:\.\w+)*\[[^\]\n]*\]\s*=[^=]|(?:\.\w+)*\.(?:{'|'.join(_MUTATORS)})\()".encode()
    )
    return bool(pat.search(src))


def _globals(body: Node) -> set[str]:
    """Names a function declares `global` (not in nested functions)."""
    out: set[str] = set()
    stack = list(body.children)
    while stack:
        n = stack.pop()
        if n.type in ("function_definition", "class_definition"):
            continue
        if n.type == "global_statement":
            out.update(text(c) for c in n.named_children if c.type == "identifier")
        stack.extend(n.children)
    return out


# Any test of the name counts as a check: `if X`, `X is None`, `not X`, `X and`, `a if X else b`, `assert X`...
_CHECK = re.compile(
    r"\b(?:if|elif|while|not|assert|and|or)\s+(?:not\s+)?(\w+)\b|\b(\w+)\s+(?:is|and|or|==|!=)\b|\bbool\((\w+)\)"
)


def _none_checked(body: str) -> set[str]:
    out: set[str] = set()
    for m in _CHECK.finditer(body):
        out.update(g for g in m.groups() if g)
    return out


@on_parse
def remember(pf: ParsedFile) -> None:
    hit = pf.cache.get(KEY)
    if isinstance(hit, dict) and hit.get("v") == VERSION:
        return
    pf.cache[KEY] = scan(pf)


def facts(pf: ParsedFile) -> dict:
    hit = pf.cache.get(KEY)
    if not (isinstance(hit, dict) and hit.get("v") == VERSION):
        hit = pf.cache[KEY] = scan(pf)
    return hit
