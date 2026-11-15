import sys
import textwrap
from pathlib import Path

from codewright import cardcheck


def write_cards(tmp_path: Path, body: str) -> Path:
    d = tmp_path / "cards"
    d.mkdir()
    (d / "extra.yaml").write_text(textwrap.dedent(body))
    return d


CARD = """
- id: test.{id}
  intent: {intent}
  title: A card
  symbol: {symbol}
  kind: stdlib
  library: null
  versions: ">=3.8"
  suggestion: Do it.
  gain: Faster.
  effort: 1
  impact: 2
  risk: None.
  when_not: Never.
  threshold: Always.
  sources: [{sources}]
  snippet: "{snippet}"
"""


def test_bundled_cards_pass():
    assert cardcheck.verify().ok


def test_shape_problems_are_reported(tmp_path):
    d = write_cards(
        tmp_path,
        CARD.format(
            id="a", intent="no-such-intent", symbol="functools.lru_cache", sources="", snippet="def f(...): pass"
        ),
    )
    problems = "\n".join(cardcheck.verify(extra_dirs=[d]).problems)
    assert "test.a: no source cited" in problems
    assert "no detector produces intent 'no-such-intent'" in problems
    assert "test.a: snippet doesn't compile" in problems


def test_missing_symbols_fail_and_real_ones_pass(tmp_path):
    body = CARD.format(id="good", intent="reference", symbol="functools.lru_cache", sources="https://x", snippet="")
    body += CARD.format(id="bad", intent="reference", symbol="functools.no_such_cache", sources="https://x", snippet="")
    report = cardcheck.verify(extra_dirs=[write_cards(tmp_path, body)], python=sys.executable)
    assert any(p.startswith("test.bad: `functools.no_such_cache` doesn't exist") for p in report.problems)
    assert not any(p.startswith("test.good") for p in report.problems)


def test_dead_links_fail(tmp_path, monkeypatch):
    body = CARD.format(
        id="l", intent="reference", symbol="functools.lru_cache", sources="https://dead.example/x", snippet=""
    )
    monkeypatch.setattr(cardcheck, "_check_url", lambda url: "HTTP 404" if "dead" in url else None)
    report = cardcheck.verify(extra_dirs=[write_cards(tmp_path, body)], library="python", links=True)
    assert "test.l: source https://dead.example/x -> HTTP 404" in report.problems
    assert (
        cardcheck._fetchable("https://github.com/o/r/blob/main/a.py")
        == "https://raw.githubusercontent.com/o/r/main/a.py"
    )
