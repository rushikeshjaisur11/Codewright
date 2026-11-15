"""Word splitting shared by code search (chat) and knowledge search."""

from __future__ import annotations

import re

_WORD = re.compile(r"[A-Za-z][a-z0-9]*|[A-Z]+(?![a-z])|\d+")
STOP = {
    "the",
    "a",
    "an",
    "is",
    "are",
    "how",
    "does",
    "do",
    "what",
    "where",
    "which",
    "this",
    "that",
    "in",
    "of",
    "to",
    "and",
    "or",
    "for",
    "it",
    "on",
    "with",
    "repo",
    "code",
    "self",
    "return",
    "def",
    "import",
    "from",
    "none",
    "true",
    "false",
    "if",
    "else",
    "as",
    "by",
    "be",
    "i",
    "my",
    "we",
    "can",
    "when",
}


def tokens(s: str) -> list[str]:
    out = []
    for part in re.split(r"[^A-Za-z0-9]+", s):
        out.extend(w.lower() for w in _WORD.findall(part))
    return [t for t in out if t not in STOP and len(t) > 1]
