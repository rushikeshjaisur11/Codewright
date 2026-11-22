"""Untangle: unused code reported only when sure, framework hooks never, and removals that keep imports working."""

import ast
import subprocess
from pathlib import Path

from codewright import graph, untangle, untangle_fix
from codewright.archmap import ArchMap
from codewright.proof import patch_error


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src.lstrip("\n"))
    return root


APP = {
    "app/__init__.py": "",
    "app/main.py": """
from fastapi import FastAPI
from app.services import handle, Plugin
from app.helpers import fmt
from app import registry  # noqa: F401

app = FastAPI()


@app.get("/")
def index():
    return handle(fmt(1)) + Plugin().dispatch("thing")
""",
    "app/services.py": """
import json
import os


def handle(x):
    return x


# Old exporter, kept "just in case".
def _old_export(rows):
    return json.dumps([_row(r) for r in rows])


def _row(r):
    return dict(r)


def only_in_tests():
    return os.getcwd()


class Plugin:
    def dispatch(self, name):
        return getattr(self, f"do_{name}")()

    def do_thing(self):
        return 2

    def forgotten(self):
        return 3
""",
    "app/helpers.py": """
from pydantic import BaseModel


def fmt(x):
    return str(x)


def _fmt(x, width):
    return str(x)


def wrapped(x):
    return fmt(x)


def caller():
    return wrapped(1) + _fmt(2, 3)


class Payload(BaseModel):
    a: int

    def model_post_init(self, ctx):
        pass

    def summary(self):
        return str(self.a)


def risky():
    try:
        return int("x")
    except Exception:
        pass
""",
    "app/registry.py": """
from app.settings_loader import load


class Converter:
    def to_python(self, v):
        return v


def register_converter(cls, name):
    return cls


register_converter(Converter, "conv")
load()
""",
    "app/settings_loader.py": """
import importlib


def load():
    return importlib.import_module("app.views")
""",
    "app/views.py": """
def view_from_settings():
    return 1
""",
    "app/legacy.py": """
def nobody():
    return 1
""",
    "app/commented.py": """
def live():
    x = 1
    # result = compute(x)
    # if result:
    #     print(result)
    return x


def explained():
    # Without the lock, two workers race:
    #
    #   await dispatch(rows)    # fires side effects
    #   await remove(batch)     # a crash here re-dispatches
    return 1
""",
    "tests/test_services.py": """
from app.services import only_in_tests


def test_it():
    assert only_in_tests()
""",
}


def inv_of(root: Path) -> dict:
    g = graph.build(root)
    return untangle.inventory(g)


def titles(inv: dict, kind: str, confidence: str | None = None) -> set[str]:
    return {
        i["name"] for i in inv["items"] if i["kind"] == kind and (confidence is None or i["confidence"] == confidence)
    }


def test_unused_code_is_found_with_its_chain(tmp_path):
    inv = inv_of(write(tmp_path, APP))
    unused = titles(inv, "unused", "high")
    assert unused <= {
        "_old_export",
        "_row",
        "Plugin.forgotten",
        "Payload.summary",
        "_fmt",
        "caller",
        "wrapped",
        "risky",
    }
    assert {"_old_export", "_row", "Plugin.forgotten", "Payload.summary"} <= unused
    row = next(i for i in inv["items"] if i["name"] == "_row")
    assert "_old_export" in row["why"] and row["chain"] == ["_old_export"]


def test_live_and_framework_code_is_never_reported(tmp_path):
    inv = inv_of(write(tmp_path, APP))
    named = {i["name"] for i in inv["items"] if i["kind"] in ("unused", "test-only")}
    # routes, a name built at run time, pydantic hooks, a class handed to a registry, a module loaded by string
    for alive in (
        "index",
        "handle",
        "fmt",
        "Plugin",
        "Plugin.dispatch",
        "Plugin.do_thing",
        "Payload",
        "Payload.model_post_init",
        "Converter",
        "Converter.to_python",
        "load",
    ):
        assert alive not in named, alive
    # a function in a module imported by name: whatever loads it may call it, so it is only "check by hand"
    assert "view_from_settings" not in titles(inv, "unused", "high")
    assert "only_in_tests" in titles(inv, "test-only")


def test_unused_module_and_dynamic_loading(tmp_path):
    root = write(tmp_path, APP)
    g = graph.build(root)
    unused = {u["file"] for u in ArchMap(g).unused}
    assert "app/legacy.py" in unused
    assert "app/views.py" not in unused  # named in a string by live code
    inv = untangle.inventory(g)
    assert any(i["kind"] == "unused-module" and i["file"] == "app/legacy.py" for i in inv["items"])


def test_wrappers_params_errors_and_commented_code(tmp_path):
    inv = inv_of(write(tmp_path, APP))
    assert "wrapped" in titles(inv, "wrapper")
    params = {i["name"]: i["data"]["params"] for i in inv["items"] if i["kind"] == "unused-param"}
    assert params.get("_fmt") == ["width"]
    assert any(i["kind"] == "swallowed" and i["name"] == "risky" for i in inv["items"])
    commented = [i for i in inv["items"] if i["kind"] == "commented-code"]
    assert [(i["file"], i["line"]) for i in commented] == [("app/commented.py", 3)]  # not the explained race


def test_getattr_with_a_computed_name_keeps_every_method(tmp_path):
    root = write(
        tmp_path,
        {
            "main.py": """
class Handlers:
    def ping(self):
        return 1

    def pong(self):
        return 2


def run(name):
    h = Handlers()
    return getattr(h, f"{name}")()


if __name__ == "__main__":
    run("ping")
""",
        },
    )
    inv = inv_of(root)
    assert not {"Handlers.ping", "Handlers.pong"} & titles(inv, "unused")


def test_library_public_api_is_kept(tmp_path):
    root = write(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "lib"\nversion = "1"\n',
            "lib/__init__.py": "from lib._impl import public_thing\n",
            "lib/_impl.py": """
def public_thing():
    return _helper()


def _helper():
    return 1


def _dead():
    return 2


def not_exported():
    return 3
""",
            "lib/api.py": """
def documented_api():
    return 1
""",
        },
    )
    inv = inv_of(root)
    assert inv["mode"] == "library"
    unused = titles(inv, "unused")
    assert "_dead" in unused and "not_exported" in unused  # private module, not re-exported
    assert "documented_api" not in unused and "public_thing" not in unused and "_helper" not in unused


def test_removal_patch_applies_and_keeps_imports_working(tmp_path):
    root = write(
        tmp_path,
        {
            "app/__init__.py": "from app.util import dead, live\n",
            "app/main.py": """
from fastapi import FastAPI
from app import live
from app.util import (
    dead,
    live as again,
)

app = FastAPI()


@app.get("/")
def index():
    return live() + again()
""",
            "app/util.py": """
import json
import os


def live():
    return os.getpid()


# Serialise the old way.
def dead():
    return json.dumps(_only_dead_uses_me())


def _only_dead_uses_me():
    return {}
""",
        },
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    g = graph.build(root)
    inv = untangle.inventory(g)
    dead = next(i for i in inv["items"] if i["name"] == "dead")
    res = untangle_fix.removal(root, g, inv["items"], [dead["id"]])
    assert not res["skipped"], res["skipped"]
    assert {i.split(":")[-1] for i in res["removed"]} == {"dead", "_only_dead_uses_me"}  # the chain goes too
    assert patch_error(root, res["patch"]) == ""
    util = (root / "app/util.py").read_text()
    assert "def dead" not in util and "_only_dead_uses_me" not in util and "Serialise" not in util
    assert "import json" not in util and "import os" in util  # json was only the dead code's
    init = (root / "app/__init__.py").read_text()
    main = (root / "app/main.py").read_text()
    assert init == "from app.util import live\n"
    assert "dead" not in main and "live as again" in main
    for f in ("app/__init__.py", "app/main.py", "app/util.py"):
        ast.parse((root / f).read_text())


def test_removal_skips_what_would_leave_an_empty_block(tmp_path):
    root = write(
        tmp_path,
        {
            "main.py": """
from fastapi import FastAPI

app = FastAPI()

if app:
    def lonely():
        return 1


@app.get("/")
def index():
    return 1
""",
        },
    )
    g = graph.build(root)
    inv = untangle.inventory(g)
    item = next(i for i in inv["items"] if i["name"] == "lonely")
    res = untangle_fix.removal(root, g, inv["items"], [item["id"]])
    assert not res["patch"] and "empty block" in res["skipped"][0]["why"]


def test_module_deletion(tmp_path):
    root = write(tmp_path, APP)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    g = graph.build(root)
    inv = untangle.inventory(g)
    mod = next(i for i in inv["items"] if i["kind"] == "unused-module" and i["file"] == "app/legacy.py")
    res = untangle_fix.removal(root, g, inv["items"], [mod["id"]])
    assert res["deleted"] == ["app/legacy.py"]
    assert patch_error(root, res["patch"]) == ""
    assert not (root / "app/legacy.py").exists()


def test_agent_task_lists_each_spot(tmp_path):
    inv = inv_of(write(tmp_path, APP))
    picked = [i for i in inv["items"] if i["kind"] in ("wrapper", "swallowed")]
    task, context = untangle.agent_task(picked)
    assert "without changing its behaviour" in task
    assert "app/helpers.py" in context and "## Pass-through wrappers" in context


def test_graph_records_star_lazy_and_shadowed_imports(tmp_path):
    root = write(
        tmp_path,
        {
            "pkg/__init__.py": "from ._api import *\n",
            "pkg/_api.py": "def get():\n    return 1\n",
            "pkg/factory.py": """
def make(kind):
    match kind:
        case "a":
            from pkg.impl_a import Client
            return Client()
        case _:
            from pkg.impl_b import Client
            return Client()
""",
            "pkg/impl_a.py": "class Client: ...\n",
            "pkg/impl_b.py": "class Client: ...\n",
            "main.py": "from pkg.factory import make\nif __name__ == '__main__':\n    make('a')\n",
        },
    )
    g = graph.build(root)
    imports = g.files["pkg/factory.py"].module.imports
    assert {"pkg.impl_a.Client", "pkg.impl_b.Client"} <= set(imports.values())
    assert "._api" in g.files["pkg/__init__.py"].module.imports.values()
    unused = {u["file"] for u in ArchMap(g).unused}
    assert not unused & {"pkg/_api.py", "pkg/impl_a.py", "pkg/impl_b.py"}


def test_subfolder_project_and_django_settings_chain(tmp_path):
    root = write(
        tmp_path,
        {
            "backend/app/__init__.py": "",
            "backend/app/main.py": "from fastapi import FastAPI\nfrom app.api.routes import router\napp = FastAPI()\n",
            "backend/app/api/__init__.py": "",
            "backend/app/api/routes.py": "from app.core import config\nrouter = None\n",
            "backend/app/core/__init__.py": "",
            "backend/app/core/config.py": "X = 1\n",
            "site/manage.py": """
import os
from django.core.management import execute_from_command_line

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "proj.settings")
    execute_from_command_line()
""",
            "site/proj/__init__.py": "",
            "site/proj/settings.py": 'ROOT_URLCONF = "proj.urls"\n',
            "site/proj/urls.py": "from proj import views\nurlpatterns = [views.home]\n",
            "site/proj/views.py": "from proj.forms import F\ndef home(request):\n    return F\n",
            "site/proj/forms.py": "F = 1\n",
        },
    )
    unused = {u["file"] for u in ArchMap(graph.build(root)).unused}
    assert not unused & {"backend/app/api/routes.py", "backend/app/core/config.py"}
    assert not unused & {"site/proj/urls.py", "site/proj/views.py", "site/proj/forms.py"}


def test_markdown_report(tmp_path):
    inv = inv_of(write(tmp_path, APP))
    md = untangle.to_markdown(inv)
    assert md.startswith("# Untangle:") and "## Unused code" in md
