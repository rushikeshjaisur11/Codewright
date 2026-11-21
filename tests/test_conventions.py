"""Library-recommended conventions: FastAPI Annotated parameters and Pydantic Annotated fields.

Each rewrite is checked the strong way: the patched app or model is imported next to the
original and must expose the same OpenAPI schema, answer requests the same and validate the same.
"""

import importlib.util
import json
import sys
import textwrap
from pathlib import Path

import pytest

from codewright import cards, graph, proof, stack
from codewright.intents import detect
from codewright.suggest import make_findings


def write(tmp_path: Path, files: dict[str, str], fastapi="0.163.1", pydantic="2.14.3") -> Path:
    lock = "version = 1\n"
    for name, v in (("fastapi", fastapi), ("pydantic", pydantic)):
        if v:
            lock += f'\n[[package]]\nname = "{name}"\nversion = "{v}"\n'
    files = {"uv.lock": lock, **files}
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body).lstrip())
    return tmp_path


def findings(tmp_path: Path) -> dict:
    g = graph.build(tmp_path)
    return {f.card_id: f for f in make_findings(detect(g), stack.discover(tmp_path), g, cards.load())}


def apply(tmp_path: Path, f) -> str:
    assert not proof.patch_error(tmp_path, f.patch)
    src = (tmp_path / f.file).read_text()
    compile(src, f.file, "exec")
    return src


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- FastAPI

API = """
from typing import List, Optional

from fastapi import Body, Depends, FastAPI, Header, Path, Query

app = FastAPI()


def common(q: Optional[str] = Query(None, max_length=10), limit: int = Query(default=10, le=100)):
    return {"q": q, "limit": limit}


@app.get("/items/{item_id}")
def read(item_id: int = Path(..., ge=1), c: dict = Depends(common), tags: List[str] = Query([])):
    return {"id": item_id, **c, "tags": tags}


@app.get("/mixed")
def mixed(q: str = Query("z", alias="item-query"), c: dict = Depends(common)):
    return {"q": q, **c}


@app.post("/body")
def body(x: int = Body(..., gt=0), ua: Optional[str] = Header(None)):
    return {"x": x, "ua": ua}
"""


def test_fastapi_params_become_annotated_with_one_import(tmp_path):
    write(tmp_path, {"api.py": API})
    f = findings(tmp_path)["fastapi.annotated-params"]
    src = apply(tmp_path, f)
    assert src.startswith("from typing import List, Optional, Annotated\n") and src.count("Annotated\n") == 1
    assert "q: Annotated[Optional[str], Query(max_length=10)] = None" in src
    assert "limit: Annotated[int, Query(le=100)] = 10" in src
    assert "item_id: Annotated[int, Path(ge=1)]," in src and "c: Annotated[dict, Depends(common)]" in src
    # `c` loses its default after a defaulted `q`: the route goes keyword-only instead of reordering.
    assert 'def mixed(*, q: Annotated[str, Query(alias="item-query")] = "z", c:' in src


def test_fastapi_rewrite_keeps_openapi_and_responses_identical(tmp_path):
    from fastapi.testclient import TestClient

    write(tmp_path, {"api.py": API})
    before = load(tmp_path / "api.py", "api_before")
    apply(tmp_path, findings(tmp_path)["fastapi.annotated-params"])
    after = load(tmp_path / "api.py", "api_after")
    assert json.dumps(before.app.openapi(), sort_keys=True) == json.dumps(after.app.openapi(), sort_keys=True)
    calls = [
        ("get", "/items/3?q=a&tags=x&tags=y", {}),
        ("get", "/items/0", {}),
        ("get", "/items/3?q=" + "x" * 11, {}),
        ("get", "/mixed?item-query=k&limit=5", {}),
        ("post", "/body", {"json": 1, "headers": {"ua": "t"}}),
        ("post", "/body", {"json": 0}),
    ]
    for method, url, kw in calls:
        a, b = (getattr(TestClient(m.app), method)(url, **kw) for m in (before, after))
        assert (a.status_code, a.json()) == (b.status_code, b.json()), url


def test_fastapi_function_called_directly_is_left_alone_when_order_would_break(tmp_path):
    write(
        tmp_path,
        {
            "api.py": """
            from fastapi import Depends, FastAPI, Query

            app = FastAPI()

            def dep(a: int = Query(1), b: int = Query(...)):
                return a + b

            @app.get("/x")
            def x(v: int = Depends(dep)):
                return v
            """
        },
    )
    f = findings(tmp_path)["fastapi.annotated-params"]
    src = apply(tmp_path, f)
    assert "def dep(a: int = Query(1), b: int = Query(...))" in src  # not a route: positional callers possible
    assert "def x(v: Annotated[int, Depends(dep)])" in src
    assert "`dep` is left as is" in f.evidence


def test_fastapi_untyped_or_commented_params_are_reported_not_rewritten(tmp_path):
    write(
        tmp_path,
        {
            "api.py": """
            from typing import Annotated
            from fastapi import FastAPI, Query

            app = FastAPI()

            @app.get("/x")
            def x(q=Query(None), r: int = Query(1, ge=0)):  # q has no type: Annotated needs one
                return q
            """
        },
    )
    src = apply(tmp_path, findings(tmp_path)["fastapi.annotated-params"])
    assert "q=Query(None), r: Annotated[int, Query(ge=0)] = 1" in src
    assert src.count("import Annotated") == 1  # reuses the existing import


def test_shared_dependency_gets_a_reusable_type_suggestion(tmp_path):
    routes = "from fastapi import Depends, FastAPI\nfrom db import Session, get_session\n\napp = FastAPI()\n"
    for i in range(3):
        routes += f'\n@app.get("/r{i}")\ndef r{i}(session: Session = Depends(get_session)):\n    return {i}\n'
    write(tmp_path, {"api.py": routes, "db.py": "class Session: ...\n\ndef get_session():\n    return Session()\n"})
    f = findings(tmp_path)["fastapi.annotated-params"]
    assert "`SessionDep = Annotated[Session, Depends(get_session)]`" in f.suggestion


@pytest.mark.parametrize(
    "fastapi,pydantic,patched",
    [("0.104.1", "2.12.5", False), ("0.104.1", "2.11.7", True), ("0.105.0", "2.14.3", True), ("0.95.1", "", True)],
)
def test_fastapi_patch_withheld_for_versions_that_break_annotated(tmp_path, fastapi, pydantic, patched):
    write(tmp_path, {"api.py": API}, fastapi=fastapi, pydantic=pydantic)
    f = findings(tmp_path)["fastapi.annotated-params"]
    assert bool(f.patch) is patched
    assert f.suggestion.startswith("Upgrade FastAPI to 0.105.0") is not patched


def test_fastapi_95_0_and_older_get_no_annotated_suggestion(tmp_path):
    write(tmp_path, {"api.py": API}, fastapi="0.95.0", pydantic="")
    assert "fastapi.annotated-params" not in findings(tmp_path)


# ---------------------------------------------------------------- Pydantic

MODELS = """
from pydantic import BaseModel, Field
from pydantic.dataclasses import dataclass


class Order(BaseModel):
    id: int = Field(...)
    qty: int = Field(gt=0, le=1000)
    name: str = Field(..., min_length=1, description="Display name")
    code: str = Field(..., alias="Code")
    note: str = Field(alias="Note")
    opt: int = Field(5, ge=0)
    tags: list = Field(default_factory=list)
    weight: float = Field(default=..., gt=0)


@dataclass
class Point:
    x: int = Field(...)
"""


def test_required_fields_become_annotated_and_defaults_keep_assignment(tmp_path):
    write(tmp_path, {"models.py": MODELS})
    f = findings(tmp_path)["pydantic.annotated-field"]
    src = apply(tmp_path, f)
    assert "    id: int\n" in src
    assert "qty: Annotated[int, Field(gt=0, le=1000)]" in src
    assert 'name: Annotated[str, Field(min_length=1, description="Display name")]' in src
    assert 'code: str = Field(alias="Code")' in src  # alias stays assigned; only the `...` goes
    assert 'note: str = Field(alias="Note")' in src
    assert "opt: int = Field(5, ge=0)" in src and "tags: list = Field(default_factory=list)" in src
    assert "weight: Annotated[float, Field(gt=0)]" in src
    assert "x: int = Field(...)" in src  # pydantic dataclass: field order rules differ, left alone


def test_pydantic_rewrite_keeps_schema_fields_and_validation_identical(tmp_path):
    from pydantic import ValidationError

    write(tmp_path, {"models.py": MODELS})
    before = load(tmp_path / "models.py", "models_before")
    apply(tmp_path, findings(tmp_path)["pydantic.annotated-field"])
    after = load(tmp_path / "models.py", "models_after")
    a, b = before.Order, after.Order
    assert a.model_json_schema() == b.model_json_schema()
    assert {k: repr(v) for k, v in a.model_fields.items()} == {k: repr(v) for k, v in b.model_fields.items()}
    good = {"id": 1, "qty": 5, "name": "n", "Code": "c", "Note": "x", "weight": 1.0}
    for data in (good, {**good, "qty": 0}, {**good, "name": ""}, {k: v for k, v in good.items() if k != "id"}):

        def run(cls, data=data):
            try:
                return cls(**data).model_dump()
            except ValidationError as e:
                return [{k: v for k, v in err.items() if k != "url"} for err in e.errors()]

        assert run(a) == run(b)


def test_repeated_constraints_suggest_one_reusable_type(tmp_path):
    write(
        tmp_path,
        {
            "a.py": """
            from pydantic import BaseModel, Field

            class Review(BaseModel):
                score: int = Field(ge=0, le=100)
                accuracy: int = Field(ge=0, le=100, description="How accurate")

            class Survey(BaseModel):
                score: int = Field(50, ge=0, le=100)
                age: int = Field(ge=0, le=150)
            """
        },
    )
    f = findings(tmp_path)["pydantic.reusable-annotated"]
    assert "on 3 fields" in f.evidence and f.patch is None
    assert "`Score = Annotated[int, Field(ge=0, le=100)]`" in f.suggestion


# ---------------------------------------------------------------- monorepos: each project's own versions


def test_each_project_in_a_monorepo_is_judged_on_its_own_lockfile(tmp_path):
    def project(name, fastapi):
        return {
            f"{name}/pyproject.toml": f"[project]\nname='{name}'\ndependencies=['fastapi']\n",
            f"{name}/uv.lock": f'version = 1\n\n[[package]]\nname = "fastapi"\nversion = "{fastapi}"\n',
            f"{name}/app.py": API,
        }

    for name, body in {**project("new", "0.163.1"), **project("old", "0.95.0")}.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body).lstrip())
    g = graph.build(tmp_path)
    fs = make_findings(detect(g), stack.discover(tmp_path), g, cards.load())
    fs = [f for f in fs if f.intent.startswith("fastapi")]
    assert [f.file for f in fs] == ["new/app.py"]  # 0.95.0 predates reliable Annotated support


def test_workspace_member_takes_versions_from_the_root_lock(tmp_path):
    (tmp_path / "uv.lock").write_text('version = 1\n\n[[package]]\nname = "pydantic"\nversion = "2.14.3"\n')
    member = tmp_path / "packages" / "core"
    member.mkdir(parents=True)
    (member / "pyproject.toml").write_text("[project]\nname='core'\ndependencies=['pydantic>=2']\n")
    here = stack.for_path(stack.discover(tmp_path), "packages/core/src/core/models.py")
    assert here.version_of("pydantic") == "2.14.3" and here.dependencies["pydantic"].specifier == ">=2"
    assert stack.for_path(stack.discover(tmp_path), "tool.py").version_of("pydantic") == "2.14.3"


def test_mutable_defaults_stay_put_and_the_import_is_sorted_in(tmp_path):
    write(
        tmp_path,
        {
            "api.py": """
            import os
            from dataclasses import dataclass
            from pathlib import Path

            from fastapi import FastAPI, Query

            app = FastAPI()

            @app.get("/x")
            def x(tags: list[str] = Query([]), n: int = Query(1)):
                return tags, n
            """
        },
    )
    src = apply(tmp_path, findings(tmp_path)["fastapi.annotated-params"])
    assert "tags: list[str] = Query([])" in src  # as a real default, [] would be shared between direct calls
    assert "from pathlib import Path\nfrom typing import Annotated\n\nfrom fastapi" in src
