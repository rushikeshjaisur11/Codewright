"""Release watcher: which dependencies have new releases, and which cards an upgrade unlocks.

Uses the public PyPI JSON API and caches answers in ``.codewright/releases.json``
for six hours. A new release since the last run is reported as a reason to
re-scan, because it can make existing code obsolete (a deprecation) or unlock a
better API (a new capability card applies).
"""

from __future__ import annotations

import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from packaging.version import InvalidVersion, Version

from . import cards as card_store
from .models import Stack

PYPI = "https://pypi.org/pypi/{name}/json"
TTL = 6 * 3600


def _cache_path(root: str | Path) -> Path:
    return Path(root) / ".codewright" / "releases.json"


def _load_cache(root: str | Path | None) -> dict:
    if root is None or not _cache_path(root).exists():
        return {}
    try:
        return json.loads(_cache_path(root).read_text())
    except json.JSONDecodeError:
        return {}


def _save_cache(root: str | Path | None, cache: dict) -> None:
    if root is None:
        return
    from .feedback import ensure_dir

    ensure_dir(root)
    p = _cache_path(root)
    p.write_text(json.dumps(cache, indent=2))


def latest(name: str, cache: dict | None = None, timeout: float = 8.0) -> dict | None:
    """{"version", "released", "requires_python"} for the newest non-prerelease, or None offline."""
    cache = cache if cache is not None else {}
    hit = cache.get(name)
    if hit and time.time() - hit.get("fetched", 0) < TTL:
        return hit
    try:
        req = urllib.request.Request(PYPI.format(name=name), headers={"User-Agent": "codewright"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except Exception:
        return hit  # stale beats nothing
    version = data["info"]["version"]
    files = data.get("releases", {}).get(version) or []
    released = min((f.get("upload_time_iso_8601", "") for f in files), default="")
    out = {
        "version": version,
        "released": released[:10],
        "requires_python": data["info"].get("requires_python"),
        "fetched": time.time(),
    }
    cache[name] = out
    return out


def prefetch(names: list[str], cache: dict, timeout: float = 8.0) -> None:
    """Look up many packages at once (8 at a time), filling `cache`; one slow package no longer
    holds up the rest, so a repo with 60 dependencies takes seconds, not minutes."""
    todo = [n for n in dict.fromkeys(names) if not (cache.get(n) and time.time() - cache[n].get("fetched", 0) < TTL)]
    if not todo:
        return
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n: latest(n, cache, timeout), todo))


def bump(installed: str | None, new: str) -> str:
    if not installed:
        return "unknown"
    try:
        a, b = Version(installed), Version(new)
    except InvalidVersion:
        return "unknown"
    if b <= a:
        return "current"
    if b.major != a.major:
        return "major"
    if b.minor != a.minor:
        return "minor"
    return "patch"


def check(stack: Stack, root: str | Path | None = None, names: list[str] | None = None) -> list[dict]:
    cache = _load_cache(root)
    previous = {k: v.get("version") for k, v in cache.items()}
    cards = card_store.load()
    out = []
    names = names or sorted(stack.dependencies)
    prefetch(names, cache)
    for name in names:
        dep = stack.dependencies.get(name)
        info = latest(name, cache)
        if info is None:
            out.append(
                {
                    "name": name,
                    "installed": dep.version if dep else None,
                    "latest": None,
                    "bump": "unknown",
                    "new_since_last_check": False,
                    "unlocks": [],
                }
            )
            continue
        installed = dep.version if dep else None
        unlocks = [
            {"card": c.id, "title": c.title}
            for c in cards
            if c.library == name
            and c.versions not in ("*", "")
            and card_store.version_matches(info["version"], c.versions)
            and not card_store.version_matches(installed, c.versions)
        ]
        out.append(
            {
                "name": name,
                "installed": installed,
                "latest": info["version"],
                "released": info["released"],
                "bump": bump(installed, info["version"]),
                "new_since_last_check": previous.get(name) not in (None, info["version"]),
                "unlocks": unlocks,
            }
        )
    _save_cache(root, cache)
    order = {"major": 0, "minor": 1, "patch": 2, "unknown": 3, "current": 4}
    return sorted(out, key=lambda r: (order[r["bump"]], r["name"]))


def latest_versions(names: list[str], fallback: dict[str, str], root: str | Path | None = None) -> dict[str, str]:
    """Exact versions for a starter repo: live from PyPI, else the bundled fallback."""
    cache = _load_cache(root)
    prefetch(names, cache, timeout=5.0)
    out = {}
    for n in names:
        info = latest(n, cache, timeout=5.0)
        out[n] = info["version"] if info else fallback.get(n, "")
    _save_cache(root, cache)
    return out
