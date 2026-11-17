"""Dependency audit: overlaps, standard-library replacements, unused dependencies, cost and licences."""

import textwrap
import time
from pathlib import Path

from codewright import deps, graph, stack

PYPROJECT = """
[project]
name = "shop"
version = "0"
requires-python = ">=3.12"
dependencies = ["requests", "httpx", "pytz", "leftpad", "uvicorn", "psycopg[binary]", "heavylib"]

[project.optional-dependencies]
dev = ["pytest", "mock"]
"""

LOCK = """
version = 1

[[package]]
name = "requests"
version = "2.32.0"
dependencies = [{ name = "urllib3" }, { name = "idna" }]

[[package]]
name = "httpx"
version = "0.27.0"
dependencies = [{ name = "idna" }, { name = "h11" }]

[[package]]
name = "leftpad"
version = "1.0"
dependencies = [{ name = "tinydep" }]

[[package]]
name = "heavylib"
version = "1.0"
dependencies = [HEAVY]
""".replace("HEAVY", ", ".join(f'{{ name = "h{i}" }}' for i in range(20)))


def write(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body).lstrip())
    return tmp_path


def run(tmp_path):
    write(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT,
            "uv.lock": LOCK,
            "Procfile": "web: uvicorn app.main:app\n",
            "app/__init__.py": "",
            "app/a.py": "import requests\nimport pytz\n",
            "app/b.py": "import httpx\n",
            "app/c.py": "import httpx\nimport heavylib\n",
            "app/db.py": 'URL = "postgresql+psycopg://db/x"\n',
            "tests/test_x.py": "import mock\n",
        },
    )
    return deps.audit(tmp_path, graph.build(tmp_path), stack.discover(tmp_path), python=None, online=False)


def test_audit_finds_each_kind(tmp_path):
    r = run(tmp_path)
    by_kind = {}
    for f in r["findings"]:
        by_kind.setdefault(f["kind"], []).append(f)
    (overlap,) = by_kind["overlap"]
    assert overlap["deps"] == ["httpx", "requests"]  # httpx has more files, so it's the one to keep
    assert "`requests`" in overlap["suggestion"] and overlap["saves"] == 2  # requests + urllib3 (idna is shared)
    stdlib = {f["deps"][0] for f in by_kind["stdlib"]}
    assert stdlib == {"pytz", "mock"}
    (unused,) = by_kind["unused"]
    assert unused["deps"] == ["leftpad"] and unused["saves"] == 2 and unused["severity"] == "warn"
    (heavy,) = by_kind["heavy"]
    assert heavy["deps"] == ["heavylib"]
    rows = {d["name"]: d for d in r["dependencies"]}
    assert rows["requests"]["requires"] == 2 and rows["requests"]["unique"] == 1
    assert rows["mock"]["group"] == "dev" and rows["mock"]["test_files"] == 1
    assert "uvicorn" not in {f["deps"][0] for f in r["findings"]}  # named in the Procfile
    assert "psycopg" not in {f["deps"][0] for f in r["findings"] if f["kind"] == "unused"}


def test_stdlib_respects_the_target_python(tmp_path):
    write(tmp_path, {"pyproject.toml": PYPROJECT.replace(">=3.12", ">=3.8"), "app/a.py": "import toml\n"})
    st = stack.discover(tmp_path)
    r = deps.audit(tmp_path, graph.build(tmp_path), st, online=False)
    assert "pytz" not in {f["deps"][0] for f in r["findings"] if f["kind"] == "stdlib"}  # zoneinfo is 3.9+


def test_licenses_and_abandoned(tmp_path, monkeypatch):
    write(
        tmp_path,
        {"pyproject.toml": '[project]\nname="x"\nversion="0"\ndependencies=["oldlib"]\n', "a.py": "import oldlib\n"},
    )
    rows = {"oldlib": deps.DepRow("oldlib", "1", "main", "pyproject.toml", ["oldlib"], 1, 0)}
    lic = deps._licenses(rows, {}, {"licenses": {"oldlib": "GNU General Public License v3 (GPLv3)"}}, {})
    assert lic and lic[0].kind == "license"
    assert not deps._licenses(rows, {}, {"licenses": {"oldlib": "GNU Lesser General Public License (LGPL)"}}, {})

    from codewright import watcher

    monkeypatch.setattr(
        watcher,
        "prefetch",
        lambda names, cache, **kw: cache.update({"oldlib": {"released": "2019-01-01", "fetched": time.time()}}),
    )
    (ab,) = deps._abandoned(rows, tmp_path, time.time())
    assert ab.kind == "abandoned" and "2019-01-01" in ab.title


def test_requirements_starting_with_http_are_kept(tmp_path):
    """`httpx` starts with "http" but isn't a URL requirement: stack discovery used to drop it."""
    write(tmp_path, {"requirements.txt": "httpx==0.27.0\nhttps://example.com/pkg.whl\n", "pyproject.toml": PYPROJECT})
    st = stack.discover(tmp_path)
    assert st.version_of("httpx") == "0.27.0"
    assert "httpx" in deps.declared(tmp_path)
