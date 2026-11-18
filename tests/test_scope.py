"""Reopening a repo re-checks only what changed, and the user (with the model's help) picks what gets proven."""

import shutil
import sys
from pathlib import Path

import pytest

from codewright import proof as proof_mod
from codewright import scope
from codewright.engine import review
from codewright.models import ProofStatus
from codewright.service import Workspace

FIXTURE = Path(__file__).parent / "fixtures" / "sample_app"


@pytest.fixture
def repo(tmp_path):
    dest = tmp_path / "app"
    shutil.copytree(FIXTURE, dest, ignore=shutil.ignore_patterns(".codewright", "__pycache__"))
    return dest


@pytest.fixture
def prove_calls(monkeypatch):
    calls = []
    real = proof_mod.prove

    def spy(findings, *a, **k):
        calls.append([f.id for f in findings])
        return real(findings, *a, **k)

    monkeypatch.setattr(proof_mod, "prove", spy)
    return calls


def memo(result):
    return next(f for f in result.findings if f.intent == "memo-cache")


def test_second_review_reuses_proof_and_runs_no_tests(repo, prove_calls):
    first = review(repo, python=sys.executable, top=None)
    assert memo(first).proof is ProofStatus.VERIFIED
    assert len(prove_calls) == 1

    again = review(repo, python=sys.executable, top=None)
    patched = {f.id for f in again.findings if f.patch}
    assert len(prove_calls) == 2 and not patched & set(prove_calls[1])  # no patch is run again
    m = memo(again)
    assert m.proof is ProofStatus.VERIFIED and m.proof_cached
    assert again.reused >= 1


def test_reuse_survives_a_quick_scan_without_proof(repo):
    review(repo, python=sys.executable, top=None)
    quick = review(repo, run_proof=False, top=None)  # what the app does on open
    assert memo(quick).proof is ProofStatus.VERIFIED
    assert quick.scope["unproven"] < len([f for f in quick.findings if f.patch])


def test_editing_the_file_invalidates_its_proof(repo, prove_calls):
    review(repo, python=sys.executable, top=None)
    target = memo(review(repo, run_proof=False, top=None)).file
    p = repo / target
    p.write_text(p.read_text() + "\n# edited\n")
    r = review(repo, python=sys.executable, top=None)
    assert any(memo(r).id in ids for ids in prove_calls[1:])  # proven again
    assert not memo(r).proof_cached


def test_files_limits_which_findings_are_proven(repo, prove_calls):
    r = review(repo, python=sys.executable, top=None, files={"not/a/file.py"}, use_state=False)
    assert prove_calls[-1] == []
    assert memo(r).proof is ProofStatus.UNVERIFIED
    assert r.proved_files == []

    r = review(repo, python=sys.executable, top=None, files={memo(r).file}, use_state=False)
    assert memo(r).proof is ProofStatus.VERIFIED
    assert memo(r).id in prove_calls[-1]


def test_changed_files_are_remembered_until_proven(repo):
    review(repo, run_proof=False, top=None)
    r = review(repo, run_proof=False, top=None)
    assert r.scope["changed"] == []
    f = memo(r).file
    (repo / f).write_text((repo / f).read_text() + "\n# touched\n")
    r = review(repo, run_proof=False, top=None)
    assert r.scope["changed"] == [f]
    r = review(repo, run_proof=False, top=None)  # a second quick scan must not forget it
    assert r.scope["changed"] == [f]
    r = review(repo, python=sys.executable, top=None, files={f})
    assert r.scope["changed"] == []


def test_plan_ranks_without_a_model_and_says_why(repo):
    r = review(repo, run_proof=False, top=None)
    plan = scope.plan(r.findings, r.scope["changed"], repo)
    assert plan["source"] == "ranking" and plan["files"]
    assert all(row["reason"] for row in plan["files"])
    assert plan["files"][0]["selected"]


def test_plan_uses_the_models_picks_and_ignores_invented_files(repo, monkeypatch):
    r = review(repo, run_proof=False, top=None)
    rows = scope.candidates(r.findings, set())
    real, other = rows[0]["file"], rows[-1]["file"]
    monkeypatch.setattr(scope, "llm_status", lambda cfg: {"available": True, "reason": ""})
    monkeypatch.setattr(
        scope,
        "complete",
        lambda *a, **k: f'Sure:\n[{{"file": "{other}", "reason": "hot"}}, {{"file": "ghost.py", "reason": "x"}}]',
    )
    plan = scope.plan(r.findings, [], repo, limit=1)
    assert plan["source"] == "ai"
    assert [f["file"] for f in plan["files"] if f["selected"]] == [other]
    assert "ghost.py" not in [f["file"] for f in plan["files"]]
    assert real in [f["file"] for f in plan["files"]]  # the rest are listed, unticked


def test_plan_falls_back_when_the_model_answers_nonsense(repo, monkeypatch):
    r = review(repo, run_proof=False, top=None)
    monkeypatch.setattr(scope, "llm_status", lambda cfg: {"available": True, "reason": ""})
    monkeypatch.setattr(scope, "complete", lambda *a, **k: "no json here")
    plan = scope.plan(r.findings, [], repo)
    assert plan["source"] == "ranking" and plan["files"]


def test_workspace_builds_the_ai_page_once_per_review(repo):
    w = Workspace(repo)
    w.run_review(proof=False)
    assert w.ai_map() is w.ai_map()
    assert w.ai() is w.ai()
    first = w.ai_map()
    w.run_review(proof=False)
    assert w.ai_map() is not first
