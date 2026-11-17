"""Performance proof: a verified patch is timed before and after, and the verdict is honest about noise."""

import sys
import textwrap
from pathlib import Path

from codewright import perf, rank
from codewright.engine import review
from codewright.models import Finding


def write(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body).lstrip())
    return tmp_path


def test_judge_needs_more_than_noise():
    assert perf.judge(10_000_000, 4_000_000) == ("faster", 2.5)
    assert perf.judge(10_000_000, 9_500_000)[0] == "no-change"
    assert perf.judge(4_000_000, 10_000_000)[0] == "slower"
    assert perf.judge(100_000, 10_000)[0] == "too-small"  # 0.1 ms is below what we can time


def test_summary_reads_like_a_measurement():
    m = perf.Measurement("faster", 12.0, 4.0, 300, 1, 1.5, 1.2, 3, 3.0)
    s = m.summary("price")
    assert s.startswith("measured 3.0× faster") and "12.0 ms → 4.0 ms in `price`" in s and "300 → 1 calls" in s
    assert "best of 3" in s


def test_measured_speedup_ranks_higher():
    def f(perf_=None):
        return Finding("x", "i", "a.py", 1, "f", "t", "", "", "", "", "", 1, 2, "", "", "", ["s"], "", perf=perf_)

    plain, fast, slow = f(), f({"verdict": "faster", "speedup": 4.0}), f({"verdict": "slower", "speedup": 0.5})
    assert rank.score(fast) > rank.score(plain) > rank.score(slow)


def test_bench_times_the_patched_function(tmp_path):
    write(
        tmp_path,
        {
            "m.py": """
            import functools

            _CACHE = {}


            def square(n):
                if n in _CACHE:
                    return _CACHE[n]
                total = sum(i * i for i in range(n))
                _CACHE[n] = total
                return total
            """,
            "tests/test_m.py": """
            from m import square

            def test_square():
                for _ in range(20000):
                    assert square(50) == 40425
            """,
        },
    )
    r = review(tmp_path, python=sys.executable, top=None, bench=True, bench_rounds=2)
    (f,) = [f for f in r.findings if f.intent == "memo-cache"]
    assert f.proof.value == "verified", f.proof_detail
    assert f.perf is not None and f.perf["rounds"] == 2, f.proof_detail
    assert f.perf["calls_before"] == 20000 and f.perf["calls_after"] == 1  # lru_cache answers without entering it
    assert f.perf["verdict"] in ("faster", "no-change", "too-small")
    assert "Speed:" in f.proof_detail
