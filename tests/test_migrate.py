"""Planned migrations: leaf modules first, small steps, combined patches, progress that follows the code."""

import textwrap
from pathlib import Path

import pytest

from codewright import graph, migrate, stack

LOCK = '[[package]]\nname = "pydantic"\nversion = "1.10.13"\n'


def write(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body).lstrip())
    return tmp_path


def model(name: str, imports: str = "") -> str:
    return f"""
    {imports}
    from pydantic import BaseModel, validator


    class {name}(BaseModel):
        x: int

        @validator("x")
        def check_x(cls, v):
            return v
    """


@pytest.fixture
def repo(tmp_path):
    write(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "shop"\nversion = "0"\ndependencies = ["pydantic<2"]\n',
            "uv.lock": LOCK,
            "app/__init__.py": "",
            "app/base.py": model("Base"),
            "app/orders.py": model("Order", "from app.base import Base"),
            "app/api.py": model("Req", "from app.orders import Order"),
            "tests/test_orders.py": "from app.orders import Order\n\ndef test_o():\n    assert Order(x=1).x == 1\n",
        },
    )
    return tmp_path


def build(root):
    return graph.build(root), stack.discover(root)


def test_plan_orders_leaves_first_and_reviews_at_the_target(repo):
    g, st = build(repo)
    assert st.version_of("pydantic") == "1.10.13"
    data = migrate.plan("pydantic-v2", repo, g, st, batch=1)
    assert data["target"] == "2.0" and data["total"] == 3
    assert [s["files"] for s in data["steps"]] == [["app/base.py"], ["app/orders.py"], ["app/api.py"]]
    assert data["steps"][1]["depends_on"] == [1] and data["steps"][2]["depends_on"] == [2]
    assert data["steps"][0]["tests"] == ["tests/test_orders.py"]  # imports base through orders
    assert all(i["autofix"] for s in data["steps"] for i in s["items"])


def test_status_follows_the_code_and_patches_combine(repo):
    g, st = build(repo)
    migrate.save(repo, migrate.plan("pydantic-v2", repo, g, st, batch=2))
    assert migrate.saved(repo) == ["pydantic-v2"]
    _, found = migrate.step_findings(repo, "pydantic-v2", 1, g, st)
    diff, skipped = migrate.combined_patch(repo, found)
    assert skipped == [] and "field_validator" in diff and diff.count("+++ b/") == 2
    from codewright.proof import apply_patch

    assert apply_patch(repo, diff)
    s = migrate.status(repo, "pydantic-v2", *build(repo))
    assert s["steps"][0]["done"] and not s["steps"][1]["done"]
    assert s["next_step"] == 2 and s["progress"] == round(2 / 3, 3)
    body = migrate.pr_body(s, s["steps"][1], "x")
    assert "step 2 of 2" in body and "No test imports" in body  # api.py has no tests
    assert "pytest -q tests/test_orders.py" in migrate.pr_body(s, s["steps"][0], "x")


def test_overview_lists_what_applies(repo):
    rows = {r["name"]: r for r in migrate.overview(*build(repo), repo)}
    assert rows["pydantic-v2"]["places"] == 3 and rows["pydantic-v2"]["files"] == 3
    assert "sqlalchemy-2" not in rows  # not a dependency here


def test_async_io_uses_the_blocking_rule(tmp_path):
    write(tmp_path, {"app/a.py": "import subprocess\n\nasync def run():\n    subprocess.run(['ls'])\n"})
    data = migrate.plan("async-io", tmp_path, *build(tmp_path))
    (item,) = data["steps"][0]["items"]
    assert item["intent"] == "no-blocking-in-async" and "subprocess.run" in item["title"]


def test_unknown_migration(repo):
    with pytest.raises(ValueError, match="known:"):
        migrate.plan("nope", repo, *build(repo))
