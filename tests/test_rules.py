"""Architecture rules: each kind catches what it names, stays quiet otherwise, and suggests a way round."""

import textwrap
from pathlib import Path

import pytest

from codewright import graph, rules


def write(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body).lstrip())
    return tmp_path


REPO = {
    "app/__init__.py": "",
    "app/api/__init__.py": "",
    "app/api/routes.py": """
        from app.db.models import User
        from app.services.users import get_user
        import stripe

        async def show(uid):
            import time
            time.sleep(1)
            return get_user(uid)
        """,
    "app/api/health.py": """
        import asyncio

        async def ping():
            await asyncio.sleep(0)
            return "ok"
        """,
    "app/services/__init__.py": "",
    "app/services/users.py": """
        from app.db.models import User

        def get_user(uid):
            return User(uid)
        """,
    "app/billing/__init__.py": "",
    "app/billing/charge.py": """
        import stripe

        def charge(amount):
            return stripe.Charge.create(amount=amount)
        """,
    "app/db/__init__.py": "",
    "app/db/models.py": """
        class User:
            def __init__(self, uid):
                self.uid = uid
        """,
    "app/db/audit.py": """
        from app.api.routes import show
        """,
}

RULES = """
rules:
  - id: api-no-db
    kind: forbid-import
    in: app/api
    import: app/db/**
    reason: routes go through services
  - id: stripe-in-billing
    kind: only-in
    import: stripe
    in: [app/billing]
  - id: layering
    kind: layers
    layers:
      - {name: api, paths: [app/api]}
      - {name: services, paths: [app/services]}
      - {name: db, paths: [app/db]}
  - id: async-io
    kind: no-blocking-in-async
    in: app
    severity: warn
"""


@pytest.fixture
def repo(tmp_path):
    write(tmp_path, REPO)
    write(tmp_path, {".codewright/rules.yaml": RULES})
    return tmp_path


def by_rule(vs):
    out = {}
    for v in vs:
        out.setdefault(v.rule, []).append(v)
    return out


def test_each_kind_reports_what_it_names(repo):
    vs = by_rule(rules.check(graph.build(repo), rules.load(repo)))
    (db,) = vs["api-no-db"]
    assert (db.file, db.line, db.target) == ("app/api/routes.py", 1, "app.db.models.User")
    assert "app/services/users.py" in db.fix  # the allowed module that already uses it
    (st,) = vs["stripe-in-billing"]
    assert st.file == "app/api/routes.py" and "app/billing/charge.py" in st.fix
    (layer,) = vs["layering"]
    assert layer.file == "app/db/audit.py" and "`db` imports" in layer.message
    (blocking,) = vs["async-io"]
    assert blocking.target == "time.sleep" and "asyncio.sleep" in blocking.fix and blocking.severity == "warn"


def test_summary_is_ok_only_without_errors(repo):
    s = rules.summary(graph.build(repo), repo)
    assert s["errors"] == 3 and s["warnings"] == 1 and not s["ok"]


def test_globs():
    assert rules.path_matches("app/api/x.py", ["app/api"])
    assert rules.path_matches("app/api/v1/x.py", ["app/api/**"])
    assert not rules.path_matches("app/api/v1/x.py", ["app/api/*.py"])
    assert rules.path_matches("x.py", ["**/*.py"])
    assert not rules.path_matches("app/apix/x.py", ["app/api"])


def test_bad_rule_is_a_clear_error(tmp_path):
    write(tmp_path, {".codewright/rules.yaml": "rules:\n  - {kind: nope}\n"})
    with pytest.raises(ValueError, match="expected one of"):
        rules.load(tmp_path)
    s = rules.summary(graph.build(tmp_path), tmp_path)
    assert s["error"] and not s["ok"]


def test_infer_freezes_todays_direction(tmp_path):
    files = dict(REPO)
    files.pop("app/db/audit.py")
    write(tmp_path, files)
    g = graph.build(tmp_path)
    inferred = rules.infer(g)
    ids = {r.id for r in inferred}
    assert "app.db-not-app.services" in ids and "app.db-not-app.api" in ids
    assert rules.check(g, inferred) == []  # today's code keeps them
    rules.save(tmp_path, inferred)
    write(tmp_path, {"app/db/audit.py": "from app.api.routes import show\n"})
    after = rules.check(graph.build(tmp_path), rules.load(tmp_path))
    assert [v.file for v in after] == ["app/db/audit.py"]


def test_design_forbid_import_still_applies(tmp_path):
    write(
        tmp_path,
        {
            "app/x.py": "import requests\n",
            ".codewright/design.json": '{"rules": [{"forbid_import": "requests", "reason": "httpx"}]}',
        },
    )
    (v,) = rules.check(graph.build(tmp_path), rules.load(tmp_path))
    assert v.target == "requests" and v.reason == "httpx"
