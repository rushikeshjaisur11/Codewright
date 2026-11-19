"""Reopening a repo re-checks only what changed, and the user (with the model's help) picks what gets proven."""

import shutil
import sys
import time
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
    assert w.ai_map() is first  # the same code: the scan is reused (an edit rebuilds it; see test_rescan_speed)


def test_map_and_dependencies_are_available_before_the_review_finishes(repo, monkeypatch):
    import threading

    from codewright import engine

    gate, reached = threading.Event(), threading.Event()
    real = engine.detect

    def slow(g, **k):
        reached.set()
        gate.wait(10)
        return real(g, **k)

    monkeypatch.setattr(engine, "detect", slow)
    w = Workspace(repo)
    t = threading.Thread(target=lambda: w.run_review(proof=False))
    t.start()
    try:
        assert reached.wait(10)  # the graph is built; detection is stuck
        assert w.result is None and w.graph_ready
        assert w.amap().system()  # the map already draws
        assert w.ai()["files"] is not None
        assert w.deps(online=False)["declared"] >= 0
    finally:
        gate.set()
        t.join(20)
    assert w.result is not None and w.early is None


def test_release_rows_stream_as_each_lookup_finishes(repo, monkeypatch):
    from codewright import watcher

    seen = []

    def fake_latest(name, cache=None, timeout=8.0, index=None):
        return {"version": "99.0", "released": "2026-01-01", "fetched": 1e12}

    monkeypatch.setattr(watcher, "latest", fake_latest)
    monkeypatch.setattr(watcher, "resolve_index", lambda root=None: watcher.PackageIndex("https://x/simple", ""))
    r = review(repo, run_proof=False, top=None)
    rows = watcher.check(r.stack, repo, on_row=seen.append)
    assert len(seen) == len(rows) == len(r.stack.dependencies)
    assert {x["name"] for x in seen} == {x["name"] for x in rows}


# ---------------------------------------------------------------- detection reused between runs


def test_second_review_reuses_detection_and_finds_the_same(repo):
    from codewright import graph

    first = review(repo, run_proof=False, top=None)
    graph._PARSED.clear()  # as after a restart
    again = review(repo, run_proof=False, top=None)
    files = [p for p in again.graph.files if not p.startswith("tests/")]
    assert again.graph.__dict__["detect_reused"] >= len(files) - 1
    assert again.graph.__dict__.get("detect_graph_reused")
    assert sorted(f.id for f in again.findings) == sorted(f.id for f in first.findings)
    assert [f.patch for f in sorted(again.findings, key=lambda f: f.id)] == [
        f.patch for f in sorted(first.findings, key=lambda f: f.id)
    ]


def test_an_edited_file_is_the_only_one_detected_again(repo):
    first = review(repo, run_proof=False, top=None)
    target = memo(first).file
    (repo / target).write_text((repo / target).read_text() + "\n\ndef extra():\n    return 1\n")
    again = review(repo, run_proof=False, top=None)
    scanned = [p for p in again.graph.files if not p.startswith("tests/") and "/tests/" not in p]
    assert again.graph.__dict__["detect_reused"] == len(scanned) - 1
    assert not again.graph.__dict__.get("detect_graph_reused")  # a file changed: whole-graph checks re-ran
    assert memo(again).id == memo(first).id


def test_detect_cache_encoding_round_trips_and_skips_live_objects():
    from codewright import detectcache

    data = {"edits": [(1, 2, "x")], "b": b"\x00raw", "s": {"a"}, "n": None, "nested": {"k": (3, [4])}}
    assert detectcache._dec(detectcache._enc(data)) == data
    with pytest.raises(detectcache.Unencodable):
        detectcache._enc({"node": object()})


def test_detect_cache_is_dropped_when_detectors_change(repo, monkeypatch):
    from codewright import detectcache

    review(repo, run_proof=False, top=None)
    assert detectcache.load(repo)
    monkeypatch.setattr(detectcache, "signature", lambda: "other")
    assert detectcache.load(repo) == {}


# ---------------------------------------------------------------- the saved review, activity and gitignore


def test_reopening_shows_the_saved_review_at_once(repo):
    from fastapi.testclient import TestClient

    from codewright.server import create_app

    c = TestClient(create_app([str(repo)]), base_url="http://localhost")
    wid = c.get("/api/workspaces").json()[0]["id"]
    c.post(f"/api/w/{wid}/review", json={"proof": False})
    for _ in range(200):
        d = c.get(f"/api/w/{wid}/review").json()
        if d["has_result"] and not d["running"]:
            break
        time.sleep(0.05)
    shown = len(d["result"]["findings"])

    c2 = TestClient(create_app([str(repo)]), base_url="http://localhost")  # a new server process
    d2 = c2.get(f"/api/w/{wid}/review").json()
    assert d2["from_cache"] and not d2["has_result"]
    assert len(d2["result"]["findings"]) == shown

    feed = c2.get(f"/api/activity?workspace={wid}").json()
    assert any("Review done" in e["message"] for e in feed["events"])
    later = c2.get(f"/api/activity?since={feed['last']}&workspace={wid}").json()
    assert later["events"] == []


def test_old_gitignore_gains_the_cache_entries(tmp_path):
    from codewright.feedback import ensure_dir

    d = tmp_path / ".codewright"
    d.mkdir()
    (d / ".gitignore").write_text("snapshots/\nreleases.json\n")
    ensure_dir(tmp_path)
    lines = (d / ".gitignore").read_text().splitlines()
    assert {"review-state.json", "cache/", "last-review.json", "snapshots/"} <= set(lines)


def test_detectors_that_read_the_graph_are_marked():
    """A function detector that reads other files (hot paths, callers) can't be cached per file or run while the
    graph is still being built: it must say so with uses_graph=True."""
    import ast
    from pathlib import Path

    from codewright.intents import detectors

    for p in sorted((Path(detectors.__file__).parent).glob("*.py")):
        for fn in ast.walk(ast.parse(p.read_text())):
            if not isinstance(fn, ast.FunctionDef):
                continue
            decos = [
                d for d in fn.decorator_list if isinstance(d, ast.Call) and getattr(d.func, "id", "") == "detector"
            ]
            if not decos or "graph" not in [a.arg for a in fn.args.args]:
                continue
            reads = any(isinstance(n, ast.Name) and n.id == "graph" for n in ast.walk(ast.Module(fn.body, [])))
            marked = any(k.arg == "uses_graph" for k in decos[0].keywords)
            intent = decos[0].args[0].value
            filtered = intent in detectors.GRAPH_FILTERS  # reads the graph only when given one; filter does the rest
            assert reads == (marked or filtered), f"{p.name}:{fn.name}: mark uses_graph or add a graph_filter"
