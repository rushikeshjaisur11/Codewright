"""The Review pipeline: discover -> graph -> intents -> cards -> critic -> proof -> feedback -> rank."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import cards as card_store
from . import critic, detectcache, diffscope, graph, proof, proof_env, rank, rules, stack
from .feedback import Feedback
from .intents import detect
from .intents import detectors as detect_mod
from .models import Finding, Stack
from .scope import ReviewState
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
    scope: dict = field(default_factory=dict)  # what changed since proof last ran, and what is still unproven
    proved_files: list[str] = field(default_factory=list)  # files whose tests ran against their patches this time
    reused: int = 0  # findings whose proof came from an earlier run (nothing changed)


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
    progress: Callable[[str, int, int], None] | None = None,
    bench: bool = False,
    bench_rounds: int = 3,
    use_state: bool = True,
    files: set[str] | None = None,
    fresh: bool = False,
    on_graph: Callable[[graph.CodeGraph, Stack], None] | None = None,
) -> ReviewResult:
    """`progress(stage, done, total)` reports each stage as it starts (stages: stack, graph, detect,
    check, baseline, proof, rank) and counts within it; total is 0 when the stage has nothing to count.

    Proof is the slow stage, so it is incremental: a finding whose file, imports, tests and patch are the same
    as when its proof last ran keeps that result (stored in ``.codewright/review-state.json``) and is not run
    again. `files` limits proof to those files (the rest keep what they had); `fresh` ignores the stored proofs."""
    t0 = time.perf_counter()
    root = Path(root).resolve()
    tell = progress or (lambda stage, done, total: None)
    tell("stack", 0, 0)
    st = stack.discover(root)
    tell("graph", 0, 0)
    dcache = detectcache.load(root) if use_state else None
    # New and edited files are detected while their syntax tree is in memory; on a big repo the trees don't all
    # fit, and detecting afterwards would parse them a second time.
    visitors = [detect_mod.fill_cache(dcache)] if dcache is not None else []
    g = graph.build(root, lambda done, total: tell("graph", done, total), visitors=visitors, detect_cache=dcache)
    if visitors:
        g.__dict__["detect_fresh"] = visitors[0].fresh
    if on_graph:  # the map, files and dependencies need only this: show them while the rest runs
        on_graph(g, st)
    tell("detect", 0, 0)
    matches = detect(g, cache=dcache, progress=lambda done, total: tell("detect", done, total))
    all_cards = card_store.for_repo(root, extra_cards)
    from . import teamrules

    matches += teamrules.detect(g, all_cards)  # conventions the team taught as checkable rules
    if dcache is not None:
        detectcache.save(root, dcache)
    findings = make_findings(matches, st, g, all_cards, progress=lambda done, total: tell("detect", done, total))

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

    tell("check", 0, 0)
    env = proof_env.resolve(root, python, build=build_env and run_proof)
    py = env.python
    test_env = env.source + (f" ({env.note})" if env.note else "")
    symbols = {f.id: f.symbol for f in findings if critic.is_dotted_symbol(f.symbol)}
    libraries = {c.id: c.library for c in all_cards if c.library}
    dists = {f.symbol: libraries[f.card_id] for f in findings if f.card_id in libraries}
    critic.check_symbols(findings, py, symbols, dists)

    # Drop malformed or hallucinated findings before spending time on proof.
    kept, rejected = critic.review(findings)
    state = ReviewState(root) if use_state else None
    reused: set[str] = set()
    if state is not None and not fresh:
        reused = state.reuse(kept, g)
        k2, broke = critic.review(kept)  # a cached "broke tests" result drops the finding again
        kept, rejected = k2, rejected + broke
    proved_files: set[str] = set()
    if run_proof:
        from .graph import is_test_file

        todo = [f for f in kept if f.id not in reused and (files is None or f.file in files)]
        proved_files = {f.file for f in todo if f.patch}
        importers = {f.file: sorted(p for p in g.importers_of(f.file) if is_test_file(p)) for f in todo if f.patch}
        proof.prove(
            todo,
            root,
            py,
            timeout=test_timeout,
            jobs=proof_jobs,
            python_label=env.source,
            importers=importers,
            full_suite=full_suite,
            progress=tell,
            bench=bench,
            bench_rounds=bench_rounds,
        )
        kept, broke = critic.review(kept)
        rejected += broke
        for f in kept:  # the ones left out of this run (not chosen) say why they are unproven
            if f.patch and f.file not in proved_files and f.id not in reused and f.proof.value == "unverified":
                f.proof_detail = f.proof_detail or "not selected for this run; choose it to run the tests against it"
    else:
        for f in kept:
            if f.id not in reused:
                f.proof_detail = (
                    "proof not run for this finding (run the review with proof, or choose its file to verify)"
                )
    scope: dict = {}
    if state is not None:
        if base is None:
            with contextlib.suppress(OSError):
                state.save(kept, g, proved_files=proved_files)
        scope = state.summary(kept)
    tell("rank", 0, 0)
    from . import aicheck

    ai = aicheck.weights(root, kept) if use_state else {}
    weights = {f.id: fb.weight_for(f) * ai.get(f.id, 1.0) for f in kept}
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
        scope,
        sorted(proved_files),
        len(reused),
    )
