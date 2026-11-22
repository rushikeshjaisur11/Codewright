"""Per-file facts for the Untangle inventory, gathered once while a file's syntax tree is in memory.

Everything here is plain data (lists, dicts, strings) stored in `pf.cache["untangle"]`, so it goes to the disk
cache with the rest of the file and a rescan reads only edited files. The whole-repo reasoning lives in
`untangle.py`; this module only says what each file defines and which names it uses where.
"""

from __future__ import annotations

import ast
import re

from tree_sitter import Node

from .graph import ParsedFile, on_parse, text

VERSION = 6
KEY = "untangle"

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_PREFIX = re.compile(r"([A-Za-z_][A-Za-z0-9_]*_)$")  # "do_" / "visit_" / "handle_": a name built at run time
_BROAD = {"", "Exception", "BaseException", "builtins.Exception"}
_SILENT = {"pass_statement", "continue_statement", "ellipsis"}
_LOGGING = re.compile(r"\b(log|logger|logging|warn|warnings|print|traceback|sentry_sdk|capture_exception)\b")
_CODE_HINT = re.compile(r"[()=\[\]]|^\s*(import|from|return|def|class|if|for|while|with|try|raise|elif|else)\b")
_NOT_CODE = re.compile(r"^\s*(type:|noqa|pragma|fmt:|isort:|pylint:|mypy:|ruff:|TODO|FIXME|XXX|NOTE|-\*-|!)", re.I)
_PARAM_TYPES = ("identifier", "typed_parameter", "default_parameter", "typed_default_parameter")
_DOTTED = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+(?::[A-Za-z_]\w*)?$")


def _param_names(params: Node | None) -> tuple[list[str], bool]:
    """(named parameters in order, whether it takes *args or **kwargs)."""
    names: list[str] = []
    splat = False
    for p in params.named_children if params is not None else []:
        if p.type in ("list_splat_pattern", "dictionary_splat_pattern"):
            splat = True
            continue
        if p.type not in _PARAM_TYPES:
            if p.type in ("typed_parameter",) or any(
                c.type in ("list_splat_pattern", "dictionary_splat_pattern") for c in p.named_children
            ):
                splat = True
            continue
        ident = p if p.type == "identifier" else (p.child_by_field_name("name") or p.named_children[0])
        if ident.type == "identifier":
            names.append(text(ident))
        else:  # typed *args: typed_parameter wrapping a list_splat_pattern
            splat = True
    return names, splat


def _docstring_free(body: Node) -> list[Node]:
    stmts = [c for c in body.named_children if c.type != "comment"]
    if stmts and stmts[0].type == "expression_statement" and stmts[0].named_children[0].type == "string":
        stmts = stmts[1:]
    return stmts


def _trivial(body: Node | None) -> bool:
    """A body that does nothing on purpose: pass, `...`, a docstring, or `raise NotImplementedError`."""
    if body is None:
        return True
    stmts = _docstring_free(body)
    if not stmts:
        return True
    if len(stmts) != 1:
        return False
    s = stmts[0]
    if s.type == "pass_statement":
        return True
    if s.type == "expression_statement" and s.named_children and s.named_children[0].type == "ellipsis":
        return True
    return s.type == "raise_statement" and "NotImplementedError" in text(s)


def _forwarding(defn: Node, params: list[str], splat: bool) -> str | None:
    """The call a function only passes its arguments on to (`def a(x, y): return b(x, y)`), else None."""
    body = defn.child_by_field_name("body")
    if body is None:
        return None
    stmts = _docstring_free(body)
    if len(stmts) != 1:
        return None
    s = stmts[0]
    if (s.type == "return_statement" and s.named_children) or (s.type == "expression_statement" and s.named_children):
        expr = s.named_children[0]
    else:
        return None
    if expr.type == "await":
        expr = expr.named_children[0] if expr.named_children else expr
    if expr.type != "call":
        return None
    fn = expr.child_by_field_name("function")
    args = expr.child_by_field_name("arguments")
    if fn is None or args is None or fn.type not in ("identifier", "attribute"):
        return None
    if not re.fullmatch(r"[A-Za-z_][\w.]*", text(fn)):
        return None  # `Signer(sep=".").sign`: it builds something first
    passed: list[str] = []
    for a in args.named_children:
        if a.type == "identifier":
            passed.append(text(a))
        elif a.type == "keyword_argument":
            name, value = a.child_by_field_name("name"), a.child_by_field_name("value")
            if value is None or value.type != "identifier" or text(name) != text(value):
                return None
            passed.append(text(value))
        elif a.type in ("list_splat", "dictionary_splat"):
            if not splat:
                return None
        else:
            return None  # a literal, an expression: it adds behaviour
    own = [p for p in params if p not in ("self", "cls")]
    if passed != own:
        return None
    target = text(fn)
    if target.split(".")[0] in ("self", "cls", "super") or target.startswith("super("):
        return None  # delegation inside a class hierarchy is a design choice, not surplus
    return target


def _silent_handler(node: Node) -> tuple[str, str] | None:
    """(caught type, what it does) for an except clause that swallows a broad exception without a trace."""
    body = next((c for c in node.named_children if c.type == "block"), None)
    if body is None:
        return None
    caught = ""
    for c in node.named_children:
        if c.type == "block":
            break
        if c.type == "as_pattern":
            c = c.named_children[0] if c.named_children else c
        caught = text(c)
        break
    names = {n.strip() for n in caught.strip("()").split(",")}
    if not names & _BROAD and caught:
        return None
    stmts = [c for c in body.named_children if c.type != "comment"]
    if len(stmts) != 1:
        return None
    s = stmts[0]
    kind = s.type
    if kind == "expression_statement" and s.named_children and s.named_children[0].type == "ellipsis":
        kind = "ellipsis"
    if kind in _SILENT:
        return caught or "everything", "pass" if kind != "continue_statement" else "continue"
    if kind == "return_statement" and (not s.named_children or text(s.named_children[0]) in ("None", "False", "[]")):
        return caught or "everything", text(s)
    if _LOGGING.search(text(body)):
        return None
    return None


def _commented_code(lines: list[str]) -> list[tuple[int, int]]:
    """(first, last) line of each block of comments that is code: at least two lines that parse as Python
    and look like statements, not prose."""
    out: list[tuple[int, int]] = []
    i, n = 0, len(lines)
    while i < n:
        if not lines[i].lstrip().startswith("#"):
            i += 1
            continue
        j = i
        while j < n and lines[j].lstrip().startswith("#"):
            j += 1
        block = [ln.lstrip()[1:] for ln in lines[i:j]]
        if (i > 2 or not any("copyright" in b.lower() or "license" in b.lower() for b in block)) and not _explains(
            block
        ):
            out.extend(_code_runs(block, i))
        i = j
    return out


def _explains(block: list[str]) -> bool:
    """A comment that explains with an example: prose around a few lines of code, or code lines that carry
    their own annotations (`foo()  # <- this races`). The code there is an illustration, not leftovers."""
    filled = [b for b in block if b.strip()]
    prose = [b for b in filled if not _looks_like_code(b)]
    if any("#" in b for b in filled if _looks_like_code(b)):
        return True
    return len(prose) >= 2 and len(prose) >= len(filled) - len(prose)


def _code_runs(block: list[str], start: int) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    k = 0
    while k < len(block):
        if not _looks_like_code(block[k]):
            k += 1
            continue
        m = k
        while m < len(block) and (_looks_like_code(block[m]) or not block[m].strip()):
            m += 1
        chunk = block[k:m]
        while chunk and not chunk[-1].strip():
            chunk.pop()
            m -= 1
        if len([c for c in chunk if c.strip()]) >= 2 and _parses(chunk):
            runs.append((start + k + 1, start + m))
        k = max(m, k + 1)
    return runs


def _looks_like_code(line: str) -> bool:
    s = line.strip()
    if not s or _NOT_CODE.match(s) or (s.endswith((".", "?", "!")) and not s.endswith("...")):
        return False
    if len(s.split()) > 3 and not _CODE_HINT.search(s):
        return False  # prose: several words and nothing a statement needs
    return bool(_CODE_HINT.search(s)) and not s.startswith(("http://", "https://", "e.g.", "i.e."))


def _parses(chunk: list[str]) -> bool:
    src = "\n".join(c[1:] if c.startswith(" ") else c for c in chunk)
    import textwrap

    src = textwrap.dedent(src)
    try:
        tree = ast.parse(src)
    except SyntaxError:
        try:  # a block body cut from its header: `    return x` / `    y = 1`
            tree = ast.parse("if 1:\n" + textwrap.indent(src, "    "))
        except SyntaxError:
            return False
    # A lone name or a string is prose that happens to parse ("# Example", "# 'quoted'").
    nodes = list(ast.walk(tree))
    return any(
        isinstance(
            n,
            ast.Call
            | ast.Assign
            | ast.AugAssign
            | ast.Return
            | ast.Import
            | ast.ImportFrom
            | ast.FunctionDef
            | ast.If
            | ast.For
            | ast.With
            | ast.Raise
            | ast.Compare
            | ast.Subscript,
        )
        for n in nodes
    )


def scan(pf: ParsedFile) -> dict:
    """What the file defines and uses. Owners are qualnames ("" is module level)."""
    defs: list[dict] = []
    refs: dict[str, set[str]] = {}
    values: set[str] = set()  # names used other than by calling them (passed, stored, returned)
    strings: set[str] = set()
    dotted: set[str] = set()  # "pkg.module.name" / "pkg.module:name": a path a framework imports by
    prefixes: set[str] = set()
    all_names: list[str] = []
    imports: list[tuple[str, str]] = []  # (bound name, imported name)
    swallowed: list[dict] = []
    getattr_dynamic: set[str] = set()  # definitions that call getattr/hasattr with a computed name
    root = pf.root

    def add_ref(owner: str, name: str) -> None:
        s = refs.get(owner)
        if s is None:
            s = refs[owner] = set()
        s.add(name)

    stack: list[tuple[Node, str, str]] = [(root, "", "")]  # (node, owner, qualname prefix)
    while stack:
        node, owner, prefix = stack.pop()
        t = node.type
        if t in ("import_statement", "import_from_statement"):
            for c in node.named_children:
                if c.type == "dotted_name" and t == "import_statement":
                    imports.append((text(c).split(".")[0], text(c).split(".")[0]))
                elif c.type == "aliased_import":
                    name, alias = c.child_by_field_name("name"), c.child_by_field_name("alias")
                    imports.append((text(alias), text(name).split(".")[-1]))
                elif c.type == "dotted_name" and c != node.child_by_field_name("module_name"):
                    imports.append((text(c), text(c).split(".")[-1]))
            continue
        if t == "decorated_definition" or t in ("function_definition", "class_definition"):
            outer = node
            defn = node.child_by_field_name("definition") if t == "decorated_definition" else node
            if defn is None:
                continue
            decorators = [text(d)[1:].strip() for d in outer.children if d.type == "decorator"]
            for d in outer.children:
                if d.type == "decorator":
                    stack.append((d, owner, prefix))
            name = text(defn.child_by_field_name("name"))
            qual = f"{prefix}{name}"
            body = defn.child_by_field_name("body")
            item: dict = {
                "q": qual,
                "n": name,
                "k": "c" if defn.type == "class_definition" else "f",
                "s": outer.start_point[0] + 1,
                "e": outer.end_point[0] + 1,
                "d": defn.start_point[0] + 1,
                "deco": decorators,
                "parent": owner,
            }
            if defn.type == "class_definition":
                supers = defn.child_by_field_name("superclasses")
                item["bases"] = [
                    text(b)
                    for b in (supers.named_children if supers is not None else [])
                    if b.type != "keyword_argument"
                ]
                item["meta"] = any(
                    b.type == "keyword_argument" for b in (supers.named_children if supers is not None else [])
                )
                if supers is not None:
                    stack.append((supers, owner, prefix))
            else:
                params, splat = _param_names(defn.child_by_field_name("parameters"))
                item["trivial"] = _trivial(body)
                item["params"] = params
                item["splat"] = splat
                ps_node = defn.child_by_field_name("parameters")
                item["defaults"] = any(
                    p.type in ("default_parameter", "typed_default_parameter")
                    for p in (ps_node.named_children if ps_node is not None else [])
                )
                fwd = _forwarding(defn, params, splat) if not decorators else None
                if fwd:
                    item["forwards"] = fwd
                # Defaults and annotations are evaluated where the function is defined.
                ps = defn.child_by_field_name("parameters")
                if ps is not None:
                    for p in ps.named_children:
                        for c in p.named_children:
                            if c.type != "identifier" or p.type == "identifier":
                                stack.append((c, owner, prefix))
                rt = defn.child_by_field_name("return_type")
                if rt is not None:
                    stack.append((rt, owner, prefix))
                if body is not None and not item["trivial"]:
                    used = _identifiers(body)
                    item["unused_params"] = [
                        p for p in params if p not in used and p not in ("self", "cls") and not p.startswith("_")
                    ]
            defs.append(item)
            if body is not None:
                stack.append((body, qual, f"{qual}."))
            continue
        if t == "identifier":
            parent = node.parent
            if parent is not None and parent.type == "keyword_argument" and parent.child_by_field_name("name") == node:
                continue
            name = text(node)
            add_ref(owner, name)
            called = parent is not None and parent.type == "call" and parent.child_by_field_name("function") == node
            object_of = (
                parent is not None and parent.type == "attribute" and parent.child_by_field_name("object") == node
            )
            if parent is not None and not called and not object_of:
                values.add(name)
            continue
        if t == "string_content":
            s = text(node)
            strings.update(_WORD.findall(s))
            if _DOTTED.match(s):
                dotted.add(s.replace(":", "."))
            m = _PREFIX.search(s)
            if m and node.next_named_sibling is not None and node.next_named_sibling.type == "interpolation":
                prefixes.add(m.group(1))
            elif m and s == m.group(1):
                prefixes.add(s)
            continue
        if t == "call":
            fn = node.child_by_field_name("function")
            if fn is not None and text(fn) in ("getattr", "hasattr", "setattr", "operator.attrgetter", "attrgetter"):
                args = node.child_by_field_name("arguments")
                second = args.named_children[1] if args is not None and len(args.named_children) > 1 else None
                if second is not None and not _literal_prefix(second):
                    getattr_dynamic.add(owner)
        elif t == "except_clause":
            hit = _silent_handler(node)
            if hit is not None:
                swallowed.append({"line": node.start_point[0] + 1, "owner": owner, "caught": hit[0], "does": hit[1]})
        elif t == "assignment" and node.parent is not None and node.parent.type == "expression_statement":
            left = node.child_by_field_name("left")
            if left is not None and text(left) == "__all__":
                right = node.child_by_field_name("right")
                all_names += re.findall(r"[\"']([A-Za-z_]\w*)[\"']", text(right))
        stack.extend((c, owner, prefix) for c in node.children)

    return {
        "v": VERSION,
        "defs": defs,
        "refs": {k: sorted(v) for k, v in refs.items()},
        "values": sorted(values),
        "strings": sorted(strings),
        "dotted": sorted(dotted),
        "prefixes": sorted(prefixes),
        "all": all_names,
        "imports": imports,
        "swallowed": swallowed,
        "dynamic": sorted(getattr_dynamic),
        "commented": _commented_code(pf.lines),
    }


def _literal_prefix(arg: Node) -> bool:
    """True when a getattr name is a plain string or an f-string that starts with a name prefix (`f"do_{x}"`):
    then only names with that prefix are reachable. Anything else (a variable, `f"{x}"`) can reach any name."""
    if arg.type != "string":
        return False
    parts = [c for c in arg.named_children if c.type in ("string_content", "interpolation")]
    if not any(c.type == "interpolation" for c in parts):
        return True
    return bool(parts) and parts[0].type == "string_content" and bool(_PREFIX.search(text(parts[0])))


def _identifiers(node: Node) -> set[str]:
    out: set[str] = set()
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == "identifier":
            out.add(text(n))
        elif n.type == "string_content" and "{" in text(n):
            out.update(_WORD.findall(text(n)))  # str.format templates name their fields
        stack.extend(n.children)
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
