"""Codewright's own code graph, built with tree-sitter (no external indexers).

For each Python file it records imports, classes, functions (with decorators, params
and calls) and builds a call graph across the repo. Calls are resolved through imports,
enclosing scopes and class hierarchies; only calls on objects of unknown type fall back
to matching a method name, and never for names that builtin types already define
(``d.items()`` is not a call to your ``items``). Entry points (web routes, CLI commands,
``__main__`` blocks) seed the hot-path calculation used in ranking.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import weakref
from collections import OrderedDict, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import tree_sitter_python
from tree_sitter import Language, Node, Parser, Query, QueryCursor

from . import graphcache
from .models import Function, Module

PY = Language(tree_sitter_python.language())
_TEST_CLIENT = re.compile(rb"\bTestClient\(|\bCliRunner\(|\.test_client\(|ASGITransport\(|WSGITransport\(")
_PARSER = Parser(PY)


def _uses_test_client(source: bytes) -> bool:
    # A plain substring test first: the regex alone is slow over thousands of test files.
    hints = (b"Client(", b".test_client(", b"CliRunner(", b"Transport(")
    return any(h in source for h in hints) and bool(_TEST_CLIENT.search(source))


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


# Syntax trees take about 24 times the memory of their source: 180 MB of Python is over 4 GB of trees. A file
# keeps its source and what was read from it (imports, functions, calls); its tree lives in this bounded cache
# and is parsed again from the source when something needs it after being evicted. Repos whose trees fit the
# budget (a few thousand files) never re-parse.
TREE_FACTOR = 24
TREE_BUDGET = int(os.environ.get("CODEWRIGHT_TREE_MB", "600")) * 2**20
_TREES: OrderedDict[int, tuple[weakref.ref, int]] = OrderedDict()
_TREE_BYTES = [0]
# Reentrant: a file's weakref callback (_forget) can run from garbage collection while _remember holds it.
_TREE_LOCK = threading.RLock()


def _forget(key: int) -> None:
    with _TREE_LOCK:
        hit = _TREES.pop(key, None)
        if hit is not None:
            _TREE_BYTES[0] -= hit[1]


def _remember(pf: ParsedFile) -> None:
    key = id(pf)
    size = len(pf.source) * TREE_FACTOR
    with _TREE_LOCK:
        if key in _TREES:
            _TREES.move_to_end(key)
            return
        _TREES[key] = (weakref.ref(pf, lambda _r, k=key: _forget(k)), size)
        _TREE_BYTES[0] += size
        while _TREE_BYTES[0] > TREE_BUDGET and len(_TREES) > 1:
            _, (ref, n) = _TREES.popitem(last=False)
            _TREE_BYTES[0] -= n
            old = ref()
            if old is not None:
                old._entry = None  # nodes already handed out keep their own tree alive until dropped


class ParsedFile:
    """One parsed file. `root`, `nodes` and `tree_cache` belong to its syntax tree, which may be dropped and
    parsed again; everything else (source, imports, functions, `cache`) stays."""

    def __init__(
        self,
        path: str,
        source: bytes,
        root: Node | None,
        module: Module,
        nodes: dict[str, Node] | None = None,
        classes: dict[str, list[str]] | None = None,
        cache: dict[str, object] | None = None,
    ):
        self.path = path  # relative
        self.source = source
        self.module = module
        self.classes: dict[str, list[str]] = classes if classes is not None else {}  # qualname -> bases as written
        self.cache: dict[str, object] = cache if cache is not None else {}  # plain per-file results, kept
        self._entry: dict | None = None
        if root is not None:
            self._entry = {"root": root, "nodes": nodes if nodes is not None else {}, "cache": {}}
            _remember(self)

    def _tree(self) -> dict:
        e = self._entry
        if e is None:
            root = _PARSER.parse(self.source).root_node
            e = self._entry = {"root": root, "nodes": _index_definitions(root), "cache": {}}
        _remember(self)
        return e

    @property
    def root(self) -> Node:
        return self._tree()["root"]

    @property
    def nodes(self) -> dict[str, Node]:
        """qualname -> function_definition node."""
        return self._tree()["nodes"]

    @property
    def tree_cache(self) -> dict:
        """Per-file results that hold syntax nodes: dropped with the tree."""
        return self._tree()["cache"]

    @cached_property
    def defined(self) -> frozenset[str]:
        """Qualnames of the file's functions (the keys of `nodes`), without needing the tree."""
        return frozenset(fn.qualname for fn in self.module.functions)

    @property
    def has_tree(self) -> bool:
        return self._entry is not None

    @cached_property
    def lines(self) -> list[str]:
        """Source lines with line endings. Shared: copy before changing."""
        return self.source.decode("utf-8", errors="replace").splitlines(keepends=True)


def is_test_file(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
        or any(p in ("tests", "test", "fixtures") for p in parts[:-1])
    )


_QUOTED = re.compile(rb'(?="([^"\n]*)")')  # every "..." on one line, overlapping: `"a"b"` holds "a" and "b"


def _quoted_stem(path: str) -> bytes:
    stem = path.rsplit("/", 1)[-1].removesuffix(".py")
    return f'"{stem}"'.encode() if stem != "__init__" else b"\0"


@dataclass
class CodeGraph:
    root: str
    files: dict[str, ParsedFile] = field(default_factory=dict)
    edges: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))  # caller -> callees
    entry_points: set[str] = field(default_factory=set)
    app_entries: dict = field(default_factory=dict)  # file -> entries.AppEntry: files that start the app
    resolver: Resolver | None = None  # set by build(): resolves names and imports across the repo

    def importers_of(self, path: str) -> set[str]:
        """Files that import `path`, directly or through other modules of the repo."""
        if self.resolver is None:
            return set()
        if "importers" not in self.__dict__:
            back: dict[str, set[str]] = defaultdict(set)
            for pf in self.files.values():
                for target in self.resolver.imported_files(pf):
                    if target != pf.path:
                        back[target].add(pf.path)
            self.__dict__["importers"] = back
        back = self.__dict__["importers"]
        seen, stack = set(), [path]
        while stack:
            for importer in back.get(stack.pop(), ()):
                if importer not in seen:
                    seen.add(importer)
                    stack.append(importer)
        return seen

    def functions(self):
        for pf in self.files.values():
            for fn in pf.module.functions:
                yield pf, fn

    def hot_functions(self) -> set[str]:
        """Every function reachable from an entry point. Computed once per graph (it is not changed after build)."""
        if "memo_hot" not in self.__dict__:
            self.__dict__["memo_hot"] = self._hot_functions()
        return set(self.__dict__["memo_hot"])

    def _hot_functions(self) -> set[str]:
        seen: set[str] = set()
        stack = list(self.entry_points) + [c for e in self.app_entries.values() for c in e.calls]
        while stack:
            q = stack.pop()
            if q in seen:
                continue
            seen.add(q)
            stack.extend(self.edges.get(q, ()))
        return seen

    def test_reachable(self) -> set[str]:
        """Every function some test function reaches through the call graph. Computed once per graph."""
        if "memo_test_reachable" not in self.__dict__:
            self.__dict__["memo_test_reachable"] = self._test_reachable()
        return set(self.__dict__["memo_test_reachable"])

    def _test_reachable(self) -> set[str]:
        starts = [self.key(fn) for pf, fn in self.functions() if is_test_file(pf.path)]
        starts += self._routes_reached_by_test_clients()
        seen: set[str] = set()
        stack = list(starts)
        while stack:
            q = stack.pop()
            if q in seen:
                continue
            seen.add(q)
            stack.extend(self.edges.get(q, ()))
        return seen - set(starts)

    def _routes_reached_by_test_clients(self) -> list[str]:
        """Web routes a test drives over HTTP: invisible to a call graph, so infer them.

        A test file that uses a test client (TestClient, CliRunner, Flask's test_client, ASGITransport)
        reaches every route and CLI command defined in the code it imports, directly or through other modules.
        """
        if self.resolver is None:
            return []
        reached: set[str] = set()
        by_stem: dict[bytes, list[str]] | None = None
        for path, pf in self.files.items():
            if not is_test_file(path) or not _uses_test_client(pf.source):
                continue
            todo = list(self.resolver.imported_files(pf))
            # Tests that load the app by file path (importlib's spec_from_file_location) name its stem.
            if by_stem is None:
                by_stem = defaultdict(list)
                for f in self.files:
                    if not is_test_file(f):
                        by_stem[_quoted_stem(f)].append(f)
            quoted = {b'"' + q + b'"' for q in _QUOTED.findall(pf.source)}
            todo += [f for stem, fs in by_stem.items() if stem in quoted for f in fs]
            while todo:
                f = todo.pop()
                if f in reached or f not in self.files:
                    continue
                reached.add(f)
                todo.extend(self.resolver.imported_files(self.files[f]))
        return [k for k in self.entry_points if k.partition("::")[0] in reached]

    def key(self, fn: Function) -> str:
        return f"{fn.file}::{fn.qualname}"


def iter_python_files(root: Path):
    """The Python files a review reads: outside tool folders, and inside the repo's scan scope (scanscope)."""
    from . import scanscope

    root = Path(root)
    scope = scanscope.load(root)
    for path in scanscope.all_python_files(root):
        if (scope["include"] or scope["exclude"]) and not scanscope.included(path.relative_to(root).as_posix(), scope):
            continue
        yield path


# Parsed files by absolute path, reused while a file's bytes are unchanged. Long-running hosts
# (the web app, the MCP server, watch mode) re-review the same repo; only edited files re-parse.
# Everything cached on a ParsedFile is derived from its own source, so reuse is safe.
_PARSED: OrderedDict[str, ParsedFile] = OrderedDict()
PARSE_CACHE_SIZE = 50_000
DISK_CACHE_MIN = 100  # files to parse before the on-disk cache is worth loading
_SAVED: dict[str, frozenset[str]] = {}  # root -> the files in the on-disk cache this process last wrote


def parse_cached(path: Path, root: Path, source: bytes | None = None) -> ParsedFile:
    source = path.read_bytes() if source is None else source
    key = str(path.resolve())
    hit = _PARSED.get(key)
    rel = path.relative_to(root).as_posix()
    if hit is not None and hit.source == source and hit.path == rel:
        _PARSED.move_to_end(key)
        return hit
    pf = parse_file(path, root, source)
    _PARSED[key] = pf
    while len(_PARSED) > PARSE_CACHE_SIZE:
        _PARSED.popitem(last=False)
    return pf


def parse_file(path: Path, root: Path, source: bytes | None = None) -> ParsedFile:
    source = path.read_bytes() if source is None else source
    tree = _PARSER.parse(source)
    rel = path.relative_to(root).as_posix()
    module = Module(file=rel)
    pf = ParsedFile(rel, source, tree.root_node, module)
    _collect(tree.root_node, pf, prefix="")
    return pf


# Statements whose bodies can hold imports and definitions: `if`/`elif`/`else`, `try`/`except`/`finally`,
# `with`, loops and `match`/`case` (a lazy import in a factory's `case "qdrant":` is still an import).
_COMPOUND = frozenset(
    {
        "block",
        "if_statement",
        "elif_clause",
        "else_clause",
        "try_statement",
        "except_clause",
        "except_group_clause",
        "finally_clause",
        "with_statement",
        "for_statement",
        "while_statement",
        "match_statement",
        "case_clause",
    }
)


def _index_definitions(root: Node) -> dict[str, Node]:
    """qualname -> function_definition node, found the way `_collect` finds them (for a re-parsed tree)."""
    out: dict[str, Node] = {}

    def visit(node: Node, prefix: str) -> None:
        for child in node.children:
            kind = child.type
            if kind == "decorated_definition":
                defn = child.child_by_field_name("definition")
            elif kind in ("function_definition", "class_definition"):
                defn = child
            else:
                if kind in _COMPOUND:
                    visit(child, prefix)
                continue
            if defn is None:
                continue
            qual = f"{prefix}{text(defn.child_by_field_name('name'))}"
            body = defn.child_by_field_name("body")
            if defn.type == "function_definition":
                out[qual] = defn
            if body is not None:
                visit(body, f"{qual}.")

    visit(root, "")
    return out


def _collect(node: Node, pf: ParsedFile, prefix: str) -> None:
    for child in node.children:
        kind = child.type
        if kind in ("import_statement", "import_from_statement"):
            record_imports(child, pf.module)
        elif kind == "if_statement" and "__name__" in text(child.child_by_field_name("condition")):
            pf.module.has_main_guard = True
            _collect(child, pf, prefix)
        elif kind == "decorated_definition":
            definition = child.child_by_field_name("definition")
            decorators = [text(d)[1:].strip() for d in child.children if d.type == "decorator"]
            _definition(definition, pf, prefix, decorators, outer=child)
        elif kind in ("function_definition", "class_definition"):
            _definition(child, pf, prefix, [], outer=child)
        elif kind in _COMPOUND:
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


def _bind(module: Module, alias: str, target: str) -> None:
    """Record `alias` -> `target`. The last binding of a name wins, as in Python; an earlier one to another module
    (`from .milvus import Client` in one branch, `from .qdrant import Client` in the next) is kept under
    `alias#target`, which no name lookup matches, so the module still counts as imported."""
    old = module.imports.get(alias)
    if old is not None and old != target:
        module.imports[f"{alias}#{old}"] = old
    module.imports[alias] = target


def record_imports(node: Node, module: Module) -> None:
    if node.type == "import_statement":
        for child in node.named_children:
            if child.type == "dotted_name":
                full = text(child)
                _bind(module, full.split(".")[0], full.split(".")[0])
                _bind(module, full, full)
            elif child.type == "aliased_import":
                _bind(module, text(child.child_by_field_name("alias")), text(child.child_by_field_name("name")))
        return
    mod_node = node.child_by_field_name("module_name")
    base = text(mod_node)
    for child in node.named_children:
        if mod_node is not None and child.start_byte == mod_node.start_byte:
            continue
        sep = "" if base.endswith(".") else "."  # `from . import x` -> ".x", not "..x"
        if child.type == "dotted_name":
            _bind(module, text(child), f"{base}{sep}{text(child)}")
        elif child.type == "aliased_import":
            alias = text(child.child_by_field_name("alias"))
            _bind(module, alias, f"{base}{sep}{text(child.child_by_field_name('name'))}")
        elif child.type == "wildcard_import":
            # `from ._api import *` binds no name we can see, but it does import the module (and runs it).
            module.imports[f"*{base}"] = base


def resolve(module: Module, dotted: str) -> str:
    """Expand a dotted name as written in code to its fully qualified import target."""
    head, _, rest = dotted.partition(".")
    target = module.imports.get(head)
    if target is None:
        return dotted
    return f"{target}.{rest}" if rest else target


# Functions that read something from every file while its tree is fresh (see `on_parse`).
FILE_VISITORS: list[Callable[[ParsedFile], None]] = []


def on_parse(fn: Callable[[ParsedFile], None]) -> Callable[[ParsedFile], None]:
    """Register `fn(pf)` to run on each file as the graph is built, while its tree is in memory. It should store
    plain results in `pf.cache` (and return early when they are there), so whole-repo passes later need no tree."""
    FILE_VISITORS.append(fn)
    return fn


_IMPORT_QUERY: list = []


def import_statements(pf: ParsedFile) -> list[tuple[int, bool, str, dict[str, str]]]:
    """(line, inside a function, statement kind, {alias: target}) for every import in the file. Kept on the
    file, so passes over the whole repo's imports don't need its tree."""
    hit = pf.cache.get("import_stmts")
    if hit is None:
        if not _IMPORT_QUERY:
            _IMPORT_QUERY.append(Query(PY, "[(import_statement) (import_from_statement)] @n"))  # compile once
        out = []
        caps = QueryCursor(_IMPORT_QUERY[0]).captures(pf.root).get("n", [])
        for node in sorted(caps, key=lambda n: n.start_byte):
            tmp = Module(pf.path)
            record_imports(node, tmp)
            deferred, p = False, node.parent
            while p is not None:
                if p.type == "function_definition":
                    deferred = True
                    break
                p = p.parent
            out.append((node.start_point[0] + 1, deferred, node.type, dict(tmp.imports)))
        hit = pf.cache["import_stmts"] = out
    return hit


on_parse(import_statements)


# Parsing in worker processes: the GIL keeps threads from parsing in parallel. A worker parses, runs the
# visitors and (optionally) detection, and sends back only plain data; the tree stays behind, and the main
# process parses a file again only if something later asks for its tree. Below PARALLEL_MIN files to parse
# (or with CODEWRIGHT_WORKERS=0) everything happens in this process.
PARALLEL_MIN = 300
CHUNK = 48


def _workers() -> int:
    env = os.environ.get("CODEWRIGHT_WORKERS")
    if env is not None:
        return max(0, int(env or 0))
    return min(8, max(1, (os.cpu_count() or 2) - 1))


def _parse_chunk(root: str, rels: list[str], detect: bool) -> list[tuple]:
    """In a worker: (path, module, classes, cache, detection entry or None) per file."""
    import pickle

    from . import entries, links_scan, untangle_scan  # noqa: F401  (register their visitors)
    from .intents import (
        architecture,  # noqa: F401
        detectors,
    )

    base = Path(root)
    out = []
    for rel in rels:
        try:
            pf = parse_file(base / rel, base)
        except (OSError, ValueError):
            continue
        for visit in FILE_VISITORS:
            visit(pf)
        det = None
        h = detectors.file_hash(pf)
        if detect and not is_test_file(pf.path):
            det = {"h": h, "m": detectors.detect_file(pf, None, local=True)}
            try:
                pickle.dumps(det["m"])
            except Exception:  # a match holding a syntax node: detect it in the main process instead
                det = None
        out.append((pf.path, pf.module, pf.classes, pf.cache, det))
    return out


def _parse_in_workers(
    root: Path, todo: list[tuple[Path, bytes]], detect_cache: dict | None, progress, done: int, total: int
) -> dict[str, ParsedFile]:
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor, as_completed

    sources = {p.relative_to(root).as_posix(): src for p, src in todo}
    rels = list(sources)
    chunks = [rels[i : i + CHUNK] for i in range(0, len(rels), CHUNK)]
    out: dict[str, ParsedFile] = {}
    # spawn, not fork: the server has threads, and a forked child can inherit a lock another thread held.
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=_workers(), mp_context=ctx) as pool:
        futures = [pool.submit(_parse_chunk, str(root), c, detect_cache is not None) for c in chunks]
        for fut in as_completed(futures):
            for rel, module, classes, cache, det in fut.result():
                src = sources.get(rel)
                if src is None:
                    continue
                pf = ParsedFile(rel, src, None, module, classes=classes, cache=cache)
                out[rel] = pf
                if det is not None and detect_cache is not None:
                    detect_cache[rel] = det
            done += CHUNK
            if progress:
                progress(min(done, total), total)
    return out


def build(
    root: str | Path,
    progress: Callable[[int, int], None] | None = None,
    visitors: list[Callable[[ParsedFile], None]] | tuple = (),
    detect_cache: dict | None = None,
) -> CodeGraph:
    """`progress(done, total)` is called as files are parsed; `visitors` (and those registered with `on_parse`)
    run on each file right after, while its syntax tree is still in memory. With `detect_cache` (see
    intents.detectors.detect) new and edited files are detected as they are parsed."""
    from . import entries, links_scan, untangle_scan  # noqa: F401  (they register their visitors)
    from .intents import architecture  # noqa: F401

    root = Path(root).resolve()
    graph = CodeGraph(root=str(root))
    paths = list(iter_python_files(root))
    # Files unchanged since this process last parsed them are reused; the rest are parsed, in parallel when many.
    fresh: list[tuple[Path, bytes]] = []
    sources: dict[Path, bytes] = {}
    for path in paths:
        try:
            source = sources[path] = path.read_bytes()
        except OSError:
            continue
        hit = _PARSED.get(str(path.resolve()))
        if hit is None or hit.source != source or hit.path != path.relative_to(root).as_posix():
            fresh.append((path, source))
    parsed: dict[str, ParsedFile] = {}
    # Files unchanged since the last run (in any process) come back from the on-disk cache without parsing.
    # Loading it costs about as much as parsing a hundred files, so a rescan after a few edits just parses them.
    disk = graphcache.load(root) if len(fresh) > DISK_CACHE_MIN else {}
    if disk:
        still = []
        for path, source in fresh:
            rel = path.relative_to(root).as_posix()
            hit = disk.get(rel)
            sha = hashlib.sha1(source).hexdigest()
            if hit is not None and hit[0] == sha:
                pf = ParsedFile(rel, source, None, hit[1], classes=hit[2], cache=dict(hit[3]))
                pf.cache["sha1"] = sha
                parsed[rel] = pf
            else:
                still.append((path, source))
        fresh = still
    rels = frozenset(p.relative_to(root).as_posix() for p in paths)
    if disk:
        _SAVED[str(root)] = frozenset(disk)
    changed = bool(fresh) or rels != (frozenset(disk) if disk else _SAVED.get(str(root)))
    if len(fresh) >= PARALLEL_MIN and _workers() > 1:
        try:
            parsed.update(_parse_in_workers(root, fresh, detect_cache, progress, len(paths) - len(fresh), len(paths)))
        except Exception as exc:  # a pool that cannot start (sandbox, frozen app): parse here instead
            import logging

            logging.getLogger("codewright").warning("parsing in this process (workers failed: %s)", exc)
    for n, path in enumerate(paths):
        if progress and n % 20 == 0 and not parsed:
            progress(n, len(paths))
        rel = path.relative_to(root).as_posix()
        if rel in parsed:
            pf = parsed[rel]
            _PARSED[str(path.resolve())] = pf
        else:
            try:
                pf = parse_cached(path, root, sources.get(path))
            except (OSError, ValueError):
                continue
        graph.files[pf.path] = pf
        for visit in (*FILE_VISITORS, *visitors):
            visit(pf)
    while len(_PARSED) > PARSE_CACHE_SIZE:
        _PARSED.popitem(last=False)
    if changed and len(graph.files) >= PARALLEL_MIN:  # small repos parse in well under a second: not worth it
        _SAVED[str(root)] = rels
        graphcache.save(
            root,
            {
                rel: (pf.cache.get("sha1") or hashlib.sha1(pf.source).hexdigest(), pf.module, pf.classes, pf.cache)
                for rel, pf in graph.files.items()
            },
        )

    _resolve_calls(graph)
    entries.detect(graph)
    return graph


# The last resolution per repo: (signature of every file's names, resolver, {path: (file, shape, its edges)}).
# Resolving calls is the slowest part of a rescan, and a call resolves the same way while the calling file is the
# same and no file's names changed, so a rescan in the same process re-resolves only the files that were edited.
_RESOLVED: dict[str, tuple[str, Resolver, dict[str, tuple[ParsedFile, bytes, dict[str, set[str]]]]]] = {}
_RESOLVED_LOCK = threading.Lock()


def _shape(pf: ParsedFile) -> bytes:
    """Everything of a file that the resolver's tables read: its path, imports, classes and their bases, and
    the names of its functions. A change to a function's body leaves it alone."""
    h = hashlib.sha1(pf.path.encode())
    h.update(repr(sorted(pf.module.imports.items())).encode())
    h.update(repr(sorted((q, tuple(b)) for q, b in pf.classes.items())).encode())
    h.update(repr(sorted(fn.qualname for fn in pf.module.functions)).encode())
    return h.digest()


def _resolve_calls(graph: CodeGraph) -> None:
    """Fill `graph.resolver`, `graph.edges` and `graph.entry_points`."""
    with _RESOLVED_LOCK:
        prev = _RESOLVED.get(graph.root)
    before = prev[2] if prev else {}
    shapes: dict[str, bytes] = {}
    for path, pf in graph.files.items():
        old = before.get(path)
        shapes[path] = old[1] if old is not None and old[0] is pf else _shape(pf)
    sig = hashlib.sha1(b"".join(p.encode() + b"\0" + s for p, s in sorted(shapes.items()))).hexdigest()
    if prev is not None and prev[0] == sig:  # no name, import or class changed anywhere: the tables are the same
        resolver = prev[1]
        resolver.g = graph
    else:
        resolver, before = Resolver(graph), {}
    graph.resolver = resolver
    files: dict[str, tuple[ParsedFile, bytes, dict[str, set[str]]]] = {}
    for path, pf in graph.files.items():
        old = before.get(path)
        if old is not None and old[0] is pf:  # the very same parse: its calls resolve as before
            out = old[2]
        else:
            out = {}
            for fn in pf.module.functions:
                caller = graph.key(fn)
                for call in fn.calls:
                    for callee in resolver.callees(pf, fn, call):
                        if callee != caller:
                            out.setdefault(caller, set()).add(callee)
        files[path] = (pf, shapes[path], out)
        for caller, callees in out.items():
            graph.edges[caller] |= callees
        for fn in pf.module.functions:
            if _is_entry_point(fn, pf.module):
                graph.entry_points.add(graph.key(fn))
    with _RESOLVED_LOCK:
        _RESOLVED[graph.root] = (sig, resolver, files)


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
            if name in pf.classes or name in pf.defined:
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
                if scope not in pf.classes and f"{scope}.{head}" in pf.defined:
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


# Handlers a framework calls: MCP tools/resources/prompts, chat and bot events, Modal and
# Cloud Functions entry points, schedulers.
HANDLER_DECORATOR_SUFFIXES = (
    ".tool",
    ".resource",
    ".prompt",
    ".on_message",
    ".on_chat_start",
    ".on_event",
    ".event",
    ".message",
    ".message_handler",
    ".function",
    ".local_entrypoint",
    ".web_endpoint",
    ".asgi_app",
    ".schedule",
    ".scheduled_job",
    ".http",
    ".cloud_event",
    ".handler",
    ".listener",
    ".action",
    ".shortcut",
    ".view",
    ".middleware",
)


def _is_entry_point(fn: Function, module: Module) -> bool:
    if fn.name == "main" and module.has_main_guard:
        return True
    for deco in fn.decorators:
        head = deco.split("(", 1)[0]
        if head.endswith(HANDLER_DECORATOR_SUFFIXES) and "." in head and not head.startswith(("pytest.", "self.")):
            return True
        # `.command`/`.group` covers click groups (`@cli.command()`) and typer apps (`@app.command()`).
        if head.endswith((*ROUTE_DECORATOR_SUFFIXES, ".command", ".group")) or head in ("command", "group", "task"):
            return True
    return False
