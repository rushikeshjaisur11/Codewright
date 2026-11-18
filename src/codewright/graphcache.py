"""What was read from each file, kept between runs, so reopening a big repo does not parse it all again.

Parsing is most of a cold review (about 35 s for 12,000 files even in parallel). What the graph keeps per
file (imports, functions, calls, classes and the visitors' results) is plain data, so it is stored per file
with the file's hash, and an unchanged file is rebuilt from it without being parsed; its syntax tree is
parsed later only if something asks for it.

The cache is pickled, so it lives in the user's cache directory (``~/.cache/codewright``), never inside the
repository: a cloned repo must not be able to ship a file that this process unpickles.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import pickle
from pathlib import Path

from . import __version__

VERSION = 1


def _dir() -> Path:
    base = os.environ.get("CODEWRIGHT_CACHE_DIR") or os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "codewright" / "graphs"


def _path(root: Path) -> Path:
    return _dir() / (hashlib.sha1(str(root.resolve()).encode()).hexdigest()[:20] + ".pkl")


def signature() -> str:
    """Changes when the code that reads files (or registers visitors) does."""
    h = hashlib.sha1(f"{VERSION}:{__version__}".encode())
    here = Path(__file__).parent
    for p in [here / "graph.py", here / "entries.py", here / "models.py", *sorted((here / "intents").glob("*.py"))]:
        with contextlib.suppress(OSError):
            h.update(p.read_bytes())
    return h.hexdigest()


def load(root: Path) -> dict[str, tuple]:
    """{relative path: (sha1, module, classes, cache)}, or {} when missing or from another version."""
    if os.environ.get("CODEWRIGHT_GRAPH_CACHE", "1") in ("0", "false", "no"):
        return {}
    try:
        with open(_path(root), "rb") as fh:
            data = pickle.load(fh)  # our own file, in the user's cache dir (see the module docstring)
        return data["files"] if data.get("sig") == signature() else {}
    except (OSError, EOFError, pickle.UnpicklingError, KeyError, AttributeError, TypeError, ValueError):
        return {}


def save(root: Path, files: dict[str, tuple]) -> None:
    if os.environ.get("CODEWRIGHT_GRAPH_CACHE", "1") in ("0", "false", "no"):
        return
    p = _path(root)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with open(tmp, "wb") as fh:
            pickle.dump({"sig": signature(), "files": files}, fh, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(p)
    except (OSError, pickle.PicklingError, TypeError, AttributeError):
        with contextlib.suppress(OSError):
            p.with_suffix(".tmp").unlink()
