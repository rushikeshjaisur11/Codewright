"""Codewright's own code graph, built with tree-sitter (no external indexers).

For each Python file it records imports, classes, functions (with decorators, params
and calls) and builds a call graph across the repo. Calls are resolved through imports,
enclosing scopes and class hierarchies; only calls on objects of unknown type fall back
to matching a method name, and never for names that builtin types already define
(``d.items()`` is not a call to your ``items``). Entry points (web routes, CLI commands,
``__main__`` blocks) seed the hot-path calculation used in ranking.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from functools import cached_property
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
    classes: dict[str, list[str]] = field(default_factory=dict)  # qualname -> base classes as written
    cache: dict[str, object] = field(default_factory=dict)  # per-file results detectors compute once

    @cached_property
    def lines(self) -> list[str]:
        """Source lines with line endings. Shared: copy before changing."""
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
        supers = defn.child_by_field_name("superclasses")
        pf.classes[qual] = [
            text(b) for b in (supers.named_children if supers is not None else []) if b.type != "keyword_argument"
        ]
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
        sep = "" if base.endswith(".") else "."  # `from . import x` -> ".x", not "..x"
        if child.type == "dotted_name":
            module.imports[text(child)] = f"{base}{sep}{text(child)}"
        elif child.type == "aliased_import":
            alias = text(child.child_by_field_name("alias"))
            module.imports[alias] = f"{base}{sep}{text(child.child_by_field_name('name'))}"


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

    resolver = Resolver(graph)
    for pf, fn in graph.functions():
        caller = graph.key(fn)
        for call in fn.calls:
            for callee in resolver.callees(pf, fn, call):
                if callee != caller:
                    graph.edges[caller].add(callee)
        if _is_entry_point(fn, pf.module):
            graph.entry_points.add(caller)
    return graph


# --------------------------------------------------------------------------- call resolution

# Methods of builtin types and common stdlib objects. A call like `x.get()` on an object
# of unknown type is far more likely one of these than a method of the repo's own.
BUILTIN_METHODS = frozenset(
    name
    for t in (dict, list, set, frozenset, str, bytes, bytearray, tuple, int, float, complex, object, BaseException)
    for name in dir(t)
) | {
    # files, sockets, streams
    *("read", "write", "close", "readline", "readlines", "writelines", "flush", "seek", "tell"),
    *("send", "sendall", "recv", "fileno", "readable", "writable"),
    # logging
    *("debug", "info", "warning", "warn", "error", "exception", "critical", "log"),
    # threads, futures, locks, queues, events
    *("submit", "result", "cancel", "done", "cancelled", "shutdown", "acquire", "release", "notify", "notify_all"),
    *("put", "put_nowait", "get_nowait", "task_done", "is_set", "set_result", "set_exception"),
    # regex matches, paths, dates
    *("match", "search", "fullmatch", "findall", "finditer", "sub", "group", "groups", "groupdict", "span"),
    *("exists", "is_file", "is_dir", "mkdir", "read_text", "write_text", "read_bytes", "write_bytes", "glob"),
    *("isoformat", "strftime", "timestamp", "total_seconds"),
}
MAX_GUESSES = 5  # an unresolved method name matching more repo methods than this is too ambiguous to link


def module_aliases(path: str) -> list[str]:
    """Every dotted name a file could be imported by, longest first.

    ``packages/devmem/src/devmem/store.py`` is importable as ``devmem.store`` once
    ``packages/devmem/src`` is on sys.path, so each suffix of the path counts.
    """
    parts = path[:-3].split("/") if path.endswith(".py") else path.split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return [".".join(parts[i:]) for i in range(len(parts))]


def resolve_relative(target: str, from_path: str) -> str:
    """Turn ``.sibling.name`` as imported in `from_path` into an absolute dotted name."""
    if not target.startswith("."):
        return target
    dots = len(target) - len(target.lstrip("."))
    aliases = module_aliases(from_path)
    pkg = aliases[0].split(".") if aliases else []
    if not from_path.endswith("__init__.py"):
        pkg = pkg[:-1]
    pkg = pkg[: len(pkg) - (dots - 1)] if dots > 1 else pkg
    rest = target.lstrip(".")
    return ".".join([*pkg, rest] if rest else pkg)


class Resolver:
    """Resolves the dotted call names recorded per function to function keys in the repo."""

    def __init__(self, graph: CodeGraph):
        self.g = graph
        self.funcs: dict[str, list[str]] = defaultdict(list)  # dotted name -> function keys
        self.classes: dict[str, list[str]] = defaultdict(list)  # dotted name -> class keys
        self.modules: dict[str, list[str]] = defaultdict(list)  # dotted name -> file paths
        self.methods: dict[str, dict[str, str]] = defaultdict(dict)  # class key -> method name -> key
        self.methods_by_name: dict[str, list[str]] = defaultdict(list)
        for path, pf in graph.files.items():
            aliases = module_aliases(path)
            for a in aliases:
                self.modules[a].append(path)
            for cq in pf.classes:
                for a in aliases:
                    self.classes[f"{a}.{cq}"].append(f"{path}::{cq}")
            for fn in pf.module.functions:
                key = graph.key(fn)
                for a in aliases:
                    self.funcs[f"{a}.{fn.qualname}"].append(key)
                parent = fn.qualname.rpartition(".")[0]
                if parent in pf.classes:
                    self.methods[f"{path}::{parent}"][fn.name] = key
                    self.methods_by_name[fn.name].append(key)
        self.bases: dict[str, list[str]] = {}
        self.subclasses: dict[str, list[str]] = defaultdict(list)
        for path, pf in graph.files.items():
            for cq, bases in pf.classes.items():
                ck = f"{path}::{cq}"
                found = []
                for b in bases:
                    target = self._target(pf, b, scope=cq.rpartition(".")[0])
                    found += self.find(target)[1] if target else []
                self.bases[ck] = found
                for b in found:
                    self.subclasses[b].append(ck)
        self._imported: dict[str, set[str]] = {}

    # ----------------------------------------------------------------- lookups
    def _target(self, pf: ParsedFile, dotted: str, scope: str = "") -> str | None:
        """The absolute dotted name `dotted` refers to in `pf`, through its imports or its own names."""
        head, _, rest = dotted.partition(".")
        if head in pf.module.imports:
            t = resolve_relative(pf.module.imports[head], pf.path)
            return f"{t}.{rest}" if rest else t
        own = module_aliases(pf.path)
        if not own:
            return None
        for prefix in _scopes(scope):
            name = f"{prefix}.{head}" if prefix else head
            if name in pf.classes or name in pf.nodes:
                return f"{own[0]}.{prefix}.{dotted}" if prefix else f"{own[0]}.{dotted}"
        return None

    def find(self, dotted: str, depth: int = 0) -> tuple[list[str], list[str]]:
        """(function keys, class keys) for an absolute dotted name, following re-exports."""
        if dotted in self.funcs:
            return self.funcs[dotted], []
        if dotted in self.classes:
            return [], self.classes[dotted]
        parts = dotted.split(".")
        cls = ".".join(parts[:-1])
        if len(parts) > 1 and cls in self.classes:  # inherited method: Model.create -> Base.create
            return [k for c in self.classes[cls] for k in self.method(c, parts[-1])], []
        if depth >= 4:
            return [], []
        for i in range(len(parts) - 1, 0, -1):  # `pkg.name` where pkg/__init__.py re-exports name
            mod = ".".join(parts[:i])
            for path in self.modules.get(mod, ()):
                imp = self.g.files[path].module.imports.get(parts[i])
                if imp:
                    full = ".".join([resolve_relative(imp, path), *parts[i + 1 :]])
                    if full != dotted:
                        return self.find(full, depth + 1)
        return [], []

    def is_internal(self, dotted: str) -> bool:
        parts = dotted.split(".")
        return any(".".join(parts[:i]) in self.modules for i in range(len(parts), 0, -1))

    def method(self, cls: str, name: str, *, overrides: bool = False, skip_self: bool = False) -> list[str]:
        """`name` as defined on `cls` or its nearest repo ancestor, plus subclass overrides if asked."""
        found: list[str] = []
        seen, queue = set(), [cls]
        while queue:
            c = queue.pop(0)
            if c in seen:
                continue
            seen.add(c)
            if not (skip_self and c == cls) and name in self.methods.get(c, {}):
                found.append(self.methods[c][name])
                break
            queue.extend(self.bases.get(c, ()))
        if overrides:
            seen, stack = {cls}, list(self.subclasses.get(cls, ()))
            while stack:
                c = stack.pop()
                if c in seen:
                    continue
                seen.add(c)
                if name in self.methods.get(c, {}):
                    found.append(self.methods[c][name])
                stack.extend(self.subclasses.get(c, ()))
        return found

    def imported_files(self, pf: ParsedFile) -> set[str]:
        if pf.path not in self._imported:
            out = set()
            for target in pf.module.imports.values():
                parts = resolve_relative(target, pf.path).split(".")
                for i in range(len(parts), 0, -1):
                    if paths := self.modules.get(".".join(parts[:i])):
                        out.update(paths)
                        break
            self._imported[pf.path] = out
        return self._imported[pf.path]

    # ----------------------------------------------------------------- calls
    def callees(self, pf: ParsedFile, fn: Function, call: str) -> list[str]:
        path = pf.path
        cls = _enclosing_class(pf, fn.qualname)
        if call.startswith("super()."):
            name = call.removeprefix("super().")
            return self.method(f"{path}::{cls}", name, skip_self=True) if cls and name.isidentifier() else []
        parts = call.split(".")
        if not all(p.isidentifier() for p in parts):  # f(x).g, (a or b).c, x[0].y: unknown object
            return self.guess(pf, parts[-1])
        head = parts[0]
        if len(parts) == 1:
            scope = fn.qualname
            while scope:  # nested helpers: `inner()` inside `outer` is `outer.inner`
                if scope not in pf.classes and f"{scope}.{head}" in pf.nodes:
                    return [f"{path}::{scope}.{head}"]
                scope = scope.rpartition(".")[0]
        if head in ("self", "cls") and cls and len(parts) == 2:
            return self.method(f"{path}::{cls}", parts[1], overrides=True) or self.guess(pf, parts[1])
        target = self._target(pf, call)
        if target is None:  # a local variable, parameter or builtin
            return self.guess(pf, parts[-1]) if len(parts) > 1 else []
        funcs, classes = self.find(target)
        if funcs or classes:
            return funcs + [k for c in classes for k in self.method(c, "__init__")]
        # An internal name we could not pin down (a module-level object, say): guess by method name.
        # An external one (requests.get, json.dumps) is not the repo's code.
        return self.guess(pf, parts[-1]) if len(parts) > 1 and self.is_internal(target) else []

    def guess(self, pf: ParsedFile, name: str) -> list[str]:
        """Repo methods named `name`, for a call on an object whose type is unknown."""
        if not name.isidentifier() or name.startswith("__") or name in BUILTIN_METHODS:
            return []
        candidates = self.methods_by_name.get(name, [])
        near = self.imported_files(pf) | {pf.path}
        close = [k for k in candidates if k.partition("::")[0] in near]
        if close:
            return close
        return candidates if len(candidates) <= MAX_GUESSES else []


def _scopes(scope: str):
    """`a.b.c` -> "a.b.c", "a.b", "a", "" (innermost first)."""
    while scope:
        yield scope
        scope = scope.rpartition(".")[0]
    yield ""


def _enclosing_class(pf: ParsedFile, qualname: str) -> str | None:
    scope = qualname.rpartition(".")[0]
    while scope:
        if scope in pf.classes:
            return scope
        scope = scope.rpartition(".")[0]
    return None


def _is_entry_point(fn: Function, module: Module) -> bool:
    if fn.name == "main" and module.has_main_guard:
        return True
    for deco in fn.decorators:
        head = deco.split("(", 1)[0]
        if head.endswith(ROUTE_DECORATOR_SUFFIXES) or head in ("app.command", "click.command", "task"):
            return True
    return False
