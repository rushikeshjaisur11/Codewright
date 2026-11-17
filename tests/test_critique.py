"""Proposal review: a plan is judged against the repo it would change, with evidence for every point."""

import json
import textwrap
from pathlib import Path

import pytest

from codewright import critique, graph, stack


def write(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body).lstrip())
    return tmp_path


CALLERS = "\n".join(f"def c{i}():\n    return charge(1)\n" for i in range(6))


@pytest.fixture
def repo(tmp_path):
    write(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "shop"\nversion = "0"\nrequires-python = ">=3.12"\n'
            'dependencies = ["httpx", "pydantic", "tenacity"]\n',
            "uv.lock": '[[package]]\nname = "httpx"\nversion = "0.27.0"\n\n[[package]]\nname = "pydantic"\n'
            'version = "2.7.0"\n\n[[package]]\nname = "tenacity"\nversion = "8.2.0"\n',
            "app/__init__.py": "",
            "app/api/__init__.py": "",
            "app/api/routes.py": "def list_orders():\n    return []\n",
            "app/db/__init__.py": "",
            "app/db/models.py": "class Order:\n    pass\n",
            "app/billing.py": "def charge(amount):\n    return amount\n\n" + CALLERS,
            ".codewright/rules.yaml": (
                "rules:\n  - {id: api-no-db, kind: forbid-import, in: app/api, import: app/db/**}\n"
            ),
            ".codewright/design.json": json.dumps({"style": "Modular monolith", "expected_kinds": ["database"]}),
        },
    )
    return tmp_path


PLAN = """
# Export orders

1. Add `requests` to call the partner API from app/api/routes.py.
2. Write our own retry helper with exponential backoff around the partner call.
3. Read orders straight from app/db/models.py in app/api/routes.py.
4. Change `charge()` to accept a currency.
5. Use pytz for time zones and pydantic 1.10 models for the payload.
6. Add a new column `exported_at` to the orders table.
7. Push export jobs to Kafka; run 3 replicas with an in-memory cache of partner tokens.
8. Write the export into app/export/writer.py.
"""


def review(repo, text=PLAN, **kw):
    return critique.review(text, repo, graph.build(repo), stack.discover(repo), **kw)


def titles(r):
    return [p["title"] for p in r["points"]]


def test_plan_review_points(repo):
    r = review(repo)
    t = "\n".join(titles(r))
    assert "Plan adds `requests`; the repo already uses `httpx`" in t
    assert "Already available" in t and any("tenacity" in p["detail"] for p in r["points"] if p["topic"] == "reuse")
    assert "rule `api-no-db` fails" in t
    assert "`charge` has 6 callers and no test" in t
    assert "`pytz` isn't needed: use `zoneinfo`" in t
    assert "Plan assumes pydantic 1.10; the lockfile has 2.7.0" in t
    assert "Schema change without a migration" in t
    assert "The saved design has no queue" in t
    assert "In-memory cache with several workers" in t
    assert "app/export/writer.py" not in t  # new file, said so
    assert r["verdict"] == "revise" and r["score"] < 50


def test_constraint_makes_new_dependency_a_blocker(repo):
    r = review(repo, "Add `requests` for the partner call. Tests: test_export.py.", constraints=["No new dependencies"])
    assert r["counts"]["blocker"] == 1 and r["verdict"] == "revise"


def test_a_good_plan_goes(repo):
    r = review(repo, "Use the httpx client to call the partner API from app/api/routes.py. Add pytest tests for it.")
    assert r["verdict"] == "go" and r["counts"]["blocker"] == 0


def test_missing_file_is_flagged(repo):
    r = review(repo, "Refactor app/services/orders.py to batch queries. Covered by tests.")
    assert "`app/services/orders.py` doesn't exist" in titles(r)
