"""Regression tests for failures found by running every command on awkward repos."""

import asyncio
import subprocess
import time
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codewright import graph
from codewright.archmap import ArchMap
from codewright.cli import app
from codewright.intents import detect

FIXTURE = Path(__file__).parent / "fixtures" / "sample_app"


def test_detection_stays_linear_on_a_file_with_many_functions(tmp_path):
    """module_level_names() used to rescan the whole file per function: 72s for 30k functions."""
    (tmp_path / "gen.py").write_text("".join(f"def f{i}(x):\n    return x + {i}\n" for i in range(4000)))
    g = graph.build(tmp_path)
    t = time.perf_counter()
    detect(g)
    assert time.perf_counter() - t < 5
    assert "module_level_names" in g.files["gen.py"].cache


def test_map_views_explain_what_is_missing(tmp_path):
    empty = ArchMap(graph.build(tmp_path))
    with pytest.raises(ValueError, match="no components"):
        empty.view("module")
    with pytest.raises(ValueError, match="no entry points"):
        empty.view("flow")
    m = ArchMap(graph.build(FIXTURE))
    with pytest.raises(ValueError, match="unknown component 'nope'; available: app"):
        m.view("module", "nope")
    with pytest.raises(ValueError, match="unknown entry point"):
        m.view("flow", "nope")
    assert m.view("flow").nodes and m.view("module").nodes


def test_cli_reports_expected_errors_without_a_traceback(tmp_path):
    runner = CliRunner()
    r = runner.invoke(app, ["map", str(tmp_path), "--level", "module"])
    assert r.exit_code == 1 and "no components" in r.output and "Traceback" not in r.output
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "a.py").write_text("x = 1\n")
    r = runner.invoke(app, ["review", str(tmp_path), "--no-proof", "--base", "nope-ref"])
    assert r.exit_code == 2 and "can't compare with git ref 'nope-ref'" in r.output
    assert r.exception is None or isinstance(r.exception, SystemExit)


def test_mcp_errors_reach_the_agent_as_messages():
    from mcp.server.mcpserver.exceptions import ToolError

    from codewright import mcp_server

    with pytest.raises(ToolError, match="not a folder: /nope/missing"):
        mcp_server.review("/nope/missing")
    with pytest.raises(ToolError, match="unknown entry point 'x'"):
        mcp_server.architecture_map(str(FIXTURE), level="flow", target="x")
    schema = {t.name: t for t in asyncio.run(mcp_server.server.list_tools())}["review"].input_schema
    assert {"path", "base", "proof", "top"} <= set(schema["properties"])  # wrapping kept the signature


def test_starter_pyproject_is_valid_and_tests_with_httpx2(tmp_path):
    import io
    import zipfile

    from fastapi.testclient import TestClient

    from codewright.server import create_app

    c = TestClient(create_app([]), base_url="http://localhost")
    sid = c.post("/api/design").json()["session"]["id"]
    for _ in range(40):
        s = c.get(f"/api/design/{sid}").json()
        q = s.get("question")
        if s.get("done") or not q:
            break
        v = q["default"] if q.get("default") not in (None, "") else (q["options"] or ["skip"])[0]
        c.post(f"/api/design/{sid}/answer", json={"qid": q["id"], "value": [v] if q["kind"] == "multi" else v})
    cid = c.post(f"/api/design/{sid}/propose", json={}).json()["candidates"][0]["id"]
    z = zipfile.ZipFile(io.BytesIO(c.get(f"/api/design/{sid}/starter.zip", params={"candidate": cid}).content))
    raw = z.read(next(n for n in z.namelist() if n.endswith("pyproject.toml"))).decode()
    data = tomllib.loads(raw)
    dev = data["project"]["optional-dependencies"]["dev"]
    assert any(d.startswith("httpx2==") for d in dev) and any(d.startswith("pytest==") for d in dev)
    assert not any(d.startswith(("httpx2", "pytest")) for d in data["project"]["dependencies"])
    dep_lines = [ln for ln in raw.splitlines() if ln.strip().startswith('"') and "==" in ln and "dev" not in ln]
    assert {len(ln) - len(ln.lstrip()) for ln in dep_lines} == {4}  # one consistent indent
