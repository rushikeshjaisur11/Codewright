"""The new features reach every surface: web API, MCP tools and CLI."""

import asyncio
import json
import textwrap
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from codewright.cli import app as cli
from codewright.server import create_app


def write(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body).lstrip())
    return tmp_path


REPO = {
    "pyproject.toml": '[project]\nname = "shop"\nversion = "0"\nrequires-python = ">=3.12"\n'
    'dependencies = ["pydantic<2", "pytz"]\n',
    "uv.lock": '[[package]]\nname = "pydantic"\nversion = "1.10.13"\n\n'
    '[[package]]\nname = "pytz"\nversion = "2024.1"\n',
    "app/__init__.py": "",
    "app/api/__init__.py": "",
    "app/api/routes.py": "from app.db.models import M\nimport pytz\n\ndef f():\n    return M\n",
    "app/db/__init__.py": "",
    "app/db/models.py": """
        from pydantic import BaseModel, validator


        class M(BaseModel):
            x: int

            @validator("x")
            def check(cls, v):
                return v
        """,
    ".codewright/rules.yaml": "rules:\n  - {id: api-no-db, kind: forbid-import, in: app/api, import: app/db/**}\n",
}


def test_web_api(tmp_path):
    write(tmp_path, REPO)
    c = TestClient(create_app([str(tmp_path)]), base_url="http://localhost")
    wid = c.get("/api/workspaces").json()[0]["id"]
    c.post(f"/api/w/{wid}/review", json={"proof": False})
    for _ in range(100):
        d = c.get(f"/api/w/{wid}/review").json()
        if d["result"] and not d["running"]:
            break
        import time

        time.sleep(0.1)
    assert d["result"]["rules"]["errors"] == 1
    assert "insights" in d["result"]["metrics"]
    assert c.get(f"/api/w/{wid}/rules").json()["violations"][0]["rule"] == "api-no-db"
    assert c.post(f"/api/w/{wid}/rules/init", json={}).status_code == 409  # rules.yaml exists
    deps = c.get(f"/api/w/{wid}/deps", params={"online": False}).json()
    assert {f["kind"] for f in deps["findings"]} >= {"stdlib"}
    mig = c.get(f"/api/w/{wid}/migrations").json()
    assert mig["applicable"][0]["name"] == "pydantic-v2"
    plan = c.post(f"/api/w/{wid}/migrations/pydantic-v2/plan", json={"batch": 3}).json()
    assert plan["total"] == 1 and plan["next_step"] == 1
    step = c.get(f"/api/w/{wid}/migrations/pydantic-v2/steps/1").json()
    assert "field_validator" in step["patch"] and "step 1 of 1" in step["pr_body"]
    applied = c.post(f"/api/w/{wid}/migrations/pydantic-v2/steps/1/apply").json()
    assert applied["status"]["progress"] == 1.0
    assert "field_validator" in (tmp_path / "app/db/models.py").read_text()
    assert c.get(f"/api/w/{wid}/migrations/nope").status_code == 404
    r = c.post(f"/api/w/{wid}/critique", json={"text": "Add a new table for exports. Use pytz."}).json()
    assert r["verdict"] in ("go", "revise") and any("migration" in p["title"] for p in r["points"])
    assert c.post(f"/api/w/{wid}/critique", json={"text": "  "}).status_code == 400
    assert c.get(f"/api/w/{wid}/insights").json() == []


def test_mcp_tools(tmp_path):
    from codewright import mcp_server

    write(tmp_path, REPO)
    names = {t.name for t in asyncio.run(mcp_server.server.list_tools())}
    assert {"check_rules", "review_plan", "dependency_audit", "migration_plan", "migration_step"} <= names
    assert {"feedback_insights"} <= names
    repo = str(tmp_path)
    assert mcp_server.check_rules(repo)["errors"] == 1
    assert mcp_server.review(repo)["rule_violations"][0]["rule"] == "api-no-db"
    assert mcp_server.migration_plan(repo)["applicable"][0]["name"] == "pydantic-v2"
    assert mcp_server.migration_plan(repo, "pydantic-v2")["total"] == 1
    assert not (tmp_path / ".codewright/migrations").exists()  # the MCP plan isn't saved
    assert mcp_server.review_plan(repo, "Write tests. Add httpx.")["verdict"] in ("go", "revise")
    assert any(f["kind"] == "stdlib" for f in mcp_server.dependency_audit(repo)["findings"])


def test_cli(tmp_path):
    write(tmp_path, REPO)
    run = CliRunner().invoke
    res = run(cli, ["check", str(tmp_path)])
    assert res.exit_code == 1 and "api-no-db" in res.output
    res = run(cli, ["rules", "--path", str(tmp_path), "--json"])
    assert json.loads(res.output)["errors"] == 1
    res = run(cli, ["deps", str(tmp_path), "--offline", "--json"])
    assert any(f["deps"] == ["pytz"] for f in json.loads(res.output)["findings"])
    res = run(cli, ["migrate", "plan", "pydantic-v2", "--path", str(tmp_path)])
    assert res.exit_code == 0 and "step 1" in res.output
    res = run(cli, ["migrate", "step", "pydantic-v2", "1", "--path", str(tmp_path), "--pr-body"])
    assert "Pydantic v1 → v2: step 1 of 1" in res.output
    plan = tmp_path / "plan.md"
    plan.write_text("Read app/db/models.py from app/api/routes.py. Tests: pytest.\n")
    res = run(cli, ["critique", str(plan), "--path", str(tmp_path)])
    assert "api-no-db" in res.output
    (tmp_path / ".codewright/rules.yaml").unlink()
    res = run(cli, ["rules", "init", "--path", str(tmp_path)])
    assert res.exit_code == 0 and "Wrote .codewright/rules.yaml" in res.output
