"""Ranking: impact (hot path, cost) x confidence / effort."""

from __future__ import annotations

from .models import Finding, ProofStatus


def confidence(f: Finding) -> float:
    c = 0.6
    if f.symbol_check == "ok":
        c += 0.15
    elif f.symbol_check == "unchecked":
        c -= 0.1
    if f.proof == ProofStatus.VERIFIED:
        c += 0.25
    return round(max(0.1, min(c, 1.0)), 2)


def score(f: Finding) -> float:
    impact = f.impact * (1.5 if f.hot_path else 1.0)
    return round(impact * f.confidence / max(f.effort, 1), 3)


def rank(findings: list[Finding], top: int | None = 10) -> list[Finding]:
    for f in findings:
        f.confidence = confidence(f)
        f.score = score(f)
    ordered = sorted(findings, key=lambda f: (-f.score, f.file, f.line))
    return ordered if top is None else ordered[:top]
