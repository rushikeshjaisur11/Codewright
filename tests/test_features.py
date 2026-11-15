import io
import subprocess
import sys
import textwrap
import time
import zipfile
from pathlib import Path

import pytest

from codewright import cards, design, graph, stack
from codewright.archmap import ArchMap
from codewright.chat import CodeIndex, answer
from codewright.diffscope import changed_lines
from codewright.engine import review
from codewright.feedback import Feedback
from codewright.intents import detect
from codewright.suggest import make_findings
from codewright.watcher import bump

FIXTURE = Path(__file__).parent / "fixtures" / "sample_app"


def write_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body).lstrip())
    return tmp_path


def findings_for(tmp_path: Path):
    g = graph.build(tmp_path)
    return {f.intent: f for f in make_findings(detect(g), stack.discover(tmp_path), g, cards.load())}


# ---------------------------------------------------------------- new detectors and auto-patches


ASYNC_SRC = """
    import time
    from datetime import datetime

    import requests
    from pydantic import BaseModel


    class Q(BaseModel):
        text: str


    async def handler(payload):
        time.sleep(0.1)
        q = Q.parse_obj(payload)
        r = requests.get("https://example.com")
        return q, datetime.utcnow(), r


    def timed(fn):
        start = time.time()
        fn()
        return time.time() - start
"""


def test_async_and_deprecation_patches(tmp_path):
    write_repo(tmp_path, {"m.py": ASYNC_SRC, "requirements.txt": "pydantic==2.9.0\nrequests==2.32.3\n"})
    f = findings_for(tmp_path)
    assert "+    await asyncio.sleep(0.1)" in f["blocking-sleep-in-async"].patch
    assert "+import asyncio" in f["blocking-sleep-in-async"].patch
    assert "+    q = Q.model_validate(payload)" in f["pydantic-v1-api"].patch
    utc = f["datetime-utcnow"].patch
    assert "+from datetime import datetime, timezone" in utc  # merged into the existing import
    assert "datetime.now(timezone.utc)" in utc
    assert f["wall-clock-timing"].patch.count("+") >= 2 and "time.perf_counter()" in f["wall-clock-timing"].patch
    assert f["blocking-http-in-async"].patch is None and "httpx" in f["blocking-http-in-async"].api


def test_pydantic_card_needs_v2(tmp_path):
    write_repo(tmp_path, {"m.py": ASYNC_SRC, "requirements.txt": "pydantic==1.10.13\n"})
    assert "pydantic-v1-api" not in findings_for(tmp_path)


def test_stdlib_reinventions(tmp_path):
    write_repo(
        tmp_path,
        {
            "m.py": """
        import os
        import threading


        def count(words):
            c = {}
            for w in words:
                if w in c:
                    c[w] += 1
                else:
                    c[w] = 1
            return c


        def group(items):
            g = {}
            for k, v in items:
                if k not in g:
                    g[k] = []
                g[k].append(v)
            return g


        def fan(urls):
            ts = []
            for u in urls:
                t = threading.Thread(target=print, args=(u,))
                t.start()
                ts.append(t)
            for t in ts:
                t.join()


        def run(cmd):
            os.system(cmd)


        def build(xs):
            out = ""
            for x in xs:
                out += str(x)
            return out
    """
        },
    )
    assert {"manual-counter", "manual-defaultdict", "manual-thread-fanout", "os-system", "string-concat-loop"} <= set(
        findings_for(tmp_path)
    )


def test_proof_requires_a_test_that_reaches_the_function(tmp_path):
    write_repo(
        tmp_path,
        {
            "m.py": """
            import time

            async def untested():
                time.sleep(0)
                return 1

            def timed(fn):
                start = time.time()
                fn()
                return time.time() - start
        """,
            "tests/test_m.py": """
            import m

            def test_timed():
                assert m.timed(lambda: None) >= 0
        """,
        },
    )
    r = review(tmp_path, python=sys.executable, top=None)
    by = {f.intent: f for f in r.findings}
    assert by["wall-clock-timing"].proof.value == "verified"
    assert by["blocking-sleep-in-async"].proof.value == "unverified"
    assert "no test reaches" in by["blocking-sleep-in-async"].proof_detail


# ---------------------------------------------------------------- feedback


def test_feedback_tunes_ranking_and_mutes(tmp_path):
    write_repo(tmp_path, {"m.py": ASYNC_SRC, "requirements.txt": "pydantic==2.9.0\n"})
    r1 = review(tmp_path, run_proof=False, top=None)
    target = next(f for f in r1.findings if f.intent == "wall-clock-timing")
    fb = Feedback.load(tmp_path)
    fb.record(target, "rejected", "not worth it")
    fb.mute("datetime-utcnow")
    fb.save(tmp_path)
    assert (tmp_path / ".codewright" / ".gitignore").read_text().count("snapshots/") == 1

    r2 = review(tmp_path, run_proof=False, top=None)
    again = next(f for f in r2.findings if f.intent == "wall-clock-timing")
    assert again.status == "rejected" and again.score < target.score
    assert not any(f.intent == "datetime-utcnow" for f in r2.findings)
    assert any(f.intent == "datetime-utcnow" for f in r2.muted)
    assert Feedback.load(tmp_path).stats()["acceptance_rate"] == 0.0


# ---------------------------------------------------------------- PR mode


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True, capture_output=True
    )


def test_pr_mode_reviews_only_changed_code(tmp_path):
    write_repo(
        tmp_path,
        {
            "a.py": """
        import time

        def old(fn):
            start = time.time()
            fn()
            return time.time() - start
    """
        },
    )
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    write_repo(
        tmp_path,
        {
            "b.py": """
        import os

        def run(cmd):
            os.system(cmd)
    """
        },
    )
    assert changed_lines(tmp_path, "HEAD") == {"b.py": {-1}}
    r = review(tmp_path, run_proof=False, top=None, base="HEAD")
    assert [f.intent for f in r.findings] == ["os-system"]


# ---------------------------------------------------------------- map and chat


def test_map_views():
    m = ArchMap(graph.build(FIXTURE))
    system = m.system()
    labels = {n.label for n in system.nodes}
    assert {"app.api", "app.rates", "PostgreSQL", "Google ADK agents"} <= labels
    assert any(e.source == "c:app.api" and e.target == "c:app.rates" for e in system.edges)
    assert system.mermaid().startswith("flowchart LR")
    flow = m.flow("app/api.py::get_rate")
    assert {n.label for n in flow.nodes} >= {"get_rate", "fetch_rate"}
    page = m.module_page("app/rates.py")
    assert page["imported_by"] == ["app.api"]
    assert any(f["name"] == "fetch_rate" and f["tested"] for f in page["functions"])


def test_chat_cites_code():
    idx = CodeIndex(graph.build(FIXTURE))
    flow = answer(idx, "How does a request flow through this repo?")
    assert "get_rate" in flow["answer"] and flow["citations"]
    db = answer(idx, "Where do we talk to the database?")
    assert "run_worker" in db["answer"]
    sym = answer(idx, "what does fetch_rate do")
    assert sym["citations"][0]["title"] == "fetch_rate"


# ---------------------------------------------------------------- design


def answered_session() -> design.Session:
    s = design.Session("t")
    for qid, value in [
        ("idea", "NL2SQL service"),
        ("name", "nl2sql"),
        ("users", "20k"),
        ("peak_rps", "skip"),
        ("data_gb", "50 GB"),
        ("latency_ms", "800"),
        ("availability", "99.9"),
        ("workload", ["background jobs", "LLM / agents"]),
        ("cloud", "GCP"),
        ("team", "4"),
        ("compliance", "PII"),
    ]:
        s.answer(qid, value)
    return s


def test_design_parsing_and_capacity():
    assert design.parse_number("20k") == 20000
    assert design.parse_size_gb("1.5 TB") == 1500
    assert design.parse_number("skip") is None
    with pytest.raises(ValueError):
        design.parse_answer(design.QMAP["cloud"], "mars")
    s = answered_session()
    assert s.next_question() is None
    cap = design.capacity(s)
    assert round(cap["peak_rps"], 1) == round(20000 * 50 / 86400 * 4, 1)
    assert cap["instances"] == 2  # ceil(46/150) + 1 spare for 99.9%
    s.overrides["rps_per_instance"] = 10
    assert design.capacity(s)["instances"] == 6


def test_design_proposal_and_starter_repo(tmp_path):
    s = answered_session()
    p = design.propose(s, live_versions=False)
    assert p["recommended"] == "api-worker"
    assert {c["id"] for c in p["candidates"]} == {"monolith", "api-worker", "services"}
    assert any("Compliance" in d["title"] for d in p["decisions"])
    z = zipfile.ZipFile(io.BytesIO(design.starter_repo(s, p, "api-worker")))
    names = z.namelist()
    assert "nl2sql/nl2sql/main.py" in names and "nl2sql/nl2sql/worker.py" in names
    assert any(n.startswith("nl2sql/docs/adr/0001-") for n in names)
    compile(z.read("nl2sql/nl2sql/main.py"), "main.py", "exec")
    compile(z.read("nl2sql/nl2sql/worker.py"), "worker.py", "exec")


def test_drift_against_live_map(tmp_path):
    s = answered_session()
    p = design.propose(s, live_versions=False)
    write_repo(
        tmp_path,
        {
            "svc/api.py": """
        import redis
        import requests

        def get(k):
            return redis.Redis().get(k), requests.get("https://x")
    """
        },
    )
    design.save_intended(tmp_path, design.intended(s, p, "monolith"))
    g = graph.build(tmp_path)
    d = design.drift(design.load_intended(tmp_path), ArchMap(g).system(), g)
    assert not d["ok"]
    assert d["unplanned"] == [{"kind": "cache", "systems": ["Redis"]}]
    assert d["violations"][0]["import"] == "requests"


# ---------------------------------------------------------------- watcher, server, MCP


def test_bump():
    assert bump("1.2.3", "2.0.0") == "major"
    assert bump("1.2.3", "1.3.0") == "minor"
    assert bump("1.2.3", "1.2.4") == "patch"
    assert bump("1.2.3", "1.2.3") == "current"
    assert bump(None, "1.0") == "unknown"


def test_web_api(tmp_path):
    from fastapi.testclient import TestClient

    from codewright.server import create_app

    repo = write_repo(tmp_path, {"m.py": ASYNC_SRC, "requirements.txt": "pydantic==2.9.0\n"})
    client = TestClient(create_app([str(repo)]))
    wid = client.get("/api/status").json()["workspaces"][0]["id"]
    client.post(f"/api/w/{wid}/review", json={"proof": False, "base": ""})
    for _ in range(100):
        data = client.get(f"/api/w/{wid}/review").json()
        if data["result"] and not data["running"]:
            break
        time.sleep(0.1)
    findings = data["result"]["findings"]
    assert findings
    fid = findings[0]["id"]
    stats = client.post(f"/api/w/{wid}/findings/{fid}/feedback", json={"status": "accepted"}).json()
    assert stats["accepted"] == 1
    assert client.get(f"/api/w/{wid}/map/system").json()["nodes"]
    assert client.post(f"/api/w/{wid}/chat", json={"question": "where is handler", "history": []}).json()["citations"]
    assert client.get(f"/api/w/{wid}/source", params={"path": "../../etc/passwd"}).status_code == 404

    st = client.post("/api/design").json()
    sid = st["session"]["id"]
    while st["question"]:
        q = st["question"]
        if q["kind"] == "multi":
            value = []
        elif q["kind"] == "choice":
            value = q["options"][0]
        else:
            value = "10" if q["kind"] in ("number", "size") else "demo"
        st = client.post(f"/api/design/{sid}/answer", json={"qid": q["id"], "value": value}).json()
    assert st["done"]
    zipped = client.get(f"/api/design/{sid}/starter.zip")
    assert zipped.status_code == 200 and zipped.content[:2] == b"PK"
    saved = client.post(f"/api/design/{sid}/save", json={"workspace": wid, "candidate": ""}).json()
    assert saved["saved"] == ".codewright/design.json"


def test_mcp_tools_registered():
    import asyncio

    from codewright.mcp_server import server

    tools = asyncio.run(server.list_tools())
    assert {"review", "architecture_map", "ask_code", "check_drift", "record_feedback"} <= {t.name for t in tools}
