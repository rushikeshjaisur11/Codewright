import sys
import textwrap
from pathlib import Path

from codewright import graph, proof
from codewright.engine import review


def write_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body).lstrip())
    return tmp_path


def callees(g: graph.CodeGraph, key: str) -> set[str]:
    return set(g.edges.get(key, ()))


# ---------------------------------------------------------------- call resolution


def test_calls_resolve_through_imports_not_by_name(tmp_path):
    write_repo(
        tmp_path,
        {
            "app/__init__.py": "from .store import save\n",
            "app/store.py": """
            def save(x):
                return x

            def items():
                return []

            def get(key):
                return key
            """,
            "app/api.py": """
            import requests
            from . import store
            from app import save

            def handler(d):
                for k, v in d.items():        # dict method, not store.items
                    store.save(v)             # `from . import store`
                save(k)                       # re-exported by app/__init__.py
                return requests.get("https://example.com")  # external, not store.get
            """,
        },
    )
    g = graph.build(tmp_path)
    assert callees(g, "app/api.py::handler") == {"app/store.py::save"}


def test_methods_resolve_through_self_super_and_inheritance(tmp_path):
    write_repo(
        tmp_path,
        {
            "base.py": """
            class Base:
                def __init__(self):
                    self.ready = True

                def run(self):
                    return self.step()

                def step(self):
                    return 1

                def helper(self):
                    return 2
            """,
            "child.py": """
            from base import Base

            class Child(Base):
                def __init__(self):
                    super().__init__()

                def step(self):
                    return self.helper()

            def make():
                return Child()
            """,
        },
    )
    g = graph.build(tmp_path)
    assert callees(g, "child.py::Child.__init__") == {"base.py::Base.__init__"}
    assert callees(g, "child.py::Child.step") == {"base.py::Base.helper"}  # inherited
    assert callees(g, "base.py::Base.run") == {"base.py::Base.step", "child.py::Child.step"}  # plus the override
    assert callees(g, "child.py::make") == {"child.py::Child.__init__"}  # instantiation runs __init__


def test_nested_helpers_and_unknown_objects(tmp_path):
    write_repo(
        tmp_path,
        {
            "m.py": """
            class Cache:
                def lookup(self, k):
                    return k

            def outer(c, d):
                def inner():
                    return c.lookup(1)        # unknown type: guessed by a repo method name
                d.get("x")                    # builtin method name: never guessed
                return inner()
            """,
        },
    )
    g = graph.build(tmp_path)
    assert callees(g, "m.py::outer") == {"m.py::outer.inner"}
    assert callees(g, "m.py::outer.inner") == {"m.py::Cache.lookup"}


def test_from_dot_import_is_one_level_up():
    pf = graph.ParsedFile("pkg/a.py", b"", None, graph.Module("pkg/a.py"))  # type: ignore[arg-type]
    graph._imports(graph._PARSER.parse(b"from . import b\n").root_node.children[0], pf.module)
    assert pf.module.imports["b"] == ".b"
    assert graph.resolve_relative(".b", "pkg/a.py") == "pkg.b"


# ---------------------------------------------------------------- proof


TIMED = """
import time

def timed_{n}(fn):
    start = time.time()
    fn()
    return time.time() - start
"""


def test_proof_uses_what_tests_actually_ran(tmp_path):
    """The call graph says the test reaches `timed`; it never runs it, so the patch is not verified."""
    write_repo(
        tmp_path,
        {
            "m.py": TIMED.format(n=1),
            "tests/test_m.py": """
            import m

            def test_never_calls_it():
                if False:
                    m.timed_1(lambda: None)
            """,
        },
    )
    g = graph.build(tmp_path)
    assert "m.py::timed_1" in g.test_reachable()
    f = next(f for f in review(tmp_path, python=sys.executable, top=None).findings if f.intent == "wall-clock-timing")
    assert f.proof.value == "unverified"
    assert "no test reaches" in f.proof_detail
    assert f.reached_by == [] and not f.covered


def test_parallel_proof_names_the_tests_that_ran(tmp_path):
    files = {f"m{n}.py": TIMED.format(n=n) for n in range(3)}
    files["tests/test_m.py"] = "\n".join(
        f"from m{n} import timed_{n}\n\ndef test_{n}():\n    assert timed_{n}(lambda: None) >= 0\n" for n in range(3)
    )
    write_repo(tmp_path, files)
    r = review(tmp_path, python=sys.executable, top=None, proof_jobs=3)
    timed = [f for f in r.findings if f.intent == "wall-clock-timing"]
    assert len(timed) == 3
    for f in timed:
        n = f.function[-1]
        assert f.proof.value == "verified", f.proof_detail
        assert f.reached_by == [f"tests/test_m.py::test_{n}"]


def test_src_layout_runs_the_patched_copy(tmp_path):
    """No install and no pytest config: the copy's src/ goes on the path, so the suite can run."""
    write_repo(
        tmp_path,
        {
            "src/pkg/__init__.py": "",
            "src/pkg/m.py": TIMED.format(n=1),
            "tests/test_m.py": """
            from pkg.m import timed_1

            def test_timed():
                assert timed_1(lambda: None) >= 0
            """,
        },
    )
    f = next(f for f in review(tmp_path, python=sys.executable, top=None).findings if f.intent == "wall-clock-timing")
    assert f.proof.value == "verified", f.proof_detail


def test_parallel_failure_is_rerun_alone(tmp_path, monkeypatch):
    files = {f"m{n}.py": TIMED.format(n=n) for n in range(2)}
    files["tests/test_m.py"] = "\n".join(
        f"from m{n} import timed_{n}\n\ndef test_{n}():\n    assert timed_{n}(lambda: None) >= 0\n" for n in range(2)
    )
    write_repo(tmp_path, files)
    real, calls = proof.run_tests, []

    def flaky(workdir, python, timeout, **kw):
        if kw.get("trace_out") is None:
            calls.append(workdir)
            if len(calls) == 1:  # the first patched run fails, as a port clash would
                return proof.TestRun(False, 0.0, "address already in use")
        return real(workdir, python, timeout, **kw)

    monkeypatch.setattr(proof, "run_tests", flaky)
    r = review(tmp_path, python=sys.executable, top=None, proof_jobs=2)
    timed = [f for f in r.findings if f.intent == "wall-clock-timing"]
    assert len(timed) == 2 and all(f.proof.value == "verified" for f in timed)
    assert sum("re-run on its own" in f.proof_detail for f in timed) == 1
    assert len(calls) == 3
