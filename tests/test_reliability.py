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


# ---------------------------------------------------------------- wall-clock timing must stay on the clock


@pytest.mark.parametrize(
    "src,flagged",
    [
        ("start = time.time()\n    fn()\n    return time.time() - start", True),
        (
            "deadline = time.time() + fn()\n    while time.time() < deadline:\n        pass\n    t0 = time.time()\n"
            "    return time.time() - t0",
            True,
        ),
        # Compared with a file's modification time: a real timestamp, perf_counter would break it (found in devmem).
        (
            "start = time.time()\n    old = time.time() - os.stat(fn).st_mtime > 60\n    return time.time() - start",
            False,
        ),
        ("start = time.time()\n    x = time.time() - start\n    return start", False),
        ("fn.started = time.time()\n    return time.time() - fn.started", False),
    ],
)
def test_wall_clock_only_flagged_when_every_value_is_a_duration(tmp_path, src, flagged):
    (tmp_path / "m.py").write_text(f"import os\nimport time\n\ndef f(fn):\n    {src}\n")
    assert any(m.intent == "wall-clock-timing" for m in detect(graph.build(tmp_path))) is flagged


# ---------------------------------------------------------------- proof: diagnosis, environment, targeting


def test_baseline_failures_say_what_really_happened():
    from codewright.proof import TestRun, diagnose

    missing = TestRun(False, 1.0, "E   ModuleNotFoundError: No module named 'devmem_app'\n", code=2)
    assert "can't import `devmem_app` with X" in diagnose(missing, "X") and "--python" in diagnose(missing, "X")
    failing = TestRun(False, 1.0, "FAILED tests/test_a.py::test_x - assert 1 == 2\n", code=1)
    assert diagnose(failing, "X") == "the test suite already fails before the change (`tests/test_a.py::test_x`)"
    assert "no tests" in diagnose(TestRun(False, 1.0, "", code=5), "X")
    assert "didn't finish" in diagnose(TestRun(False, 1.0, "timed out after 300s", code=None), "X")


def test_environment_prefers_the_repo_and_explains_fallbacks(tmp_path, monkeypatch):
    import sys

    from codewright import proof_env

    assert proof_env.resolve(tmp_path, "/usr/bin/python3").source == "--python"
    monkeypatch.setattr(proof_env.shutil, "which", lambda name: None)
    env = proof_env.resolve(tmp_path)
    assert env.python == sys.executable and "uv isn't installed" in env.note
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").symlink_to(sys.executable)
    assert proof_env.resolve(tmp_path).source == "the repo's .venv"


def test_patches_run_only_the_tests_that_reach_or_import_them(tmp_path):
    from codewright.engine import review

    files = {
        "a.py": "import time\n\ndef timed(fn):\n    start = time.time()\n    fn()\n    return time.time() - start\n",
        "b.py": "def other():\n    return 1\n",
        "tests/test_a.py": "from a import timed\n\ndef test_a():\n    assert timed(lambda: None) >= 0\n",
        "tests/test_b.py": "from b import other\n\ndef test_b():\n    assert other() == 1\n",
    }
    for name, body in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(body)
    import sys

    f = next(f for f in review(tmp_path, python=sys.executable, top=None).findings if f.intent == "wall-clock-timing")
    assert f.proof.value == "verified" and "ran the 1 test file(s)" in f.proof_detail


def test_tests_that_call_git_work_in_the_sandbox(tmp_path):
    import sys

    from codewright.engine import review

    (tmp_path / "a.py").write_text(
        "import time\n\ndef timed(fn):\n    start = time.time()\n    fn()\n    return time.time() - start\n"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(
        "import subprocess\nfrom a import timed\n\n"
        "def test_tracked():\n"
        "    assert 'a.py' in subprocess.run(['git', 'ls-files'], capture_output=True, text=True).stdout\n"
        "    assert timed(lambda: None) >= 0\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    f = next(f for f in review(tmp_path, python=sys.executable, top=None).findings if f.intent == "wall-clock-timing")
    assert f.proof.value == "verified", f.proof_detail
