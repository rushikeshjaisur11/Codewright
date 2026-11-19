"""Architecture review: whole-codebase checks that never touch business logic.

These run once over the code graph, after the per-function and per-file detectors. Each
compares the codebase with itself (its own dominant layering, its own most-called code)
rather than with fixed rules, so a large or unusual codebase is judged on its own terms.
They only report; every suggested change (move a name, extract a helper, add a test,
route a call through an existing layer) leaves behaviour as it is.
"""

from __future__ import annotations

import functools
import hashlib
import re
from collections import defaultdict
from pathlib import Path

from tree_sitter import Node

from ..graph import CodeGraph, ParsedFile, import_statements, on_parse, text
from ..models import Function, IntentMatch
from .detectors import graph_detector, is_test_file, line, walk

DB_LIBRARIES = ("sqlalchemy", "psycopg", "psycopg2", "asyncpg", "sqlite3", "pymongo", "motor", "pymysql", "aiosqlite")
DB_METHODS = (
    "execute",
    "executemany",
    "scalars",
    "scalar",
    "query",
    "fetch",
    "fetchrow",
    "fetchall",
    "fetchone",
    "fetchval",
    "commit",
    "find_one",
    "insert_one",
    "update_one",
    "delete_one",
)
DB_OBJECTS = re.compile(r"(^|_)(session|db|conn|connection|cursor|cur|tx|transaction)s?$", re.I)
ROUTE_SUFFIXES = (".get", ".post", ".put", ".patch", ".delete", ".route", ".api_route", ".websocket")


def _module_fn(pf: ParsedFile, ln: int) -> Function:
    return Function("<module>", "<module>", pf.path, ln, ln, ln)


def _components(graph: CodeGraph) -> tuple[dict[str, str], dict[str, str]]:
    """(file -> component, dotted module -> file) for non-test files, as the Map draws them."""
    from ..archmap import ArchMap

    amap = ArchMap(graph)
    files = {p: amap._comp(p) for p in amap.modules.values()}
    return files, amap


def _import_sites(graph: CodeGraph, amap) -> dict[tuple[str, str], list[dict]]:
    """(importer file, imported file) -> [{line, names, deferred}] for imports between repo files."""
    from ..archmap import module_name, resolve_relative

    sites: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for path, pf in graph.files.items():
        if is_test_file(path):
            continue
        for ln, deferred, kind, imported in import_statements(pf):  # recorded at parse time: no tree needed
            for alias, target in imported.items():
                full = resolve_relative(target, path)
                mod = amap._internal_target(full)
                if mod is None or mod == module_name(path):
                    continue
                dest = amap.modules.get(mod)
                if dest is None or dest == path:
                    continue
                name = target.rsplit(".", 1)[-1] if kind == "import_from_statement" else alias
                if f"/{dest}".endswith((f"/{name}.py", f"/{name}/__init__.py")):
                    # `from . import docs` / `import pkg.docs`: the module itself, so name what is used from it
                    used = sorted(_attribute_uses(pf).get(alias, ()))
                    names = [f"{name}.{u}" for u in used] or [name]
                else:
                    names = [name]
                sites[(path, dest)].append({"line": ln, "names": names, "deferred": deferred})
    return sites


_ATTR = re.compile(rb"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)")


def _attribute_uses(pf) -> dict[str, set[str]]:
    """name -> attributes used on it (`docs.render` -> {"docs": {"render"}}), one scan per file."""
    hit = pf.cache.get("attribute_uses")
    if hit is None:
        hit = defaultdict(set)
        for m in _ATTR.finditer(pf.source):
            hit[m.group(1).decode()].add(m.group(2).decode())
        pf.cache["attribute_uses"] = hit
    return hit


def _short(graph: CodeGraph, path: str) -> str:
    """A path relative to its project (the nearest pyproject.toml), which is how its developers read it."""
    project = _project_of(graph, path)
    return path if project == "." else path[len(project) + 1 :]


def _parents(node: Node):
    p = node.parent
    while p is not None:
        yield p
        p = p.parent


# --------------------------------------------------------------------------- import cycles


def _sccs(nodes: set[str], edges: dict[str, set[str]]) -> list[list[str]]:
    """Strongly connected components with more than one member (Tarjan, iterative)."""
    index, low, on, stack, out, counter = {}, {}, set(), [], [], [0]
    for start in sorted(nodes):
        if start in index:
            continue
        work = [(start, iter(sorted(edges.get(start, ()))))]
        index[start] = low[start] = counter[0]
        counter[0] += 1
        stack.append(start)
        on.add(start)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter[0]
                    counter[0] += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(sorted(edges.get(w, ())))))
                    advanced = True
                    break
                if w in on:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > 1:
                    out.append(sorted(comp))
    return out


@graph_detector("arch-import-cycle")
def detect_import_cycles(graph: CodeGraph) -> list[IntentMatch]:
    """Modules that import each other, directly or around a loop. Reports the weakest link to cut."""
    files, amap = _components(graph)
    sites = _import_sites(graph, amap)
    edges: dict[str, set[str]] = defaultdict(set)
    for a, b in sites:
        edges[a].add(b)
    out = []
    for cycle in _sccs(set(files), edges):
        members = set(cycle)
        links = [(a, b) for (a, b) in sites if a in members and b in members]

        # The link to cut: fewest names to move; on a tie, the top-level import (the one that can fail).
        def cost(ab):
            # A package __init__ re-exporting its own submodule is public API: cut elsewhere if possible.
            reexport = ab[0].endswith("__init__.py") and ab[1].startswith(ab[0].rpartition("/")[0] + "/")
            names_ = len({n for x in sites[ab] for n in x["names"]})
            return (reexport, names_, all(x["deferred"] for x in sites[ab]), ab)

        weakest = min(links, key=cost)
        cut = sites[weakest]
        names = sorted({n for s in cut for n in s["names"]})
        import_time = [ab for ab in links if not all(s["deferred"] for s in sites[ab])]
        pf = graph.files[weakest[0]]
        a, b = _short(graph, weakest[0]), _short(graph, weakest[1])
        shown = ", ".join(f"`{n}`" for n in names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")
        if all(x["deferred"] for x in cut):
            advice = (
                f"Here `{a}` already imports {shown} inside a function, so nothing breaks at import time; "
                f"to remove the coupling, move {shown} out of `{b}` into a small module that both can import."
            )
        else:
            advice = (
                f"Here the cheapest cut is `{a}` line {cut[0]['line']}, which imports {shown} from `{b}`: move "
                f"{shown} into a new module that both import, so `{a}` no longer needs `{b}`; or, if only one "
                "function needs them, import them inside that function."
            )
        loop = " -> ".join(_short(graph, m) for m in [*cycle, cycle[0]]) if len(cycle) <= 4 else f"{len(cycle)} modules"
        out.append(
            IntentMatch(
                "arch-import-cycle",
                _module_fn(pf, cut[0]["line"]),
                cut[0]["line"],
                f"import cycle ({loop}); weakest link: `{a}` imports {shown} from `{b}`"
                + ("" if import_time else "; every link is inside a function, so only coupling"),
                {
                    "modules": cycle,
                    "cut": list(weakest),
                    "names": names,
                    "import_time": bool(import_time),
                    "components": sorted({files[m] for m in cycle}),
                    "advice": advice,
                },
            )
        )
    return out


# --------------------------------------------------------------------------- routes that bypass the service layer


def _db_calls(pf: ParsedFile, node: Node) -> list[int]:
    uses_db = any(v.split(".", 1)[0] in DB_LIBRARIES for v in pf.module.imports.values())
    if not uses_db and not _passes_session(node):
        return []
    lines = []
    for call in walk(node):
        if call.type != "call":
            continue
        f = call.child_by_field_name("function")
        if f is None or f.type != "attribute":
            continue
        method = text(f.child_by_field_name("attribute"))
        obj = text(f.child_by_field_name("object")).rsplit(".", 1)[-1]
        if method in DB_METHODS and DB_OBJECTS.search(obj):
            lines.append(line(call))
    return lines


def _passes_session(node: Node) -> bool:
    params = node.child_by_field_name("parameters")
    if params is None:
        return False
    for p in params.named_children:  # `session`, `db: Session`, `session=Depends(get_session)`
        name = p if p.type == "identifier" else next((c for c in p.named_children if c.type == "identifier"), None)
        if name is not None and DB_OBJECTS.search(text(name)):
            return True
    return False


@graph_detector("arch-layer-bypass")
def detect_layer_bypass(graph: CodeGraph) -> list[IntentMatch]:
    """Routes that query the database themselves when most of this repo's routes go through other modules."""
    direct, layered = [], 0
    layers: dict[str, int] = defaultdict(int)  # module that holds the queries -> routes going through it
    for pf, fn in graph.functions():
        if is_test_file(pf.path) or not any(d.split("(", 1)[0].endswith(ROUTE_SUFFIXES) for d in fn.decorators):
            continue
        src = "".join(pf.lines[fn.start_line - 1 : fn.end_line])
        could = any(f".{m}(" in src for m in DB_METHODS)  # no query method named: no tree needed to know
        node = pf.nodes.get(fn.qualname) if could else None
        own = _db_calls(pf, node) if node is not None else []
        if own:
            direct.append((pf, fn, own[0]))
            continue
        # Reaches the database through a function in another module?
        seen, todo = set(), list(graph.edges.get(graph.key(fn), ()))
        while todo:
            k = todo.pop()
            if k in seen or len(seen) > 200:
                continue
            seen.add(k)
            path, _, qual = k.partition("::")
            target = graph.files.get(path)
            if target is None or is_test_file(path):
                continue
            tnode = target.nodes.get(qual)
            if tnode is not None and path != pf.path and _db_calls(target, tnode):
                layered += 1
                layers[path] += 1
                break
            todo.extend(graph.edges.get(k, ()))
    total = layered + len(direct)
    if layered < 3 or not direct or layered / total < 0.6:
        return []  # no dominant layered pattern to be consistent with
    main = max(layers, key=lambda p: (layers[p], p))
    return [
        IntentMatch(
            "arch-layer-bypass",
            fn,
            ln,
            f"route `{fn.name}` queries the database itself; {layered} of this repo's {total} routes that use the "
            "database go through another module",
            {
                "layered": layered,
                "direct": len(direct),
                "layer": main,
                "advice": f"Here most routes reach the database through `{main}` ({layers[main]} routes): move the "
                f"query on line {ln} into a function there that takes the same session, and call it from "
                f"`{fn.name}`.",
            },
        )
        for pf, fn, ln in direct
    ]


# --------------------------------------------------------------------------- heavily used, untested code


def _statements(pf: ParsedFile, fn: Function) -> int:
    return _statement_counts(pf).get(fn.qualname, 0)


@on_parse
def _fingerprints(pf: ParsedFile) -> dict[str, tuple]:
    """qualname -> body fingerprint, read while the tree is in memory (duplicate logic compares the whole repo)."""
    hit = pf.cache.get("fingerprints")
    if hit is None:
        hit = {}
        if not is_test_file(pf.path):
            for fn in pf.module.functions:
                node = pf.nodes.get(fn.qualname)
                fp = _body_fingerprint(pf, node) if node is not None else None
                if fp:
                    hit[fn.qualname] = fp
        pf.cache["fingerprints"] = hit
    return hit


@on_parse
def _statement_counts(pf: ParsedFile) -> dict[str, int]:
    hit = pf.cache.get("statement_counts")
    if hit is None:
        hit = {}
        for fn in pf.module.functions:
            node = pf.nodes.get(fn.qualname)
            body = node.child_by_field_name("body") if node is not None else None
            hit[fn.qualname] = 0 if body is None else sum(1 for s in body.named_children if s.type != "comment")
        pf.cache["statement_counts"] = hit
    return hit


@graph_detector("arch-untested-hotspot")
def detect_untested_hotspots(graph: CodeGraph) -> list[IntentMatch]:
    """The most-called functions on a request or command path that no test reaches (top 5)."""
    fan_in: dict[str, int] = defaultdict(int)
    for caller, callees in graph.edges.items():
        if is_test_file(caller.partition("::")[0]):
            continue
        for c in callees:
            fan_in[c] += 1
    if not fan_in:
        return []
    counts = sorted(fan_in.values())
    bar = max(5, counts[int(len(counts) * 0.9)])  # this repo's top tenth, and at least 5 callers
    hot, tested = graph.hot_functions(), graph.test_reachable()
    # Only where the project already has tests: a project with none needs a test suite, not a list.
    tested_projects = {_test_area(graph, p) for p in graph.files if is_test_file(p)}
    picks = []
    for pf, fn in graph.functions():
        key = graph.key(fn)
        if is_test_file(pf.path) or fn.name.startswith("__") or key not in hot or key in tested:
            continue
        if fan_in.get(key, 0) < bar or _statements(pf, fn) < 3:  # one-line helpers (a log call) aren't risk
            continue
        if _test_area(graph, pf.path) in tested_projects:
            picks.append((fan_in[key], pf, fn))
    picks.sort(key=lambda x: (-x[0], x[2].file, x[2].start_line))
    callers: dict[str, list[str]] = defaultdict(list)
    for caller, callees in graph.edges.items():
        for c in callees:
            path, _, qual = caller.partition("::")
            callers[c].append(qual if path == c.partition("::")[0] else f"{path.rsplit('/', 1)[-1]}:{qual}")

    def advice(fn: Function) -> str:
        by = ", ".join(f"`{c}`" for c in sorted(set(callers[graph.key(fn)]))[:3])
        return (
            f"Here it is called by {by} and others. Add a test for `{fn.qualname}` that asserts what it returns "
            f"today{_test_style(graph, fn.file)}; it pins current behaviour so later changes to it are caught."
        )

    return [
        IntentMatch(
            "arch-untested-hotspot",
            fn,
            fn.def_line,
            f"`{fn.qualname}` is called from {n} places on request/command paths and no test reaches it",
            {"callers": n, "advice": advice(fn)},
        )
        for n, pf, fn in picks[:5]
    ]


def _test_area(graph: CodeGraph, path: str) -> str:
    """The project a file belongs to: its tests live beside it, under the same pyproject.toml."""
    return _project_of(graph, path)


def _test_style(graph: CodeGraph, path: str) -> str:
    """How this project already tests: reuse its runner and client so a new test looks like the others."""
    area = _test_area(graph, path)
    sources = [pf.source for p, pf in graph.files.items() if is_test_file(p) and _test_area(graph, p) == area]
    if any(b"TestClient(" in s or b"ASGITransport(" in s for s in sources):
        return " (or drive one of its routes with the test client your tests already use)"
    if any(b"CliRunner(" in s for s in sources):
        return " (or invoke one of its commands with the CliRunner your tests already use)"
    return ""


# --------------------------------------------------------------------------- duplicated logic


def _body_fingerprint(pf: ParsedFile, node: Node) -> tuple[str, int] | None:
    body = node.child_by_field_name("body")
    if body is None:
        return None
    stmts = [s for s in body.named_children if s.type != "comment"]
    if stmts and stmts[0].type == "expression_statement" and stmts[0].named_children[0].type == "string":
        stmts = stmts[1:]  # docstrings don't make logic different
    if not stmts:
        return None
    src = "\n".join(text(s) for s in stmts)
    src = re.sub(r"#[^\n]*", "", src)
    lines = [ln.strip() for ln in src.splitlines() if ln.strip()]
    if len(lines) < 8:
        return None  # short bodies repeat by coincidence
    params = text(node.child_by_field_name("parameters"))
    return hashlib.sha1((params + "\n" + "\n".join(lines)).encode()).hexdigest(), len(lines)


def _project_of(graph: CodeGraph, path: str) -> str:
    """The nearest folder with a pyproject.toml or setup.py: separate projects may copy code on purpose."""
    return _project_dir(graph.root, path.rpartition("/")[0])


@functools.lru_cache(maxsize=4096)
def _project_dir(root: str, folder: str) -> str:
    base = Path(root)
    while folder:
        here = base / folder
        # A package (has __init__.py) is never a project root, even with a module named setup.py in it.
        if not (here / "__init__.py").exists() and ((here / "pyproject.toml").exists() or (here / "setup.py").exists()):
            return folder
        folder = folder.rpartition("/")[0]
    return "."


@graph_detector("arch-duplicate-logic")
def detect_duplicate_logic(graph: CodeGraph) -> list[IntentMatch]:
    """Functions in the same project whose bodies are identical apart from their names (8+ lines)."""
    groups: dict[tuple[str, str], list[tuple[ParsedFile, Function, int]]] = defaultdict(list)
    projects: dict[str, str] = {}
    seen: set[tuple[str, str]] = set()
    for pf, fn in graph.functions():
        if is_test_file(pf.path) or any(d.split("(", 1)[0].rsplit(".", 1)[-1] == "overload" for d in fn.decorators):
            continue
        if (pf.path, fn.qualname) in seen:
            continue  # a redefinition shares its name (and node) with the first: one function, not a copy
        seen.add((pf.path, fn.qualname))
        fp = _fingerprints(pf).get(fn.qualname)
        if fp:
            if pf.path not in projects:
                projects[pf.path] = _project_of(graph, pf.path)
            groups[(projects[pf.path], fp[0])].append((pf, fn, fp[1]))
    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda m: (m[1].file, m[1].start_line))
        pf, fn, size = members[0]
        files_ = sorted({m[1].file for m in members})
        if len(files_) == 1:
            advice = f"Here all copies are in `{files_[0]}`: keep `{fn.qualname}` and have the others call it."
        else:
            home = _common_folder(files_)
            advice = (
                f"Here the copies are in {', '.join(f'`{f}`' for f in files_[:4])}: move one copy into a module under "
                f"`{home or '.'}` that both import (or keep it in `{fn.file}` and import it from there), and replace "
                "the others with calls to it. Same body, same arguments, so callers see no change."
            )
        others = ", ".join(f"`{m[1].file}:{m[1].def_line}` ({m[1].qualname})" for m in members[1:4])
        out.append(
            IntentMatch(
                "arch-duplicate-logic",
                fn,
                fn.def_line,
                f"`{fn.qualname}` ({size} lines) has the same body as {others}"
                + (f" and {len(members) - 4} more" if len(members) > 4 else ""),
                {"copies": [f"{m[1].file}::{m[1].qualname}" for m in members], "advice": advice},
            )
        )
    return out


def _common_folder(paths: list[str]) -> str:
    parts = [p.split("/")[:-1] for p in paths]
    common = []
    for level in zip(*parts, strict=False):
        if len(set(level)) != 1:
            break
        common.append(level[0])
    return "/".join(common)


# --------------------------------------------------------------------------- private names across components


@functools.lru_cache(maxsize=4096)
def _package_root(root: str, folder: str) -> str:
    """The top-level importable package holding `folder` (highest folder in an __init__.py chain)."""
    base, top = Path(root), ""
    while folder and (base / folder / "__init__.py").exists():
        top, folder = folder, folder.rpartition("/")[0]
    return top or folder


@graph_detector("arch-private-import")
def detect_private_imports(graph: CodeGraph) -> list[IntentMatch]:
    """`from other_package.module import _helper`: depending on another package's internals.

    The boundary is the top-level package: inside one package, sharing underscore helpers
    between modules is ordinary; across packages it is depending on someone else's internals.
    """
    _, amap = _components(graph)
    sites = _import_sites(graph, amap)

    def pkg(path: str) -> str:
        return _package_root(graph.root, path.rpartition("/")[0])

    users: dict[tuple[str, str], int] = defaultdict(int)
    for (src, dest), found in sites.items():
        if pkg(src) != pkg(dest):
            for n in {n.rsplit(".", 1)[-1] for s in found for n in s["names"]}:
                users[(dest, n)] += 1
    out = []
    for (src, dest), found in sorted(sites.items()):
        if pkg(src) == pkg(dest):
            continue
        private = sorted({n.rsplit(".", 1)[-1] for s in found for n in s["names"]})
        private = [n for n in private if n.startswith("_") and not n.startswith("__")]
        if private:
            pf = graph.files[src]
            owner = pkg(dest).rsplit("/", 1)[-1] or dest
            out.append(
                IntentMatch(
                    "arch-private-import",
                    _module_fn(pf, found[0]["line"]),
                    found[0]["line"],
                    f"imports private {', '.join(f'`{n}`' for n in private)} from `{_short(graph, dest)}` in "
                    f"package `{owner}`",
                    {
                        "names": private,
                        "from": dest,
                        "advice": _private_advice(private, _short(graph, dest), owner, users[(dest, private[0])]),
                    },
                )
            )
    return out


def _private_advice(names: list[str], dest: str, owner: str, count: int) -> str:
    n = names[0]
    public = n.lstrip("_")
    spread = f"{count} files outside `{owner}` use it" if count > 1 else "this is the only file outside it that does"
    return (
        f"Here {spread}: give it a public name in `{dest}` (`{public}`, keeping `{n} = {public}` as an alias so "
        f"nothing inside `{owner}` breaks) and import the public name. The function itself doesn't change."
    )
