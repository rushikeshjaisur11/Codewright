"""Making sure an upgrade does not break the repo: every use checked against the target version, the code
changes written for what breaks, and the tests run before and after in a sandbox."""

import textwrap
import time

from codewright import llm, proof, proof_env, upgradesafety
from codewright.engine import review

OLD = {
    "httpx.Client": {
        "exists": True,
        "kind": "type",
        "signature": "(*, proxies=None, app=None, timeout=5)",
        "params": ["proxies", "app", "timeout"],
        "var_kw": False,
        "doc": "",
        "deprecated": None,
    },
    "httpx.get": {"exists": True, "kind": "function", "signature": "(url)", "params": ["url"], "var_kw": False},
}
NEW = {
    "httpx.Client": {
        "exists": True,
        "kind": "type",
        "signature": "(*, proxy=None, timeout=5)",
        "params": ["proxy", "timeout"],
        "var_kw": False,
        "doc": "An HTTP client.",
        "deprecated": None,
    },
    "httpx.get": {"exists": True, "kind": "function", "signature": "(url)", "params": ["url"], "var_kw": False},
}


def _repo(tmp_path):
    (tmp_path / "requirements.txt").write_text("httpx==0.27.2\n")
    (tmp_path / "client.py").write_text(
        textwrap.dedent("""
        import httpx


        def fetch(url):
            with httpx.Client(proxies="http://p:1", timeout=3) as c:
                return c.get(url)


        def ping(url):
            return httpx.get(url)
    """)
    )
    return tmp_path


def _fake_envs(monkeypatch):
    monkeypatch.setattr(upgradesafety, "target_env", lambda *a, **k: "NEW")
    monkeypatch.setattr(proof_env, "resolve", lambda root, py=None, build=True: proof_env.TestEnv("OLD", "test"))
    monkeypatch.setattr(
        upgradesafety,
        "inspect_symbols",
        lambda py, syms: {s: (NEW if py == "NEW" else OLD).get(s, {"exists": True}) for s in syms},
    )


def test_usage_sites_find_calls_with_their_keywords(tmp_path):
    root = _repo(tmp_path)
    r = review(root, run_proof=False, top=None)
    sites = upgradesafety.usage_sites(r.graph, "httpx")
    calls = {(s["function"], s["symbol"]): s for s in sites if s["kind"] == "call"}
    assert calls[("fetch", "httpx.Client")]["kwargs"] == ["proxies", "timeout"]
    assert ("ping", "httpx.get") in calls
    assert any(s["kind"] == "import" for s in sites)


def test_check_uses_flags_the_keyword_the_new_version_dropped(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    r = review(root, run_proof=False, top=None)
    _fake_envs(monkeypatch)
    out = upgradesafety.check_uses(root, r.graph, r.stack, "httpx", "0.28.1")
    keyword = [p for p in out["problems"] if p["issue"] == "keyword"]
    assert len(keyword) == 1 and keyword[0]["function"] == "fetch" and "`proxies=`" in keyword[0]["detail"]
    assert keyword[0]["new_signature"] == "(*, proxy=None, timeout=5)"
    assert out["breaking"] == 1 and "1 will break" in out["summary"]
    assert not [p for p in out["problems"] if p["symbol"] == "httpx.get"]  # unchanged: nothing to say


def test_rewrite_writes_a_compiling_patch_per_function(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    r = review(root, run_proof=False, top=None)
    _fake_envs(monkeypatch)
    problems = upgradesafety.check_uses(root, r.graph, r.stack, "httpx", "0.28.1")["problems"]
    monkeypatch.setattr(llm, "status", lambda cfg: {"available": True, "reason": ""})
    asked = []

    def complete(cfg, system, messages, **k):
        asked.append(messages[0]["content"])
        return (
            "Use proxy= instead of proxies=.\n```python\ndef fetch(url):\n"
            '    with httpx.Client(proxy="http://p:1", timeout=3) as c:\n        return c.get(url)\n```'
        )

    monkeypatch.setattr(llm, "complete", complete)
    out = upgradesafety.rewrite(root, r.graph, "httpx", "0.27.2", "0.28.1", problems)
    assert "new signature: httpx.Client(*, proxy=None, timeout=5)" in asked[0]  # grounded in the real signature
    [p] = out["patches"]
    assert p["file"] == "client.py" and p["function"] == "fetch" and p["explanation"].startswith("Use proxy=")
    assert '-    with httpx.Client(proxies="http://p:1"' in p["patch"] and "+    with httpx.Client(proxy=" in p["patch"]
    assert proof.apply_patch(root, p["patch"]) and 'proxy="http://p:1"' in (root / "client.py").read_text()


def test_rewrite_refuses_code_that_does_not_compile(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    r = review(root, run_proof=False, top=None)
    _fake_envs(monkeypatch)
    problems = upgradesafety.check_uses(root, r.graph, r.stack, "httpx", "0.28.1")["problems"]
    monkeypatch.setattr(llm, "status", lambda cfg: {"available": True, "reason": ""})
    monkeypatch.setattr(llm, "complete", lambda *a, **k: "x\n```python\ndef fetch(url:\n    pass\n```")
    out = upgradesafety.rewrite(root, r.graph, "httpx", "0.27.2", "0.28.1", problems)
    assert out["patches"] == [] and out["skipped"][0]["function"] == "fetch"


def _fake_tests(monkeypatch, root):
    """Today's env runs the code as is; the upgraded env fails while `proxies=` is still passed."""
    monkeypatch.setattr(proof, "has_tests", lambda r: True)
    monkeypatch.setattr(
        proof_env, "resolve", lambda r, py=None, build=True: proof_env.TestEnv("OLD" if r == root else "NEW", "t")
    )

    def run_tests(work, python, timeout, fail_fast=True, **k):
        broken = python == "NEW" and "proxies=" in (work / "client.py").read_text()
        out = "FAILED tests/test_client.py::test_fetch - TypeError\n1 failed, 1 passed" if broken else "2 passed"
        return proof.TestRun(not broken, 0.1, out, 1 if broken else 0)

    monkeypatch.setattr(proof, "run_tests", run_tests)


def test_trial_shows_what_breaks_and_that_the_patch_fixes_it(tmp_path, monkeypatch):
    root = _repo(tmp_path).resolve()
    _fake_tests(monkeypatch, root)
    bad = upgradesafety.trial(root, "httpx", "0.28.1", [])
    assert bad["before"]["passed"] == 2 and bad["new_failures"] == ["tests/test_client.py::test_fetch"]
    assert not bad["safe"]
    patch = (
        "--- a/client.py\n+++ b/client.py\n@@ -5,3 +5,3 @@\n def fetch(url):\n"
        '-    with httpx.Client(proxies="http://p:1", timeout=3) as c:\n'
        '+    with httpx.Client(proxy="http://p:1", timeout=3) as c:\n         return c.get(url)\n'
    )
    good = upgradesafety.trial(root, "httpx", "0.28.1", [patch])
    assert good["safe"] and good["new_failures"] == [] and good["patches_applied"] == 1
    assert "proxies=" in (root / "client.py").read_text()  # the repo itself is untouched
    assert (root / "requirements.txt").read_text() == "httpx==0.27.2\n"


def test_safety_endpoint_runs_the_check_in_the_background(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from codewright.server import create_app

    root = _repo(tmp_path)
    _fake_envs(monkeypatch)
    c = TestClient(create_app([str(root)]), base_url="http://localhost")
    wid = c.get("/api/workspaces").json()[0]["id"]
    s = c.post(f"/api/w/{wid}/upgrade/safety", json={"step": "check", "name": "httpx", "version": "0.28.1"}).json()
    assert s["key"] == "httpx==0.28.1"
    for _ in range(100):
        s = c.get(f"/api/w/{wid}/upgrade/safety").json()
        if not s["running"]:
            break
        time.sleep(0.1)
    assert s["error"] is None and s["check"]["breaking"] == 1
    s = c.post(f"/api/w/{wid}/upgrade/safety", json={"step": "rewrite", "name": "httpx", "version": "0.28.1"}).json()
    for _ in range(100):
        s = c.get(f"/api/w/{wid}/upgrade/safety").json()
        if not s["running"]:
            break
        time.sleep(0.1)
    assert "Settings" in s["error"] and s["check"]["breaking"] == 1  # no model: says so, keeps the check


# ---------------------------------------------------------------- without the repo's tests


def _lib_repo(tmp_path):
    """A repo with no tests that uses `fakelib`; the "upgrade" is a patch that changes fakelib's behaviour."""
    (tmp_path / "requirements.txt").write_text("fakelib==1.0\n")
    (tmp_path / "fakelib").mkdir()
    (tmp_path / "fakelib" / "__init__.py").write_text(
        "import json\n\n\ndef encode(d):\n    return json.dumps(d)\n\n\ndef ok():\n    return 1\n"
    )
    (tmp_path / "app.py").write_text("import fakelib\n\n\ndef body(d):\n    return fakelib.encode(d).encode()\n")
    (tmp_path / "other.py").write_text("from fakelib import ok\n\n\ndef one():\n    return ok()\n")
    return tmp_path


UPGRADE = (  # the new fakelib: compact JSON, and `ok` is gone
    "--- a/fakelib/__init__.py\n+++ b/fakelib/__init__.py\n@@ -1,9 +1,5 @@\n import json\n \n \n"
    " def encode(d):\n-    return json.dumps(d)\n-\n-\n-def ok():\n-    return 1\n"
    '+    return json.dumps(d, separators=(",", ":"))\n'
)


def _no_tests(tmp_path, monkeypatch):
    """No tests; today's env is this Python, the upgraded one the same Python under another path."""
    import os
    import sys

    root = _lib_repo(tmp_path).resolve()
    monkeypatch.setattr(proof, "has_tests", lambda r: False)
    after_py = os.path.join(tmp_path, "py-after")
    os.symlink(sys.executable, after_py)

    def resolve(r, py=None, build=True):
        return proof_env.TestEnv(sys.executable if r == root else after_py, "t")

    monkeypatch.setattr(proof_env, "resolve", resolve)
    return root


BODY = 'from app import body\nrecord("body: dict", lambda: body({"a": 1}))\n'


def test_trial_without_tests_compares_imports_and_behaviour(tmp_path, monkeypatch):
    root = _no_tests(tmp_path, monkeypatch)
    r = review(root, run_proof=False, top=None)
    targets = upgradesafety.probe_targets(r.graph, "fakelib")
    assert {t["function"] for t in targets} == {"body", "one"}
    probes = [
        {
            "file": "app.py",
            "function": "body",
            "code": BODY + 'record("body: empty", lambda: body({}))\n',
        },
    ]
    out = upgradesafety.trial(root, "fakelib", "2.0", [UPGRADE], graph=r.graph, probes=probes)
    assert out["tests_ran"] is False and out["patches_applied"] == 1
    assert out["imports"]["modules"] == 2
    assert [b["module"] for b in out["imports"]["broken"]] == ["other"]  # `from fakelib import ok` fails after
    [changed] = out["behaviour"]["changed"]  # the empty dict encodes the same; the other doesn't
    assert changed["scenario"] == "body: dict"
    assert changed["before"] == {"returned": {"bytes": '{"a": 1}'}} and changed["after"] == {
        "returned": {"bytes": '{"a":1}'}
    }
    assert not out["safe"] and len(out["reasons"]) == 2
    probs = upgradesafety.behaviour_problems(r.graph, "fakelib", "2.0", out, probes)
    assert (
        probs[0]["function"] == "body" and probs[0]["issue"] == "behaviour" and "today's result" in probs[0]["detail"]
    )


def test_trial_without_tests_is_safe_when_nothing_changes(tmp_path, monkeypatch):
    root = _no_tests(tmp_path, monkeypatch)
    r = review(root, run_proof=False, top=None)
    probes = [
        {
            "file": "app.py",
            "function": "body",
            "code": 'from app import body\nrecord("body: dict", lambda: body({"a": 1}))\n',
        }
    ]
    out = upgradesafety.trial(root, "fakelib", "2.0", [], graph=r.graph, probes=probes)
    assert out["safe"] and out["behaviour"]["changed"] == [] and out["imports"]["broken"] == []
    assert "1 call(s) in 1 function(s) compared" in out["evidence"]


def test_write_probes_asks_for_inputs_only(tmp_path, monkeypatch):
    root = _lib_repo(tmp_path)
    r = review(root, run_proof=False, top=None)
    monkeypatch.setattr(llm, "status", lambda cfg: {"available": True, "reason": ""})
    seen = []
    monkeypatch.setattr(
        llm,
        "complete",
        lambda cfg, system, msgs, **k: seen.append(system) or '```python\nrecord("body: x", lambda: 1)\n```',
    )
    out = upgradesafety.write_probes(root, r.graph, "fakelib", upgradesafety.probe_targets(r.graph, "fakelib"))
    assert len(out["probes"]) == 2 and "Never assert" in seen[0]
    monkeypatch.setattr(llm, "status", lambda cfg: {"available": False, "reason": "no key"})
    assert "no key" in upgradesafety.write_probes(root, r.graph, "fakelib", [])["why"]
