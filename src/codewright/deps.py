"""Dependency audit: which dependencies the repo could drop, merge or should worry about.

For each dependency the project declares (pyproject, requirements files), with what the code
actually imports:

* ``overlap``: two declared libraries that do the same job (``requests`` and ``httpx``), with
  how many files use each, so the team can settle on one;
* ``stdlib``: a backport or old package the target Python now covers (``mock``, ``pytz``,
  ``toml``, ``six``...);
* ``unused``: declared but imported nowhere and named in no config (drivers loaded by URL,
  servers started from a Procfile and pytest plugins are recognised and kept);
* ``cost``: packages it pulls in, from ``uv.lock``/``poetry.lock``, and how many would leave
  with it (those nothing else needs);
* ``license``: a copyleft licence (GPL, AGPL) among runtime dependencies, read from the
  installed metadata;
* ``abandoned``: no release in three years, when the package index can be reached.

Nothing here edits a dependency file; each finding says what to change and what it saves.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import tomllib
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version

from .graph import CodeGraph, is_test_file
from .models import normalise
from .stack import python_version

# Libraries that do the same job; only declared ones that the code imports count.
OVERLAPS: dict[str, list[str]] = {
    "HTTP client": ["requests", "httpx", "aiohttp", "urllib3", "pycurl", "treq"],
    "JSON": ["simplejson", "ujson", "orjson", "rapidjson", "python-rapidjson"],
    "Dates and times": ["arrow", "pendulum", "maya", "delorean"],
    "YAML": ["pyyaml", "ruamel-yaml", "oyaml"],
    "CLI framework": ["click", "typer", "fire", "docopt", "cleo"],
    "Data validation": ["marshmallow", "pydantic", "cerberus", "voluptuous", "schema", "attrs-strict"],
    "Task queue": ["celery", "rq", "dramatiq", "huey", "arq"],
    "Retry helper": ["tenacity", "backoff", "retrying", "retry"],
    "Postgres driver": ["psycopg", "psycopg2", "psycopg2-binary", "asyncpg", "pg8000"],
    "MySQL driver": ["pymysql", "mysqlclient", "mysql-connector-python", "aiomysql"],
    "Test runner": ["pytest", "nose", "nose2"],
    "Logging": ["loguru", "structlog", "logbook"],
    "Environment files": ["python-dotenv", "environs", "python-decouple"],
    "Templating": ["jinja2", "mako", "chameleon"],
    "Web framework": ["flask", "fastapi", "django", "starlette", "aiohttp", "tornado", "sanic", "bottle", "falcon"],
}

# package -> (Python it is in the standard library from, the replacement, note)
STDLIB: dict[str, tuple[str, str, str]] = {
    "mock": ("3.3", "unittest.mock", "same API under `unittest.mock`"),
    "six": ("3.0", "Python 3 syntax", "Python-2 compatibility layer"),
    "future": ("3.0", "Python 3 syntax", "Python-2 compatibility layer"),
    "enum34": ("3.4", "enum", ""),
    "pathlib2": ("3.4", "pathlib", ""),
    "futures": ("3.2", "concurrent.futures", ""),
    "dataclasses": ("3.7", "dataclasses", ""),
    "contextvars": ("3.7", "contextvars", ""),
    "typing": ("3.5", "typing", "the PyPI `typing` package shadows the standard module"),
    "argparse": ("3.2", "argparse", ""),
    "contextlib2": ("3.7", "contextlib", "nullcontext, AsyncExitStack and friends are in contextlib"),
    "cached-property": ("3.8", "functools.cached_property", ""),
    "importlib-metadata": (
        "3.10",
        "importlib.metadata",
        "`packages_distributions()` and the selectable entry points API",
    ),
    "importlib-resources": ("3.9", "importlib.resources", "`files()` is in the standard library"),
    "backports-zoneinfo": ("3.9", "zoneinfo", ""),
    "pytz": ("3.9", "zoneinfo", "`ZoneInfo('Europe/Paris')`; no more `localize()`/`normalize()` pitfalls"),
    "toml": ("3.11", "tomllib", "reading TOML; keep a writer (tomli-w) if you write it"),
    "tomli": ("3.11", "tomllib", "same API, renamed"),
    "async-timeout": ("3.11", "asyncio.timeout", ""),
    "exceptiongroup": ("3.11", "built-in ExceptionGroup", ""),
    "asyncio": ("3.4", "asyncio", "the PyPI `asyncio` package is an old snapshot that shadows the standard module"),
    "simplejson": ("2.6", "json", "unless you need Decimal round-trips (json has `parse_float=Decimal` too)"),
    "uuid": ("2.5", "uuid", "the PyPI `uuid` package is an old snapshot"),
    "ordereddict": ("2.7", "dict", "dicts keep insertion order since 3.7"),
}

# Distribution name -> import names, where they differ (fallback when the environment can't tell).
IMPORT_NAMES: dict[str, list[str]] = {
    "pyyaml": ["yaml"],
    "beautifulsoup4": ["bs4"],
    "pillow": ["PIL"],
    "scikit-learn": ["sklearn"],
    "python-dateutil": ["dateutil"],
    "python-dotenv": ["dotenv"],
    "python-multipart": ["multipart", "python_multipart"],
    "psycopg2-binary": ["psycopg2"],
    "psycopg-binary": ["psycopg"],
    "pymysql": ["pymysql"],
    "mysqlclient": ["MySQLdb"],
    "mysql-connector-python": ["mysql"],
    "opencv-python": ["cv2"],
    "protobuf": ["google.protobuf"],
    "google-adk": ["google.adk"],
    "google-cloud-storage": ["google.cloud.storage"],
    "google-genai": ["google.genai"],
    "ruamel-yaml": ["ruamel"],
    "attrs": ["attr", "attrs"],
    "pyjwt": ["jwt"],
    "python-jose": ["jose"],
    "msgpack-python": ["msgpack"],
    "typing-extensions": ["typing_extensions"],
    "backports-zoneinfo": ["backports.zoneinfo"],
    "importlib-metadata": ["importlib_metadata"],
    "importlib-resources": ["importlib_resources"],
    "cached-property": ["cached_property"],
    "python-rapidjson": ["rapidjson"],
    "pyzmq": ["zmq"],
    "pycryptodome": ["Crypto"],
    "faiss-cpu": ["faiss"],
    "sentry-sdk": ["sentry_sdk"],
}

# Used without being imported: servers, drivers named in a URL, plugins, type stubs, tools.
RUNTIME_ONLY = re.compile(
    r"^(uvicorn|gunicorn|hypercorn|daphne|granian|waitress|"
    r"psycopg2?(-binary)?|psycopg-binary|psycopg-c|asyncpg|aiosqlite|pymysql|mysqlclient|cx-oracle|oracledb|pyodbc|"
    r"python-multipart|email-validator|uvloop|httptools|watchfiles|websockets|"
    r"pytest(-.+)?|coverage|tox|nox|ruff|black|isort|flake8(-.+)?|pylint|mypy|pyright|pre-commit|"
    r"types-.+|.+-stubs|setuptools|wheel|hatchling|build|twine|pip|uv|poetry(-core)?|"
    r"sphinx(-.+)?|mkdocs(-.+)?|ipython|ipykernel|jupyter(lab)?|notebook|debugpy|"
    r"gevent|eventlet|greenlet|brotli|certifi|tzdata|cryptography)$"
)
COPYLEFT = re.compile(r"\b(A?GPL|GNU (Affero )?General Public)", re.I)
ABANDONED_DAYS = 3 * 365


@dataclass
class Declared:
    name: str
    group: str  # main | optional:<extra> | dev
    spec: str
    source: str


@dataclass
class DepRow:
    name: str
    version: str | None
    group: str
    source: str
    imports: list[str]
    files: int  # non-test files importing it
    test_files: int
    requires: int = 0  # packages it pulls in (transitively)
    unique: int = 0  # of those, needed by nothing else
    license: str = ""
    released: str = ""  # latest release date, when the index was reached
    flags: list[str] = field(default_factory=list)


@dataclass
class DepFinding:
    kind: str  # overlap | stdlib | unused | license | abandoned | heavy
    title: str
    deps: list[str]
    detail: str
    suggestion: str
    saves: int = 0  # packages that leave the install
    effort: int = 1  # files to touch, roughly
    severity: str = "info"  # info | warn

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- what the project declares


def declared(root: str | Path) -> dict[str, Declared]:
    root = Path(root)
    out: dict[str, Declared] = {}

    def add(raw: str, group: str, source: str) -> None:
        raw = raw.split("#", 1)[0].strip()
        if not raw or raw.startswith(("-", "git+", "http://", "https://")):
            return
        try:
            req = Requirement(raw)
        except InvalidRequirement:
            return
        key = normalise(req.name)
        if key not in out or (out[key].group != "main" and group == "main"):
            out[key] = Declared(key, group, str(req.specifier), source)

    pp = root / "pyproject.toml"
    if pp.exists():
        try:
            data = tomllib.loads(pp.read_text())
        except tomllib.TOMLDecodeError:
            data = {}
        project = data.get("project", {})
        own = normalise(project.get("name", "")) if project.get("name") else None
        for d in project.get("dependencies", []):
            add(d, "main", "pyproject.toml")
        for extra, items in project.get("optional-dependencies", {}).items():
            group = (
                "dev" if extra.lower() in ("dev", "test", "tests", "lint", "docs", "testing") else f"optional:{extra}"
            )
            for d in items:
                add(d, group, "pyproject.toml")
        for items in data.get("dependency-groups", {}).values():
            for d in items:
                if isinstance(d, str):
                    add(d, "dev", "pyproject.toml")
        poetry = data.get("tool", {}).get("poetry", {})
        for name in poetry.get("dependencies", {}):
            if name.lower() != "python":
                add(name, "main", "pyproject.toml")
        for g in [
            poetry.get("dev-dependencies", {}),
            *(x.get("dependencies", {}) for x in poetry.get("group", {}).values()),
        ]:
            for name in g:
                add(name, "dev", "pyproject.toml")
        if own:
            out.pop(own, None)
    for req in sorted(root.glob("requirements*.txt")):
        group = "dev" if re.search(r"dev|test|lint|doc", req.name) else "main"
        for line in req.read_text(errors="replace").splitlines():
            add(line, group, req.name)
    return out


# --------------------------------------------------------------------------- the lockfile's dependency tree


def lock_tree(root: str | Path) -> dict[str, set[str]]:
    """package -> packages it depends on, from uv.lock or poetry.lock ({} without one)."""
    root = Path(root)
    tree: dict[str, set[str]] = {}
    for name in ("uv.lock", "poetry.lock"):
        p = root / name
        if not p.exists():
            continue
        try:
            data = tomllib.loads(p.read_text())
        except tomllib.TOMLDecodeError:
            continue
        for pkg in data.get("package", []):
            key = normalise(pkg.get("name", ""))
            deps = pkg.get("dependencies", [])
            names = (
                [d["name"] for d in deps if isinstance(d, dict) and "name" in d]
                if isinstance(deps, list)
                else list(deps)
            )
            tree.setdefault(key, set()).update(normalise(n) for n in names)
        if tree:
            return tree
    return tree


def closure(tree: dict[str, set[str]], start: str) -> set[str]:
    seen: set[str] = set()
    todo = list(tree.get(start, ()))
    while todo:
        n = todo.pop()
        if n in seen or n == start:
            continue
        seen.add(n)
        todo.extend(tree.get(n, ()))
    return seen


# --------------------------------------------------------------------------- the environment's view


_ENV_SCRIPT = r"""
import importlib.metadata as md, json
out = {"modules": {}, "licenses": {}}
try:
    for mod, dists in md.packages_distributions().items():
        for d in dists:
            out["modules"].setdefault(d, []).append(mod)
except Exception:
    pass
for dist in md.distributions():
    meta = dist.metadata
    name = meta.get("Name") or ""
    lic = meta.get("License-Expression") or ""
    if not lic:
        classifiers = [
            c.rsplit("::", 1)[-1].strip() for c in (meta.get_all("Classifier") or []) if c.startswith("License ::")
        ]
        lic = "; ".join(c for c in classifiers if c != "OSI Approved") or (meta.get("License") or "")[:80]
    out["licenses"][name] = lic.splitlines()[0] if lic else ""
print(json.dumps(out))
"""


def environment(python: str | None) -> dict:
    """{"modules": {dist: [import names]}, "licenses": {dist: licence}} from the repo's interpreter."""
    if not python:
        return {"modules": {}, "licenses": {}}
    try:
        proc = subprocess.run([python, "-c", _ENV_SCRIPT], capture_output=True, text=True, timeout=60)
        data = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {"modules": {}, "licenses": {}}
    return {
        "modules": {normalise(k): v for k, v in data.get("modules", {}).items()},
        "licenses": {normalise(k): v for k, v in data.get("licenses", {}).items()},
    }


def import_names(dist: str, env: dict) -> list[str]:
    if names := env.get("modules", {}).get(dist):
        return sorted(set(names))
    return IMPORT_NAMES.get(dist) or [dist.replace("-", "_")]


def imported_modules(graph: CodeGraph) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """(dotted import -> non-test files, dotted import -> test files) for absolute imports."""
    code: dict[str, set[str]] = defaultdict(set)
    tests: dict[str, set[str]] = defaultdict(set)
    for path, pf in graph.files.items():
        bucket = tests if is_test_file(path) or "/conftest.py" in f"/{path}" else code
        for target in pf.module.imports.values():
            if not target.startswith("."):
                bucket[target].add(path)
    return code, tests


def _files_using(names: list[str], imports: dict[str, set[str]]) -> set[str]:
    out: set[str] = set()
    for target, files in imports.items():
        if any(target == n or target.startswith(n + ".") for n in names):
            out |= files
    return out


def _pyproject_without_dependencies(p: Path) -> str:
    """pyproject.toml minus its dependency lists (which name everything): scripts, tool config, plugins."""
    try:
        data = tomllib.loads(p.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    project = dict(data.get("project", {}))
    for k in ("dependencies", "optional-dependencies"):
        project.pop(k, None)
    tool = dict(data.get("tool", {}))
    poetry = {k: v for k, v in tool.get("poetry", {}).items() if k not in ("dependencies", "dev-dependencies", "group")}
    tool["poetry"] = poetry
    return json.dumps({"project": project, "tool": tool})


def _named_in_config(root: Path, dist: str) -> bool:
    """A server or tool named in a Procfile, Dockerfile, compose file, Makefile or pyproject scripts."""
    words = {dist, dist.replace("-", "_")}
    patterns = ("Procfile", "Dockerfile*", "*compose*.y*ml", "Makefile", "*.cfg", "*.ini", ".env*", "*.toml", "*.sh")
    for p in sorted({p for pattern in patterns for p in root.glob(pattern)}):
        if p.name in ("uv.lock", "poetry.lock") or not p.is_file():
            continue
        try:
            txt = _pyproject_without_dependencies(p) if p.name == "pyproject.toml" else p.read_text(errors="replace")
        except OSError:
            continue
        if any(re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", txt) for w in words):
            return True
    return False


def _named_in_strings(graph: CodeGraph, dist: str) -> bool:
    """`create_engine("postgresql+psycopg://...")`, `"uvicorn"` in a launcher, a plugin listed by name."""
    words = {dist.encode(), dist.replace("-", "_").encode()}
    return any(any(w in pf.source for w in words) for pf in graph.files.values())


# --------------------------------------------------------------------------- the audit


def audit(
    root: str | Path,
    graph: CodeGraph,
    stack,
    *,
    python: str | None = None,
    online: bool = True,
    now: float | None = None,
) -> dict:
    root = Path(root).resolve()
    decl = declared(root)
    tree = lock_tree(root)
    env = environment(python)
    code_imports, test_imports = imported_modules(graph)
    py = python_version(stack)
    rows: dict[str, DepRow] = {}

    # Who needs what, so "unique" counts only packages that leave with a dependency.
    needed_by: dict[str, set[str]] = defaultdict(set)
    reach = {n: closure(tree, n) for n in decl}
    for n, pkgs in reach.items():
        for p in pkgs:
            needed_by[p].add(n)

    for name, d in sorted(decl.items()):
        names = import_names(name, env)
        files, tfiles = _files_using(names, code_imports), _files_using(names, test_imports)
        pulled = reach.get(name, set())
        unique = {p for p in pulled if needed_by[p] == {name} and p not in decl}
        rows[name] = DepRow(
            name,
            stack.version_of(name),
            d.group,
            d.source,
            names,
            len(files),
            len(tfiles),
            len(pulled),
            len(unique),
            env["licenses"].get(name, ""),
        )

    findings: list[DepFinding] = []
    findings += _overlaps(rows)
    findings += _stdlib(rows, py)
    findings += _unused(rows, root, graph)
    findings += _licenses(rows, tree, env, decl)
    if online:
        findings += _abandoned(rows, root, now or time.time())
    findings += _heavy(rows)
    for f in findings:
        for n in f.deps:
            if n in rows and f.kind not in rows[n].flags:
                rows[n].flags.append(f.kind)
    order = {"warn": 0, "info": 1}
    findings.sort(key=lambda f: (order[f.severity], -f.saves, f.kind, f.deps))
    return {
        "python": py,
        "declared": len(decl),
        "locked": len(tree) or len(stack.dependencies),
        "lockfile": next((n for n in ("uv.lock", "poetry.lock") if (root / n).exists()), None),
        "dependencies": [asdict(r) for r in rows.values()],
        "findings": [f.to_dict() for f in findings],
        "could_remove": sum(f.saves for f in findings if f.kind in ("unused", "stdlib")),
    }


def _overlaps(rows: dict[str, DepRow]) -> list[DepFinding]:
    out = []
    for job, members in OVERLAPS.items():
        used = [rows[m] for m in members if m in rows and rows[m].group != "dev" and rows[m].files]
        if job == "Web framework":  # FastAPI is built on Starlette, and aiohttp is also a client
            used = [r for r in used if r.name not in ("starlette", "aiohttp")]
        if job == "Postgres driver" and {r.name for r in used} <= {"psycopg2", "psycopg2-binary"}:
            continue
        if len(used) < 2:
            continue
        used.sort(key=lambda r: (-r.files, r.name))
        keep, rest = used[0], used[1:]
        moves = sum(r.files for r in rest)
        listing = ", ".join(f"`{r.name}` ({r.files} file{'s' if r.files != 1 else ''})" for r in used)
        out.append(
            DepFinding(
                "overlap",
                f"Two {job.lower()} libraries: {', '.join(r.name for r in used)}",
                [r.name for r in used],
                f"{listing} do the same job.",
                f"Settle on `{keep.name}`, which most of the code already uses: move the {moves} file(s) that use "
                f"{', '.join(f'`{r.name}`' for r in rest)} and drop {'it' if len(rest) == 1 else 'them'}. One library "
                "means one set of timeouts, retries, errors and mocks to get right.",
                saves=sum(1 + r.unique for r in rest),
                effort=moves,
                severity="info",
            )
        )
    return out


def _ver(v: str) -> Version | None:
    try:
        return Version(v)
    except InvalidVersion:
        return None


def _stdlib(rows: dict[str, DepRow], py: str) -> list[DepFinding]:
    out = []
    target = _ver(py)
    for name, (since, replacement, note) in STDLIB.items():
        row = rows.get(name)
        if row is None or target is None or _ver(since) is None or target < _ver(since):
            continue
        out.append(
            DepFinding(
                "stdlib",
                f"`{name}` is in the standard library as `{replacement}`",
                [name],
                f"The project targets Python {py}; `{replacement}` is built in since Python {since}"
                + (f" ({note})." if note else "."),
                f"Replace imports of `{'`, `'.join(row.imports)}` with `{replacement}` in the "
                f"{row.files + row.test_files} file(s) that use it, then remove `{name}` from {row.source}.",
                saves=1 + row.unique,
                effort=max(1, row.files + row.test_files),
                severity="info",
            )
        )
    return out


def _unused(rows: dict[str, DepRow], root: Path, graph: CodeGraph) -> list[DepFinding]:
    out = []
    for row in rows.values():
        if row.files or row.test_files or RUNTIME_ONLY.match(row.name) or row.name in STDLIB:
            continue
        if row.group == "dev":
            continue  # tools and plugins are rarely imported
        if _named_in_config(root, row.name) or _named_in_strings(graph, row.name):
            continue
        out.append(
            DepFinding(
                "unused",
                f"`{row.name}` is declared but never imported",
                [row.name],
                f"No file imports {' or '.join(f'`{n}`' for n in row.imports)}, and `{row.name}` isn't named in any "
                "config, launcher or string in the code.",
                f"Remove `{row.name}` from {row.source}"
                + (f"; {row.unique} package(s) it pulls in leave with it." if row.unique else ".")
                + " If something loads it by name (a plugin entry point), keep it and say so in a comment.",
                saves=1 + row.unique,
                effort=1,
                severity="warn" if row.group == "main" else "info",
            )
        )
    return out


def _licenses(rows: dict[str, DepRow], tree, env: dict, decl: dict[str, Declared]) -> list[DepFinding]:
    out = []
    licenses = env.get("licenses", {})
    for row in rows.values():
        if row.group == "dev":
            continue
        pkgs = {row.name, *closure(tree, row.name)}
        bad = sorted(p for p in pkgs if COPYLEFT.search(licenses.get(p, "")) and "LGPL" not in licenses.get(p, ""))
        if not bad:
            continue
        what = ", ".join(f"`{p}` ({licenses[p]})" for p in bad[:4])
        via = "" if bad == [row.name] else f" through `{row.name}`"
        out.append(
            DepFinding(
                "license",
                f"Copyleft licence in the runtime dependencies{via}",
                [row.name],
                f"{what}. GPL/AGPL terms can apply to software you distribute (AGPL also to software you serve).",
                "Check with whoever owns licensing; look for a permissively licensed alternative if you ship or "
                "serve this code to others.",
                severity="warn",
            )
        )
    return out


def _abandoned(rows: dict[str, DepRow], root: Path, now: float) -> list[DepFinding]:
    from . import watcher
    from .index import resolve as resolve_index

    index = resolve_index(root)
    cache = watcher._load_cache(root, index)
    names = [r.name for r in rows.values() if r.group != "dev"]
    try:
        watcher.prefetch(names, cache, timeout=5.0, index=index)
    except Exception:
        return []
    watcher._save_cache(root, cache, index)
    out = []
    for name in names:
        info = cache.get(name) or {}
        released = info.get("released") or ""
        rows[name].released = released
        if not released:
            continue
        try:
            age = (now - time.mktime(time.strptime(released[:10], "%Y-%m-%d"))) / 86400
        except ValueError:
            continue
        if age < ABANDONED_DAYS:
            continue
        out.append(
            DepFinding(
                "abandoned",
                f"`{name}` has had no release since {released[:10]}",
                [name],
                f"The newest release is {int(age // 365)} years old: security fixes and support for new Python "
                "versions are unlikely.",
                "Look for a maintained fork or a replacement, or vendor the small part you use.",
                severity="warn",
            )
        )
    return out


def _heavy(rows: dict[str, DepRow]) -> list[DepFinding]:
    """A dependency that brings many packages of its own for little use (one or two files)."""
    out = []
    for row in rows.values():
        if row.group == "dev" or row.unique < 15 or row.files > 2 or row.files == 0:
            continue
        out.append(
            DepFinding(
                "heavy",
                f"`{row.name}` brings {row.unique} packages for {row.files} file(s)",
                [row.name],
                f"{row.unique} of the {row.requires} packages it pulls in are needed by nothing else.",
                "If those files use a small part of it, a lighter library or the standard library may do; "
                "otherwise move it to an optional extra so installs that don't need it skip it.",
                saves=row.unique,
                effort=row.files,
            )
        )
    return out
