"""Regression tests for failures found by running every command on awkward repos."""

import asyncio
import subprocess
import sys
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
    body = "".join(f"def f{i}(x):\n    if x in D:\n        return D[x]\n    return x + {i}\n" for i in range(4000))
    (tmp_path / "gen.py").write_text("D = {}\n" + body)  # `if x in D` makes the memo-cache detector run
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
    assert data["tool"]["setuptools"]["packages"]["find"]["include"] == ["my_service*"]  # not migrations/
    names = {n.split("/", 1)[1] for n in z.namelist()}
    assert {
        "alembic.ini",
        "migrations/env.py",
        "migrations/versions/0001_initial.py",
        "my_service/models.py",
        "tests/conftest.py",
        "tests/test_api.py",
        "tests/test_migrations.py",
    } <= names
    for n in z.namelist():
        if n.endswith(".py"):
            compile(z.read(n), n, "exec")


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


# ---------------------------------------------------------------- speed: triggers and the parse cache


def test_parse_cache_reuses_unchanged_files_and_reparses_edits(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    (tmp_path / "b.py").write_text("def g():\n    return 2\n")
    first = graph.build(tmp_path)
    (tmp_path / "b.py").write_text("def g():\n    return 3\n\ndef h():\n    return 4\n")
    second = graph.build(tmp_path)
    assert second.files["a.py"] is first.files["a.py"]  # unchanged: reused
    assert second.files["b.py"] is not first.files["b.py"]
    assert [fn.name for fn in second.files["b.py"].module.functions] == ["g", "h"]


def test_trigger_words_include_names_imported_under_another_name(tmp_path):
    (tmp_path / "m.py").write_text(
        "from time import sleep as pause\n\ndef call(fn):\n"
        "    for attempt in range(3):\n        try:\n            return fn()\n"
        "        except OSError:\n            pause(1)\n"
    )
    assert any(m.intent == "retry" for m in detect(graph.build(tmp_path)))


# ---------------------------------------------------------------- auto-patches that can't change behaviour


def _findings(tmp_path):
    from codewright import cards, stack
    from codewright.suggest import make_findings

    g = graph.build(tmp_path)
    return make_findings(detect(g), stack.discover(tmp_path), g, cards.load())


def test_os_system_patched_only_for_a_plain_command_whose_result_is_unused(tmp_path):
    (tmp_path / "m.py").write_text(
        "import os\n\n"
        "def build():\n    os.system('git status --short')\n\n"
        "def concat(name):\n    os.system('rm -rf ' + name)\n\n"
        "def piped():\n    os.system('ls | wc -l')\n\n"
        "def var():\n    os.system('echo $HOME')\n\n"
        "def used():\n    return os.system('make test')\n"
    )
    by_fn = {f.function: f for f in _findings(tmp_path) if f.intent == "os-system"}
    assert set(by_fn) == {"build", "concat", "piped", "var", "used"}
    assert '+    subprocess.run(["git", "status", "--short"], check=False)' in by_fn["build"].patch
    assert "+import subprocess" in by_fn["build"].patch
    assert all(by_fn[n].patch is None for n in ("concat", "piped", "var", "used"))


def test_os_system_patch_keeps_behaviour(tmp_path):
    (tmp_path / "m.py").write_text("import os\n\ndef touch():\n    os.system('touch made-by-patch')\n")
    f = next(f for f in _findings(tmp_path) if f.intent == "os-system")
    subprocess.run(["git", "apply", "-"], cwd=tmp_path, input=f.patch, text=True, check=True)
    subprocess.run([sys.executable, "-c", "import m; m.touch()"], cwd=tmp_path, check=True)
    assert (tmp_path / "made-by-patch").exists()


def test_save_live_audio_is_renamed(tmp_path):
    (tmp_path / "uv.lock").write_text('version = 1\n\n[[package]]\nname = "google-adk"\nversion = "2.14.1"\n')
    (tmp_path / "rc.py").write_text("from google.adk.agents import RunConfig\ncfg = RunConfig(save_live_audio=True)\n")
    f = next(f for f in _findings(tmp_path) if f.card_id == "adk.save-live-blob")
    assert "+cfg = RunConfig(save_live_blob=True)" in f.patch


# ---------------------------------------------------------------- SARIF and the GitHub Action


def test_sarif_output_from_the_same_run(tmp_path):
    import json

    out = tmp_path / "out.sarif"
    r = CliRunner().invoke(
        app,
        [
            "review",
            str(FIXTURE),
            "--no-proof",
            "--top",
            "0",
            "-f",
            "pr",
            "-o",
            str(tmp_path / "s.md"),
            "--sarif-output",
            str(out),
        ],
    )
    assert r.exit_code == 0, r.output
    run = json.loads(out.read_text())["runs"][0]
    assert json.loads(out.read_text())["version"] == "2.1.0" and run["tool"]["driver"]["name"] == "Codewright"
    rule_ids = {rule["id"] for rule in run["tool"]["driver"]["rules"]}
    assert run["results"] and all(res["ruleId"] in rule_ids for res in run["results"])
    loc = run["results"][0]["locations"][0]["physicalLocation"]
    assert loc["artifactLocation"]["uri"].startswith("app/") and loc["region"]["startLine"] >= 1
    assert all(res["level"] in ("note", "warning") for res in run["results"])


def test_action_passes_inputs_through_the_environment():
    import yaml

    action = yaml.safe_load((Path(__file__).parent.parent / "action.yml").read_text())
    review = next(s for s in action["runs"]["steps"] if s.get("id") == "review")
    assert "${{" not in review["run"]  # no expression interpolated into the script (injection-safe)
    assert {"REVIEW_PATH", "PR_ONLY", "PROOF", "BASE_REF"} <= set(review["env"])


def test_watch_reports_new_and_resolved_findings():
    from types import SimpleNamespace as F

    from codewright.cli import diff_findings

    seen, new, gone = diff_findings({}, [F(id="a"), F(id="b")])
    assert set(seen) == {"a", "b"} and [f.id for f in new] == ["a", "b"] and gone == []
    seen, new, gone = diff_findings(seen, [F(id="b"), F(id="c")])
    assert [f.id for f in new] == ["c"] and [f.id for f in gone] == ["a"]
