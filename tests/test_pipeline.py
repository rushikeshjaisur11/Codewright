import sys
import textwrap
from pathlib import Path

import pytest

from codewright import cards, critic, graph, stack
from codewright.engine import review
from codewright.intents import detect
from codewright.models import ProofStatus
from codewright.suggest import make_findings

FIXTURE = Path(__file__).parent / "fixtures" / "sample_app"


def write_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body).lstrip())
    return tmp_path


# ---------------------------------------------------------------- stack


def test_stack_prefers_lockfile_and_concrete_python():
    st = stack.discover(FIXTURE)
    assert st.version_of("google_adk") == "2.9.0"
    assert st.version_of("SQLAlchemy") == "2.0.36"
    assert st.dependencies["fastapi"].specifier == ">=0.110"
    assert st.python == "3.12"  # Dockerfile beats requires-python lower bound
    assert "uv.lock" in st.sources


def test_stack_requirements_pins(tmp_path):
    write_repo(tmp_path, {"requirements.txt": "requests==2.32.3  # http\ntenacity>=8\n-e .\n"})
    st = stack.discover(tmp_path)
    assert st.version_of("requests") == "2.32.3"
    assert st.version_of("tenacity") is None
    assert st.dependencies["tenacity"].specifier == ">=8"


# ---------------------------------------------------------------- graph


def test_graph_entry_points_and_hot_path():
    g = graph.build(FIXTURE)
    assert "app/api.py::get_rate" in g.entry_points
    hot = g.hot_functions()
    assert "app/rates.py::fetch_rate" in hot  # called from a route
    assert "app/worker.py::run_worker" not in hot
    mod = g.files["app/api.py"].module
    assert mod.imports["create_engine"] == "sqlalchemy.create_engine"


# ---------------------------------------------------------------- intents


def test_detectors_fire_on_fixture():
    intents = {(m.intent, m.function.qualname) for m in detect(graph.build(FIXTURE))}
    assert ("memo-cache", "fetch_rate") in intents
    assert ("retry", "call_with_retry") in intents
    assert ("poll-queue", "run_worker") in intents
    assert ("connection-per-request", "get_rate") in intents
    assert ("connection-per-request", "proxy") in intents
    assert ("agent-orchestrator", "<module>") in intents


def test_detectors_stay_quiet_on_plain_code(tmp_path):
    write_repo(
        tmp_path,
        {
            "m.py": """
        import time
        SEEN = {}

        def loop():
            for i in range(3):
                print(i)
            time.sleep(1)

        def lookup(k):
            return SEEN.get(k)
    """
        },
    )
    assert detect(graph.build(tmp_path)) == []


# ---------------------------------------------------------------- cards


ADK_LOOP = """
    from google.adk.runners import Runner

    async def drive(agents, runner):
        for agent in agents:
            async for event in runner.run_async(agent):
                print(event)
"""


@pytest.mark.parametrize("version,card_id", [("2.0.0", "adk.workflow"), ("2.9.0", "adk.workflow"), ("1.39.1", None)])
def test_adk_card_depends_on_installed_version(tmp_path, version, card_id):
    """ADK knowledge covers 2.x only: on 1.x nothing but the upgrade card applies."""
    write_repo(
        tmp_path,
        {
            "agent.py": ADK_LOOP,
            "uv.lock": f'version = 1\n\n[[package]]\nname = "google-adk"\nversion = "{version}"\n',
        },
    )
    g = graph.build(tmp_path)
    st = stack.discover(tmp_path)
    [match] = detect(g)
    best = cards.best_cards(match, st, cards.load())
    assert (best[0][0].id if best else None) == card_id


def test_adk_1x_repo_gets_only_the_upgrade_card(tmp_path):
    write_repo(
        tmp_path,
        {
            "agent.py": (
                "from google.adk.agents import SequentialAgent\nroot_agent = SequentialAgent(name='x', sub_agents=[])\n"
            ),
            "uv.lock": 'version = 1\n\n[[package]]\nname = "google-adk"\nversion = "1.20.0"\n',
        },
    )
    g = graph.build(tmp_path)
    findings = make_findings(detect(g), stack.discover(tmp_path), g, cards.load())
    assert [f.card_id for f in findings] == ["adk.upgrade-to-2"]


def test_installed_library_beats_new_dependency(tmp_path):
    write_repo(
        tmp_path,
        {
            "m.py": """
        import time

        def fetch(fn):
            for i in range(3):
                try:
                    return fn()
                except OSError:
                    time.sleep(1)
    """
        },
    )
    g = graph.build(tmp_path)
    [match] = detect(g)
    [(_, kind, api)] = cards.best_cards(match, stack.discover(tmp_path), cards.load())
    assert kind == "wellknown" and "not installed" in api
    (tmp_path / "requirements.txt").write_text("tenacity==9.0.0\n")
    [(_, kind, api)] = cards.best_cards(match, stack.discover(tmp_path), cards.load())
    assert kind == "installed" and "9.0.0" in api


# ---------------------------------------------------------------- patch + critic


def test_lru_cache_patch_skipped_when_dict_used_elsewhere(tmp_path):
    write_repo(
        tmp_path,
        {
            "m.py": """
        CACHE = {}

        def get(k):
            if k in CACHE:
                return CACHE[k]
            v = k * 2
            CACHE[k] = v
            return v

        def reset():
            CACHE.clear()
    """
        },
    )
    g = graph.build(tmp_path)
    [f] = make_findings(detect(g), stack.discover(tmp_path), g, cards.load())
    assert f.patch is None


def test_critic_drops_incomplete_and_hallucinated(tmp_path):
    g = graph.build(FIXTURE)
    findings = make_findings(detect(g), stack.discover(FIXTURE), g, cards.load())
    a, b = findings[0], findings[1]
    a.when_not = ""
    b.symbol = "functools.lru_cache_that_does_not_exist"
    critic.check_symbols([b], sys.executable, {b.id: b.symbol})
    assert b.symbol_check == "missing"
    kept, rejected = critic.review(findings)
    reasons = {f.id: f.rejected_reason for f in rejected}
    assert reasons[a.id] == "missing when_not"
    assert "does not exist" in reasons[b.id]
    assert len(kept) == len(findings) - 2


# ---------------------------------------------------------------- end to end


def test_review_proves_lru_cache_patch():
    result = review(FIXTURE, python=sys.executable, top=None)
    by_intent = {f.intent: f for f in result.findings}
    lru = by_intent["memo-cache"]
    assert lru.proof is ProofStatus.VERIFIED, lru.proof_detail
    assert lru.symbol_check == "ok"
    assert "+@functools.lru_cache(maxsize=1024)" in lru.patch
    # Proof raises confidence above every unproven finding (exact rank depends on what's installed).
    assert lru.confidence > max(f.confidence for f in result.findings if f is not lru)
    assert all(f.file and f.api and f.gain and f.when_not for f in result.findings)


def test_review_rejects_patch_that_breaks_tests(tmp_path):
    # The suite passes today but asserts the function is not wrapped, so the patch must fail it.
    write_repo(
        tmp_path,
        {
            "m.py": """
            _C = {}

            def get(k):
                if k in _C:
                    return _C[k]
                v = k * 2
                _C[k] = v
                return v
        """,
            "tests/test_m.py": """
            import m

            def test_not_wrapped():
                assert m.get(2) == 4
                assert not hasattr(m.get, "cache_info")
        """,
        },
    )
    result = review(tmp_path, python=sys.executable)
    assert result.findings == []
    assert [f.rejected_reason for f in result.rejected] == ["patch breaks the test suite"]
