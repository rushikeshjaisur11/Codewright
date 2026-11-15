"""Turn intent matches into findings: pick the best card, write the smallest diff."""

from __future__ import annotations

import difflib
import hashlib

from tree_sitter import Node

from .cards import best_cards
from .graph import CodeGraph, ParsedFile, text
from .intents.detectors import snippet as code_snippet
from .intents.detectors import walk
from .models import Card, Finding, IntentMatch, Stack
from .stack import for_path


def make_findings(matches: list[IntentMatch], stack: Stack, graph: CodeGraph, cards: list[Card]) -> list[Finding]:
    hot = graph.hot_functions()
    findings: list[Finding] = []
    for m in matches:
        here = for_path(stack, m.function.file)  # a monorepo's projects each have their own versions
        options = best_cards(m, here, cards)
        if not options:
            continue
        card, kind, api = options[0]
        pf = graph.files[m.function.file]
        fn = m.function
        effort = card.effort + (1 if kind == "wellknown" else 0)
        suggestion = card.suggestion
        if m.data.get("advice"):  # what the change means in this codebase, ahead of the general advice
            suggestion = f"{m.data['advice']} {suggestion}"
        if kind == "wellknown":
            suggestion += f" (Adds a dependency: {card.library}.)"
        fid = hashlib.sha1(f"{card.id}:{fn.file}:{fn.qualname}:{m.line}".encode()).hexdigest()[:8]
        finding = Finding(
            id=fid,
            intent=m.intent,
            file=fn.file,
            line=m.line,
            function=fn.qualname,
            title=card.title,
            code_today=code_snippet(pf, fn.start_line, fn.end_line),
            suggestion=suggestion,
            symbol=card.symbol,
            api=api,
            gain=card.gain,
            effort=effort,
            impact=card.impact,
            risk=card.risk,
            when_not=card.when_not,
            threshold=card.threshold,
            sources=list(card.sources),
            evidence=m.evidence,
            snippet=card.snippet,
            snippet_lang=card.snippet_lang,
            hot_path=graph.key(fn) in hot,
            span=(fn.start_line, fn.end_line),
            card_id=card.id,
        )
        builder = PATCHERS.get(card.id)
        if builder is not None:
            finding.patch = builder(pf, m)
        elif card.autofix and m.data.get("edits"):
            finding.patch = _span_patch(pf, m)
        guard = PATCH_GUARDS.get(card.id)
        blocked = guard(here) if guard and finding.patch else None
        if blocked:  # the rewrite is right in general but breaks with this repo's versions
            finding.patch = None
            finding.suggestion = f"{blocked} {finding.suggestion}"
        findings.append(finding)
    return findings


def unified_diff(path: str, before: list[str], after: list[str]) -> str | None:
    if before == after:
        return None
    return "".join(difflib.unified_diff(before, after, fromfile=f"a/{path}", tofile=f"b/{path}"))


def _identifier_uses(root: Node, name: str) -> list[int]:
    return [n.start_point[0] + 1 for n in walk(root, into_defs=True) if n.type == "identifier" and text(n) == name]


def _lru_cache_patch(pf: ParsedFile, m: IntentMatch) -> str | None:
    """Remove the manual memo dict and decorate with functools.lru_cache.

    Only patches the safe shape: a single-parameter function whose cache dict is
    used nowhere else, storing a local name that is returned on the next line.
    """
    d = m.data
    fn = m.function
    if d["params"] != [d["key"]]:
        return None
    lines = pf.lines
    s_start, s_end = d["store_lines"]
    value = d["store_value"]
    if not value.isidentifier() or s_end >= len(lines) or lines[s_end].strip() != f"return {value}":
        return None
    uses = _identifier_uses(pf.root, d["cache"])
    c_start, c_end = d["check_lines"]
    inside = [u for u in uses if fn.start_line <= u <= fn.end_line]
    outside = [u for u in uses if not (fn.start_line <= u <= fn.end_line)]
    if len(inside) != 3 or len(outside) != 1:
        return None  # dict is read, cleared or shared elsewhere: not a safe mechanical change

    decl_line = outside[0]
    drop = set(range(c_start, c_end + 1)) | set(range(s_start, s_end + 1)) | {decl_line}
    def_text = lines[fn.def_line - 1]
    indent = def_text[: len(def_text) - len(def_text.lstrip())]

    after: list[str] = []
    for i, ln in enumerate(lines, start=1):
        if i == fn.def_line:
            after.append(f"{indent}@functools.lru_cache(maxsize=1024)\n")
        if i in drop:
            continue
        after.append(ln)

    if "functools" not in pf.module.imports:
        after = _add_import(pf, after, "import functools\n")
    # Collapse a blank line left behind by the removed declaration.
    cleaned: list[str] = []
    for ln in after:
        if ln.strip() == "" and len(cleaned) >= 2 and cleaned[-1].strip() == "" and cleaned[-2].strip() == "":
            continue
        cleaned.append(ln)
    return unified_diff(pf.path, lines, cleaned)


def _add_import(pf: ParsedFile, lines: list[str], statement: str) -> list[str]:
    # `from X import Y`: extend an existing single-line `from X import ...` when there is one.
    parts = statement.split()
    if len(parts) == 4 and parts[0] == "from" and parts[2] == "import":
        prefix = f"from {parts[1]} import "
        for i, ln in enumerate(lines):
            if ln.startswith(prefix) and "(" not in ln and "#" not in ln and "\\" not in ln:
                names = [n.strip() for n in ln[len(prefix) :].split(",")]
                if parts[3] not in names:
                    lines = list(lines)
                    lines[i] = prefix + ", ".join([*names, parts[3]]) + "\n"
                return lines
    stdlib_at = _stdlib_position(pf, statement)
    if stdlib_at is not None:
        return [*lines[:stdlib_at], statement, *lines[stdlib_at:]]
    last_import = 0
    for child in pf.root.children:
        if child.type in ("import_statement", "import_from_statement", "future_import_statement"):
            last_import = child.end_point[0] + 1
        elif (
            child.type == "expression_statement"
            and last_import == 0
            and child.named_children
            and child.named_children[0].type == "string"
        ):
            last_import = child.end_point[0] + 1  # after the module docstring
    return [*lines[:last_import], statement, *lines[last_import:]]


def _import_key(statement: str) -> tuple[bool, str] | None:
    """isort's order inside a section: `import x` lines before `from x import` lines, then by module."""
    parts = statement.split()
    if len(parts) >= 2 and parts[0] in ("import", "from"):
        return parts[0] == "from", parts[1].lower()
    return None


def _stdlib_position(pf: ParsedFile, statement: str) -> int | None:
    """Line index to insert a standard-library import at, keeping the stdlib block sorted."""
    import sys

    key = _import_key(statement)
    if key is None or key[1].split(".")[0] not in sys.stdlib_module_names:
        return None
    stdlib = []
    for child in pf.root.children:
        if child.type in ("import_statement", "import_from_statement"):
            k = _import_key(text(child))
            if k and k[1].split(".")[0] in sys.stdlib_module_names:
                stdlib.append((k, child))
            elif stdlib:
                break  # the stdlib block ended
        elif stdlib and child.type not in ("comment",):
            break
    if not stdlib:
        return None
    for k, child in stdlib:
        if k > key:
            return child.start_point[0]
    return stdlib[-1][1].end_point[0] + 1


def _span_patch(pf: ParsedFile, m: IntentMatch) -> str | None:
    """Apply a detector's (start_byte, end_byte, replacement) edits and any imports it needs."""
    src = pf.source
    for start, end, new in sorted(m.data["edits"], reverse=True):
        src = src[:start] + new.encode() + src[end:]
    after = src.decode("utf-8", errors="replace").splitlines(keepends=True)
    for stmt in m.data.get("imports", []):
        after = _add_import(pf, after, stmt + "\n")
    return unified_diff(pf.path, pf.lines, after)


def _fastapi_annotated_guard(stack: Stack) -> str | None:
    """FastAPI 0.100-0.104 crash on Annotated parameters with Pydantic 2.12+ (fixed in 0.105.0).

    Found by running the rewrite: `'FieldInfo' object has no attribute 'in_'` when an Annotated
    dependency is shared by two routes, with FastAPI 0.100.1-0.104.1 and Pydantic 2.12.5 / 2.14.3.
    """
    from packaging.version import InvalidVersion, Version

    fa, pyd = stack.version_of("fastapi"), stack.version_of("pydantic")
    try:
        if fa is None or not (Version("0.100.0") <= Version(fa) < Version("0.105.0")):
            return None
        if pyd is not None and Version(pyd) < Version("2.12.0"):
            return None
    except InvalidVersion:
        return None
    return (
        f"Upgrade FastAPI to 0.105.0 or later first: FastAPI {fa} with Pydantic {pyd or '2.12+'} fails on "
        "Annotated parameters, so Codewright doesn't offer the rewrite yet."
    )


PATCH_GUARDS = {
    "fastapi.annotated-params": _fastapi_annotated_guard,
}


PATCHERS = {
    "stdlib.functools.lru_cache": _lru_cache_patch,
}
