"""Search the knowledge base (capability and reference cards) and say whether each fits a repo.

Used by the web Knowledge page, the `search_knowledge` MCP tool for coding agents, and
anything else that needs "what does my installed version offer for X?".
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import asdict

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from .cards import REFERENCE, version_matches
from .models import Card, Stack
from .stack import python_version
from .words import tokens


def fit(card: Card, stack: Stack | None) -> dict:
    """How the card relates to the repo: fits, upgrade (needs a newer version), older, unused or unknown."""
    if stack is None:
        return {"status": "unknown", "detail": ""}
    if card.kind == "practice":
        return {"status": "fits", "detail": "applies to any codebase"}
    if card.kind == "stdlib":
        py = python_version(stack)
        return _versioned(py, card.versions, f"Python {py}")
    if card.kind == "platform":
        found = [r for r in card.requires_any if stack.has(r)]
        if card.requires_any and not found:
            return {"status": "unused", "detail": f"needs one of {', '.join(card.requires_any)}"}
        return {"status": "fits", "detail": f"via {found[0]}" if found else ""}
    lib = card.library or ""
    if not stack.has(lib):
        return {"status": "unused", "detail": f"{lib} is not a dependency"}
    present = [p for p in card.unless_installed if stack.has(p)]
    if present:  # e.g. "install google-adk[db]" when SQLAlchemy is already locked
        return {"status": "unused", "detail": f"{', '.join(present)} is already installed"}
    installed = stack.version_of(lib)
    if installed is None:
        return {"status": "unknown", "detail": f"{lib} is declared but not locked"}
    return _versioned(installed, card.versions, f"{lib} {installed}")


def _versioned(installed: str, spec: str, label: str) -> dict:
    if version_matches(installed, spec):
        return {"status": "fits", "detail": label}
    try:
        lows = [s.version for s in SpecifierSet(spec) if s.operator in (">=", ">", "~=", "==")]
        newer = any(Version(installed) < Version(v) for v in lows)
    except (InvalidSpecifier, InvalidVersion):
        return {"status": "unknown", "detail": label}
    if newer:
        return {"status": "upgrade", "detail": f"{label}; needs {spec}"}
    return {"status": "older", "detail": f"{label}; applies to {spec}"}


def card_dict(card: Card, stack: Stack | None = None) -> dict:
    d = asdict(card)
    d["reference"] = card.intent == REFERENCE
    d["fit"] = fit(card, stack)
    return d


def _text(card: Card) -> list[str]:
    return (
        tokens(card.title) * 3
        + tokens(card.symbol.replace(".", " ")) * 2
        + tokens(card.topic) * 2
        + tokens(card.library or "python")
        + tokens(card.intent)
        + tokens(card.suggestion)
        + tokens(card.gain)
        + tokens(card.when_not)
    )


def search(
    query: str,
    cards: list[Card],
    stack: Stack | None = None,
    *,
    library: str = "",
    only_fitting: bool = False,
    limit: int | None = 10,
) -> list[dict]:
    """Cards ranked by BM25 against `query` (all cards when empty), each with its fit for `stack`."""
    pool = [c for c in cards if not library or (c.library or "python") == library]
    if only_fitting and stack is not None:
        pool = [c for c in pool if fit(c, stack)["status"] == "fits"]
    q = tokens(query)
    score_of: dict[int, float] = {}
    if not q:
        ranked = pool
    else:
        docs = [Counter(_text(c)) for c in pool]
        n = len(docs) or 1
        avg = sum(sum(d.values()) for d in docs) / n
        df = Counter(t for d in docs for t in d)
        scores = []
        for c, d in zip(pool, docs, strict=True):
            length = sum(d.values())
            s = 0.0
            for t in q:
                if t in d:
                    idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                    s += idf * d[t] * 2.2 / (d[t] + 1.2 * (0.25 + 0.75 * length / (avg or 1)))
            if s > 0:
                scores.append((s, c))
        scores.sort(key=lambda x: -x[0])
        ranked = [c for _, c in scores]
        score_of = {id(c): s for s, c in scores}
    if stack is not None:  # what the repo can use now first, then what an upgrade unlocks
        order = {"fits": 0, "upgrade": 1, "unknown": 2, "older": 3, "unused": 4}
        ranked = sorted(ranked, key=lambda c: order[fit(c, stack)["status"]])
    out = []
    for c in ranked if limit is None else ranked[:limit]:
        d = card_dict(c, stack)
        d["score"] = round(score_of.get(id(c), 0.0), 2)
        out.append(d)
    return out
