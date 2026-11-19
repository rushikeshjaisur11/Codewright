"""Map mode: the current architecture, generated from the code graph (never drawn by hand).

Three levels:
- system:  internal components (top-level packages) + external systems inferred from imports
- module:  the files inside one component and the imports between them
- flow:    the call tree from one entry point

Every node carries the files and lines that implement it, and an overlay of
Review findings, fan-in hotspots and untested functions.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .graph import CodeGraph, is_test_file
from .models import Finding

# import prefix -> (external system label, kind)
EXTERNAL = [
    ("sqlalchemy", "SQL database", "database"),
    ("psycopg", "PostgreSQL", "database"),
    ("psycopg2", "PostgreSQL", "database"),
    ("asyncpg", "PostgreSQL", "database"),
    ("sqlite3", "SQLite", "database"),
    ("pymysql", "MySQL", "database"),
    ("pymongo", "MongoDB", "database"),
    ("motor", "MongoDB", "database"),
    ("google.cloud.bigquery", "BigQuery", "database"),
    ("google.cloud.spanner", "Spanner", "database"),
    ("google.cloud.firestore", "Firestore", "database"),
    ("redis", "Redis", "cache"),
    ("pymemcache", "Memcached", "cache"),
    ("celery", "Celery broker", "queue"),
    ("kafka", "Kafka", "queue"),
    ("confluent_kafka", "Kafka", "queue"),
    ("pika", "RabbitMQ", "queue"),
    ("google.cloud.pubsub", "Pub/Sub", "queue"),
    ("google.cloud.tasks", "Cloud Tasks", "queue"),
    ("boto3", "AWS", "cloud"),
    ("google.cloud.storage", "Cloud Storage", "storage"),
    ("httpx", "External HTTP APIs", "http"),
    ("requests", "External HTTP APIs", "http"),
    ("aiohttp", "External HTTP APIs", "http"),
    ("urllib.request", "External HTTP APIs", "http"),
    ("google.adk", "Google ADK agents", "llm"),
    ("google.genai", "Gemini API", "llm"),
    ("vertexai", "Vertex AI", "llm"),
    ("openai", "OpenAI API", "llm"),
    ("anthropic", "Claude API", "llm"),
    ("litellm", "LLM providers", "llm"),
    ("langchain", "LangChain", "llm"),
    ("langgraph", "LangGraph", "llm"),
    ("fastapi", "HTTP clients (FastAPI)", "ingress"),
    ("flask", "HTTP clients (Flask)", "ingress"),
    ("django", "HTTP clients (Django)", "ingress"),
    ("typer", "CLI users", "ingress"),
    ("click", "CLI users", "ingress"),
]


@dataclass
class Node:
    id: str
    label: str
    kind: str  # component | module | function | external kinds
    files: list[dict] = field(default_factory=list)  # [{file, line}]
    findings: int = 0
    hotspot: int = 0  # fan-in
    untested: int = 0
    detail: str = ""
    arch: int = 0  # architecture findings in these files (a subset of `findings`)


@dataclass
class Edge:
    source: str
    target: str
    label: str = ""
    weight: int = 1


@dataclass
class View:
    level: str
    title: str
    nodes: list[Node]
    edges: list[Edge]

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "title": self.title,
            "nodes": [asdict(n) for n in self.nodes],
            "edges": [asdict(e) for e in self.edges],
        }

    def mermaid(self) -> str:
        shapes = {
            "database": ("[(", ")]"),
            "cache": ("[(", ")]"),
            "queue": ("[[", "]]"),
            "ingress": ("([", "])"),
            "llm": ("{{", "}}"),
        }
        ids = {n.id: f"n{i}" for i, n in enumerate(self.nodes)}
        lines = ["flowchart LR"]
        for n in self.nodes:
            left, right = shapes.get(n.kind, ("[", "]"))
            label = n.label.replace('"', "'")
            lines.append(f'    {ids[n.id]}{left}"{label}"{right}')
        for e in self.edges:
            if e.source in ids and e.target in ids:
                arrow = f"-- {e.label} -->" if e.label else "-->"
                lines.append(f"    {ids[e.source]} {arrow} {ids[e.target]}")
        return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- module naming


def module_name(path: str) -> str:
    parts = path[:-3].split("/") if path.endswith(".py") else path.split("/")
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def component_of(path: str, depth: int = 1) -> str:
    name = module_name(path)
    if not name:
        return "(root)"
    return ".".join(name.split(".")[:depth])


NAMESPACE_DIRS = {"packages", "apps", "libs", "services", "projects", "modules", "components", "plugins"}


def assign_components(
    paths: list[str], max_rounds: int = 4, share: float = 0.4, max_children: int = 24
) -> dict[str, str]:
    """Pick the system view's components so no single box swallows the repo.

    Start from top-level packages. A component is split one level deeper when it holds
    more than `share` of all modules, or is a monorepo namespace folder (packages/, apps/...),
    and its modules spread over at least two subpackages. Levels with a single child
    (``src/``, ``pkg/pkg/``) are passed through. Labels drop ``src``, repeated names and
    the namespace folder, so ``packages/devmem/src/devmem/core`` reads ``devmem.core``.
    """
    parts = {p: module_name(p).split(".") if module_name(p) else ["(root)"] for p in paths}
    depth = dict.fromkeys(paths, 1)
    total = len(paths) or 1
    for _ in range(max_rounds):
        groups: dict[tuple[str, ...], list[str]] = defaultdict(list)
        for p in paths:
            groups[tuple(parts[p][: depth[p]])].append(p)
        changed = False
        for key, members in groups.items():
            if not (len(members) > total * share or key[-1] in NAMESPACE_DIRS):
                continue
            extra = 0
            while True:  # pass through single-child levels
                deeper = [m for m in members if len(parts[m]) > depth[m] + extra]
                children = {parts[m][depth[m] + extra] for m in deeper}
                if len(children) != 1 or len(deeper) != len(members):
                    break
                extra += 1
            if len(children) < 2:
                continue
            if len(children) > max_children:
                # Too many boxes: push down real subpackages only; loose modules stay together.
                at = {m: depth[m] + extra for m in deeper}
                deeper = [m for m in deeper if len(parts[m]) > at[m] + 1]
            for m in deeper:
                depth[m] += extra + 1
            changed = changed or bool(deeper)
        if not changed:
            break
    return {p: _component_label(parts[p][: depth[p]]) for p in paths}


def importable_names(mod: str) -> list[str]:
    """Dotted names other code can import `mod` by, besides its file path."""
    parts = mod.split(".")
    out = []
    if "src" in parts:
        out.append(".".join(parts[len(parts) - parts[::-1].index("src") :]))
    if parts[0] in NAMESPACE_DIRS and len(parts) > 2:
        out.append(".".join(parts[2:]))
    return [o for o in out if o and o != mod]


def _component_label(parts: list[str]) -> str:
    out: list[str] = []
    for i, x in enumerate(parts):
        if x == "src" or (out and out[-1].replace("-", "_") == x.replace("-", "_")):
            continue
        if i == 0 and x in NAMESPACE_DIRS and len(parts) > 1:
            continue
        out.append(x)
    return ".".join(out) or ".".join(parts)


def resolve_relative(target: str, from_path: str) -> str:
    if not target.startswith("."):
        return target
    dots = len(target) - len(target.lstrip("."))
    pkg = module_name(from_path).split(".")
    if not from_path.endswith("__init__.py"):
        pkg = pkg[:-1]
    base = pkg[: len(pkg) - (dots - 1)] if dots > 1 else pkg
    rest = target.lstrip(".")
    return ".".join([*base, rest] if rest else base)


# Folders whose files are scripts, docs or samples rather than part of the running system.
TOOLING_DIRS = {
    "scripts",
    "script",
    "tools",
    "examples",
    "example",
    "samples",
    "sample",
    "docs",
    "doc",
    "benchmarks",
    "bench",
    "migrations",
    "alembic",
    "notebooks",
    "sandbox",
    "playground",
    "demo",
    "demos",
    "experiments",
}
LAUNCH_PREFIXES = ("run_", "start_", "serve", "worker", "consume", "listen", "poll_", "main_")
TOOLING_FILES = {"setup.py", "noxfile.py", "fabfile.py", "tasks.py", "conftest.py"}
# Files at the repo root that start the app by module name or path (uvicorn app.main:app, gunicorn, Procfile...).
LAUNCH_FILES = (
    "pyproject.toml",
    "setup.cfg",
    "Dockerfile",
    "Procfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
    "Makefile",
    "app.yaml",
    "fly.toml",
    "render.yaml",
    "tox.ini",
)


def is_tooling(path: str) -> bool:
    parts = path.split("/")
    return parts[-1] in TOOLING_FILES or any(p in TOOLING_DIRS for p in parts[:-1])


# A quote followed by a dotted name: `"pkg.plugins.csv"` or `'a.b:func'` (importlib, plugin lists, entry points).
_LAUNCH_TOKEN = re.compile(r"(?:^|(?<=[\s\"'/=\[,]))([^\s\"'/=\[,]+)")
_STRING_DOTTED = re.compile(rb"[\"']([A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)+)")


class ArchMap:
    def __init__(self, graph: CodeGraph, findings: list[Finding] | None = None, include_unused: bool = False):
        self.g = graph
        self.findings = findings or []
        self.modules = {module_name(p): p for p in graph.files if not is_test_file(p)}
        self._compmap = assign_components(list(self.modules.values()))
        # Imports name modules by their importable path (``devmem.store``), not their file path
        # (``packages/devmem/src/devmem/store.py``): index both.
        self._import_names: dict[str, str] = {}
        for mod in self.modules:
            for alias in importable_names(mod):
                self._import_names.setdefault(alias, mod)
        self._fan_in = Counter(c for callees in graph.edges.values() for c in callees)
        self._tested = self._tested_names()
        self._findings_by_file = Counter(f.file for f in self.findings)
        self._findings_by_fn = Counter(f"{f.file}::{f.function}" for f in self.findings)
        arch = [f for f in self.findings if getattr(f, "category", "") == "architecture"]
        self._arch_by_file = Counter(f.file for f in arch)
        self._arch_by_fn = Counter(f"{f.file}::{f.function}" for f in arch)
        self.unused = self._unused()
        hidden = {u["file"] for u in self.unused}
        shown = [p for p in self.modules.values() if p not in hidden]
        # Never hide everything: a repo of loose scripts still gets a map.
        self.shown = set(self.modules.values()) if include_unused or not shown else set(shown)

    # ----------------------------------------------------------------- what is part of the architecture
    def _launch_text(self) -> str:
        """Config and launch files that can start a module by name: the root ones, plus deploy and
        compose files, shell scripts and agent configs (langgraph.json...) anywhere in the repo."""
        root = Path(self.g.root)
        out = []
        for name in LAUNCH_FILES:
            with contextlib.suppress(OSError):
                out.append((root / name).read_text(errors="replace"))
        cached = self.g.__dict__.get("_launch_text")
        if cached is not None:
            return cached
        from .scanscope import walk_files

        def wanted(name: str) -> bool:
            if name in ("package-lock.json", "uv.lock", "poetry.lock", "yarn.lock", "pnpm-lock.yaml"):
                return False
            return name.endswith((".yml", ".yaml", ".json", ".toml", ".cfg", ".ini", ".sh")) or name.startswith(
                ("Procfile", "Dockerfile")
            )

        seen = 0
        for f in walk_files(root, wanted, keep_dirs=(".github",)):  # one pass, never into .venv or node_modules
            if seen >= 300:
                break
            with contextlib.suppress(OSError):
                if f.stat().st_size < 200_000:
                    out.append(f.read_text(errors="replace"))
                    seen += 1
        self.g.__dict__["_launch_text"] = text = "\n".join(out)
        return text

    def _launched_outside(self, path: str) -> bool:
        """A module the repo never calls but something outside it likely starts: an ADK `root_agent`,
        or a top-level `run_*`/`start_*`/`serve*`/`worker*`/`consume*` function nothing in the repo calls."""
        pf = self.g.files[path]
        if b"root_agent =" in pf.source or b"root_agent=" in pf.source:
            return True
        for fn in pf.module.functions:
            if "." in fn.qualname or fn.name.startswith("_"):
                continue
            if fn.name.startswith(LAUNCH_PREFIXES) and self._fan_in.get(f"{path}::{fn.qualname}", 0) == 0:
                return True
        return False

    def _unused(self) -> list[dict]:
        """Modules the running system never loads, each with why: {file, reason}.

        In use: entry points (routes, CLI commands, `main`), `__main__` blocks and `__main__.py`
        outside script folders, modules named in pyproject/Dockerfile/Procfile, ADK `root_agent`
        modules, and everything they import, directly or through other modules. A repo with no
        entry point at all is a library, so every module outside script folders is in use.
        """
        cached = self.g.__dict__.get("_unused_modules")  # depends only on the graph and the launch files
        if cached is not None:
            return [dict(u) for u in cached]
        out = self._find_unused()
        self.g.__dict__["_unused_modules"] = [dict(u) for u in out]
        return out

    def _find_unused(self) -> list[dict]:
        files = list(self.modules.values())
        launch = self._launch_text()
        # Every file name the launch text mentions after a separator, found in one pass (a regex per module
        # over all the config text took seconds on big repos).
        launch_names: set[str] = set()
        for m in _LAUNCH_TOKEN.finditer(launch):
            tok = m.group(1)
            launch_names.add(tok)
            for i, ch in enumerate(tok):
                if not (ch.isalnum() or ch == "_"):
                    launch_names.add(tok[:i])
        roots = set()
        weak = set()  # roots only because of their file name (app.py nobody imports): not proof of an app
        entry_files = {k.partition("::")[0] for k in self.g.entry_points}
        for p in files:
            if is_tooling(p):
                continue
            pf = self.g.files[p]
            mod = module_name(p)
            names = [mod, *importable_names(mod)]
            base = p.rsplit("/", 1)[-1]
            entry = self.g.app_entries.get(p)
            if entry is not None and all(r.startswith("is named") for r in entry.reasons) and p not in entry_files:
                weak.add(p)
            if (
                p in entry_files
                or p in self.g.app_entries
                or pf.module.has_main_guard
                or p.endswith(("__main__.py", "wsgi.py", "asgi.py"))
                or self._launched_outside(p)
                or any(f"{n}:" in launch or f"-m {n}" in launch for n in names if n)
                or p in launch
                or (p.count("/") <= 3 and base in launch_names)
            ):
                roots.add(p)
        if not roots - weak:  # a library: consumers may import any module
            roots = {p for p in files if not is_tooling(p)}
        imports = {p: self._module_imports(p)[0] for p in files}
        used, stack = set(), list(roots)
        while stack:
            p = stack.pop()
            if p in used:
                continue
            used.add(p)
            parts = module_name(p).split(".")
            for i in range(1, len(parts)):  # importing a.b.c runs a/__init__ and a/b/__init__
                parent = self.modules.get(".".join(parts[:i]))
                if parent and parent not in used:
                    stack.append(parent)
            stack.extend(self.modules[m] for m in imports[p] if self.modules[m] not in used)
        # Dynamic imports: a module named in a string by code in use (importlib, plugin lists).
        # One pass over the used files collects every dotted name a string literal starts with, so each
        # unused module is a set lookup (the old substring search over all sources was O(modules x bytes)).
        literals: set[str] = set()
        for p in used:
            for m in _STRING_DOTTED.finditer(self.g.files[p].source):
                parts = m.group(1).decode().split(".")
                literals.update(".".join(parts[:i]) for i in range(2, len(parts) + 1))
        for p in files:
            if p not in used and not is_tooling(p):
                mod = module_name(p)
                if any(n in literals for n in [mod, *importable_names(mod)] if "." in n):
                    used.add(p)
        tested_only = set()
        for path in self.g.files:
            if is_test_file(path):
                for mod in self._module_imports(path)[0]:
                    tested_only.add(self.modules[mod])
        out = []
        for p in sorted(files):
            if p in used:
                continue
            pf = self.g.files[p]
            if p.endswith("__init__.py") and not pf.module.functions and not pf.module.imports:
                continue  # an empty package marker: nothing to show either way
            if is_tooling(p):
                reason = "script, docs or example folder"
            elif p in tested_only:
                reason = "only tests import it"
            else:
                reason = "nothing in use imports it"
            out.append({"file": p, "reason": reason, "functions": len(pf.module.functions)})
        return out

    # ----------------------------------------------------------------- helpers
    def _comp(self, path: str) -> str:
        return self._compmap.get(path) or component_of(path)

    def _tested_names(self) -> set[str]:
        names = set()
        for path, pf in self.g.files.items():
            if is_test_file(path):
                for fn in pf.module.functions:
                    names.update(c.rsplit(".", 1)[-1] for c in fn.calls)
                names.update(t.rsplit(".", 1)[-1] for t in pf.module.imports.values())
        return names

    def _internal_target(self, target: str) -> str | None:
        """Longest internal module that `target` (a dotted import) points into."""
        parts = target.split(".")
        for i in range(len(parts), 0, -1):
            cand = ".".join(parts[:i])
            if cand in self.modules:
                return cand
            if cand in self._import_names:
                return self._import_names[cand]
        return None

    def _module_imports(self, path: str) -> tuple[set[str], set[str]]:
        """(internal module names, external import targets) for one file."""
        internal, external = set(), set()
        for target in self.g.files[path].module.imports.values():
            t = resolve_relative(target, path)
            mod = self._internal_target(t)
            if mod and mod != module_name(path):
                internal.add(mod)
            elif not mod:
                external.add(t)
        return internal, external

    @staticmethod
    def _external_for(target: str) -> tuple[str, str] | None:
        for prefix, label, kind in EXTERNAL:
            if target == prefix or target.startswith(prefix + "."):
                return label, kind
        return None

    def _fn_stats(self, path: str) -> tuple[int, int]:
        pf = self.g.files[path]
        hot = max((self._fan_in.get(f"{path}::{fn.qualname}", 0) for fn in pf.module.functions), default=0)
        untested = sum(1 for fn in pf.module.functions if not fn.name.startswith("_") and fn.name not in self._tested)
        return hot, untested

    # ----------------------------------------------------------------- views
    def system(self) -> View:
        comps: dict[str, list[str]] = defaultdict(list)
        for path in self.modules.values():
            if path in self.shown:
                comps[self._comp(path)].append(path)
        nodes: dict[str, Node] = {}
        edges: Counter = Counter()
        ext_edges: Counter = Counter()
        for comp, paths in sorted(comps.items()):
            if (
                all(p.endswith("__init__.py") and not self.g.files[p].module.functions for p in paths)
                and len(comps) > 1
            ):
                continue  # empty package marker
            fn_count = sum(len(self.g.files[p].module.functions) for p in paths)
            hot = max((self._fn_stats(p)[0] for p in paths), default=0)
            nodes[f"c:{comp}"] = Node(
                f"c:{comp}",
                comp,
                "component",
                files=[{"file": p, "line": 1} for p in sorted(paths)],
                findings=sum(self._findings_by_file[p] for p in paths),
                arch=sum(self._arch_by_file[p] for p in paths),
                hotspot=hot,
                untested=sum(self._fn_stats(p)[1] for p in paths),
                detail=f"{len(paths)} modules · {fn_count} functions",
            )
            for p in paths:
                internal, external = self._module_imports(p)
                for mod in internal:
                    other = self._comp(self.modules[mod])
                    if other != comp:
                        edges[(comp, other)] += 1
                for t in external:
                    if ext := self._external_for(t):
                        ext_edges[(comp, ext)] += 1
        for comp, (label, kind) in ext_edges:
            nid = f"x:{label}"
            if nid not in nodes:
                nodes[nid] = Node(nid, label, kind, detail=kind)
            nodes[nid].files.extend(
                {"file": p, "line": 1}
                for p in comps[comp]
                if any(self._external_for(t) == (label, kind) for t in self._module_imports(p)[1])
            )
        out_edges = [
            Edge(f"c:{a}", f"c:{b}", "imports", w)
            for (a, b), w in edges.items()
            if f"c:{a}" in nodes and f"c:{b}" in nodes
        ]
        for (comp, (label, kind)), w in ext_edges.items():
            if f"c:{comp}" not in nodes:
                continue
            src, dst = (f"x:{label}", f"c:{comp}") if kind == "ingress" else (f"c:{comp}", f"x:{label}")
            out_edges.append(
                Edge(
                    src,
                    dst,
                    {
                        "database": "queries",
                        "cache": "reads/writes",
                        "queue": "publishes",
                        "http": "calls",
                        "llm": "prompts",
                        "ingress": "requests",
                    }.get(kind, "uses"),
                    w,
                )
            )
        return View("system", "System", list(nodes.values()), out_edges)

    def module(self, component: str) -> View:
        paths = sorted(p for p in self.shown if self._comp(p) == component)
        nodes, edges = [], []
        for p in paths:
            pf = self.g.files[p]
            hot, untested = self._fn_stats(p)
            nodes.append(
                Node(
                    f"m:{module_name(p)}",
                    module_name(p).split(".", 1)[-1] if "." in module_name(p) else module_name(p),
                    "module",
                    files=[{"file": p, "line": fn.def_line, "name": fn.qualname} for fn in pf.module.functions]
                    or [{"file": p, "line": 1}],
                    findings=self._findings_by_file[p],
                    arch=self._arch_by_file[p],
                    hotspot=hot,
                    untested=untested,
                    detail=f"{len(pf.module.functions)} functions",
                )
            )
            internal, external = self._module_imports(p)
            for mod in internal:
                if self.modules[mod] in self.shown and self._comp(self.modules[mod]) == component:
                    edges.append(Edge(f"m:{module_name(p)}", f"m:{mod}", "imports"))
            for label_kind in {self._external_for(t) for t in external} - {None}:
                nid = f"x:{label_kind[0]}"
                if not any(n.id == nid for n in nodes):
                    nodes.append(Node(nid, label_kind[0], label_kind[1], detail=label_kind[1]))
                edges.append(Edge(f"m:{module_name(p)}", nid, "uses"))
        return View("module", f"Component: {component}", nodes, edges)

    def flow(self, entry: str, depth: int = 6) -> View:
        nodes: dict[str, Node] = {}
        edges: list[Edge] = []
        frontier = [(entry, 0)]
        while frontier:
            key, d = frontier.pop(0)
            if key in nodes:
                continue
            path, _, qual = key.partition("::")
            if qual == "<module>" and path in self.g.app_entries:
                e = self.g.app_entries[path]
                nodes[key] = Node(
                    key,
                    e.trigger,
                    "entry",
                    files=[{"file": path, "line": e.line}],
                    findings=self._findings_by_file[path],
                    detail=f"{e.framework} · {path}",
                )
                for callee in e.calls:
                    if not is_test_file(callee.partition("::")[0]):
                        edges.append(Edge(key, callee, "runs"))
                        frontier.append((callee, d + 1))
                continue
            pf = self.g.files.get(path)
            fn = next((f for f in pf.module.functions if f.qualname == qual), None) if pf else None
            if fn is None:
                continue
            nodes[key] = Node(
                key,
                qual,
                "entry" if key == entry else "function",
                files=[{"file": path, "line": fn.def_line}],
                findings=self._findings_by_fn[key],
                arch=self._arch_by_fn[key],
                hotspot=self._fan_in.get(key, 0),
                untested=0 if fn.name in self._tested or fn.name.startswith("_") else 1,
                detail=f"{path}:{fn.def_line}",
            )
            if d >= depth:
                continue
            for callee in sorted(self.g.edges.get(key, ())):
                if not is_test_file(callee.partition("::")[0]):
                    edges.append(Edge(key, callee, "calls"))
                    frontier.append((callee, d + 1))
            _, external = self._module_imports(path)
            used = {
                self._external_for(t)
                for t in external
                if any(
                    c.split(".")[0] in {a for a, v in pf.module.imports.items() if v == t or v.startswith(t)}
                    for c in fn.calls
                )
            } - {None}
            for label, kind in used:
                nid = f"x:{label}"
                nodes.setdefault(nid, Node(nid, label, kind, detail=kind))
                edges.append(Edge(key, nid, "uses"))
        title = entry.partition("::")[2]
        if title == "<module>":
            title = (
                self.g.app_entries[entry.partition("::")[0]].trigger
                if entry.partition("::")[0] in self.g.app_entries
                else entry
            )
        return View("flow", f"Request flow: {title}", list(nodes.values()), edges)

    def view(self, level: str, target: str = "") -> View:
        """The system, module or flow view, defaulting the target. Raises ValueError with what is available."""
        if level == "system":
            return self.system()
        if level == "module":
            comps = self.components()
            if not comps:
                raise ValueError("no components: the repo has no Python modules outside tests")
            if target and target not in comps:
                raise ValueError(f"unknown component {target!r}; available: {', '.join(comps)}")
            return self.module(target or comps[0])
        if level == "flow":
            eps = self.entry_points()
            if not eps:
                raise ValueError("no entry points found (web routes, CLI commands or __main__ blocks)")
            ids = [e["id"] for e in eps]
            if target and target not in ids:
                raise ValueError(f"unknown entry point {target!r}; available: {', '.join(ids[:20])}")
            return self.flow(target or ids[0])
        raise ValueError("level must be system, module or flow")

    def entry_points(self) -> list[dict]:
        """Apps first (files started directly: `uvicorn app:app`, `streamlit run app.py`...), then routes,
        commands and handlers. A `main` run only from its file's `__main__` block is folded into that file."""
        out = []
        apps = self.g.app_entries
        for path in sorted(apps):
            e = apps[path]
            if is_test_file(path) or is_tooling(path):
                continue
            out.append(
                {
                    "id": f"{path}::<module>",
                    "name": path,
                    "file": path,
                    "line": e.line,
                    "trigger": e.trigger,
                    "framework": e.framework,
                    "reasons": e.reasons,
                    "app": True,
                }
            )
        for key in sorted(self.g.entry_points):
            path, _, qual = key.partition("::")
            if qual == "<module>":
                continue
            if qual == "main" and path in apps and key in apps[path].calls:
                continue
            fn = next((f for f in self.g.files[path].module.functions if f.qualname == qual), None)
            if fn is None:
                continue
            route = next((d for d in fn.decorators if "(" in d), fn.decorators[0] if fn.decorators else "main")
            out.append({"id": key, "name": qual, "file": path, "line": fn.def_line, "trigger": route})
        return out

    def module_page(self, path: str) -> dict:
        pf = self.g.files[path]
        mod = module_name(path)
        internal, external = self._module_imports(path)
        imported_by = sorted(m for m, p in self.modules.items() if mod in self._module_imports(p)[0])
        doc = ""
        first = pf.root.named_children[0] if pf.root.named_children else None
        if (
            first is not None
            and first.type == "expression_statement"
            and first.named_children
            and first.named_children[0].type == "string"
        ):
            doc = first.text.decode(errors="replace").strip("\"' \n")
        return {
            "file": path,
            "module": mod,
            "doc": doc,
            "functions": [
                {
                    "name": fn.qualname,
                    "line": fn.def_line,
                    "end": fn.end_line,
                    "async": fn.is_async,
                    "decorators": fn.decorators,
                    "calls": fn.calls[:20],
                    "fan_in": self._fan_in.get(f"{path}::{fn.qualname}", 0),
                    "tested": fn.name in self._tested,
                    "findings": self._findings_by_fn[f"{path}::{fn.qualname}"],
                }
                for fn in pf.module.functions
            ],
            "imports": sorted(internal),
            "external": sorted(external),
            "imported_by": imported_by,
            "findings": [f.id for f in self.findings if f.file == path],
        }

    def components(self) -> list[str]:
        return sorted({self._comp(p) for p in self.shown})


# --------------------------------------------------------------------------- snapshots / changes


def snapshot(root: str | Path, view: View) -> Path:
    from .feedback import ensure_dir

    d = ensure_dir(root) / "snapshots"
    d.mkdir(exist_ok=True)
    stamp = time.strftime("%Y-%m-%d")
    p = d / f"{stamp}.json"
    p.write_text(json.dumps({"at": time.time(), **view.to_dict()}))
    return p


def changes_since(root: str | Path, view: View, days: int = 7) -> dict:
    """Compare the current system view with the oldest snapshot from the last `days` days."""
    d = Path(root) / ".codewright" / "snapshots"
    cutoff = time.time() - days * 86400
    old = None
    for p in sorted(d.glob("*.json")) if d.exists() else []:
        data = json.loads(p.read_text())
        if data.get("at", 0) >= cutoff:
            old = data
            break
    if old is None:
        return {"since": None, "added_nodes": [], "removed_nodes": [], "added_edges": [], "removed_edges": []}
    old_nodes = {n["id"]: n["label"] for n in old["nodes"]}
    new_nodes = {n.id: n.label for n in view.nodes}
    old_edges = {(e["source"], e["target"]) for e in old["edges"]}
    new_edges = {(e.source, e.target) for e in view.edges}
    return {
        "since": old["at"],
        "added_nodes": sorted(new_nodes[k] for k in new_nodes.keys() - old_nodes.keys()),
        "removed_nodes": sorted(old_nodes[k] for k in old_nodes.keys() - new_nodes.keys()),
        "added_edges": sorted(f"{a} -> {b}" for a, b in new_edges - old_edges),
        "removed_edges": sorted(f"{a} -> {b}" for a, b in old_edges - new_edges),
    }
