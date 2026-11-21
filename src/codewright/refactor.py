"""Architectural refactorings as patches: the structural changes Codewright can make by itself.

Each builder returns a unified diff (several files, new files included) or None when the change
can't be made without risk. "Without risk" is checked, not assumed:

* ``arch-private-import``: give the private name a public alias where it is defined
  (``helper = _helper``) and import the public name. Nothing else moves.
* ``arch-duplicate-logic``: replace each extra copy with an import of the one kept, at the same
  place in the file. Only when the copies' decorators, signature and async-ness match, when every
  global name the body uses means the same thing in both modules, and when the import can't make
  a cycle.
* ``arch-import-cycle``: move the names the weakest link needs into a new module next to their
  current one (``<module>_shared.py``), re-export them from the old module so nothing else
  changes, and point the importer at the new module. Helpers the moved code uses move with it.
  Only when the moved code needs nothing of the cycle. Otherwise, when the importer uses the names
  only inside functions, the import moves into those functions instead (Python's FAQ fix).
* rule ``no-blocking-in-async``: ``time.sleep(x)`` becomes ``await asyncio.sleep(x)``; any other
  blocking call ``f(args)`` becomes ``await asyncio.to_thread(f, args)``.

Patches from findings then go through proof like any other: the tests run before and after.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

from tree_sitter import Node

from .graph import CodeGraph, ParsedFile, text
from .models import IntentMatch

# --------------------------------------------------------------------------- diffs


def unified(before: list[str], after: list[str], fromfile: str, tofile: str) -> str:
    """difflib's unified diff, made applicable: a last line without a newline gets git's
    `\\ No newline at end of file` marker instead of running into the next line (which corrupts the patch)."""
    out = []
    for line in difflib.unified_diff(before, after, fromfile=fromfile, tofile=tofile):
        if line.startswith(("---", "+++")) and not line.endswith("\n"):
            line += "\n"
        out.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(out)


def diff(changes: dict[str, tuple[list[str] | None, list[str]]]) -> str | None:
    """One unified diff for several files. `before` None means a new file."""
    out = []
    for path in sorted(changes):
        before, after = changes[path]
        if before == after:
            continue
        a = "/dev/null" if before is None else f"a/{path}"
        out.append(unified(before or [], after, a, f"b/{path}"))
    return "".join(out) or None


def read_source(path: Path) -> str:
    """A file's text with its line endings as they are (read_text would turn CRLF into LF, and a diff made
    from that doesn't apply to the file)."""
    return path.read_bytes().decode("utf-8", errors="replace")


def _lines(src: str) -> list[str]:
    return src.splitlines(keepends=True)


# --------------------------------------------------------------------------- module scope


def import_name(path: str) -> str:
    """The dotted name code imports `path` by."""
    from .archmap import importable_names, module_name

    mod = module_name(path)
    alts = importable_names(mod)
    return alts[0] if alts else mod


def _module_statements(pf: ParsedFile):
    """Statements at module scope, including those inside top-level if/try/with blocks."""
    stack = list(reversed(pf.root.children))
    while stack:
        n = stack.pop()
        if n.type in (
            "if_statement",
            "try_statement",
            "with_statement",
            "block",
            "else_clause",
            "elif_clause",
            "except_clause",
            "finally_clause",
        ):
            stack.extend(reversed(n.children))
            continue
        yield n


def _def_name(node: Node) -> str | None:
    target = node
    if node.type == "decorated_definition":
        target = node.child_by_field_name("definition")
    if target is not None and target.type in ("function_definition", "class_definition"):
        return text(target.child_by_field_name("name"))
    return None


def _assigned(node: Node) -> list[str]:
    if node.type == "expression_statement" and node.named_children:
        expr = node.named_children[0]
        if expr.type in ("assignment", "augmented_assignment"):
            left = expr.child_by_field_name("left")
            if left is not None:
                return [text(n) for n in _walk(left) if n.type == "identifier"]
    return []


def _walk(node: Node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def bindings(pf: ParsedFile) -> dict[str, list[tuple[str, Node]]]:
    """name -> [(kind, statement)] for every module-scope binding. kind: import | def | assign | nested."""
    from .models import Module

    out: dict[str, list[tuple[str, Node]]] = {}
    top = set(id(c) for c in pf.root.children)
    for st in _module_statements(pf):
        nested = id(st) not in top
        if st.type in ("import_statement", "import_from_statement"):
            from .graph import record_imports

            tmp = Module(pf.path)
            record_imports(st, tmp)
            for alias in tmp.imports:
                if "." not in alias:
                    out.setdefault(alias, []).append(("nested" if nested else "import", st))
        elif (name := _def_name(st)) is not None:
            out.setdefault(name, []).append(("nested" if nested else "def", st))
        else:
            for name in _assigned(st):
                out.setdefault(name, []).append(("nested" if nested else "assign", st))
    return out


def referenced(node: Node) -> set[str]:
    """Identifiers a block reads, minus attribute names and keyword-argument names."""
    out = set()
    for n in _walk(node):
        if n.type != "identifier":
            continue
        p = n.parent
        if p is not None and p.type == "attribute" and p.child_by_field_name("attribute") == n:
            continue
        if p is not None and p.type == "keyword_argument" and p.child_by_field_name("name") == n:
            continue
        out.add(text(n))
    return out


def _import_target(pf: ParsedFile, stmt: Node, alias: str) -> str | None:
    from .archmap import resolve_relative
    from .graph import record_imports
    from .models import Module

    tmp = Module(pf.path)
    record_imports(stmt, tmp)
    t = tmp.imports.get(alias)
    return resolve_relative(t, pf.path) if t else None


def _file_of(graph: CodeGraph, dotted: str) -> str | None:
    from .archmap import ArchMap

    if "archmap" not in graph.__dict__:
        graph.__dict__["archmap"] = ArchMap(graph)
    amap = graph.__dict__["archmap"]
    mod = amap._internal_target(dotted)
    return amap.modules.get(mod) if mod else None


def _span(node: Node) -> tuple[int, int]:
    """0-based first line and exclusive end line of a statement, with a decorated def's decorators."""
    return node.start_point[0], node.end_point[0] + 1


# --------------------------------------------------------------------------- private import -> public alias


def private_import(graph: CodeGraph, m: IntentMatch) -> str | None:
    src = graph.files.get(m.function.file)
    dest = graph.files.get(m.data.get("from", ""))
    if src is None or dest is None or src.path == dest.path:
        return None
    dest_b, src_b = bindings(dest), bindings(src)
    dest_lines = list(dest.lines)
    inserts: list[tuple[int, str]] = []
    renames: dict[str, str] = {}
    for private in m.data.get("names", []):
        public = private.lstrip("_")
        if not public.isidentifier() or public in dest_b or public in src_b:
            return None  # the public name is taken in one of the modules
        found = dest_b.get(private, [])
        if len(found) != 1 or found[0][0] not in ("def", "assign", "import"):
            return None
        _, end = _span(found[0][1])
        inserts.append((end, f"{public} = {private}  # public name; `{private}` stays for existing callers\n"))
        renames[private] = public
    for at, line in sorted(inserts, reverse=True):
        dest_lines.insert(at, line)
    # In the importer: import the public name, and use it.
    edits: list[tuple[int, int, str]] = []
    dest_names = {import_name(dest.path)}
    for st in _module_statements(src):
        if st.type != "import_from_statement":
            continue
        mod = st.child_by_field_name("module_name")
        from .archmap import resolve_relative

        target = resolve_relative(text(mod), src.path) if mod is not None else ""
        if _file_of(graph, target) != dest.path and target not in dest_names:
            continue
        for child in st.named_children:
            if mod is not None and child.start_byte == mod.start_byte:
                continue
            name_node = child if child.type == "dotted_name" else child.child_by_field_name("name")
            if name_node is not None and text(name_node) in renames:
                edits.append((name_node.start_byte, name_node.end_byte, renames[text(name_node)]))
    if not edits:
        return None
    aliased = {
        text(c.child_by_field_name("name"))
        for st in _module_statements(src)
        if st.type == "import_from_statement"
        for c in st.named_children
        if c.type == "aliased_import"
    }
    for n in _walk(src.root):
        if n.type == "identifier" and text(n) in renames and text(n) not in aliased:
            p = n.parent
            if p is not None and p.type in ("dotted_name", "aliased_import", "import_from_statement"):
                continue  # the import itself, handled above
            if p is not None and p.type == "attribute" and p.child_by_field_name("attribute") == n:
                continue
            edits.append((n.start_byte, n.end_byte, renames[text(n)]))
    data = src.source
    for start, end, new in sorted(set(edits), reverse=True):
        data = data[:start] + new.encode() + data[end:]
    return diff({dest.path: (dest.lines, dest_lines), src.path: (src.lines, _lines(data.decode()))})


# --------------------------------------------------------------------------- duplicate logic -> one copy


def _decorators(node: Node) -> list[str]:
    p = node.parent
    if p is not None and p.type == "decorated_definition":
        return [text(c) for c in p.children if c.type == "decorator"]
    return []


def _same_meaning(graph: CodeGraph, a: ParsedFile, b: ParsedFile, names: set[str]) -> bool:
    """Every global name in `names` means the same in modules a and b (builtins, or the same import)."""
    ba, bb = bindings(a), bindings(b)
    for n in names:
        x, y = ba.get(n), bb.get(n)
        if not x and not y:
            continue
        if not x or not y or len(x) != 1 or len(y) != 1 or x[0][0] != "import" or y[0][0] != "import":
            return False
        if _import_target(a, x[0][1], n) != _import_target(b, y[0][1], n):
            return False
    return True


def duplicate_logic(graph: CodeGraph, m: IntentMatch) -> str | None:
    copies = [c.split("::", 1) for c in m.data.get("copies", [])]
    if len(copies) < 2 or any("." in q for _, q in copies):
        return None  # methods: their class decides what `self` is; left to a person
    (keep_file, keep_name), rest = copies[0], copies[1:]
    keep_pf = graph.files.get(keep_file)
    keep_node = keep_pf.nodes.get(keep_name) if keep_pf else None
    if keep_node is None:
        return None
    edits: dict[str, list[tuple[int, int, str]]] = {}
    for file, name in rest:
        pf = graph.files.get(file)
        node = pf.nodes.get(name) if pf else None
        if node is None:
            return None
        if _decorators(node) != _decorators(keep_node) or text(node).startswith("async") != text(keep_node).startswith(
            "async"
        ):
            return None
        used = referenced(node.child_by_field_name("body")) | referenced(node.child_by_field_name("parameters"))
        used -= {name, keep_name}
        if not _same_meaning(graph, pf, keep_pf, used):
            return None
        outer = node.parent if node.parent is not None and node.parent.type == "decorated_definition" else node
        start, end = _span(outer)
        if file == keep_file:
            if keep_node.start_byte > node.start_byte or name == keep_name:
                return None
            line = f"{name} = {keep_name}  # same body as `{keep_name}`\n"
        else:
            if keep_file in graph.importers_of(file):
                return None  # importing it here would make a cycle
            alias = "" if name == keep_name else f" as {name}"
            line = f"from {import_name(keep_file)} import {keep_name}{alias}  # same body; one copy to maintain\n"
        edits.setdefault(file, []).append((start, end, line))
    changes = {}
    for file, rows in edits.items():
        before = graph.files[file].lines
        after = list(before)
        for start, end, line in sorted(rows, reverse=True):  # bottom-up keeps earlier line numbers valid
            after[start:end] = [line]
        changes[file] = (before, after)
    return diff(changes)


# --------------------------------------------------------------------------- import cycle -> move or defer


def import_cycle(graph: CodeGraph, m: IntentMatch) -> str | None:
    cut = m.data.get("cut") or []
    names = m.data.get("names") or []
    if len(cut) != 2 or not names or any("." in n for n in names):
        return None
    a, b = graph.files.get(cut[0]), graph.files.get(cut[1])
    if a is None or b is None:
        return None
    return _move(graph, a, b, names, set(m.data.get("modules", []))) or _defer(graph, a, b, names)


def _statements_from(graph: CodeGraph, a: ParsedFile, b: ParsedFile) -> list[Node]:
    from .archmap import resolve_relative

    out = []
    for st in a.root.children:
        if st.type != "import_from_statement":
            continue
        mod = st.child_by_field_name("module_name")
        if mod is not None and _file_of(graph, resolve_relative(text(mod), a.path)) == b.path:
            out.append(st)
    return out


def _move(graph: CodeGraph, a: ParsedFile, b: ParsedFile, names: list[str], cycle: set[str]) -> str | None:
    if b.path.endswith("__init__.py"):
        return None
    binds = bindings(b)
    moved: list[str] = []
    todo = list(names)
    while todo:  # the names, and the module-level helpers they use
        n = todo.pop()
        if n in moved:
            continue
        found = binds.get(n, [])
        if len(found) != 1 or found[0][0] not in ("def", "assign"):
            return None
        moved.append(n)
        for used in referenced(found[0][1]):
            kinds = binds.get(used, [])
            if any(k == "nested" for k, _ in kinds):
                return None
            if kinds and kinds[0][0] in ("def", "assign") and used not in moved:
                todo.append(used)
    blocks = sorted({id(binds[n][0][1]): binds[n][0][1] for n in moved}.values(), key=lambda s: s.start_byte)
    if any(len(_assigned(s)) > 1 for s in blocks):
        return None  # `a, b = ...`: one statement binding several names
    # Imports the moved code needs, copied as written (same folder, so relative imports keep their meaning).
    need = set().union(*(referenced(s) for s in blocks)) - set(moved)
    imports, seen = [], set()
    for n in sorted(need):
        found = binds.get(n, [])
        if not found:
            continue
        kind, st = found[0]
        if kind != "import":
            return None
        target = _import_target(b, st, n)
        if target and _file_of(graph, target) in cycle:
            return None  # the new module would join the cycle
        if id(st) not in seen:
            seen.add(id(st))
            imports.append(st)
    future = [st for st in b.root.children if st.type == "future_import_statement"]
    folder, stem = b.path.rpartition("/")[0], Path(b.path).stem
    new_stem = f"{stem}_shared"
    while (
        any(p == f"{folder + '/' if folder else ''}{new_stem}.py" for p in graph.files)
        or (Path(graph.root) / folder / f"{new_stem}.py").exists()
    ):
        new_stem += "_"
    new_path = f"{folder}/{new_stem}.py" if folder else f"{new_stem}.py"
    parts = [f'"""Moved out of `{stem}` so `{Path(a.path).stem}` can use it without importing `{stem}`."""\n']
    if future or imports:
        parts.append("\n" + "".join(text(st) + "\n" for st in future + sorted(imports, key=lambda s: s.start_byte)))
    for st in blocks:
        start, end = _span(st)
        parts.append("\n\n" + "".join(b.lines[start:end]).rstrip("\n") + "\n")
    body = _lines("".join(parts))
    # Old module: the moved blocks become one re-export at the first one's place.
    b_after: list[str] = []
    ranges = [_span(s) for s in blocks]
    first = ranges[0][0]
    for i, line in enumerate(b.lines):
        if any(s <= i < e for s, e in ranges):
            if i == first:
                b_after.append(f"from .{new_stem} import {', '.join(sorted(moved))}  # noqa: F401  (re-exported)\n")
            continue
        b_after.append(line)
    b_after = _squeeze_blank(b_after)
    if "." not in import_name(b.path):  # a top-level module: no package for a relative import
        b_after = [ln.replace(f"from .{new_stem} import", f"from {new_stem} import") for ln in b_after]
    # The importer: same names, from the new module.
    a_after = list(a.lines)
    stmts = _statements_from(graph, a, b)
    if not stmts:
        return None
    for st in stmts:
        mod = st.child_by_field_name("module_name")
        written = text(mod)
        new = re.sub(rf"(^|\.){re.escape(stem)}$", rf"\g<1>{new_stem}", written)
        if new == written:
            return None
        row = mod.start_point[0]
        a_after[row] = a_after[row][: mod.start_point[1]] + new + a_after[row][mod.end_point[1] :]
    return diff({new_path: (None, body), b.path: (b.lines, b_after), a.path: (a.lines, a_after)})


def _squeeze_blank(lines: list[str]) -> list[str]:
    """At most two blank lines in a row, as after any removed top-level block."""
    out: list[str] = []
    for ln in lines:
        if not ln.strip() and len(out) >= 2 and not out[-1].strip() and not out[-2].strip():
            continue
        out.append(ln)
    return out


def _defer(graph: CodeGraph, a: ParsedFile, b: ParsedFile, names: list[str]) -> str | None:
    """Move `from b import names` into the functions of `a` that use them."""
    stmts = _statements_from(graph, a, b)
    if not stmts:
        return None
    future = b"from __future__ import annotations" in a.source
    wanted = set(names)
    # Used at module level (decorators, class bodies, default values)? Then deferring would break it.
    skip = {st.start_byte for st in stmts}
    for st in a.root.children:
        if st.start_byte in skip:
            continue
        defn = st.child_by_field_name("definition") if st.type == "decorated_definition" else st
        if defn is not None and defn.type == "function_definition":
            outside = [c for c in (st.children if st.type == "decorated_definition" else []) if c.type == "decorator"]
            params = defn.child_by_field_name("parameters")
            ret = defn.child_by_field_name("return_type")
            sig = set().union(*(referenced(x) for x in [*outside, params, ret] if x is not None))
            if sig & wanted and not (future and not set().union(*(referenced(x) for x in outside)) & wanted):
                return None
            continue
        if referenced(st) & wanted:
            return None
    after = list(a.lines)
    inserts: list[tuple[int, str]] = []
    for fn in a.module.functions:
        node = a.nodes.get(fn.qualname)
        body = node.child_by_field_name("body") if node is not None else None
        if body is None or not body.named_children:
            continue
        own = referenced(body) & wanted
        if not own:
            continue
        first = body.named_children[0]
        if first.type == "expression_statement" and first.named_children and first.named_children[0].type == "string":
            if len(body.named_children) == 1:
                continue
            first = body.named_children[1]
        indent = " " * first.start_point[1]
        mod = text(stmts[0].child_by_field_name("module_name"))
        stmt = f"{indent}from {mod} import {', '.join(sorted(own))}  # deferred: breaks an import cycle\n"
        inserts.append((first.start_point[0], stmt))
    if not inserts:
        return None
    for st in sorted(stmts, key=lambda s: -s.start_byte):
        s, e = _span(st)
        del after[s:e]
        inserts = [(row - (e - s) if row >= e else row, line) for row, line in inserts]
    for row, line in sorted(inserts, reverse=True):
        after.insert(row, line)
    return diff({a.path: (a.lines, after)})


PATCHERS = {
    "arch-private-import": private_import,
    "arch-duplicate-logic": duplicate_logic,
    "arch-import-cycle": import_cycle,
}


# --------------------------------------------------------------------------- rule fix: blocking call in async def


def blocking_call(pf: ParsedFile, call: Node, name: str) -> str | None:
    """`time.sleep(x)` -> `await asyncio.sleep(x)`; `f(args)` -> `await asyncio.to_thread(f, args)`."""
    f = call.child_by_field_name("function")
    args = call.child_by_field_name("arguments")
    if f is None or args is None or args.type != "argument_list":
        return None
    inner = pf.source[args.start_byte + 1 : args.end_byte - 1].decode().strip()
    if name == "time.sleep":
        new = f"await asyncio.sleep({inner})"
    else:
        new = f"await asyncio.to_thread({text(f)}{', ' + inner if inner else ''})"
    parent = call.parent
    if parent is not None and parent.type not in (
        "expression_statement",
        "assignment",
        "return_statement",
        "parenthesized_expression",
        "argument_list",
        "keyword_argument",
    ):
        new = f"({new})"
    data = pf.source[: call.start_byte] + new.encode() + pf.source[call.end_byte :]
    after = _lines(data.decode())
    if "asyncio" not in pf.module.imports:
        from .suggest import _add_import

        after = _add_import(pf, after, "import asyncio\n")
    return diff({pf.path: (pf.lines, after)})


# --------------------------------------------------------------------------- applying several patches at once


def combine(root: str | Path, patches: list[tuple[str, str]]) -> tuple[str, list[str]]:
    """One diff from several (id, patch) pairs, applied in turn to a scratch copy of the files they touch.
    Returns (diff, ids whose patch didn't apply on top of the ones before it)."""
    import shutil
    import tempfile

    from .proof import apply_patch

    root = Path(root)
    touched: set[str] = set()
    for _, p in patches:
        touched |= {m.group(1) for m in re.finditer(r"^\+\+\+ b/(.+)$", p, re.M)}
    if not touched:
        return "", []
    skipped = []
    with tempfile.TemporaryDirectory(prefix="codewright-combine-") as tmp:
        work = Path(tmp)
        for rel in touched:
            if (root / rel).is_file():
                (work / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(root / rel, work / rel)
        for pid, p in patches:
            if not apply_patch(work, p):
                skipped.append(pid)
        changes = {}
        for rel in sorted(touched):
            before = _lines(read_source(root / rel)) if (root / rel).is_file() else None
            after = _lines(read_source(work / rel)) if (work / rel).is_file() else []
            changes[rel] = (before, after)
    return diff(changes) or "", skipped
