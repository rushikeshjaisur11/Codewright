"""Untangle: what the codebase carries that nothing needs, found with evidence and reported only when sure.

Python is too dynamic for a static tool to prove code dead, so the inventory leans the other way: a name counts
as used wherever it appears, in code, in a string (getattr, registries, Django settings), in a template or a
config file. A method `run` stays alive while anything calls any `.run`. Each finding says how sure it is and
why, and the high-confidence ones are the ones a team can delete without a second look.

* **Unused code**: functions, methods and classes nothing live names. Code that only dead code uses is dead
  too, and is reported with the chain ("only `_old_export` uses it, and nothing uses that").
  Never reported when something outside the code may call it: decorated handlers, dunders and protocol names
  (`_repr_html_`, `load_ipython_extension`, `pytest_*`), overrides of methods of classes from outside the repo,
  classes a framework loads (subclasses of models, admin, commands, widgets; classes named in settings strings
  or handed to a registry such as `register_converter(MyConverter)`), methods a name built at run time can
  reach (`getattr(self, f"do_{x}")`), modules loaded by their path, and, in a library, its public API.
* **Only tests use it**: the same, where the only users are tests.
* **Pass-through wrappers**: a function of the repo whose body only forwards its arguments to another function
  of the repo. Wrapping a third-party library is a deliberate seam, so it is not reported.
* **Unused parameters**: parameters a function never reads, where every caller calls it by name (a function
  passed as a callback, or named in a string, has its signature chosen by whoever calls it) and there is no
  other implementation with the same name (platform variants, interfaces).
* **Silenced errors**: `except Exception: pass` and friends, which turn a bug into silent success.
* **Commented-out code**: blocks of comments that parse as Python statements.

Unused modules come from the Map's in-use check.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .archmap import is_tooling, module_name
from .entries import WRAPPER_DECORATORS
from .graph import CodeGraph, is_test_file
from .untangle_scan import facts

# Bases that make a plain value type: subclassing them hands no method to a framework.
PLAIN_BASES = re.compile(
    r"^(object|Exception|BaseException|\w*Error|\w*Exception|\w*Warning|ABC|abc\.ABC|Generic(\[.*\])?|"
    r"(typing\.)?(NamedTuple|TypedDict|Protocol)(\[.*\])?|(enum\.)?(Enum|IntEnum|StrEnum|Flag|IntFlag)|"
    r"str|int|float|dict|list|tuple|set|frozenset|bytes)$"
)
# Names that something outside the code calls by convention.
CONVENTION_NAMES = re.compile(
    r"^(main|setup|teardown|setup_module|teardown_module|setup_function|teardown_function|load_tests|"
    r"application|app|handler|lambda_handler|upgrade|downgrade|run_migrations_online|run_migrations_offline|"
    r"includeme|load_ipython_extension|unload_ipython_extension|_repr_\w+_|_ipython_\w+_|_jupyter_\w+|"
    r"pytest_\w+|setUp\w*|tearDown\w*|test\w*|__\w+__)$"
)
# Folders whose modules a framework imports by file name.
LOADED_BY_PATH = re.compile(r"(^|/)(management/commands|templatetags|migrations|alembic|versions|hooks)/")
# Non-Python files whose words count as uses: templates, settings, entry points, CI and deploy files.
TEXT_SUFFIXES = (
    ".html",
    ".htm",
    ".jinja",
    ".jinja2",
    ".j2",
    ".txt",
    ".toml",
    ".cfg",
    ".ini",
    ".yaml",
    ".yml",
    ".json",
    ".xml",
    ".js",
    ".ts",
    ".sh",
    ".mako",
    ".tpl",
    ".svg",
    ".rst",
    ".md",
)
TEXT_NAMES = ("Procfile", "Dockerfile", "Makefile", "justfile")
MAX_TEXT_BYTES = 400_000
_IDENT = re.compile(r"^[A-Za-z_]\w*$")

# Well-known bases from outside the repo, and the methods of a subclass they call: anything else on the
# subclass is the repo's own code and as dead as any when nothing uses it. Keyed by the base's import path
# (its last part is enough when the import can't be followed).
_DUNDER = r"__\w+__"
KNOWN_BASES: dict[str, str] = {
    "pydantic.BaseModel": r"model_\w+|" + _DUNDER,
    "pydantic.RootModel": r"model_\w+|" + _DUNDER,
    "pydantic_settings.BaseSettings": r"model_\w+|settings_customise_sources|" + _DUNDER,
    "pydantic.BaseSettings": r"model_\w+|settings_customise_sources|" + _DUNDER,
    "sqlmodel.SQLModel": r"model_\w+|" + _DUNDER,
    "enum.Enum": r"_missing_|_generate_next_value_|" + _DUNDER,
    "enum.StrEnum": r"_missing_|_generate_next_value_|" + _DUNDER,
    "enum.IntEnum": r"_missing_|_generate_next_value_|" + _DUNDER,
    "logging.Handler": r"emit|handle|handleError|format|flush|close|createLock|acquire|release|setFormatter|filter|"
    + _DUNDER,
    "logging.StreamHandler": r"emit|handle|handleError|format|flush|close|setStream|filter|" + _DUNDER,
    "logging.Formatter": r"format|formatTime|formatException|formatStack|formatMessage|usesTime|" + _DUNDER,
    "logging.Filter": r"filter|" + _DUNDER,
    "logging.LoggerAdapter": r"process|log|" + _DUNDER,
    "json.JSONEncoder": r"default|encode|iterencode|" + _DUNDER,
    "threading.Thread": r"run|" + _DUNDER,
    "ast.NodeVisitor": r"visit\w*|generic_visit|" + _DUNDER,
    "ast.NodeTransformer": r"visit\w*|generic_visit|" + _DUNDER,
    "argparse.Action": r"format_usage|" + _DUNDER,
    "html.parser.HTMLParser": r"handle_\w+|feed|close|reset|unknown_decl|" + _DUNDER,
    "contextlib.ContextDecorator": r"_recreate_cm|" + _DUNDER,
    "typing.NamedTuple": _DUNDER,
    "collections.UserDict": r"get|keys|items|values|copy|" + _DUNDER,
    "dict": r"get|keys|items|values|copy|update|setdefault|pop|popitem|clear|fromkeys|" + _DUNDER,
}
_KNOWN_BY_NAME: dict[str, str] = {}
for _path, _hooks in KNOWN_BASES.items():
    _KNOWN_BY_NAME.setdefault(_path.rsplit(".", 1)[-1], _hooks)


def _known_hooks(target: str) -> re.Pattern | None:
    """The hook-name pattern of a well-known outside base (`pydantic.main.BaseModel` counts as
    `pydantic.BaseModel`), or None when the base is unknown and may call anything."""
    head, last = target.split(".", 1)[0], target.rsplit(".", 1)[-1]
    for path, hooks in KNOWN_BASES.items():
        if path == target or (path.split(".", 1)[0] == head and path.rsplit(".", 1)[-1] == last):
            return re.compile(hooks)
    if "." not in target and last in _KNOWN_BY_NAME:
        return re.compile(_KNOWN_BY_NAME[last])
    return None


KINDS = {
    "unused": "Unused code",
    "test-only": "Only tests use it",
    "unused-module": "Unused modules",
    "wrapper": "Pass-through wrappers",
    "unused-param": "Unused parameters",
    "swallowed": "Silenced errors",
    "commented-code": "Commented-out code",
}


@dataclass
class Item:
    kind: str  # one of KINDS
    file: str
    line: int
    end_line: int
    name: str
    title: str
    why: str
    confidence: str  # high | medium
    lines: int = 0  # lines it would remove
    chain: list[str] = field(default_factory=list)  # dead code that is the only user of this
    data: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.file}:{self.name or self.line}"

    def to_dict(self) -> dict:
        return {"id": self.id, **asdict(self)}


@dataclass
class Def:
    file: str
    q: str
    name: str
    kind: str  # f | c
    start: int
    end: int
    raw: dict
    owner: str  # qualname of the enclosing definition ("" = module level)

    @property
    def key(self) -> str:
        return f"{self.file}::{self.q}"


def _text_words(root: Path) -> set[str]:
    """Identifier-like words in the repo's non-Python files (templates, settings, entry points, docs)."""
    from .scanscope import walk_files

    words: set[str] = set()
    pat = re.compile(rb"[A-Za-z_][A-Za-z0-9_]{2,}")
    for p in walk_files(root, lambda name: name.endswith(TEXT_SUFFIXES) or name in TEXT_NAMES, (".github",)):
        try:
            if p.stat().st_size > MAX_TEXT_BYTES:
                continue
            data = p.read_bytes()
        except OSError:
            continue
        words.update(w.decode() for w in pat.findall(data))
    return words


def is_service(graph: CodeGraph) -> bool:
    """True when the repo runs as an app or service; False for a library, whose public names are its product."""
    root = Path(graph.root)
    packaged = (root / "setup.py").exists() or (root / "setup.cfg").exists()
    try:
        meta = (root / "pyproject.toml").read_text(errors="replace")
        packaged = packaged or "[project]" in meta or "[tool.poetry]" in meta
    except OSError:
        pass
    if not packaged:
        return True
    return any(
        e.framework not in ("script", "CLI", "entry file")
        for f, e in graph.app_entries.items()
        if not (is_tooling(f) or is_test_file(f))
    )


class Inventory:
    def __init__(self, graph: CodeGraph, unused_modules: list[dict] | None = None):
        self.g = graph
        self.service = is_service(graph)
        if unused_modules is None:
            from .archmap import ArchMap

            unused_modules = ArchMap(graph).unused
        self._script_used = self._used_by_scripts()
        self.unused_modules = [u for u in unused_modules if self._module_is_dead(u)]
        self._dead_files = {u["file"] for u in self.unused_modules}
        self.facts = {p: facts(pf) for p, pf in graph.files.items()}
        self.defs: dict[str, Def] = {}
        for path, f in self.facts.items():
            for raw in f["defs"]:
                d = Def(path, raw["q"], raw["n"], raw["k"], raw["s"], raw["e"], raw, raw["parent"])
                self.defs[d.key] = d
        self.by_name: dict[str, list[Def]] = defaultdict(list)
        for d in self.defs.values():
            self.by_name[d.name].append(d)
        self.strings: set[str] = _text_words(Path(graph.root))
        self.values: set[str] = set()
        self.dotted: set[tuple[str, str]] = set()  # (module or class part, name) of "a.b.name" strings
        self.prefixes: set[str] = set()
        self.exported: set[str] = set()
        self.dynamic: set[str] = set()
        self.loaded_names: set[str] = set()  # dotted names in strings: modules a framework imports by name
        dynamic_users: list[tuple[str, str]] = []
        for path, f in self.facts.items():
            self.strings.update(f["strings"])
            self.values.update(f["values"])
            self.prefixes.update(p for p in f["prefixes"] if len(p) > 2)
            self.exported.update(f["all"])
            for s in f["dotted"]:
                parts = s.split(".")
                self.dotted.add((parts[-2], parts[-1]))
            if not self.service and path.endswith("__init__.py") and not is_test_file(path):
                self.exported.update(n for b, n in f["imports"] if not b.startswith("_"))
            for owner in f["dynamic"]:
                # getattr(self, name) in a method reaches its class's methods; getattr(obj, name) anywhere reaches
                # the methods of the classes that code names.
                d = self.defs.get(f"{path}::{owner}")
                cls = d if d is not None and d.kind == "c" else self._parent(d) if d is not None else None
                if cls is not None and cls.kind == "c":
                    self.dynamic.add(cls.key)
                dynamic_users.append((path, owner))
            for s in f["dotted"]:
                parts = s.split(".")
                self.loaded_names.update(".".join(parts[:i]) for i in range(2, len(parts) + 1))
        for path, owner in dynamic_users:
            for name in self.facts[path]["refs"].get(owner, ()):
                self.dynamic.update(d.key for d in self.by_name.get(name, ()) if d.kind == "c")
        self.framework = self._framework_classes()

    # ------------------------------------------------------------------ helpers
    def _used_by_scripts(self) -> set[str]:
        """Modules that scripts, tools and examples import, directly or through other modules: people run
        those by hand, so what they need is not dead."""
        if self.g.resolver is None:
            return set()
        seen: set[str] = set()
        todo = [p for p in self.g.files if is_tooling(p) and not is_test_file(p)]
        while todo:
            p = todo.pop()
            for t in self.g.resolver.imported_files(self.g.files[p]):
                if t not in seen and t in self.g.files:
                    seen.add(t)
                    todo.append(t)
        return seen

    def _module_is_dead(self, u: dict) -> bool:
        p = u["file"]
        if u["reason"] == "script, docs or example folder" or LOADED_BY_PATH.search(p) or p in self._script_used:
            return False
        if not _IDENT.match(p.rsplit("/", 1)[-1].removesuffix(".py")):
            return False  # `unicode10-0-0.py` can't be imported by name: something loads it by path
        return not (not self.service and self._public_module(p))

    @staticmethod
    def _public_module(path: str) -> bool:
        return not any(p.startswith("_") and not p.startswith("__") for p in module_name(path).split("."))

    def _public(self, d: Def) -> bool:
        """Part of a library's API: a public name in a public module, or a public method of a public class."""
        if not self._public_module(d.file):
            return False
        if d.name.startswith("_") and not (d.name.startswith("__") and d.name.endswith("__")):
            return False
        if d.owner == "":
            return True
        parent = self.defs.get(f"{d.file}::{d.owner}")
        return parent is not None and parent.kind == "c" and self._public(parent)

    def _loaded_by_name(self, path: str) -> bool:
        from .archmap import importable_names

        mod = module_name(path)
        names = [mod, *importable_names(mod)]
        parts = mod.split(".")
        names += [".".join(parts[i:]) for i in range(1, len(parts) - 1)]  # a project in a subfolder
        return any(n in self.loaded_names for n in names if "." in n)

    def _parent(self, d: Def) -> Def | None:
        return self.defs.get(f"{d.file}::{d.owner}") if d.owner else None

    def _framework_classes(self) -> dict[str, re.Pattern | None]:
        """Classes whose methods code outside the repo may call, each with the method names it may call (None:
        any of them). A base from outside the repo (resolved through imports, so `urllib.request.Request` is not
        the repo's own `Request`) hands its subclass's methods to that library, unless it is one of the
        well-known bases in KNOWN_BASES, whose hook names are listed. A metaclass, a repo base that registers
        its subclasses, or the class itself handed over as a value or named in a string: any method."""
        resolver = self.g.resolver
        registering = self._registering_bases()
        direct: dict[str, list[re.Pattern] | None] = {}
        internal_bases: dict[str, list[str]] = {}
        for key, d in self.defs.items():
            if d.kind != "c":
                continue
            pf = self.g.files[d.file]
            hooks: list[re.Pattern] | None = []
            found: list[str] = []
            for b in d.raw.get("bases", []):
                bare = b.split("[", 1)[0]
                hits: list[str] = []
                target = None
                if resolver is not None:
                    target = resolver._target(pf, bare, scope=d.q.rpartition(".")[0])
                    hits = resolver.find(target)[1] if target else []
                if hits:
                    found += hits
                    if any(h.rsplit("::", 1)[-1].rsplit(".", 1)[-1] in registering for h in hits):
                        hooks = None
                elif not PLAIN_BASES.match(b) and hooks is not None:
                    known = _known_hooks(target or bare)
                    hooks = None if known is None else [*hooks, known]
            if d.raw.get("meta"):
                hooks = None
            if d.name in self.values or (d.name in self.strings and not self._class_named_in_code(d)):
                hooks = None  # handed to a registry or loaded from settings: whoever gets it calls its methods
            direct[key] = hooks
            internal_bases[key] = found
        memo: dict[str, list[re.Pattern] | None] = {}

        def collect(key: str, seen: frozenset = frozenset()) -> list[re.Pattern] | None:
            if key in memo:
                return memo[key]
            out = direct.get(key, [])
            for b in internal_bases.get(key, ()):
                if out is None:
                    break
                if b in seen or b == key:
                    continue
                more = collect(b, seen | {key})
                out = None if more is None else [*out, *more]
            memo[key] = out
            return out

        result: dict[str, re.Pattern | None] = {}
        for k in direct:
            hooks = collect(k)
            if hooks is None:
                result[k] = None
            elif hooks:
                result[k] = re.compile("|".join(f"(?:{h.pattern})" for h in hooks))
        return result

    def _hook(self, parent: Def, name: str) -> bool:
        """True when code outside the repo may call `parent.name`."""
        if parent.key not in self.framework:
            return False
        hooks = self.framework[parent.key]
        return hooks is None or bool(hooks.fullmatch(name))

    def _class_named_in_code(self, d: Def) -> bool:
        return any(d.name in f["refs"].get(q, ()) for f in self.facts.values() for q in f["refs"])

    def _registering_bases(self) -> set[str]:
        """Repo classes whose subclasses register themselves (`__init_subclass__`) or are found through
        `__subclasses__()`: a subclass nothing names is still in use."""
        out = {d.q.rsplit(".", 2)[-2] for d in self.defs.values() if d.name == "__init_subclass__" and "." in d.q}
        for path, f in self.facts.items():
            if any("__subclasses__" in names for names in f["refs"].values()):
                out.update(d.name for d in self.defs.values() if d.file == path and d.kind == "c")
        return out

    def _alive_reason(self, d: Def) -> str:
        raw = d.raw
        if is_test_file(d.file):
            return "test code"
        if CONVENTION_NAMES.match(d.name):
            return "called by convention"
        if any(not WRAPPER_DECORATORS.match(x) for x in raw["deco"]):
            return "registered by a decorator"
        if any("abstractmethod" in x or x in ("overload", "typing.overload") for x in raw["deco"]):
            return "abstract"
        if LOADED_BY_PATH.search(d.file):
            return "loaded by its path"
        if d.name in self.exported and (d.owner == "" or not self.service):
            return "exported"
        if not self.service and self._public(d):
            return "public API of the library"
        parent = self._parent(d)
        if (
            parent.name if parent is not None and parent.kind == "c" else module_name(d.file).rsplit(".", 1)[-1],
            d.name,
        ) in self.dotted:
            return "named by its import path in a string"
        if parent is not None and parent.kind == "c":
            if self._hook(parent, d.name):
                return "may be called by the framework its class belongs to"
            if parent.key in self.dynamic:
                return "its class looks methods up by computed name"
            if any(d.name.startswith(p) for p in self.prefixes):
                return "a name built at run time can reach it"
            if raw.get("trivial"):
                return "an interface stub"
        if d.kind == "c" and self.framework.get(d.key, 0) is None and d.name in self.strings | self.values:
            return "loaded by a framework"
        return ""

    # ------------------------------------------------------------------ dead code
    def dead(self) -> tuple[list[Item], list[Item]]:
        """(unused, only tests use it)."""
        roots = [k for k, d in self.defs.items() if not is_test_file(d.file) and self._alive_reason(d)]
        # Imports bind a name; using the bound name uses the imported one (`import x as y` then `y()`).
        alias_of = {p: {b: n for b, n in f["imports"] if b != n} for p, f in self.facts.items()}

        def reach(include_tests: bool) -> set[str]:
            alive: set[str] = set()
            todo = [
                f"{p}::" for p in self.facts if (include_tests or not is_test_file(p)) and p not in self._dead_files
            ]
            todo += roots
            if include_tests:
                todo += [k for k, d in self.defs.items() if is_test_file(d.file)]
            used: set[str] = set()
            while todo:
                key = todo.pop()
                if key in alive:
                    continue
                alive.add(key)
                path, _, q = key.partition("::")
                d = self.defs.get(key)
                if d is not None and d.owner:
                    todo.append(f"{path}::{d.owner}")  # a live method keeps its class, a closure its function
                aliases = alias_of[path]
                for n in self.facts[path]["refs"].get(q, ()):
                    for name in (n, aliases.get(n)):
                        if name is not None and name not in used:
                            used.add(name)
                            todo.extend(x.key for x in self.by_name.get(name, ()) if x.key not in alive)
            return alive

        live = reach(include_tests=False)
        with_tests = reach(include_tests=True)
        unused: list[Item] = []
        tests_only: list[Item] = []
        for key, d in self.defs.items():
            if key in live or is_test_file(d.file) or is_tooling(d.file) or d.file in self._dead_files:
                continue
            parent = self._parent(d)
            if parent is not None and (parent.key not in live or parent.kind == "f"):
                continue  # reported with its class, or a closure of a function
            item = self._dead_item(d, live, key in with_tests)
            (tests_only if key in with_tests else unused).append(item)
        order = lambda i: (i.confidence != "high", i.file, i.line)  # noqa: E731
        return sorted(unused, key=order), sorted(tests_only, key=order)

    def _dead_item(self, d: Def, live: set[str], tests: bool) -> Item:
        users = sorted(
            {
                o.key
                for path, f in self.facts.items()
                if not is_test_file(path)
                for q, names in f["refs"].items()
                if q and d.name in names
                for o in [self.defs.get(f"{path}::{q}")]
                if o is not None and o.key != d.key and not o.key.startswith(d.key + ".")
            }
        )
        dead_users = [u for u in users if u not in live]
        what = "class" if d.kind == "c" else ("method" if self._parent(d) is not None else "function")
        confidence = "high"
        if d.name in self.strings:
            confidence = "medium"
            why = "No code uses it, but its name appears in a string, a doc or a non-Python file"
        elif dead_users:
            names = ", ".join(f"`{u.split('::')[1]}`" for u in dead_users[:3])
            one = len(dead_users) == 1
            why = f"Only {names} use{'s' if one else ''} it, and nothing live uses {'that' if one else 'those'}"
        else:
            why = "Nothing in the repo uses it" + (" outside tests" if tests else "")
        if len(self.by_name[d.name]) > 3:
            confidence = "medium"  # a common name: one use of any of them would keep all alive, so this is rarer
        if d.owner == "" and self._loaded_by_name(d.file):
            confidence = "medium"
            why += ". Its module is imported by name at run time, and whatever loads it may call it"
        if d.kind == "c" and d.key in self.framework and self.framework[d.key] is None:
            confidence = "medium"
            why += ". Its base comes from a framework, which may find it by itself (Django app configs, plugins)"
        title = f"`{d.q}` is only used by tests" if tests else f"Unused {what} `{d.q}`"
        return Item(
            kind="test-only" if tests else "unused",
            file=d.file,
            line=d.start,
            end_line=d.end,
            name=d.q,
            title=title,
            why=why,
            confidence=confidence,
            lines=d.end - d.start + 1,
            chain=[u.split("::")[1] for u in dead_users],
            data={"what": what, "chain_keys": dead_users},
        )

    # ------------------------------------------------------------------ other surplus
    def wrappers(self) -> list[Item]:
        counts: dict[str, int] = defaultdict(int)
        for pf in self.g.files.values():
            for fn in pf.module.functions:
                for c in fn.calls:
                    counts[c.rsplit(".", 1)[-1]] += 1
        out = []
        for d in self.defs.values():
            fwd = d.raw.get("forwards")
            if not fwd or d.owner or d.raw.get("defaults") or is_test_file(d.file) or is_tooling(d.file):
                continue
            target = fwd.rsplit(".", 1)[-1]
            if target == d.name or not self._repo_function(d, fwd):
                continue  # a re-export, or a seam around a library (or a dict, a ContextVar...)
            if d.name in self.values or d.name in self.exported or self._alive_reason(d):
                continue  # passed as a callback, exported or registered: the name itself is the point
            if not self.service and self._public(d):
                continue
            n = counts.get(d.name, 0)
            facade = target.lstrip("_") == d.name.lstrip("_") and target.startswith("_")
            out.append(
                Item(
                    kind="wrapper",
                    file=d.file,
                    line=d.start,
                    end_line=d.end,
                    name=d.q,
                    title=f"`{d.q}` only passes its arguments on to `{fwd}`",
                    why=f"Its {n} caller{'s' if n != 1 else ''} could call `{fwd}` directly",
                    confidence="high" if n <= 5 and not facade else "medium",
                    lines=d.end - d.start + 1,
                    data={"target": fwd, "callers": n},
                )
            )
        return out

    def _repo_function(self, d: Def, dotted: str) -> bool:
        """True when `dotted`, as written in `d`'s file, names a function defined in the repo."""
        resolver = self.g.resolver
        if resolver is None:
            return False
        pf = self.g.files[d.file]
        target = resolver._target(pf, dotted, scope=d.owner)
        if target is None:
            return False
        funcs, _ = resolver.find(target)
        return bool(funcs)

    def unused_params(self) -> list[Item]:
        out = []
        for d in self.defs.values():
            ps = d.raw.get("unused_params") or []
            if not ps or d.kind != "f" or is_test_file(d.file) or is_tooling(d.file):
                continue
            if d.raw["deco"] or d.name in self.values or d.name in self.strings or CONVENTION_NAMES.match(d.name):
                continue  # a callback, handler or something loaded by name: its caller decides the signature
            if len(self.by_name[d.name]) > 1:
                continue  # one of several implementations (platform variants, a shared interface)
            parent = self._parent(d)
            if parent is not None and (parent.kind == "f" or self._hook(parent, d.name)):
                continue
            if not self.service and self._public(d):
                continue  # removing a parameter breaks the library's users
            out.append(
                Item(
                    kind="unused-param",
                    file=d.file,
                    line=d.raw["d"],
                    end_line=d.raw["d"],
                    name=d.q,
                    title=f"`{d.q}` never reads {', '.join(f'`{p}`' for p in ps)}",
                    why="Every caller still has to work out a value for it, and nothing passes the function on",
                    confidence="high",
                    data={"params": ps},
                )
            )
        return out

    def swallowed(self) -> list[Item]:
        out = []
        for path, f in self.facts.items():
            if is_test_file(path) or is_tooling(path):
                continue
            for s in f["swallowed"]:
                out.append(
                    Item(
                        kind="swallowed",
                        file=path,
                        line=s["line"],
                        end_line=s["line"] + 1,
                        name=s["owner"] or "<module>",
                        title=f"`except {s['caught']}: {s['does']}` in `{s['owner'] or path}`",
                        why="A bug here looks like success. Catch the error you expect, or log it before moving on",
                        confidence="high" if s["caught"] in ("everything", "BaseException") else "medium",
                        data=s,
                    )
                )
        return out

    def commented_code(self) -> list[Item]:
        out = []
        for path, f in self.facts.items():
            if is_tooling(path):
                continue
            for a, b in f["commented"]:
                out.append(
                    Item(
                        kind="commented-code",
                        file=path,
                        line=a,
                        end_line=b,
                        name=f"L{a}",
                        title=f"{b - a + 1} lines of commented-out code",
                        why="Git keeps the history. Commented-out code goes stale and reads like it matters",
                        confidence="high" if b - a >= 2 else "medium",
                        lines=b - a + 1,
                    )
                )
        return out

    def modules(self) -> list[Item]:
        out = []
        for u in self.unused_modules:
            pf = self.g.files.get(u["file"])
            n = len(pf.lines) if pf is not None else 0
            tests = u["reason"] == "only tests import it"
            out.append(
                Item(
                    kind="test-only" if tests else "unused-module",
                    file=u["file"],
                    line=1,
                    end_line=n,
                    name="<module>",
                    title=f"`{u['file']}` is only imported by tests" if tests else f"Nothing imports `{u['file']}`",
                    why=f"{u['reason'].capitalize()}, and nothing names it in a string or launch file",
                    confidence="medium",
                    lines=n,
                    data={"functions": u.get("functions", 0)},
                )
            )
        return out


def inventory(graph: CodeGraph, unused_modules: list[dict] | None = None) -> dict:
    """Everything the Untangle page lists, grouped by kind, with totals."""
    inv = Inventory(graph, unused_modules)
    unused, tests_only = inv.dead()
    mods = inv.modules()
    items = [
        *unused,
        *[m for m in mods if m.kind == "unused-module"],
        *tests_only,
        *[m for m in mods if m.kind == "test-only"],
        *inv.wrappers(),
        *inv.unused_params(),
        *inv.swallowed(),
        *inv.commented_code(),
    ]
    removable = sum(i.lines for i in items if i.kind in ("unused", "unused-module") and i.confidence == "high")
    return {
        "mode": "service" if inv.service else "library",
        "items": [i.to_dict() for i in items],
        "counts": {k: sum(1 for i in items if i.kind == k) for k in KINDS},
        "high": sum(1 for i in items if i.confidence == "high"),
        "removable_lines": removable,
        "kinds": KINDS,
    }


AGENT_STEPS = {
    "wrapper": "Call the target directly at every call site, then delete the wrapper.",
    "unused-param": "Remove the parameter, and the argument at every call site (positional and keyword).",
    "swallowed": "Catch only the errors this block is expected to raise, and let the rest propagate, or log the "
    "exception before carrying on. Keep the current result for the expected errors.",
    "unused": "Delete it, and the imports only it used.",
    "unused-module": "Delete the module, after checking nothing loads it by name (settings, entry points).",
    "test-only": "The product doesn't use it. Delete it together with the tests that only exercise it.",
    "commented-code": "Delete the commented-out code; git keeps the history.",
}


def agent_task(items: list[dict]) -> tuple[str, str]:
    """(task, context) for the AI agent: the chosen items, what to do with each, and how to stay safe."""
    kinds = sorted({i["kind"] for i in items}, key=list(KINDS).index)
    task = (
        f"Untangle {len(items)} spot(s) in this repository without changing its behaviour: "
        + ", ".join(KINDS[k].lower() for k in kinds)
        + ". Each spot is listed with its file, line and the evidence. Make each change at every place it "
        "reaches, keep the code style, and run the tests that cover the files you touch. If a change turns out "
        "unsafe (something outside the code uses it, or a test needs it), leave that spot and say why."
    )
    lines = []
    for k in kinds:
        lines.append(f"## {KINDS[k]}\n{AGENT_STEPS[k]}\n")
        for i in (x for x in items if x["kind"] == k):
            lines.append(f"- {i['file']}:{i['line']} {i['title']}. {i['why']}.")
        lines.append("")
    return task, "\n".join(lines)


def to_markdown(inv: dict, everything: bool = False) -> str:
    """The inventory as a Markdown report."""
    out = [f"# Untangle: {inv['high']} sure finding(s), {inv['removable_lines']} line(s) to remove", ""]
    for kind, label in KINDS.items():
        rows = [i for i in inv["items"] if i["kind"] == kind and (everything or i["confidence"] == "high")]
        if not rows:
            continue
        out += [f"## {label} ({len(rows)})", ""]
        for i in rows:
            mark = "" if i["confidence"] == "high" else " _(check)_"
            out.append(f"- `{i['file']}:{i['line']}` {i['title']}{mark}. {i['why']}.")
        out.append("")
    return "\n".join(out)
