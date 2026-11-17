"""Learning from feedback: per-area weights, decay, and insights the Overview shows."""

import time

from codewright.feedback import HALF_LIFE_DAYS, Feedback, Verdict, area
from codewright.models import Finding


def finding(fid="x", intent="retry", file="app/api/a.py") -> Finding:
    return Finding(fid, intent, file, 1, "f", "Use tenacity", "", "", "", "", "", 1, 1, "", "", "", [], "")


def verdicts(*rows):
    return {
        f"v{i}": Verdict(s, intent, file, "Use tenacity", reason, at)
        for i, (s, intent, file, reason, at) in enumerate(rows)
    }


def test_area_is_the_top_two_folders():
    assert area("app/api/v1/x.py") == "app/api" and area("x.py") == "" and area("tests/t.py") == "tests"


def test_weight_prefers_verdicts_from_the_same_area():
    now = time.time()
    fb = Feedback(
        verdicts=verdicts(
            ("rejected", "retry", "scripts/a.py", "", now),
            ("rejected", "retry", "scripts/b.py", "", now),
            ("rejected", "retry", "scripts/c.py", "", now),
            ("accepted", "retry", "app/api/a.py", "", now),
            ("accepted", "retry", "app/api/b.py", "", now),
        )
    )
    assert fb.weight_for(finding(file="app/api/z.py"), now) > 1.0 > fb.weight_for(finding(file="scripts/z.py"), now)


def test_old_verdicts_count_less():
    now = time.time()
    old = now - 3 * HALF_LIFE_DAYS * 86400
    fresh = Feedback(
        verdicts=verdicts(*[("rejected", "retry", "a/b.py", "", now)] * 1, ("rejected", "retry", "a/c.py", "", now))
    )
    stale = Feedback(
        verdicts=verdicts(("rejected", "retry", "a/b.py", "", old), ("rejected", "retry", "a/c.py", "", old))
    )
    assert fresh.weight_for(finding(), now) < stale.weight_for(finding(), now) < 1.0


def test_insights_suggest_scoped_mutes_trust_and_reasons():
    now = time.time()
    fb = Feedback(
        verdicts=verdicts(
            ("rejected", "retry", "scripts/a.py", "Not worth the churn", now),
            ("rejected", "retry", "scripts/b.py", "not worth the churn.", now),
            ("rejected", "retry", "scripts/c.py", "one-off script", now),
            ("rejected", "retry", "app/x/c.py", "", now),
            ("accepted", "memo-cache", "app/a.py", "", now),
            ("accepted", "memo-cache", "app/b.py", "", now),
            ("accepted", "memo-cache", "app/c.py", "", now),
        )
    )
    got = {i["kind"]: i for i in fb.insights()}
    assert got["mute"]["intent"] == "retry" and got["mute"]["path"] == "scripts/"
    assert "rejected 4 of 4" in got["mute"]["message"] and "Mute it under `scripts/`" in got["mute"]["message"]
    assert got["mute"]["reasons"][0] == {"reason": "Not worth the churn", "count": 2}
    assert got["trusted"]["intent"] == "memo-cache"
    assert got["reasons"]["reasons"][0]["count"] == 2
    fb.mute("retry", "scripts/")
    assert "mute" not in {i["kind"] for i in fb.insights()}  # already done
