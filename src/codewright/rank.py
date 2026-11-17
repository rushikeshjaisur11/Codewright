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


def speed_factor(f: Finding) -> float:
    """A measured speedup ranks a finding higher, a measured slowdown lower; unmeasured changes nothing."""
    import math

    perf = f.perf or {}
    if perf.get("verdict") == "faster":
        return 1.0 + 0.25 * min(math.log2(max(perf.get("speedup", 1.0), 1.0)), 2.0)
    if perf.get("verdict") == "slower":
        return 0.6
    return 1.0


def score(f: Finding, weight: float = 1.0) -> float:
    impact = f.impact * (1.5 if f.hot_path else 1.0) * speed_factor(f)
    return round(impact * f.confidence * weight / max(f.effort, 1), 3)


def rank(findings: list[Finding], top: int | None = 10, weights: dict[str, float] | None = None) -> list[Finding]:
    """`weights` maps a finding id (or, as a fallback, an intent) to a feedback multiplier."""
    weights = weights or {}
    for f in findings:
        f.confidence = confidence(f)
        f.score = score(f, weights.get(f.id, weights.get(f.intent, 1.0)))
    ordered = sorted(findings, key=lambda f: (-f.score, f.file, f.line))
    return ordered if top is None else ordered[:top]
