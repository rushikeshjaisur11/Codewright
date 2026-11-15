import subprocess
import textwrap
from pathlib import Path

import pytest

from codewright import cards, graph, stack
from codewright.intents import detect
from codewright.suggest import make_findings


def repo(tmp_path: Path, files: dict[str, str], locked: dict[str, str]) -> Path:
    lock = "version = 1\n" + "".join(f'\n[[package]]\nname = "{n}"\nversion = "{v}"\n' for n, v in locked.items())
    (tmp_path / "uv.lock").write_text(lock)
    for name, body in files.items():
        (tmp_path / name).write_text(textwrap.dedent(body).lstrip())
    return tmp_path


def findings(tmp_path: Path) -> dict[str, list]:
    g = graph.build(tmp_path)
    out: dict[str, list] = {}
    for f in make_findings(detect(g), stack.discover(tmp_path), g, cards.load()):
        out.setdefault(f.card_id, []).append(f)
    return out


PYDANTIC = """
from pydantic import BaseModel, validator

class User(BaseModel):
    name: str

    class Config:
        orm_mode = True

    @validator("name")
    def strip(cls, v):
        return v.strip()

class Plain:
    class Config:
        debug = True
"""


def test_pydantic_v1_patterns_on_v2(tmp_path):
    fs = findings(repo(tmp_path, {"m.py": PYDANTIC}, {"pydantic": "2.14.3"}))
    assert [f.function for f in fs["pydantic.field-validator"]] == ["User.strip"]
    assert [f.function for f in fs["pydantic.config-dict"]] == ["User"]  # not the unrelated Plain.Config


def test_pydantic_cards_need_pydantic_2(tmp_path):
    fs = findings(repo(tmp_path, {"m.py": PYDANTIC}, {"pydantic": "1.10.13"}))
    assert "pydantic.field-validator" not in fs and "pydantic.config-dict" not in fs


def test_basesettings_from_pydantic_is_flagged(tmp_path):
    src = "from pydantic import BaseModel, BaseSettings\n\nclass S(BaseSettings):\n    url: str\n"
    fs = findings(repo(tmp_path, {"m.py": src}, {"pydantic": "2.0.3"}))
    assert fs["pydantic.settings-package"][0].line == 1


def test_sqlalchemy_ext_declarative_import_is_patched(tmp_path):
    src = "from sqlalchemy.ext.declarative import declarative_base\n\nBase = declarative_base()\n"
    root = repo(tmp_path, {"models.py": src}, {"sqlalchemy": "2.1.8"})
    [f] = findings(root)["sqlalchemy.orm-declarative-base"]
    assert "+from sqlalchemy.orm import declarative_base" in f.patch
    subprocess.run(["git", "apply", "-"], cwd=root, input=f.patch, text=True, check=True)
    compile((root / "models.py").read_text(), "models.py", "exec")


def test_sqlalchemy_ext_declarative_with_other_names_is_not_patched(tmp_path):
    src = "from sqlalchemy.ext.declarative import declarative_base, declared_attr\n"
    [f] = findings(repo(tmp_path, {"models.py": src}, {"sqlalchemy": "2.1.8"}))["sqlalchemy.orm-declarative-base"]
    assert f.patch is None


@pytest.mark.parametrize(
    "call,flagged",
    [
        ("requests.get(url)", True),
        ("requests.post(url, json={})", True),
        ("requests.get(url, timeout=5)", False),
        ("requests.get(url, **opts)", False),  # opts may carry a timeout
    ],
)
def test_requests_without_timeout(tmp_path, call, flagged):
    src = f"import requests\n\ndef fetch(url, opts):\n    return {call}\n"
    fs = findings(repo(tmp_path, {"m.py": src}, {"requests": "2.34.2"}))
    assert ("requests.timeout" in fs) is flagged


def test_retry_loop_in_a_celery_task_suggests_autoretry(tmp_path):
    src = """
    import time
    from celery import Celery

    app = Celery("x")

    @app.task
    def sync(order_id):
        for attempt in range(5):
            try:
                return push(order_id)
            except ConnectionError:
                time.sleep(2 ** attempt)

    def plain(order_id):
        for attempt in range(5):
            try:
                return push(order_id)
            except ConnectionError:
                time.sleep(1)
    """
    fs = findings(repo(tmp_path, {"tasks.py": src}, {"celery": "5.7.0", "tenacity": "9.2.1"}))
    assert [f.function for f in fs["celery.autoretry"]] == ["sync"]
    assert [f.function for f in fs["tenacity.retry"]] == ["plain"]


# ---------------------------------------------------------------- Pydantic auto-patches keep behaviour


MODELS = """
from pydantic import BaseModel, validator


class A(BaseModel):
    name: str
    tags: list = []

    class Config:
        str_strip_whitespace = True
        str_max_length = 20
        extra = "forbid"

    @validator("name")
    def not_admin(cls, v):
        if v == "admin":
            raise ValueError("reserved")
        return v.title()

    @validator("tags", pre=True, allow_reuse=True)
    def split(cls, v):
        return v.split(",") if isinstance(v, str) else v

    @validator("name")
    def uses_values(cls, v, values):
        return v


class Legacy(BaseModel):
    name: str

    class Config:
        anystr_strip_whitespace = True
"""


def _apply_all(root: Path) -> list[str]:
    applied = []
    for _ in range(10):
        fs = [f for fl in findings(root).values() for f in fl if f.patch]
        if not fs:
            return applied
        subprocess.run(["patch", "-p1", "--quiet"], cwd=root, input=fs[0].patch, text=True, check=True)
        applied.append(fs[0].function)
    return applied


def test_pydantic_patches_keep_config_and_validation_identical(tmp_path):
    import importlib.util
    import warnings

    import pydantic

    root = repo(tmp_path, {"models.py": MODELS}, {"pydantic": pydantic.VERSION})
    fs = findings(root)
    assert {f.function for f in fs["pydantic.ignored-config"]} == {"Legacy"}  # v1 names: reported...
    assert next(f for f in fs["pydantic.config-dict"] if f.function == "Legacy").patch is None  # ...not patched
    validators = {f.function: f.patch for f in fs["pydantic.field-validator"]}
    assert validators["A.uses_values"] is None  # takes `values`: behaves differently, left alone
    original = (root / "models.py").read_text()
    assert sorted(_apply_all(root)) == ["A", "A.not_admin", "A.split"]
    patched = (root / "models.py").read_text()
    assert "model_config = ConfigDict(" in patched and '@field_validator("tags", mode="before")' in patched

    def load(name, src):
        path = tmp_path / f"{name}.py"
        path.write_text(src)
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            spec.loader.exec_module(mod)
        return mod

    before, after = load("before", original), load("after", patched)

    def outcome(model, data):
        try:
            return ("ok", model.model_validate(data).model_dump())
        except pydantic.ValidationError as exc:
            return ("error", sorted(e["type"] for e in exc.errors()))

    for cls, inputs in {
        "A": [
            {"name": "  bob "},
            {"name": "admin"},
            {"name": "x" * 30},
            {"name": "z", "tags": "a,b"},
            {"name": "z", "other": 1},
        ],
        "Legacy": [{"name": "  x "}],
    }.items():
        a, b = getattr(before, cls), getattr(after, cls)
        assert dict(a.model_config) == dict(b.model_config), cls
        assert [outcome(a, d) for d in inputs] == [outcome(b, d) for d in inputs], cls
