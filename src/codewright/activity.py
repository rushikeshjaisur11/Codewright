"""What Codewright is doing right now, for the terminal running ``codewright serve`` and for the web app.

Every long step (parsing, detection, proof, a release lookup, building a map) calls ``note``. The
message goes to the ``codewright`` logger, which ``serve`` prints, and into a small ring buffer that
the web app polls with ``since`` to show a live activity feed.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

log = logging.getLogger("codewright")

_EVENTS: deque[dict] = deque(maxlen=1000)
_LOCK = threading.Lock()
_SEQ = 0


def note(repo: str, message: str, level: str = "info") -> None:
    global _SEQ
    with _LOCK:
        _SEQ += 1
        _EVENTS.append({"seq": _SEQ, "at": time.time(), "repo": repo, "message": message, "level": level})
    log.log(logging.WARNING if level in ("warn", "error") else logging.INFO, "%-18s %s", repo[:18], message)


def since(seq: int = 0, repo: str | None = None, limit: int = 200) -> dict:
    """Events after `seq` (for one repo when given), oldest first, and the newest seq to ask from next."""
    with _LOCK:
        events = [e for e in _EVENTS if e["seq"] > seq and (repo is None or e["repo"] == repo)]
        last = _SEQ
    return {"events": events[-limit:], "last": last}


class Timer:
    """`with Timer(repo, "Building the AI map"):` notes the start and how long it took (or that it failed)."""

    def __init__(self, repo: str, what: str):
        self.repo, self.what = repo, what

    def __enter__(self) -> Timer:
        self.t0 = time.perf_counter()
        note(self.repo, f"{self.what}…")
        return self

    def __exit__(self, kind, exc, tb) -> None:
        took = time.perf_counter() - self.t0
        if exc is None:
            note(self.repo, f"{self.what}: done in {took:.1f}s")
        else:
            note(self.repo, f"{self.what}: failed after {took:.1f}s ({type(exc).__name__}: {exc})", "error")


def configure_terminal() -> None:
    """Print activity lines in the terminal running the server: `12:03:41 myrepo   Parsing files 1200/3071`."""
    if any(getattr(h, "_codewright", False) for h in log.handlers):
        return
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
    h._codewright = True  # type: ignore[attr-defined]
    log.addHandler(h)
    log.setLevel(logging.INFO)
    log.propagate = False
