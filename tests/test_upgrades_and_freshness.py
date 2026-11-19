"""Keeping the knowledge base current, and upgrading a dependency with the code changes it needs."""

import json
import subprocess
import textwrap

import pytest

from codewright import knowledgefresh as kf
from codewright import llm, upgrade, watcher
from codewright.engine import review

# ---------------------------------------------------------------- knowledge freshness


def _cache(root, versions: dict):
    from codewright.index import resolve

    idx = resolve(root)
    watcher._save_cache(
        root, {k: {"version": v, "released": "2026-11-01", "fetched": 1e12} for k, v in versions.items()}, idx
    )


def test_freshness_compares_checked_with_newest_and_team_can_mark_rechecked(tmp_path):
    _cache(tmp_path, {"pydantic": "2.15.0", "fastapi": "0.163.1", "tenacity": "9.2.1"})
    rows = {r["library"]: r for r in kf.freshness(tmp_path, None, online=False)}
    assert rows["pydantic"]["status"] == "behind" and rows["pydantic"]["bump"] == "minor"
    assert rows["pydantic"]["checked"] == "2.14.3" and rows["pydantic"]["checked_by"] == "built-in"
    assert rows["fastapi"]["status"] == "current"
    assert rows["tenacity"]["status"] == "unrecorded"
    kf.mark_checked(tmp_path, "pydantic", "2.15.0")
    rows = {r["library"]: r for r in kf.freshness(tmp_path, None, online=False)}
    assert rows["pydantic"]["status"] == "current" and rows["pydantic"]["checked_by"] == "your team"
    with pytest.raises(ValueError):
        kf.mark_checked(tmp_path, "pydantic", "not-a-version")


def test_changelog_sections_from_html_and_markdown(monkeypatch):
    html_page = (
        "<html><body><nav>menu 9.9.9</nav><h2>0.165.0</h2><p>Add form limits.</p>"
        "<h2>0.164.0</h2><ul><li>Drop Python 3.9.</li></ul><h2>0.163.1</h2><p>old</p></body></html>"
    )
    md = "# Changes\n\n## Version 2.1.9 (2026-11-02)\n\n- Fix a leak.\n\n## 2.1.8\n\n- older\n"

    class R:
        def __init__(self, body):
            self.body = body.encode()

        def read(self, n=-1):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    pages = {"https://x/html": html_page, "https://x/md": md}
    monkeypatch.setattr(kf.urllib.request, "urlopen", lambda req, timeout=10: R(pages[req.full_url]))
    got = kf._changelog_sections("https://x/html", {"0.165.0", "0.164.0"}, 10)
    assert [(g["version"], g["body"]) for g in got] == [
        ("0.165.0", "Add form limits."),
        ("0.164.0", "Drop Python 3.9."),
    ]
    got = kf._changelog_sections("https://x/md", {"2.1.9"}, 10)
    assert got == [{"version": "2.1.9", "date": "", "body": "- Fix a leak."}]


def test_release_notes_from_github_releases(monkeypatch):
    pypi = {
        "releases": {"1.0.0": [], "1.1.0": [], "1.2.0rc1": [], "1.2.0": []},
        "info": {"project_urls": {"Source": "https://github.com/acme/lib", "Changelog": "https://acme.dev/changes"}},
    }
    gh = [
        {"tag_name": "v1.2.0", "published_at": "2026-11-10T00:00:00Z", "body": "New `fetch_all()`."},
        {"tag_name": "v1.1.0", "published_at": "2026-10-01T00:00:00Z", "body": "Deprecate `fetch()`."},
        {"tag_name": "v1.0.0", "body": "old"},
    ]
    monkeypatch.setattr(kf, "_get_json", lambda url, timeout=8.0: gh if "api.github.com" in url else pypi)
    n = kf.release_notes("lib", "1.0.0", "1.2.0")
    assert n["versions"] == ["1.2.0", "1.1.0"] and [x["version"] for x in n["notes"]] == ["1.2.0", "1.1.0"]
    assert n["links"][0]["url"] == "https://acme.dev/changes"


def test_draft_updates_keeps_only_real_cards(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "status", lambda cfg: {"available": True, "reason": ""})
    reply = {
        "new_cards": [
            {
                "title": "Use model_dump_json(round_trip=True)",
                "suggestion": "Use it.",
                "versions": ">=2.15",
                "sources": ["https://docs.pydantic.dev/x"],
            },
            {"title": "no suggestion"},
        ],
        "outdated": [
            {"card": "pydantic.field-validator", "why": "w", "change": "c"},
            {"card": "invented.card", "why": "w"},
        ],
    }
    monkeypatch.setattr(llm, "complete", lambda *a, **k: json.dumps(reply))
    out = kf.draft_updates(tmp_path, "pydantic", {"notes": [{"version": "2.15.0", "date": "", "body": "notes"}]})
    assert [c["title"] for c in out["new_cards"]] == ["Use model_dump_json(round_trip=True)"]
    assert out["new_cards"][0]["library"] == "pydantic"
    assert [o["card"] for o in out["outdated"]] == ["pydantic.field-validator"]
    with pytest.raises(RuntimeError, match="no release notes"):
        kf.draft_updates(tmp_path, "pydantic", {"notes": []})


# ---------------------------------------------------------------- upgrading a dependency


@pytest.mark.parametrize(
    "spec,version,want",
    [
        ("==2.0.36", "2.1.9", "==2.1.9"),
        (">=0.110", "0.165.0", ">=0.165.0"),
        (">=1.0,<2", "2.3.0", ">=2.3.0,<3"),
        (">=1,<3", "2.3.0", ">=2.3.0,<3"),
        (">=0.27,<0.28", "0.28.1", ">=0.28.1,<0.29"),
        ("^0.110", "0.165.0", "^0.165.0"),
        ("!=1.2,>=1.0", "1.5", "!=1.2,>=1.5"),
    ],
)
def test_new_spec(spec, version, want):
    assert upgrade.new_spec(spec, version) == want


def test_requirement_edits_keep_formatting(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent("""
        [project]
        name = "app"
        dependencies = [
            "fastapi>=0.110",  # web
            "pydantic[email]>=2.0,<3",
        ]
        [tool.poetry.dependencies]
        python = "^3.11"
        httpx = "^0.27"
    """)
    )
    (tmp_path / "requirements-dev.txt").write_text("pydantic==2.9.0  # pinned for tests\nrequests\n")
    edits = {e["file"]: e for e in upgrade.requirement_edits(tmp_path, "pydantic", "2.14.3")}
    assert '"pydantic[email]>=2.14.3,<3",' in edits["pyproject.toml"]["text"]
    assert '"fastapi>=0.110",  # web' in edits["pyproject.toml"]["text"]
    assert edits["requirements-dev.txt"]["text"] == "pydantic==2.14.3  # pinned for tests\nrequests\n"
    poetry = upgrade.requirement_edits(tmp_path, "httpx", "0.28.1")
    assert 'httpx = "^0.28.1"' in poetry[0]["text"] and 'python = "^3.11"' in poetry[0]["text"]
    assert upgrade.requirement_edits(tmp_path, "requests", "2.34.2") == []  # unpinned: the lockfile decides


def _pydantic_repo(tmp_path):
    (tmp_path / "requirements.txt").write_text("pydantic==1.10.13  # models\n")
    (tmp_path / "models.py").write_text(
        textwrap.dedent("""
        from pydantic import BaseModel


        class User(BaseModel):
            name: str


        def load(raw):
            return User.parse_obj(raw)
    """)
    )
    for args in (["init", "-q", "-b", "main"], ["add", "-A"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "init"], cwd=tmp_path, check=True
    )
    return tmp_path


def test_plan_and_apply_an_upgrade_with_its_code_change(tmp_path):
    root = _pydantic_repo(tmp_path)
    r = review(root, run_proof=False, top=None)
    p = upgrade.plan(root, r.graph, r.stack, "pydantic", "2.14.3")
    assert p["installed"] == "1.10.13" and p["requirements"][0]["file"] == "requirements.txt"
    assert p["lock"]["tool"] is None  # requirements.txt only: the edit is the upgrade
    assert [x["title"] for x in p["patches"]] == ["Use the Pydantic v2 method names"]
    assert p["uses"] == [{"file": "models.py", "names": ["BaseModel"]}]
    out = upgrade.apply(root, r.graph, r.stack, "pydantic", "2.14.3", branch="codewright/upgrade-pydantic")
    assert out["branch"] == "codewright/upgrade-pydantic" and out["applied"] == [p["patches"][0]["id"]]
    assert (root / "requirements.txt").read_text() == "pydantic==2.14.3  # models\n"
    assert "User.model_validate(raw)" in (root / "models.py").read_text()
    st = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True).stdout
    assert "requirements.txt" in st and "models.py" in st  # applied, not committed


def test_lock_step_follows_the_lockfile(tmp_path):
    (tmp_path / "uv.lock").write_text("version = 1\n")
    assert upgrade.lock_step(tmp_path, "x", "1.0")["command"] == ["uv", "lock", "--upgrade-package", "x==1.0"]


# ---------------------------------------------------------------- add a library, deepen one


def _fake_pypi(monkeypatch, readme="# langgraph\nUse `Send` to fan out."):
    monkeypatch.setattr(
        kf,
        "_get_json",
        lambda url, timeout=8.0: {
            "info": {
                "name": "langgraph",
                "version": "1.2.22",
                "summary": "Graphs for agents",
                "description": readme,
                "project_urls": {"Docs": "https://docs.x/lg"},
            }
        },
    )


def test_add_a_library_drafts_grounded_cards(tmp_path, monkeypatch):
    _fake_pypi(monkeypatch)
    monkeypatch.setattr(llm, "status", lambda cfg: {"available": True, "reason": ""})
    asked = []
    reply = [
        {
            "title": "Fan out with Send",
            "suggestion": "Return Send() from a conditional edge.",
            "versions": ">=0.2",
            "sources": ["https://docs.x/lg", "not-a-url"],
        },
        {"title": "No suggestion here"},
    ]
    monkeypatch.setattr(
        llm, "complete", lambda cfg, sys_, msgs, **k: asked.append(msgs[0]["content"]) or json.dumps(reply)
    )
    out = kf.draft_library(
        tmp_path, "LangGraph", focus="fan-out", usage=[{"file": "app/graph.py", "names": ["StateGraph"]}]
    )
    assert out["library"] == "langgraph" and out["existing"] == 0
    assert [c["title"] for c in out["cards"]] == ["Fan out with Send"]
    assert out["cards"][0]["sources"] == ["https://docs.x/lg"] and out["cards"][0]["library"] == "langgraph"
    prompt = asked[0]
    assert "Use `Send` to fan out." in prompt and "app/graph.py: StateGraph" in prompt and "Focus on: fan-out" in prompt


def test_deepen_a_library_skips_cards_it_has(tmp_path, monkeypatch):
    _fake_pypi(monkeypatch, readme="")
    monkeypatch.setattr(llm, "status", lambda cfg: {"available": True, "reason": ""})
    from codewright import cards

    existing = next(c for c in cards.load() if c.library == "pydantic")
    asked = []
    reply = [
        {"title": existing.title, "suggestion": "dup"},
        {"title": "Validate with TypeAdapter", "suggestion": "Use TypeAdapter."},
    ]
    monkeypatch.setattr(
        llm, "complete", lambda cfg, sys_, msgs, **k: asked.append(msgs[0]["content"]) or json.dumps(reply)
    )
    out = kf.draft_library(tmp_path, "pydantic", pasted="TypeAdapter validates any type.")
    assert out["existing"] > 0 and [c["title"] for c in out["cards"]] == ["Validate with TypeAdapter"]
    assert existing.title in asked[0] and "TypeAdapter validates any type." in asked[0]


def test_library_endpoints(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from codewright.server import create_app

    _fake_pypi(monkeypatch)
    (tmp_path / "a.py").write_text("x = 1\n")
    c = TestClient(create_app([str(tmp_path)]), base_url="http://localhost")
    wid = c.get("/api/workspaces").json()[0]["id"]
    info = c.get(f"/api/w/{wid}/knowledge/library?name=langgraph").json()
    assert info["version"] == "1.2.22" and info["cards"] == 0 and "readme" not in info
    r = c.post(f"/api/w/{wid}/knowledge/library", json={"name": "langgraph"})
    assert r.status_code == 409 and "Settings" in r.json()["detail"]  # no model set up
