"""The Review pipeline: discover -> graph -> intents -> cards -> critic -> proof -> feedback -> rank."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import cards as card_store
from . import critic, diffscope, graph, proof, rank, stack
from .feedback import Feedback
from .intents import detect
from .models import Finding, Stack
from .suggest import make_findings


@dataclass
class ReviewResult:
    stack: Stack
    graph: graph.CodeGraph
    findings: list[Finding]  # shown, ranked
    rejected: list[Finding] = field(default_factory=list)  # dropped by the critic
    muted: list[Finding] = field(default_factory=list)  # hidden by feedback
    total_matches: int = 0
    base: str | None = None  # PR mode: the ref compared against
    seconds: float = 0.0
    feedback: Feedback = field(default_factory=Feedback)


def review(
    root: str | Path,
    *,
    run_proof: bool = True,
    top: int | None = 10,
    python: str | None = None,
    extra_cards: list[Path] | None = None,
    test_timeout: int = 300,
    base: str | None = None,
    use_feedback: bool = True,
    proof_jobs: int | None = None,
) -> ReviewResult:
    t0 = time.perf_counter()
    root = Path(root).resolve()
    st = stack.discover(root)
    g = graph.build(root)
    matches = detect(g)
    findings = make_findings(matches, st, g, card_store.load(extra_cards))

    if base:
        changed = diffscope.changed_lines(root, base)
        findings = [f for f in findings if diffscope.in_scope(f, f.span, changed)]

    covered = g.test_reachable()
    for f in findings:
        f.covered = f"{f.file}::{f.function}" in covered

    fb = Feedback.load(root) if use_feedback else Feedback()
    muted = [f for f in findings if fb.is_muted(f)]
    findings = [f for f in findings if not fb.is_muted(f)]
    for f in findings:
        f.status = fb.status_of(f)

    py = python or critic.target_python(root)
    if os.sep in py:
        py = os.path.abspath(py)  # tests run with a different cwd; keep venv symlinks intact
    symbols = {f.id: f.symbol for f in findings if critic.is_dotted_symbol(f.symbol)}
    critic.check_symbols(findings, py, symbols)

    # Drop malformed or hallucinated findings before spending time on proof.
    kept, rejected = critic.review(findings)
    if run_proof:
        proof.prove(kept, root, py, timeout=test_timeout, jobs=proof_jobs)
        kept, broke = critic.review(kept)
        rejected += broke
    else:
        for f in kept:
            f.proof_detail = "proof skipped (--no-proof)"
    weights = {f.intent: fb.intent_weight(f.intent) for f in kept}
    return ReviewResult(
        st,
        g,
        rank.rank(kept, top, weights),
        rejected,
        muted,
        len(matches),
        base,
        round(time.perf_counter() - t0, 2),
        fb,
    )
