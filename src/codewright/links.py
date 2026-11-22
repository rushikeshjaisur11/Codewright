"""Hidden links: places that must change together, or agree, without the code saying so.

The import graph shows the links code declares. These are the ones it doesn't, and each is a change that breaks
quietly when someone touches one side and not the other:

* **Change together** (git history): files that keep changing in the same commits although neither imports the
  other. Typically a contract written twice (a backend route and the frontend code that calls it), parallel
  implementations that copy each other, or a feature split across layers.
* **Stale copy of a global**: `from config import client` when `config.setup()` later reassigns `client` with
  `global client`. The importing module keeps the old object forever.
* **Settings read in several places**: one environment variable read by several modules, worst when their
  defaults differ (one place uses 30 seconds, another 60).
* **Copied constants**: the same upper-case constant defined in several modules, worst when the values differ.
* **Works only after another call**: a module global that starts as None, is set by an `init()`-style function,
  and is used elsewhere without a check, so the order of calls is a rule nothing enforces.
* **State set from another module**: `config.DEBUG = True` or `registry.HANDLERS.append(...)` from outside, so
  what a module does depends on who touched it first.
* **Copied logic**: function bodies that are the same in several places (from the architecture checks).
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from .archmap import is_tooling, module_name
from .graph import CodeGraph, is_test_file
from .links_scan import facts
from .untangle import Item

FRONTEND = (".ts", ".tsx", ".js", ".jsx", ".vue", ".svelte")
TEMPLATES = (".html", ".jinja", ".jinja2", ".j2")
# Upper-case names that each module defines for itself by design (plugins, blocks, commands).
PER_MODULE = {
    "NAME",
    "ID",
    "TYPE",
    "KIND",
    "DESCRIPTION",
    "ICON",
    "LABEL",
    "TITLE",
    "VERSION",
    "SLUG",
    "CATEGORY",
    "TAGS",
    "HELP",
    "ORDER",
    "PRIORITY",
    "PATTERN",
    "REGEX",
    "URL",
    "PREFIX",
    "SCOPES",
    "MODEL",
    "COLOR",
    "LOGGER",
}


# Variables a framework sets for itself at startup: reading them in several places is how they work.
BOOTSTRAP_VARS = {"DJANGO_SETTINGS_MODULE", "PYTHONPATH", "PATH", "HOME", "PWD", "CI", "TERM", "USER"}
_CONTAINER = r"(\{|\[|set\(|dict\(|list\(|defaultdict\(|OrderedDict\(|deque\(|Counter\(|WeakValueDictionary\()"


def _norm(default: str | None) -> str | None:
    """`"30"` and `'30'` are the same default."""
    if default is None or default.strip() in ('""', "''", "None"):
        return None  # unset and empty read the same for a setting
    d = default.strip()
    if len(d) >= 2 and d[0] == d[-1] and d[0] in "'\"":
        return repr(d[1:-1])
    return d


def _skip(path: str) -> bool:
    return is_test_file(path) or is_tooling(path)


class Links:
    def __init__(self, graph: CodeGraph):
        self.g = graph
        self.facts = {p: facts(pf) for p, pf in graph.files.items() if not _skip(p)}
        self.imports = {
            p: (graph.resolver.imported_files(pf) if graph.resolver is not None else set())
            for p, pf in graph.files.items()
        }

    # ------------------------------------------------------------------ git
    def change_together(self) -> tuple[list[Item], str]:
        from . import cochange

        root = Path(self.g.root)
        commits, note = cochange.history(root)
        if not commits:
            return [], note or "no git history"
        pairs = cochange.coupled(commits, lambda p: (root / p).is_file())
        out = []
        for p in pairs:
            why = self._explained(p.a, p.b)
            if why:
                continue
            a, b = (p.a, p.b) if p.a.endswith(".py") else (p.b, p.a)
            kind, advice = self._describe(a, b)
            pct = round(p.degree * 100)
            out.append(
                Item(
                    kind="co-change",
                    file=a,
                    line=1,
                    end_line=1,
                    name=f"{a}<->{b}",
                    title=f"`{a}` and `{b}` change together",
                    why=(
                        f"{p.shared} commits changed both ({pct}% of their changes), yet neither imports or names "
                        f"the other: {kind}. {advice}"
                    ),
                    confidence="high" if p.degree >= 0.5 else "medium",
                    data={
                        "other": b,
                        "shared": p.shared,
                        "degree": p.degree,
                        "commits": [p.revs_a, p.revs_b],
                        "subjects": p.subjects,
                        "last": p.last,
                    },
                )
            )
        return out, note

    def _explained(self, a: str, b: str) -> bool:
        """A link the code shows: one imports the other (directly or through one module), or names its file."""
        for x, y in ((a, b), (b, a)):
            if x.endswith(".py"):
                direct = self.imports.get(x, set())
                if y in direct or any(y in self.imports.get(m, ()) for m in direct):
                    return True
                pf = self.g.files.get(x)
                if pf is None:
                    continue
                if y.endswith(".py"):
                    parts = module_name(y).split(".")
                    # named by its dotted path in a string (Django's include("app.urls"), importlib, settings)
                    if any(".".join(parts[i:]).encode() in pf.source for i in range(len(parts) - 1)):
                        return True
                    continue
                stem = y.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                if len(stem) > 2 and stem.encode() in pf.source:
                    return True  # a template or file it opens by name
        return False

    @staticmethod
    def _describe(a: str, b: str) -> tuple[str, str]:
        if b.endswith(FRONTEND):
            return (
                "an API contract written twice",
                "Generate the client and its types from the backend's schema (OpenAPI), so a change on one side "
                "fails the build on the other",
            )
        if b.endswith(TEMPLATES):
            return "a template kept in step by hand", "Pass the template what it needs explicitly, in one place"
        if b.endswith(".sql"):
            return "a query kept in step with the code by hand", "Keep the query next to the code that runs it"
        if not b.endswith(".py"):
            return "two languages kept in step by hand", "Generate one side from the other, or share a schema"
        same_dir = a.rsplit("/", 1)[0] == b.rsplit("/", 1)[0]
        return (
            "likely parallel copies" if same_dir else "one change split across two places",
            "Read a few of the shared commits: if the same edit lands in both, pull the shared part into one module "
            "both use; if one feature spans both, move it together",
        )

    # ------------------------------------------------------------------ code
    def _module_file(self, pf_path: str, bound: str) -> tuple[str, str] | None:
        """(file, name) a module-level `from m import name` in `pf_path` refers to, when m is in the repo."""
        r = self.g.resolver
        pf = self.g.files[pf_path]
        target = r._target(pf, bound) if r is not None else None
        if not target or "." not in target:
            return None
        mod, _, name = target.rpartition(".")
        files = r.modules.get(mod, [])
        return (files[0], name) if len(files) == 1 else None

    def stale_globals(self) -> list[Item]:
        rebound: dict[tuple[str, str], list[str]] = defaultdict(list)
        for path, f in self.facts.items():
            for name, fn, _line in f["rebinds"]:
                rebound[(path, name)].append(fn)
        if not rebound:
            return []
        out = []
        for path, f in self.facts.items():
            for bound, line in f["top_imports"]:
                hit = self._module_file(path, bound)
                if hit is None or (hit[0], hit[1]) not in rebound or hit[0] == path:
                    continue
                src, name = hit
                fns = sorted(set(rebound[(src, name)]))
                out.append(
                    Item(
                        kind="stale-global",
                        file=path,
                        line=line,
                        end_line=line,
                        name=f"{path}:{bound}",
                        title=f"`from {module_name(src)} import {name}` keeps the value it had at import",
                        why=(
                            f"`{', '.join(fns[:3])}` in `{src}` reassigns `{name}` (`global {name}`), but this module "
                            f"copied it when it was imported and never sees the new one. Import the module and read "
                            f"`{module_name(src).rsplit('.', 1)[-1]}.{name}`, or expose a function that returns it"
                        ),
                        confidence="high",
                        data={"source": src, "global": name, "setters": fns},
                    )
                )
        return out

    def settings(self) -> list[Item]:
        reads: dict[str, list[tuple[str, str | None, int]]] = defaultdict(list)
        for path, f in self.facts.items():
            for key, default, line, _owner in f["envs"]:
                if key not in BOOTSTRAP_VARS:
                    reads[key].append((path, _norm(default), line))
        out = []
        for key, rs in sorted(reads.items()):
            files = sorted({p for p, _, _ in rs})
            if len(files) < 2:
                continue
            defaults = {d for _, d, _ in rs}
            differ = len(defaults) > 1
            first = min(rs, key=lambda r: (r[0], r[2]))
            where = ", ".join(f"`{p}`" for p in files[:4]) + (f" and {len(files) - 4} more" if len(files) > 4 else "")
            if differ:
                shown = ", ".join(sorted(d if d is not None else "no default" for d in defaults))
                why = (
                    f"Read in {where} with different defaults ({shown}): unset, the app disagrees with itself. "
                    "Read it once, in one settings object, and pass the value on"
                )
            else:
                why = (
                    f"Read in {where}. Read it once, in one settings object, so its default and parsing are one "
                    "decision"
                )
            out.append(
                Item(
                    kind="split-setting",
                    file=first[0],
                    line=first[2],
                    end_line=first[2],
                    name=key,
                    title=f"`{key}` is read in {len(files)} modules" + (" with different defaults" if differ else ""),
                    why=why,
                    confidence="high" if differ else "medium",
                    data={"reads": [{"file": p, "line": ln, "default": d} for p, d, ln in rs]},
                )
            )
        return out

    def constants(self) -> list[Item]:
        defs: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        lines: dict[tuple[str, str], int] = {}
        for path, f in self.facts.items():
            for name, value, line in f["consts"]:
                defs[name][path].add(value)
                lines.setdefault((name, path), line)
        out = []
        for name, by_file in sorted(defs.items()):
            files = sorted(by_file)
            if not 2 <= len(files) <= 4 or name in PER_MODULE or len(name) < 6:
                continue  # in many modules: a per-module convention, not a copy
            if any(len(v) > 1 for v in by_file.values()):
                continue  # True in a try, False in its except: a flag each module works out for itself
            values = {next(iter(v)) for v in by_file.values()}
            differ = len(values) > 1
            value = next(iter(values))
            # The same string under the same name is a key both sides must agree on: a sure copy.
            sure = not differ and value.startswith("'") and len(value) > 4
            first = files[0]
            where = ", ".join(f"`{p}`" for p in files)
            out.append(
                Item(
                    kind="copied-constant",
                    file=first,
                    line=lines[(name, first)],
                    end_line=lines[(name, first)],
                    name=name,
                    title=f"`{name}` is defined in {len(files)} modules" + (" with different values" if differ else ""),
                    why=(
                        f"In {where}"
                        + (f" as {', '.join(sorted(values))}" if differ else f", each as {value}")
                        + (
                            ". If they mean the same thing, define it once and import it, and decide which value "
                            "is right"
                            if differ
                            else ". Define it once and import it, so changing it is one edit"
                        )
                    ),
                    confidence="high" if sure else "medium",
                    data={"defs": [{"file": p, "line": lines[(name, p)], "values": sorted(by_file[p])} for p in files]},
                )
            )
        return out

    def call_order(self) -> list[Item]:
        out = []
        for path, f in self.facts.items():
            setters: dict[str, set[str]] = defaultdict(set)
            for name, fn, _line in f["rebinds"]:
                setters[name].add(fn)
            if not setters:
                continue
            module_calls = set(self._module_level_names(path))
            seen: set[tuple[str, str]] = set()
            for name, fn, line in f["unchecked"]:
                if name not in setters or fn in setters[name] or (name, fn) in seen:
                    continue
                if setters[name] & module_calls:
                    continue  # set when the module is imported
                seen.add((name, fn))
                init = sorted(setters[name])[0]
                out.append(
                    Item(
                        kind="call-order",
                        file=path,
                        line=line,
                        end_line=line,
                        name=f"{fn}:{name}",
                        title=f"`{fn}` works only after `{init}` has run",
                        why=(
                            f"It uses `{name}`, which is None until `{init}` sets it, and doesn't check. The order is "
                            f"a rule nothing enforces: build `{name}` on first use, or pass it in"
                        ),
                        confidence="medium",
                        data={"global": name, "setters": sorted(setters[name])},
                    )
                )
        return out

    def _module_level_names(self, path: str) -> list[str]:
        from .untangle_scan import facts as untangle_facts

        return untangle_facts(self.g.files[path])["refs"].get("", [])

    def outside_state(self) -> list[Item]:
        r = self.g.resolver
        out = []
        for path, f in self.facts.items():
            for obj, attr, how, line, owner in f["outside"]:
                pf = self.g.files[path]
                target = r._target(pf, obj) if r is not None else None
                if not target:
                    continue
                files = r.modules.get(target, [])  # `import config` / `from app import config`: a module
                if len(files) == 1 and attr and files[0] != path:
                    src, name = files[0], attr
                else:
                    hit = self._module_file(path, obj)  # `from config import HANDLERS`: a name in a module
                    if hit is None or attr or how == "set":
                        continue
                    src, name = hit
                if src == path or not self._is_variable(src, name):
                    continue
                if how != "set" and not self._is_container(src, name):
                    continue
                verb = {"set": "sets", "item": "writes into"}.get(how, f"calls `.{how}()` on")
                out.append(
                    Item(
                        kind="outside-state",
                        file=path,
                        line=line,
                        end_line=line,
                        name=f"{path}:{line}",
                        title=f"`{owner or '<module>'}` {verb} `{name}` of `{src}`",
                        why=(
                            f"`{src}` behaves differently depending on whether this has run. Give `{src}` a function "
                            "that makes the change (or take the value as an argument) so the dependency is visible"
                        ),
                        confidence="high" if how == "set" else "medium",
                        data={"source": src, "name": name, "how": how},
                    )
                )
        return out

    def _is_variable(self, path: str, name: str) -> bool:
        pf = self.g.files.get(path)
        if pf is None or name in pf.classes or any(fn.qualname == name for fn in pf.module.functions):
            return False
        return name.encode() + b" =" in pf.source or name.encode() + b": " in pf.source

    def _is_container(self, path: str, name: str) -> bool:
        """A module-level dict, list, set... (not an object whose methods happen to be called `update`)."""
        import re

        pf = self.g.files.get(path)
        if pf is None:
            return False
        pat = re.compile(rf"^{re.escape(name)}\s*(?::[^=\n]+)?=\s*{_CONTAINER}", re.M)
        return bool(pat.search(pf.source.decode("utf-8", "replace")))

    def copied_logic(self) -> list[Item]:
        """Copies grouped by the files they span: a copied class is one finding, not one per method."""
        from .intents.architecture import detect_duplicate_logic

        groups: dict[tuple[str, ...], list] = defaultdict(list)
        for m in detect_duplicate_logic(self.g):
            if _skip(m.function.file):
                continue
            files = tuple(sorted({c.split("::")[0] for c in m.data.get("copies", [])})) or (m.function.file,)
            groups[files].append(m)
        out = []
        for files, ms in groups.items():
            ms.sort(key=lambda m: (m.function.file, m.function.def_line))
            first = ms[0].function
            names = [m.function.qualname for m in ms]
            lines = sum(m.function.end_line - m.function.start_line + 1 for m in ms)
            if len(ms) == 1:
                title = f"`{first.qualname}` is copied"
                why = f"{ms[0].evidence}. A fix in one copy misses the others. {ms[0].data.get('advice', '')}"
            else:
                where = " and ".join(f"`{f}`" for f in files) if len(files) > 1 else f"`{files[0]}`"
                shown = ", ".join(f"`{n}`" for n in names[:4]) + (
                    f" and {len(names) - 4} more" if len(names) > 4 else ""
                )
                title = f"{len(ms)} functions are copied" + (f" between {where}" if len(files) > 1 else f" in {where}")
                why = (
                    f"{shown} have the same bodies in each place. A fix in one copy misses the others: keep one "
                    "copy in a module the others import"
                )
            out.append(
                Item(
                    kind="copied-logic",
                    file=first.file,
                    line=first.def_line,
                    end_line=first.end_line,
                    name=f"{first.file}::{first.qualname}",
                    title=title,
                    why=why.strip(),
                    confidence="high",
                    lines=lines,
                    data={"copies": sorted({c for m in ms for c in m.data.get("copies", [])}), "functions": names},
                )
            )
        return out


def hidden_links(graph: CodeGraph, *, git: bool = True) -> tuple[list[Item], str]:
    """(items, note about git history)."""
    links = Links(graph)
    co, note = links.change_together() if git else ([], "")
    items = [
        *co,
        *links.stale_globals(),
        *links.settings(),
        *links.constants(),
        *links.call_order(),
        *links.outside_state(),
        *links.copied_logic(),
    ]
    return items, note
