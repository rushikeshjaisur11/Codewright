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
