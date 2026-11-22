"""Remove what the Untangle inventory found, as one patch: definitions, modules and commented-out code.

Removing a definition is more than deleting its lines. The patch also:

* takes the comment lines attached above it, and keeps the blank lines around it as they were;
* drops imports that only the removed code used, in its own file;
* drops `from x import removed_name` in every other file (following re-exports through `__init__.py`), so no
  import breaks;
* takes the dead code that only the removed code used along with it (the chain the inventory reported).

Every changed file must still parse. A removal that would leave an empty block, or whose name is still used
somewhere once the rest is gone, is skipped with the reason, and the rest goes ahead.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from .graph import CodeGraph
from .refactor import diff, read_source, unified

# Code only tests use isn't here: removing it breaks those tests, so it goes to the AI agent with them.
REMOVABLE = ("unused", "unused-module", "commented-code")
_COMMENT = re.compile(r"^\s*#")


def _split(text: str) -> list[str]:
    """Lines ending in "\n" only: str.splitlines also breaks at form feeds and Unicode line separators, which
    corrupts a diff of a file that has them."""
    return re.findall(r"[^\n]*\n|[^\n]+$", text)


def _names_used(tree: ast.AST) -> set[str]:
    """Every name the code reads, plus identifier-like words in its strings (annotations, __all__, getattr)."""
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and len(n.value) < 500:
            out.update(re.findall(r"[A-Za-z_]\w*", n.value))
    return out


def _import_nodes(tree: ast.Module) -> list[ast.Import | ast.ImportFrom]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Import | ast.ImportFrom)]


def _bound(alias: ast.alias) -> str:
    return alias.asname or alias.name.split(".")[0]


def _statement_text(node: ast.Import | ast.ImportFrom, keep: list[ast.alias], indent: str, multiline: bool) -> str:
    parts = [a.name + (f" as {a.asname}" if a.asname else "") for a in keep]
    if isinstance(node, ast.Import):
        return f"{indent}import {', '.join(parts)}\n"
    head = f"{indent}from {'.' * node.level}{node.module or ''} import "
    one = head + ", ".join(parts) + "\n"
    if not multiline and len(one) <= 100:
        return one
    inner = indent + "    "
    return head + "(\n" + "".join(f"{inner}{p},\n" for p in parts) + f"{indent})\n"


def _drop_imports(lines: list[str], drop: dict[int, set[str]]) -> list[str] | None:
    """`lines` with the named bindings removed from the import statements starting at each line number.
    None when a statement can't be rewritten safely (it shares its line with other code)."""
    try:
        tree = ast.parse("".join(lines))
    except SyntaxError:
        return None
    edits: list[tuple[int, int, str]] = []
    for node in _import_nodes(tree):
        names = drop.get(node.lineno)
        if not names:
            continue
        keep = [a for a in node.names if _bound(a) not in names]
        if len(keep) == len(node.names):
            continue
        first, last = lines[node.lineno - 1], lines[node.end_lineno - 1]
        rest = last[node.end_col_offset :].strip()
        if first[: node.col_offset].strip() or (rest and not rest.startswith("#")):
            return None  # `x = 1; import a`: leave it to a person
        indent = first[: node.col_offset]
        text = _statement_text(node, keep, indent, node.end_lineno > node.lineno) if keep else ""
        edits.append((node.lineno, node.end_lineno, text))
    out = list(lines)
    for a, b, text in sorted(edits, reverse=True):
        out[a - 1 : b] = _split(text) if text else []
    return out


def _binding_lines(tree: ast.Module) -> dict[str, list[int]]:
    """Bound name -> line numbers of the import statements that bind it."""
    out: dict[str, list[int]] = {}
    for node in _import_nodes(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        for a in node.names:
            if a.name != "*":
                out.setdefault(_bound(a), []).append(node.lineno)
    return out


def _cut(lines: list[str], spans: list[tuple[int, int]]) -> list[str]:
    """Remove 1-based inclusive line spans, with their attached comments, keeping blank lines as they were."""
    keep = [True] * len(lines)
    for a, b in spans:
        a0, b0 = a - 1, b - 1
        while a0 > 0 and _COMMENT.match(lines[a0 - 1]) and not lines[a0 - 1].lstrip().startswith("#!"):
            a0 -= 1  # comments that belong to it
        before = 0
        i = a0 - 1
        while i >= 0 and not lines[i].strip():
            before += 1
            i -= 1
        j = b0 + 1
        if before or i < 0:
            while j < len(lines) and not lines[j].strip():
                j += 1  # the blank lines above stay; the ones below go with it
        for k in range(a0, min(j, len(lines))):
            keep[k] = False
    out = [ln for ln, k in zip(lines, keep, strict=True) if k]
    while out and not out[-1].strip():
        out.pop()
    if out and not out[-1].endswith("\n"):
        out[-1] += "\n"
    return out


def removal(root: str | Path, graph: CodeGraph, items: list[dict], ids: list[str]) -> dict:
    """One patch removing the chosen inventory items (and the dead code only they use).

    Returns {patch, removed: [ids], skipped: [{id, why}], files}."""
    root = Path(root)
    by_id = {i["id"]: i for i in items}
    by_key = {f"{i['file']}::{i['name']}": i for i in items if i["kind"] in ("unused", "test-only")}
    chosen: dict[str, dict] = {}
    skipped: list[dict] = []
    for iid in ids:
        it = by_id.get(iid)
        if it is None:
            skipped.append({"id": iid, "why": "not in the inventory any more: scan again"})
        elif it["kind"] not in REMOVABLE:
            skipped.append(
                {"id": iid, "why": "only unused code, unused modules and commented-out code are removed by patch"}
            )
        else:
            chosen[iid] = it
    # Dead code whose only users all go goes too (`_row`, used only by `_old_export`).
    gone = {f"{i['file']}::{i['name']}" for i in chosen.values()}
    grew = True
    while grew:
        grew = False
        for key, it in by_key.items():
            users = it.get("data", {}).get("chain_keys") or []
            if it["kind"] == "unused" and key not in gone and users and set(users) <= gone:
                chosen[it["id"]] = it
                gone.add(key)
                grew = True

    deleted_files = {
        i["file"] for i in chosen.values() if i["kind"] == "unused-module" and not i["file"].endswith("__init__.py")
    }
    spans: dict[str, list[tuple[int, int, dict]]] = {}
    for it in chosen.values():
        if it["file"] in deleted_files:
            continue
        if it["kind"] == "unused-module":
            skipped.append({"id": it["id"], "why": "a package's __init__.py: remove the package by hand"})
            continue
        spans.setdefault(it["file"], []).append((it["line"], it["end_line"], it))

    # Top-level definitions that go, for imports of them in other files.
    removed_keys: set[str] = set()
    for f, ss in spans.items():
        for _a, _b, it in ss:
            if it["kind"] in ("unused", "test-only") and "." not in it["name"]:
                removed_keys.add(f"{f}::{it['name']}")
    for f in deleted_files:
        pf = graph.files.get(f)
        if pf is not None:
            removed_keys |= {f"{f}::{fn.qualname}" for fn in pf.module.functions if "." not in fn.qualname}
            removed_keys |= {f"{f}::{q}" for q in pf.classes if "." not in q}

    changes: dict[str, tuple[list[str] | None, list[str]]] = {}
    texts: dict[str, list[str]] = {}

    def lines_of(rel: str) -> list[str]:
        if rel not in texts:
            texts[rel] = _split(read_source(root / rel))
        return texts[rel]

    removed_ids: list[str] = []
    for f, ss in sorted(spans.items()):
        before = lines_of(f)
        # Outer spans only: a method inside a class that goes is already gone.
        ss.sort(key=lambda s: (s[0], -s[1]))
        outer: list[tuple[int, int, dict]] = []
        for s in ss:
            if outer and s[0] >= outer[-1][0] and s[1] <= outer[-1][1]:
                removed_ids.append(s[2]["id"])
                continue
            outer.append(s)
        good: list[tuple[int, int, dict]] = []
        for s in outer:
            trial = _cut(before, [(a, b) for a, b, _ in [*good, s]])
            try:
                ast.parse("".join(trial))
            except SyntaxError:
                skipped.append({"id": s[2]["id"], "why": "removing it would leave an empty block: do it by hand"})
                continue
            good.append(s)
        if not good:
            continue
        after = _cut(before, [(a, b) for a, b, _ in good])
        after = _tidy_imports(before, after) or after
        changes[f] = (before, after)
        removed_ids += [s[2]["id"] for s in good]

    # Imports of removed names elsewhere (also through re-exports), and of deleted modules.
    resolver = graph.resolver
    if resolver is not None and removed_keys:
        for path, pf in graph.files.items():
            if path in deleted_files:
                continue
            drop: dict[str, set[str]] = {}
            for alias in list(pf.module.imports):
                if "#" in alias or alias.startswith("*"):
                    continue
                abs_target = resolver._target(pf, alias)
                if not abs_target:
                    continue
                funcs, classes = resolver.find(abs_target)
                keys = set(funcs) | set(classes)
                if keys and keys <= removed_keys:
                    drop.setdefault(alias, set())
            if not drop:
                continue
            before = changes[path][1] if path in changes else lines_of(path)
            try:
                tree = ast.parse("".join(before))
            except SyntaxError:
                continue
            used = _names_used(_without_imports(tree))
            still = [a for a in drop if a in used]
            if still:
                for it in chosen.values():
                    if it["name"].rsplit(".", 1)[-1] in still and it["id"] in removed_ids:
                        removed_ids.remove(it["id"])
                        skipped.append({"id": it["id"], "why": f"`{path}` still uses it"})
                drop = {a: s for a, s in drop.items() if a not in still}
            lines_by_name = _binding_lines(tree)
            plan: dict[int, set[str]] = {}
            for alias in drop:
                for ln in lines_by_name.get(alias, []):
                    plan.setdefault(ln, set()).add(alias)
            new = _drop_imports(before, plan)
            if new is None:
                continue
            orig = changes[path][0] if path in changes else lines_of(path)
            changes[path] = (orig, new)

    # A module goes only when nothing that stays imports it (a lazy import in a dead function that stays...).
    if resolver is not None:
        for f in sorted(deleted_files):
            users = [p for p, pf in graph.files.items() if p not in deleted_files and f in resolver.imported_files(pf)]
            users = [p for p in users if not (p in changes and _imports_gone(changes[p][1], f))]
            if users:
                deleted_files.discard(f)
                for it in chosen.values():
                    if it["file"] == f:
                        skipped.append({"id": it["id"], "why": f"`{users[0]}` still imports it"})
    for f in sorted(deleted_files):
        removed_ids += [i["id"] for i in chosen.values() if i["file"] == f]

    # Every changed file still parses.
    for f, (_before, after) in list(changes.items()):
        try:
            ast.parse("".join(after))
        except SyntaxError as exc:
            del changes[f]
            for it in chosen.values():
                if it["file"] == f and it["id"] in removed_ids:
                    removed_ids.remove(it["id"])
                    skipped.append({"id": it["id"], "why": f"the file would not parse afterwards ({exc.msg})"})

    patch = diff(changes) or ""
    for f in sorted(deleted_files):
        patch += unified(lines_of(f), [], f"a/{f}", "/dev/null")
    return {
        "patch": patch,
        "removed": sorted(set(removed_ids)),
        "skipped": skipped,
        "files": sorted({*changes, *deleted_files}),
        "deleted": sorted(deleted_files),
        "lines": sum(len(b or []) - len(a) for b, a in changes.values()) + sum(len(lines_of(f)) for f in deleted_files),
    }


def _imports_gone(lines: list[str], module_file: str) -> bool:
    """True when no import statement left in `lines` names the module in `module_file`."""
    stem = module_file.rsplit("/", 1)[-1].removesuffix(".py")
    try:
        tree = ast.parse("".join(lines))
    except SyntaxError:
        return False
    for node in _import_nodes(tree):
        mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
        names = [a.name for a in node.names] if isinstance(node, ast.ImportFrom) else []
        if any(m.rsplit(".", 1)[-1] == stem for m in mods) or stem in names:
            return False
    return True


def _without_imports(tree: ast.Module) -> ast.Module:
    body = [n for n in tree.body if not isinstance(n, ast.Import | ast.ImportFrom)]
    return ast.Module(body=body, type_ignores=[])


def _tidy_imports(before: list[str], after: list[str]) -> list[str] | None:
    """Drop the imports that only the removed code used: bound names used before the cut and not after."""
    try:
        old, new = ast.parse("".join(before)), ast.parse("".join(after))
    except SyntaxError:
        return None
    used_before = _names_used(_without_imports(old))
    used_after = _names_used(_without_imports(new))
    gone = {n for n in used_before - used_after}
    if not gone:
        return None
    plan: dict[int, set[str]] = {}
    for name, lines in _binding_lines(new).items():
        if name in gone:
            for ln in lines:
                plan.setdefault(ln, set()).add(name)
    return _drop_imports(after, plan) if plan else None
