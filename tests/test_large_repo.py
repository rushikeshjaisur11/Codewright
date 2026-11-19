"""Big-repo controls: which folders are reviewed, and live re-review that costs nothing while idle."""

import json
import time

import pytest

from codewright import graph, scanscope
from codewright.service import Workspace


def _repo(tmp_path):
    files = {
        "app/main.py": "def handler():\n    return 1\n",
        "app/db/migrations/0001_init.py": "X = 1\n",
        "vendor/lib/thing.py": "def vendored():\n    return 2\n",
        "gen/api_pb2.py": "Y = 2\n",
        "tools/script.py": "print(1)\n",
    }
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return tmp_path


def test_scope_leaves_out_folders_and_patterns(tmp_path):
    root = _repo(tmp_path)
    assert len(list(graph.iter_python_files(root))) == 5
    scanscope.save(root, [], ["vendor", "**/migrations", "*_pb2.py"])
    kept = sorted(p.relative_to(root).as_posix() for p in graph.iter_python_files(root))
    assert kept == ["app/main.py", "tools/script.py"]
    assert set(graph.build(root).files) == set(kept)
    scanscope.save(root, ["app"], ["**/migrations"])
    assert [p.relative_to(root).as_posix() for p in graph.iter_python_files(root)] == ["app/main.py"]


def test_scope_summary_and_config_kept(tmp_path):
    root = _repo(tmp_path)
    cfg = root / ".codewright" / "config.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"index_url": "https://example/simple"}))
    out = scanscope.save(root, [], ["vendor"])
    assert json.loads(cfg.read_text())["index_url"] == "https://example/simple"  # other settings kept
    s = scanscope.summary(root)
    assert s["total"] == 5 and s["kept"] == 4 and out["exclude"] == ["vendor"]
    vendor = next(f for f in s["folders"] if f["path"] == "vendor")
    assert vendor == {"path": "vendor", "files": 1, "kept": 0, "depth": 1}


def _wait_for(cond, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


@pytest.mark.parametrize("events", [True, False], ids=["file-events", "polling"])
def test_live_rereviews_a_saved_file_in_scope_only(tmp_path, monkeypatch, events):
    if not events:
        import watchfiles

        monkeypatch.setattr(watchfiles, "watch", lambda *a, **k: (_ for _ in ()).throw(OSError("no inotify")))
    root = _repo(tmp_path)
    scanscope.save(root, [], ["vendor"])
    w = Workspace(root)
    w.run_review(proof=False)
    w.start_watch(interval=0.2)
    try:
        time.sleep(1.0)
        rev = w.revision
        (root / "vendor" / "lib" / "thing.py").write_text("def vendored():\n    return 3\n")  # out of scope
        time.sleep(1.5)
        assert w.revision == rev
        (root / "app" / "main.py").write_text("def handler():\n    return 2\n")
        assert _wait_for(lambda: w.revision > rev and not w.running)
        assert w.last_change["files"] == ["app/main.py"]
    finally:
        w.stop_watch()
