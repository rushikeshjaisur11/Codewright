"""Branches for fixes, verifying a finding (steps and an AI-written test), and the Python 3.11 floor."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from codewright import gitops, llm, stack, verify
from codewright.engine import review
from codewright.models import Stack

FIX = Path(__file__).parent / "fixtures"


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    dest = tmp_path / "app"
    shutil.copytree(FIX / "sample_app", dest, ignore=shutil.ignore_patterns(".codewright", "__pycache__"))
    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "-c", "user.email=t@example.com", "-c", "user.name=t", "add", "-A")
    _git(dest, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init")
    _git(dest, "config", "user.email", "t@example.com")
    _git(dest, "config", "user.name", "t")
    return dest


# ---------------------------------------------------------------- branches


def test_fix_branch_switch_and_commit(repo):
    s = gitops.status(repo)
    assert s["git"] and s["branch"] == "main" and s["changed"] == [] and "main" in s["branches"]
    (repo / "app" / "rates.py").write_text((repo / "app" / "rates.py").read_text() + "\n# fixed\n")
    with pytest.raises(gitops.GitError, match="uncommitted"):
        gitops.switch(repo, "main")  # refuses: the change would follow or block
    s = gitops.create(repo, "codewright/fix-rates")
    assert s["branch"] == "codewright/fix-rates" and s["changed"] == [{"file": "app/rates.py", "status": "M"}]
    with pytest.raises(gitops.GitError, match="not an uncommitted change"):
        gitops.commit(repo, "x", ["app/api.py"])
    s = gitops.commit(repo, "Apply Codewright fixes", ["app/rates.py"])
    assert s["changed"] == [] and s["committed"]
    s = gitops.switch(repo, "main")
    assert s["branch"] == "main" and "# fixed" not in (repo / "app" / "rates.py").read_text()
    with pytest.raises(gitops.GitError, match="valid branch name"):
        gitops.create(repo, "bad name..")


def test_branch_api(repo):
    from fastapi.testclient import TestClient

    from codewright.server import create_app

    c = TestClient(create_app([str(repo)]), base_url="http://localhost")
    wid = c.get("/api/workspaces").json()[0]["id"]
    assert c.get(f"/api/w/{wid}/git").json()["branch"] == "main"
    assert c.post(f"/api/w/{wid}/git/branch", json={"name": "fixes"}).json()["branch"] == "fixes"
    (repo / "new.py").write_text("x = 1\n")
    r = c.post(f"/api/w/{wid}/git/switch", json={"branch": "main"})
    assert r.status_code == 409 and "uncommitted" in r.json()["detail"]
    out = c.post(f"/api/w/{wid}/git/commit", json={"message": "add new", "files": ["new.py"]}).json()
    assert out["changed"] == [] and out["branch"] == "fixes"


def test_not_a_git_repo(tmp_path):
    assert gitops.status(tmp_path) == {"git": False}


# ---------------------------------------------------------------- verifying a finding


def _memo(root):
    r = review(root, run_proof=False, top=None)
    return r, next(f for f in r.findings if f.intent == "memo-cache")


def test_steps_say_how_to_verify(repo):
    r, f = _memo(repo)
    s = verify.steps(f, r.graph)
    kinds = [x["kind"] for x in s]
    assert kinds[0] == "branch" and "tests" in kinds and kinds[-1] == "ai"
    tests = next(x for x in s if x["kind"] == "tests")
    assert "tests/test_rates.py" in tests["command"]
    assert any(x["title"].startswith("Make sure this is not a case") for x in s)


GOOD_TEST = """```python
from app.rates import fetch_rate


def test_rate_is_stable():
    assert fetch_rate("eur") == 4.5
    assert fetch_rate("eur") == fetch_rate("eur")
```"""

STRICT_TEST = """```python
import app.rates as r


def test_not_wrapped():
    assert not hasattr(r.fetch_rate, "cache_info")
```"""


@pytest.fixture
def model(monkeypatch):
    monkeypatch.setattr(llm, "status", lambda cfg: {"available": True, "reason": "", "model": "m", "provider": "p"})
    holder = {"reply": GOOD_TEST}
    monkeypatch.setattr(llm, "complete", lambda *a, **k: holder["reply"])
    return holder


def test_ai_test_runs_before_and_after_the_patch(repo, model):
    r, f = _memo(repo)
    out = verify.ai_test(repo, f, r.graph, python=sys.executable)
    assert out["verdict"] == "verified", out
    assert out["before"]["ok"] and out["after"]["ok"] and out["path"].startswith("tests/test_codewright_")
    assert not (repo / out["path"]).exists()  # ran in a copy; the repo is untouched
    assert verify.save_test(repo, f.id) == out["path"] and (repo / out["path"]).is_file()

    model["reply"] = STRICT_TEST  # passes today, fails once lru_cache wraps the function
    out = verify.ai_test(repo, f, r.graph, python=sys.executable)
    assert out["verdict"] == "broke" and out["before"]["ok"] and not out["after"]["ok"]

    model["reply"] = (
        "```python\nfrom app.rates import fetch_rate\n\n\ndef test_bad():\n    assert fetch_rate('x') == 99\n```"
    )
    assert verify.ai_test(repo, f, r.graph, python=sys.executable)["verdict"] == "test-fails-today"

    model["reply"] = "```python\ndef broken(:\n```"
    with pytest.raises(RuntimeError, match="not valid Python"):
        verify.ai_test(repo, f, r.graph, python=sys.executable)


# ---------------------------------------------------------------- Python 3.11 and newer


def test_older_python_is_called_out():
    assert stack.unsupported_python(Stack(root=".", python="3.9")) == "3.9"
    assert stack.unsupported_python(Stack(root=".", python=">=3.10")) == "3.10"
    assert stack.unsupported_python(Stack(root=".", python="3.11")) is None
    assert stack.unsupported_python(Stack(root=".", python=None)) is None


def test_no_card_targets_python_older_than_3_11():
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version

    from codewright import cards

    for c in cards.load():
        if c.library is None and c.versions not in ("*", ""):
            assert "3.10" not in SpecifierSet(c.versions) or "3.11" in SpecifierSet(c.versions), c.id
            assert not any(
                s.operator in (">=", ">") and Version(s.version) < Version("3.11") for s in SpecifierSet(c.versions)
            ), c.id
        assert "Python 3.8" not in c.risk + c.suggestion, c.id


# ---------------------------------------------------------------- new branch from, fetch, pull, sync


@pytest.fixture
def clones(tmp_path):
    """A bare 'origin' and two clones of it: `mine` (the repo Codewright opens) and `theirs` (a teammate)."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    for c in (seed,):
        _git(c, "config", "user.email", "t@example.com")
        _git(c, "config", "user.name", "t")
    (seed / "a.py").write_text("x = 1\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "seed")
    _git(seed, "push", "-q", "origin", "main")
    out = {}
    for name in ("mine", "theirs"):
        p = tmp_path / name
        _git(tmp_path, "clone", "-q", str(origin), str(p))
        _git(p, "config", "user.email", f"{name}@example.com")
        _git(p, "config", "user.name", name)
        out[name] = p
    return out


def _commit(repo, name, body="y = 2\n"):
    (repo / name).write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"add {name}")


def test_fetch_then_pull_fast_forwards(clones):
    mine, theirs = clones["mine"], clones["theirs"]
    _commit(theirs, "b.py")
    _git(theirs, "push", "-q", "origin", "main")
    s = gitops.status(mine)
    assert s["upstream"] == "origin/main" and s["behind"] == 0  # not fetched yet
    s = gitops.fetch(mine)
    assert (s["ahead"], s["behind"]) == (0, 1)
    s = gitops.pull(mine)
    assert s["pulled"] == 1 and s["behind"] == 0 and (mine / "b.py").exists()


def test_pull_refuses_diverged_branches(clones):
    mine, theirs = clones["mine"], clones["theirs"]
    _commit(theirs, "b.py")
    _git(theirs, "push", "-q", "origin", "main")
    _commit(mine, "c.py")
    with pytest.raises(gitops.GitError, match="diverged"):
        gitops.pull(mine)
    assert not (mine / "b.py").exists()  # nothing merged


def test_sync_pulls_then_pushes(clones):
    mine, theirs = clones["mine"], clones["theirs"]
    _commit(theirs, "b.py")
    _git(theirs, "push", "-q", "origin", "main")
    s = gitops.pull(mine)
    _commit(mine, "c.py")
    s = gitops.sync(mine)
    assert s["pushed"] == 1 and s["ahead"] == 0 and s["behind"] == 0
    _git(theirs, "pull", "-q", "--ff-only")
    assert (theirs / "c.py").exists()


def test_new_branch_from_a_remote_branch_does_not_track_it_and_sync_publishes_it(clones):
    mine, theirs = clones["mine"], clones["theirs"]
    _commit(theirs, "b.py")
    _git(theirs, "push", "-q", "origin", "main")
    gitops.fetch(mine)
    s = gitops.create(mine, "codewright/fix", "origin/main")
    assert s["branch"] == "codewright/fix" and s["upstream"] is None and (mine / "b.py").exists()
    assert "origin/main" in s["remote_branches"]
    with pytest.raises(gitops.GitError, match="no upstream"):
        gitops.pull(mine)
    _commit(mine, "fix.py")
    s = gitops.sync(mine)  # first push: sets the upstream to origin/codewright/fix, never main
    assert s["upstream"] == "origin/codewright/fix" and s["ahead"] == 0
    _git(theirs, "fetch", "-q")
    assert (
        "fix.py"
        not in subprocess.run(
            ["git", "ls-tree", "--name-only", "origin/main"], cwd=theirs, capture_output=True, text=True
        ).stdout
    )
    with pytest.raises(gitops.GitError, match="already exists"):
        gitops.create(mine, "codewright/fix")
    with pytest.raises(gitops.GitError, match="no branch or commit"):
        gitops.create(mine, "other", "origin/nope")
