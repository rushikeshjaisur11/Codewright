"""The Review pipeline: discover -> graph -> intents -> cards -> critic -> proof -> feedback -> rank."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from . import cards as card_store
from . import critic, diffscope, graph, proof, proof_env, rank, rules, stack
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
    test_env: str = ""  # which interpreter ran the tests during proof, and why
    rules: dict = field(default_factory=dict)  # architecture rules and their violations (rules.summary)


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
    full_suite: bool = False,
    build_env: bool = True,
    bench: bool = False,
    bench_rounds: int = 3,
) -> ReviewResult:
    t0 = time.perf_counter()
    root = Path(root).resolve()
    st = stack.discover(root)
    g = graph.build(root)
    matches = detect(g)
    all_cards = card_store.load(extra_cards)
    findings = make_findings(matches, st, g, all_cards)

    arch = rules.summary(g, root)
    if base:
        changed = diffscope.changed_lines(root, base)
        findings = [f for f in findings if diffscope.in_scope(f, f.span, changed)]
        arch["violations"] = [v for v in arch["violations"] if v["line"] in changed.get(v["file"], ())]
        arch["errors"] = sum(1 for v in arch["violations"] if v["severity"] == "error")
        arch["warnings"] = sum(1 for v in arch["violations"] if v["severity"] == "warn")
        arch["ok"] = not arch["errors"] and arch["error"] is None

    covered = g.test_reachable()
    for f in findings:
        f.covered = f"{f.file}::{f.function}" in covered

    fb = Feedback.load(root) if use_feedback else Feedback()
    muted = [f for f in findings if fb.is_muted(f)]
    findings = [f for f in findings if not fb.is_muted(f)]
    for f in findings:
        f.status = fb.status_of(f)

    env = proof_env.resolve(root, python, build=build_env and run_proof)
    py = env.python
    test_env = env.source + (f" ({env.note})" if env.note else "")
    symbols = {f.id: f.symbol for f in findings if critic.is_dotted_symbol(f.symbol)}
    libraries = {c.id: c.library for c in all_cards if c.library}
    dists = {f.symbol: libraries[f.card_id] for f in findings if f.card_id in libraries}
    critic.check_symbols(findings, py, symbols, dists)

    # Drop malformed or hallucinated findings before spending time on proof.
    kept, rejected = critic.review(findings)
    if run_proof:
        from .graph import is_test_file

        importers = {f.file: sorted(p for p in g.importers_of(f.file) if is_test_file(p)) for f in kept if f.patch}
        proof.prove(
            kept,
            root,
            py,
            timeout=test_timeout,
            jobs=proof_jobs,
            python_label=env.source,
            importers=importers,
            full_suite=full_suite,
            bench=bench,
            bench_rounds=bench_rounds,
        )
        kept, broke = critic.review(kept)
        rejected += broke
    else:
        for f in kept:
            f.proof_detail = "proof skipped (--no-proof)"
    weights = {f.id: fb.weight_for(f) for f in kept}
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
        test_env if run_proof else "",
        arch,
    )
