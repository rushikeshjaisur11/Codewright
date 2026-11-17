"""Architectural refactorings: each patch applies, keeps the tests green, and removes what it fixes."""

import sys
import textwrap
from pathlib import Path

from codewright import graph, rules
from codewright.engine import review
from codewright.intents import detect
from codewright.proof import apply_patch


def write(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body).lstrip())
    return tmp_path


def finding(root, intent):
    r = review(root, python=sys.executable, top=None)
    found = [f for f in r.findings + r.rejected if f.intent == intent]
    assert found, [f.intent for f in r.findings]
    return found[0]


def still(root, intent):
    return detect(graph.build(root), only={intent})


def test_private_import_gets_a_public_alias(tmp_path):
    write(
        tmp_path,
        {
            "core/__init__.py": "",
            "core/text.py": "def _slug(s):\n    return s.lower().replace(' ', '-')\n",
            "web/__init__.py": "",
            "web/views.py": "from core.text import _slug\n\n\ndef page(t):\n    return _slug(t)\n",
            "tests/test_views.py": (
                "from web.views import page\n\n\ndef test_page():\n    assert page('A B') == 'a-b'\n"
            ),
        },
    )
    f = finding(tmp_path, "arch-private-import")
    assert f.patch and f.proof.value == "verified", f.proof_detail
    assert apply_patch(tmp_path, f.patch)
    assert "slug = _slug" in (tmp_path / "core/text.py").read_text()
    assert (tmp_path / "web/views.py").read_text().startswith("from core.text import slug\n")
    assert "return slug(t)" in (tmp_path / "web/views.py").read_text()
    assert not still(tmp_path, "arch-private-import")


BODY = """
def {name}(rows):
    out = []
    for r in rows:
        if r is None:
            continue
        v = str(r).strip()
        if not v:
            continue
        out.append(v.upper())
    out.sort()
    return out
"""


def test_duplicate_becomes_an_import(tmp_path):
    write(
        tmp_path,
        {
            "app/__init__.py": "",
            "app/a.py": BODY.format(name="clean"),
            "app/b.py": "import json\n" + BODY.format(name="tidy") + "\n\ndef other():\n    return tidy([' x'])\n",
            "tests/test_ab.py": "from app.a import clean\nfrom app.b import other, tidy\n\n\n"
            "def test_same():\n    assert clean([' b', None, 'a']) == tidy(['a', ' b']) == ['A', 'B']\n"
            "    assert other() == ['X']\n",
        },
    )
    f = finding(tmp_path, "arch-duplicate-logic")
    assert f.patch and f.proof.value == "verified", f.proof_detail
    assert apply_patch(tmp_path, f.patch)
    b = (tmp_path / "app/b.py").read_text()
    assert "from app.a import clean as tidy" in b and "def tidy" not in b
    assert not still(tmp_path, "arch-duplicate-logic")


def test_duplicate_skipped_when_a_global_differs(tmp_path):
    body = BODY.replace("v.upper()", "PREFIX + v")
    write(
        tmp_path,
        {
            "app/__init__.py": "",
            "app/a.py": "PREFIX = 'a'\n" + body.format(name="clean"),
            "app/b.py": "PREFIX = 'b'\n" + body.format(name="tidy"),
        },
    )
    f = finding(tmp_path, "arch-duplicate-logic")
    assert f.patch is None  # same text, different PREFIX: not the same function


def test_cycle_moves_the_names_to_a_shared_module(tmp_path):
    write(
        tmp_path,
        {
            "shop/__init__.py": "",
            "shop/orders.py": """
            from shop.pricing import price


            def total(items):
                return sum(price(i) for i in items)


            def label(o):
                return f"order {o}"
            """,
            "shop/pricing.py": """
            import math

            RATE = 1.2


            def price(i):
                return math.ceil(i * RATE)


            def describe(o):
                return label(o) + "!"


            from shop.orders import label  # noqa: E402  (works only in this import order)
            """,
            "tests/test_shop.py": """
            from shop.pricing import describe  # pricing first, or the cycle fails at import
            from shop.orders import total


            def test_it():
                assert total([1, 2]) == 2 + 3
                assert describe(1) == "order 1!"
            """,
        },
    )
    f = finding(tmp_path, "arch-import-cycle")
    assert f.patch, f.evidence
    assert f.proof.value == "verified", f.proof_detail
    assert apply_patch(tmp_path, f.patch)
    assert not still(tmp_path, "arch-import-cycle")
    shared = next(p for p in (tmp_path / "shop").glob("*_shared.py"))
    assert "def " in shared.read_text() and "import shop" not in shared.read_text().replace("import math", "")


def test_cycle_defers_the_import_when_moving_is_unsafe(tmp_path):
    write(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "from pkg.b import helper\n\n\ndef run():\n    return helper()\n",
            "pkg/b.py": "from pkg.a import run\n\nSTATE = {}\nSTATE['x'] = 1\n\n\ndef helper():\n"
            "    return STATE['x']\n\n\ndef again():\n    return run()\n",
            "tests/test_a.py": "from pkg.a import run\n\n\ndef test_run():\n    assert run() == 1\n",
        },
    )
    f = finding(tmp_path, "arch-import-cycle")
    assert f.patch, f.evidence
    assert apply_patch(tmp_path, f.patch)
    a = (tmp_path / "pkg/a.py").read_text()
    assert "    from pkg.b import helper" in a and not a.startswith("from pkg.b")


def test_blocking_rule_fix(tmp_path):
    write(
        tmp_path,
        {
            "app/a.py": "import subprocess\nimport time\n\n\nasync def go():\n    time.sleep(1)\n"
            "    out = subprocess.run(['ls'], check=True)\n    return out\n",
            ".codewright/rules.yaml": "rules:\n  - {id: aio, kind: no-blocking-in-async}\n",
        },
    )
    vs = rules.check(graph.build(tmp_path), rules.load(tmp_path))
    assert all(v.patch for v in vs) and len(vs) == 2
    from codewright.refactor import combine

    patch, skipped = combine(tmp_path, [(str(v.line), v.patch) for v in sorted(vs, key=lambda v: -v.line)])
    assert skipped == []
    assert apply_patch(tmp_path, patch)
    src = (tmp_path / "app/a.py").read_text()
    assert "await asyncio.sleep(1)" in src and "await asyncio.to_thread(subprocess.run, ['ls'], check=True)" in src
    assert "import asyncio" in src
    compile(src, "a.py", "exec")
    assert rules.check(graph.build(tmp_path), rules.load(tmp_path)) == []


def test_apply_everywhere(tmp_path):
    """Bulk apply through the CLI, the web API and MCP (MCP only returns the diff)."""
    import json

    from fastapi.testclient import TestClient
    from typer.testing import CliRunner

    from codewright import mcp_server
    from codewright.cli import app as cli
    from codewright.server import create_app

    files = {
        "core/__init__.py": "",
        "core/text.py": "def _slug(s):\n    return s.lower()\n",
        "web/__init__.py": "",
        "web/views.py": "import time\n\nfrom core.text import _slug\n\n\nasync def page(t):\n    time.sleep(0)\n"
        "    return _slug(t)\n",
        "tests/test_views.py": "import asyncio\n\nfrom web.views import page\n\n\n"
        "def test_page():\n    assert asyncio.run(page('A')) == 'a'\n",
        ".codewright/rules.yaml": "rules:\n  - {id: aio, kind: no-blocking-in-async}\n",
    }
    write(tmp_path, files)
    repo = str(tmp_path)
    diff = mcp_server.combined_patch(repo, rule_fixes=True)["patch"]
    assert "await asyncio.sleep(0)" in diff and "time.sleep(0)" in (tmp_path / "web/views.py").read_text()

    c = TestClient(create_app([repo]), base_url="http://localhost")
    wid = c.get("/api/workspaces").json()[0]["id"]
    fixed = c.post(f"/api/w/{wid}/rules/fix", json={}).json()
    assert fixed["files"] == ["web/views.py"] and fixed["rules"]["violations"] == []
    assert c.post(f"/api/w/{wid}/rules/fix", json={}).status_code == 404  # nothing left to fix

    res = CliRunner().invoke(cli, ["apply", repo, "--dry-run", "--python", sys.executable])
    assert res.exit_code == 0 and "+slug = _slug" in res.output, res.output
    res = CliRunner().invoke(cli, ["apply", repo, "--python", sys.executable])
    assert res.exit_code == 0 and "Applied" in res.output, res.output
    assert "from core.text import slug" in (tmp_path / "web/views.py").read_text()
    verdicts = json.loads((tmp_path / ".codewright/feedback.json").read_text())["verdicts"]
    assert {v["status"] for v in verdicts.values()} == {"accepted"}
