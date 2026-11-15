"""Codewright's own code graph, built with tree-sitter (no external indexers).

For each Python file it records imports, functions (with decorators, params and calls)
and builds a name-resolved call graph across the repo. Entry points (web routes, CLI
commands, ``__main__`` blocks) seed the hot-path calculation used in ranking.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_python
from tree_sitter import Language, Node, Parser

from .models import Function, Module
from .stack import SKIP_DIRS

PY = Language(tree_sitter_python.language())
_PARSER = Parser(PY)

ROUTE_DECORATOR_SUFFIXES = (
    ".get",
    ".post",
    ".put",
    ".patch",
    ".delete",
    ".route",
    ".websocket",
    ".api_route",
    ".command",
    ".callback",
    ".task",
)


def text(node: Node | None) -> str:
    return node.text.decode("utf-8", errors="replace") if node is not None else ""


@dataclass
class ParsedFile:
    path: str  # relative
    source: bytes
    root: Node
    module: Module
    nodes: dict[str, Node] = field(default_factory=dict)  # qualname -> function_definition node

    @property
    def lines(self) -> list[str]:
        return self.source.decode("utf-8", errors="replace").splitlines(keepends=True)


@dataclass
class CodeGraph:
    root: str
    files: dict[str, ParsedFile] = field(default_factory=dict)
    edges: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))  # caller -> callees
    entry_points: set[str] = field(default_factory=set)

    def functions(self):
        for pf in self.files.values():
            for fn in pf.module.functions:
                yield pf, fn

    def hot_functions(self) -> set[str]:
        """Every function reachable from an entry point."""
        seen: set[str] = set()
        stack = list(self.entry_points)
        while stack:
            q = stack.pop()
            if q in seen:
                continue
            seen.add(q)
            stack.extend(self.edges.get(q, ()))
        return seen

    def test_reachable(self) -> set[str]:
        """Every function some test function reaches through the call graph."""
        from .intents.detectors import is_test_file

        starts = [self.key(fn) for pf, fn in self.functions() if is_test_file(pf.path)]
        seen: set[str] = set()
        stack = list(starts)
        while stack:
            q = stack.pop()
            if q in seen:
                continue
            seen.add(q)
            stack.extend(self.edges.get(q, ()))
        return seen - set(starts)

    def key(self, fn: Function) -> str:
        return f"{fn.file}::{fn.qualname}"


def iter_python_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
            continue
        yield path


def parse_file(path: Path, root: Path) -> ParsedFile:
    source = path.read_bytes()
    tree = _PARSER.parse(source)
    rel = str(path.relative_to(root))
    module = Module(file=rel)
    pf = ParsedFile(rel, source, tree.root_node, module)
    _collect(tree.root_node, pf, prefix="")
    return pf


def _collect(node: Node, pf: ParsedFile, prefix: str) -> None:
    for child in node.children:
        kind = child.type
        if kind in ("import_statement", "import_from_statement"):
            _imports(child, pf.module)
        elif kind == "if_statement" and "__name__" in text(child.child_by_field_name("condition")):
            pf.module.has_main_guard = True
            _collect(child, pf, prefix)
        elif kind == "decorated_definition":
            definition = child.child_by_field_name("definition")
            decorators = [text(d)[1:].strip() for d in child.children if d.type == "decorator"]
            _definition(definition, pf, prefix, decorators, outer=child)
        elif kind in ("function_definition", "class_definition"):
            _definition(child, pf, prefix, [], outer=child)
        elif kind in ("block", "if_statement", "try_statement", "with_statement", "else_clause"):
            _collect(child, pf, prefix)


def _definition(defn: Node | None, pf: ParsedFile, prefix: str, decorators: list[str], outer: Node) -> None:
    if defn is None:
        return
    name = text(defn.child_by_field_name("name"))
    qual = f"{prefix}{name}"
    body = defn.child_by_field_name("body")
    if defn.type == "class_definition":
        if body is not None:
            _collect(body, pf, prefix=f"{qual}.")
        return
    params = []
    for p in (defn.child_by_field_name("parameters") or defn).named_children:
        ident = (
            p
            if p.type == "identifier"
            else p.child_by_field_name("name") or (p.named_children[0] if p.named_children else None)
        )
        if ident is not None and ident.type == "identifier":
            params.append(text(ident))
    fn = Function(
        qualname=qual,
        name=name,
        file=pf.path,
        start_line=outer.start_point[0] + 1,
        end_line=outer.end_point[0] + 1,
        def_line=defn.start_point[0] + 1,
        decorators=decorators,
        calls=sorted(set(_calls(body))) if body is not None else [],
        params=params,
        is_async=text(defn).startswith("async"),
    )
    pf.module.functions.append(fn)
    pf.nodes[qual] = defn
    if body is not None:
        _collect(body, pf, prefix=f"{qual}.")


def _calls(node: Node) -> list[str]:
    out = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in ("function_definition", "class_definition") and n is not node:
            continue  # nested definitions own their calls
        if n.type == "call":
            fn = n.child_by_field_name("function")
            if fn is not None and fn.type in ("identifier", "attribute"):
                out.append(text(fn))
        stack.extend(n.children)
    return out


def _imports(node: Node, module: Module) -> None:
    if node.type == "import_statement":
        for child in node.named_children:
            if child.type == "dotted_name":
                full = text(child)
                module.imports[full.split(".")[0]] = full.split(".")[0]
                module.imports[full] = full
            elif child.type == "aliased_import":
                module.imports[text(child.child_by_field_name("alias"))] = text(child.child_by_field_name("name"))
        return
    mod_node = node.child_by_field_name("module_name")
    base = text(mod_node)
    for child in node.named_children:
        if mod_node is not None and child.start_byte == mod_node.start_byte:
            continue
        if child.type == "dotted_name":
            module.imports[text(child)] = f"{base}.{text(child)}"
        elif child.type == "aliased_import":
            alias = text(child.child_by_field_name("alias"))
            module.imports[alias] = f"{base}.{text(child.child_by_field_name('name'))}"


def resolve(module: Module, dotted: str) -> str:
    """Expand a dotted name as written in code to its fully qualified import target."""
    head, _, rest = dotted.partition(".")
    target = module.imports.get(head)
    if target is None:
        return dotted
    return f"{target}.{rest}" if rest else target


def build(root: str | Path) -> CodeGraph:
    root = Path(root).resolve()
    graph = CodeGraph(root=str(root))
    for path in iter_python_files(root):
        try:
            pf = parse_file(path, root)
        except (OSError, ValueError):
            continue
        graph.files[pf.path] = pf

    by_name: dict[str, list[str]] = defaultdict(list)
    for _, fn in graph.functions():
        by_name[fn.name].append(graph.key(fn))

    for pf, fn in graph.functions():
        caller = graph.key(fn)
        for call in fn.calls:
            last = call.rsplit(".", 1)[-1]
            for callee in by_name.get(last, ()):
                if callee != caller:
                    graph.edges[caller].add(callee)
        if _is_entry_point(fn, pf.module):
            graph.entry_points.add(caller)
    return graph


def _is_entry_point(fn: Function, module: Module) -> bool:
    if fn.name == "main" and module.has_main_guard:
        return True
    for deco in fn.decorators:
        head = deco.split("(", 1)[0]
        if head.endswith(ROUTE_DECORATOR_SUFFIXES) or head in ("app.command", "click.command", "task"):
            return True
    return False
