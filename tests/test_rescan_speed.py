"""A rescan of a big repo must not stall in "Matching code against capability cards": the per-finding work
(which project a file belongs to, which cards apply, the patch text) is cached or bounded, it reports
progress, and an unchanged detection cache is not rewritten."""

import difflib
import random
import subprocess

from codewright import detectcache, stack
from codewright.engine import review
from codewright.suggest import unified_diff


def _repo(tmp_path, n=40):
    for i in range(n):
        p = tmp_path / f"pkg{i % 4}" / f"m{i}.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "import time\n\n" + "".join(f"async def f{j}(u):\n    time.sleep(1)\n    return u\n\n" for j in range(5))
        )
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n")
    return tmp_path


def test_project_lookup_is_done_once_per_folder(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    st = stack.discover(root)
    calls = []
    real = stack.Path.exists
    monkeypatch.setattr(stack.Path, "exists", lambda self: calls.append(self) or real(self))
    for i in range(40):
        for _ in range(5):
            stack.for_path(st, f"pkg{i % 4}/m{i}.py")
    assert len(calls) <= 4 * 8  # one walk per folder, not one per finding


def test_patch_text_applies_like_difflib(tmp_path):
    rnd = random.Random(7)
    for _ in range(300):
        before = [f"l{rnd.randint(0, 4)}\n" for _ in range(rnd.randint(0, 30))]
        after = list(before)
        for _ in range(rnd.randint(1, 3)):
            i = rnd.randint(0, len(after))
            if rnd.random() < 0.5 or not after or i == len(after):
                after.insert(i, f"new{rnd.randint(0, 9)}\n")
            else:
                after[i] = "changed\n"
        got = unified_diff("x.py", before, after)
        if before == after:
            assert got is None
            continue
        want = "".join(difflib.unified_diff(before, after, fromfile="a/x.py", tofile="b/x.py"))
        assert got.splitlines()[:2] == want.splitlines()[:2]
        (tmp_path / "x.py").write_text("".join(before))
        (tmp_path / "p.diff").write_text(got)
        subprocess.run(["git", "apply", "p.diff"], cwd=tmp_path, check=True, capture_output=True)
        assert (tmp_path / "x.py").read_text() == "".join(after)


def test_rescan_reports_progress_and_keeps_the_cache_file(tmp_path):
    root = _repo(tmp_path)
    first = review(root, run_proof=False, top=None)
    cache = detectcache._path(root)
    stamp = cache.stat().st_mtime_ns
    seen = []
    second = review(root, run_proof=False, top=None, progress=lambda s, d, t: seen.append((s, d, t)))
    assert len(second.findings) == len(first.findings) > 0
    assert cache.stat().st_mtime_ns == stamp  # nothing changed: not rewritten
    assert [t for s, d, t in seen if s == "detect" and t]  # the stage counts, so the bar moves
    (root / "pkg0" / "m0.py").write_text("x = 1\n")
    review(root, run_proof=False, top=None)
    assert cache.stat().st_mtime_ns != stamp  # a file changed: saved


def _edges(g):
    return {k: set(v) for k, v in g.edges.items() if v}, set(g.entry_points)


def _fresh(root):
    from codewright import graph

    graph._RESOLVED.clear()
    return graph.build(root)


def test_rescan_in_the_same_process_resolves_only_edited_files_and_matches_a_full_build(tmp_path, monkeypatch):
    from codewright import graph

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "a.py").write_text(
        "from app.b import helper, Thing\n\ndef run():\n    helper()\n    Thing().go()\n\n"
        "if __name__ == '__main__':\n    run()\n\ndef main():\n    run()\n"
    )
    (tmp_path / "app" / "b.py").write_text(
        "def helper():\n    return other()\n\ndef other():\n    return 1\n\nclass Thing:\n    def go(self):\n"
        "        return helper()\n"
    )
    (tmp_path / "app" / "c.py").write_text("from app.b import other, helper\n\ndef c():\n    return other()\n")
    first = graph.build(tmp_path)
    assert _edges(first) == _edges(_fresh(tmp_path))

    graph.build(tmp_path)  # prime the in-process resolution
    calls, inside = [], [False]
    real, resolve = graph.Resolver.callees, graph._resolve_calls

    def spy(self, pf, fn, call):
        if inside[0]:  # only call-graph resolution; entry detection asks the resolver too
            calls.append(pf.path)
        return real(self, pf, fn, call)

    def resolving(g):
        inside[0] = True
        try:
            resolve(g)
        finally:
            inside[0] = False

    monkeypatch.setattr(graph.Resolver, "callees", spy)
    monkeypatch.setattr(graph, "_resolve_calls", resolving)
    again = graph.build(tmp_path)
    assert calls == []  # nothing changed: nothing resolved again
    assert _edges(again) == _edges(first)

    # A body edit: only that file is resolved again, and the result is what a full build gives.
    (tmp_path / "app" / "c.py").write_text("from app.b import other, helper\n\ndef c():\n    return helper()\n")
    edited = graph.build(tmp_path)
    assert set(calls) == {"app/c.py"}
    monkeypatch.setattr(graph.Resolver, "callees", real)
    assert _edges(edited) == _edges(_fresh(tmp_path))
    assert "app/b.py::helper" in edited.edges["app/c.py::c"]

    # A new name somewhere: everything is resolved again, and still matches.
    graph.build(tmp_path)
    (tmp_path / "app" / "b.py").write_text(
        (tmp_path / "app" / "b.py").read_text() + "\ndef extra():\n    return helper()\n"
    )
    assert _edges(graph.build(tmp_path)) == _edges(_fresh(tmp_path))


def test_test_clients_found_by_quoted_stem(tmp_path):
    from codewright import graph

    (tmp_path / "server.py").write_text("import click\n\n@click.command()\ndef serve():\n    pass\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_cli.py").write_text(
        "import importlib.util\nfrom click.testing import CliRunner\n"
        "spec = importlib.util.spec_from_file_location(\"server\", 'server.py')\n\n"
        "def test_it():\n    CliRunner().invoke(None)\n"
    )
    g = graph.build(tmp_path)
    assert "server.py::serve" in g._routes_reached_by_test_clients()
