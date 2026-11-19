"""`codewright cards verify`: keep the knowledge base honest as libraries release.

Checks every card (bundled, plus `--cards` folders):

* shape: required text, sources, a "when not to", a detector for its intent (or intent
  "reference"), a valid version range, and a snippet that compiles;
* symbols: with `--python`, the card's dotted symbol must exist in that interpreter when the
  library is installed there at a version the card covers (cards outside it are skipped);
* links: with `links=True`, every source URL must still resolve.
"""

from __future__ import annotations

import ast
import json
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from packaging.specifiers import InvalidSpecifier, SpecifierSet

from . import cards as card_store
from . import critic
from .models import Card, Finding

TEXT_FIELDS = ("title", "suggestion", "gain", "risk", "when_not", "threshold")


@dataclass
class Report:
    checked: int = 0
    problems: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # informational, not failures

    @property
    def ok(self) -> bool:
        return not self.problems


def _shape(c: Card, intents: set[str]) -> list[str]:
    out = [f"{c.id}: missing {name}" for name in TEXT_FIELDS if not str(getattr(c, name) or "").strip()]
    if not c.sources:
        out.append(f"{c.id}: no source cited")
    if c.intent != card_store.REFERENCE and c.intent not in intents:
        out.append(f"{c.id}: no detector produces intent {c.intent!r}")
    if c.versions not in ("*", ""):
        try:
            SpecifierSet(c.versions)
        except InvalidSpecifier:
            out.append(f"{c.id}: invalid version range {c.versions!r}")
    if not (1 <= c.effort <= 6) or not (1 <= c.impact <= 5):
        out.append(f"{c.id}: effort or impact out of range")
    if c.snippet and c.snippet_lang == "python":
        try:
            compile(c.snippet, c.id, "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        except SyntaxError as exc:
            out.append(f"{c.id}: snippet doesn't compile: {exc.msg} (line {exc.lineno})")
    return out


def _installed(python: str, dists: list[str]) -> dict[str, str | None]:
    script = (
        "import importlib.metadata as m, json, sys\n"
        "out = {}\n"
        "for d in json.loads(sys.argv[1]):\n"
        "    try: out[d] = m.version(d)\n"
        "    except m.PackageNotFoundError: out[d] = None\n"
        "print(json.dumps(out))\n"
    )
    proc = subprocess.run([python, "-c", script, json.dumps(dists)], capture_output=True, text=True, timeout=60)
    return json.loads(proc.stdout or "{}")


def _symbols(cards: list[Card], python: str, report: Report) -> None:
    libs = sorted({c.library for c in cards if c.library})
    versions = _installed(python, libs)
    todo: list[Card] = []
    for c in cards:
        if not critic.is_dotted_symbol(c.symbol) or c.kind == "platform":
            continue
        if c.library:
            v = versions.get(c.library)
            if v is None:
                report.skipped.append(f"{c.id}: {c.library} isn't installed in {python}")
                continue
            if not card_store.version_matches(v, c.versions):
                report.skipped.append(f"{c.id}: {c.library} {v} is outside the card's range {c.versions}")
                continue
        todo.append(c)
    findings = [Finding(c.id, "", "", 1, "", "", "", "", c.symbol, "", "", 1, 1, "", "", "", [], "") for c in todo]
    critic.check_symbols(findings, python, {f.id: f.symbol for f in findings}, {c.symbol: c.library for c in todo})
    for c, f in zip(todo, findings, strict=True):
        if f.symbol_check == "missing":
            where = f"{c.library} {versions.get(c.library)}" if c.library else "this Python"
            report.problems.append(f"{c.id}: `{c.symbol}` doesn't exist in {where}")
        elif f.symbol_check == "unchecked":
            report.skipped.append(f"{c.id}: `{c.symbol}` couldn't be imported to check (an extra may be missing)")


def _fetchable(url: str) -> str:
    # github.com blob pages refuse scripted requests; the raw file says the same about existence.
    if url.startswith("https://github.com/") and "/blob/" in url:
        return url.replace("https://github.com/", "https://raw.githubusercontent.com/").replace("/blob/", "/", 1)
    return url


def _check_url(url: str, retry_after: float = 3.0) -> tuple[str, str] | None:
    """None when the page answers. Otherwise ("dead", why) for a page that is gone (404, 410, other 4xx), or
    ("down", why) when the site failed or did not answer twice: a server error is the site's outage, not
    evidence the source is wrong, so it is reported without failing the check."""
    req = urllib.request.Request(_fetchable(url), headers={"User-Agent": "codewright-cards-verify"})
    why = ""
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.status < 400:
                    return None
                why = f"HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            if exc.code < 500 and exc.code != 429:
                return "dead", f"HTTP {exc.code}"
            why = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            why = f"unreachable ({getattr(exc, 'reason', exc)})"
        if attempt == 0:
            time.sleep(retry_after)
    return "down", why


def _links(cards: list[Card], report: Report) -> None:
    urls = sorted({u for c in cards for u in c.sources})
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(zip(urls, pool.map(_check_url, urls), strict=True))
    for c in cards:
        for u in c.sources:
            if results[u] is None:
                continue
            kind, why = results[u]
            if kind == "dead":
                report.problems.append(f"{c.id}: source {u} -> {why}")
            else:  # the site is down or slow right now: say so, but don't fail on someone else's outage
                report.skipped.append(f"{c.id}: source {u} -> {why} (site error; checked again next run)")


def verify(
    *,
    extra_dirs: list[Path] | None = None,
    library: str = "",
    python: str | None = None,
    links: bool = False,
) -> Report:
    from .intents.detectors import all_intents

    cards = [c for c in card_store.load(extra_dirs) if not library or (c.library or "python") == library]
    report = Report(checked=len(cards))
    seen: set[str] = set()
    for c in cards:
        if c.id in seen:
            report.problems.append(f"{c.id}: duplicate card id")
        seen.add(c.id)
        report.problems += _shape(c, all_intents())
    if python:
        _symbols(cards, python, report)
    if links:
        _links(cards, report)
    return report
