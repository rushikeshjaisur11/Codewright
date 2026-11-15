"""Architecture review: each check fires on the shape it names, stays quiet on look-alikes, and
its advice is about this codebase (which module, which callers) without touching behaviour."""

import textwrap
from pathlib import Path

from codewright import cards, graph, stack
from codewright.intents import detect
from codewright.suggest import make_findings


def write(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body).lstrip())
    return tmp_path


def arch(tmp_path: Path, intent: str):
    return detect(graph.build(tmp_path), only={intent})


# ---------------------------------------------------------------- import cycles


def test_cycle_names_the_cheapest_cut_and_what_to_move(tmp_path):
    write(
        tmp_path,
        {
            "app/__init__.py": "",
            "app/orders.py": """
            from app.pricing import total, discount, tax

            def place(o):
                return total(o) - discount(o) + tax(o)
            """,
            "app/pricing.py": """
            from app.orders import place

            def total(o): return o
            def discount(o): return 0
            def tax(o): return 0
            def reprice(o): return place(o)
            """,
        },
    )
    (m,) = arch(tmp_path, "arch-import-cycle")
    assert m.data["cut"] == ["app/pricing.py", "app/orders.py"]  # one name to move beats three
    assert "`place`" in m.data["advice"] and "line 1" in m.data["advice"]
    assert m.data["import_time"] is True


def test_cycle_of_deferred_imports_is_reported_as_coupling_only(tmp_path):
    write(
        tmp_path,
        {
            "cli.py": "def main():\n    from server import serve\n    serve()\n",
            "server.py": "def serve():\n    from cli import VERSION\n    return VERSION\n",
        },
    )
    (m,) = arch(tmp_path, "arch-import-cycle")
    assert m.data["import_time"] is False
    assert "only coupling" in m.evidence and "nothing breaks at import time" in m.data["advice"]


def test_whole_module_import_names_the_attributes_used(tmp_path):
    write(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/docs.py": "from pkg.reuse import stem\n\ndef search(q): return stem(q)\ndef libraries(): return []\n",
            "pkg/reuse.py": "def stem(w): return w\n\ndef compare():\n    from pkg import docs\n"
            "    return docs.search('x'), docs.libraries()\n",
        },
    )
    sites_names = {n for m in arch(tmp_path, "arch-import-cycle") for n in m.data["names"]}
    assert sites_names == {"stem"}  # one name to move, rather than docs.search and docs.libraries


def test_a_package_reexport_is_not_the_link_to_cut(tmp_path):
    write(
        tmp_path,
        {
            "pkg/__init__.py": "from pkg.core import build\n",
            "pkg/core.py": "from pkg.settings import NAME\n\ndef build(): return NAME\n",
            "pkg/settings.py": "import pkg\n\nNAME = 'x'\nVERSION = pkg.build\n",
        },
    )
    (m,) = arch(tmp_path, "arch-import-cycle")
    assert m.data["cut"][0] != "pkg/__init__.py"


# ---------------------------------------------------------------- layer bypass


ROUTES = """
from fastapi import FastAPI, Depends
from app.db import get_session
from app.services import users

api = FastAPI()

@api.get("/a")
def a(session=Depends(get_session)):
    return users.list_users(session)

@api.get("/b")
def b(session=Depends(get_session)):
    return users.get_user(session, 1)

@api.get("/c")
def c(session=Depends(get_session)):
    return users.count(session)
"""

SERVICES = """
from sqlalchemy import select, func

def list_users(session):
    return session.scalars(select(1)).all()

def get_user(session, uid):
    return session.get(1, uid) or session.execute(select(1)).first()

def count(session):
    return session.scalar(select(func.count()))
"""


def test_route_querying_directly_is_pointed_at_the_repos_own_layer(tmp_path):
    direct = """
    @api.get("/d")
    def d(session=Depends(get_session)):
        return session.execute("select 1").all()
    """
    write(
        tmp_path,
        {
            "app/__init__.py": "",
            "app/db.py": "def get_session():\n    yield None\n",
            "app/services/__init__.py": "",
            "app/services/users.py": SERVICES,
            "app/routes.py": ROUTES + textwrap.dedent(direct),
        },
    )
    (m,) = arch(tmp_path, "arch-layer-bypass")
    assert m.function.name == "d" and m.data["layer"] == "app/services/users.py"
    assert "`app/services/users.py` (3 routes)" in m.data["advice"]


def test_no_layer_bypass_when_direct_queries_are_the_repos_pattern(tmp_path):
    routes = """
    from fastapi import FastAPI
    api = FastAPI()

    @api.get("/a")
    def a(session):
        return session.execute("select 1").all()

    @api.get("/b")
    def b(session):
        return session.execute("select 2").all()
    """
    write(tmp_path, {"routes.py": routes})
    assert arch(tmp_path, "arch-layer-bypass") == []


# ---------------------------------------------------------------- untested hotspots


def _hot_repo(tmp_path: Path, test: str, extra: dict | None = None) -> Path:
    routes = "from fastapi import FastAPI\nfrom app.core import normalize\n\napi = FastAPI()\n"
    for i in range(6):
        routes += f'\n@api.get("/r{i}")\ndef r{i}(x: str):\n    return normalize(x)\n'
    core = """
    def normalize(x):
        x = x.strip()
        if not x:
            return None
        return x.lower()
    """
    return write(
        tmp_path,
        {"app/__init__.py": "", "app/core.py": core, "app/routes.py": routes, "tests/test_x.py": test, **(extra or {})},
    )


def test_most_called_untested_function_is_flagged_with_its_callers(tmp_path):
    _hot_repo(tmp_path, "def test_nothing():\n    assert True\n")
    (m,) = arch(tmp_path, "arch-untested-hotspot")
    assert m.function.qualname == "normalize" and m.data["callers"] == 6
    assert "routes.py:r0" in m.data["advice"]


def test_function_reached_through_a_test_client_is_not_untested(tmp_path):
    test = "from fastapi.testclient import TestClient\nfrom app.routes import api\n\n"
    test += "def test_r0():\n    assert TestClient(api).get('/r0?x=A').status_code == 200\n"
    _hot_repo(tmp_path, test)
    assert arch(tmp_path, "arch-untested-hotspot") == []


def test_cli_commands_driven_by_clirunner_count_as_tested(tmp_path):
    cli = "import click\nfrom app.core import normalize\n\n@click.group()\ndef cli():\n    pass\n"
    for i in range(6):
        cli += f"\n@cli.command()\ndef c{i}():\n    normalize('x')\n"
    write(
        tmp_path,
        {
            "app/__init__.py": "",
            "app/core.py": "def normalize(x):\n    x = x.strip()\n    y = x.lower()\n    return y\n",
            "app/cli.py": cli,
            "tests/test_cli.py": "from click.testing import CliRunner\nfrom app.cli import cli\n\n"
            "def test_c0():\n    CliRunner().invoke(cli, ['c0'])\n",
        },
    )
    g = graph.build(tmp_path)
    assert "app/cli.py::c0" in g.entry_points
    assert detect(g, only={"arch-untested-hotspot"}) == []


def test_no_hotspot_list_for_a_project_without_tests(tmp_path):
    _hot_repo(tmp_path, "")
    (tmp_path / "tests/test_x.py").unlink()
    assert arch(tmp_path, "arch-untested-hotspot") == []


# ---------------------------------------------------------------- duplicated logic


BODY = """
def {name}(rows):
    out = []
    for r in rows:
        if r is None:
            continue
        key = r.strip().lower()
        if key in out:
            continue
        out.append(key)
    out.sort()
    return out
"""


def test_identical_bodies_in_one_project_are_flagged_with_a_home(tmp_path):
    write(
        tmp_path,
        {
            "pyproject.toml": "[project]\nname='x'\n",
            "src/x/a.py": BODY.format(name="clean"),
            "src/x/b.py": "# same logic, other name\n" + BODY.format(name="tidy"),
        },
    )
    (m,) = arch(tmp_path, "arch-duplicate-logic")
    assert "src/x/b.py" in m.evidence and "under `src/x`" in m.data["advice"]


def test_copies_in_separate_projects_or_short_bodies_are_not_duplicates(tmp_path):
    write(
        tmp_path,
        {
            "one/pyproject.toml": "[project]\nname='one'\n",
            "two/pyproject.toml": "[project]\nname='two'\n",
            "one/a.py": BODY.format(name="clean"),
            "two/a.py": BODY.format(name="clean"),
            "short_a.py": "def f(x):\n    return x + 1\n",
            "short_b.py": "def g(x):\n    return x + 1\n",
        },
    )
    assert arch(tmp_path, "arch-duplicate-logic") == []


# ---------------------------------------------------------------- private names across components


def test_private_import_across_components_counts_every_user(tmp_path):
    write(
        tmp_path,
        {
            "core/__init__.py": "",
            "core/index.py": "def _tokens(s):\n    return s.split()\n",
            "core/search.py": "from core.index import _tokens\n",  # same component: fine
            "app/__init__.py": "",
            "app/memory.py": "from core.index import _tokens\n",
            "app/report.py": "from core import index\n\ndef run(s):\n    return index._tokens(s)\n",
        },
    )
    ms = arch(tmp_path, "arch-private-import")
    assert sorted(m.function.file for m in ms) == ["app/memory.py", "app/report.py"]
    assert all("2 files outside `core`" in m.data["advice"] for m in ms)


# ---------------------------------------------------------------- end to end


def test_findings_lead_with_codebase_advice_and_never_patch(tmp_path):
    write(
        tmp_path,
        {
            "a.py": "from b import helper\n\nVALUE = 1\n",
            "b.py": "from a import VALUE\n\ndef helper():\n    return VALUE\n",
        },
    )
    g = graph.build(tmp_path)
    fs = [f for f in make_findings(detect(g), stack.discover(tmp_path), g, cards.load()) if f.intent.startswith("arch")]
    assert [f.card_id for f in fs] == ["arch.import-cycle"]
    assert fs[0].suggestion.startswith("Here ") and fs[0].patch is None and fs[0].sources


def test_overloads_and_redefinitions_are_not_duplicates(tmp_path):
    sig = "self, value: int, a: int = 1, b: int = 2, c: int = 3, d: int = 4, e: int = 5, f: int = 6"
    write(
        tmp_path,
        {
            "pyproject.toml": "[project]\nname='x'\n",
            "x/api.py": "import typing as t\n\n\nclass S:\n"
            + "".join(f"    @t.overload\n    def load(\n        {sig},\n    ) -> int: ...\n\n" for _ in range(2))
            + "    def load(self, *args):\n"
            + "".join(f"        v{i} = args[{i}] if len(args) > {i} else None\n" for i in range(9))
            + "        return v0\n",
        },
    )
    assert arch(tmp_path, "arch-duplicate-logic") == []
