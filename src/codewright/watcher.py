"""Release watcher: which dependencies have new releases, and which cards an upgrade unlocks.

Asks the configured package index (PyPI by default, or your own Artifactory, Nexus...;
see ``index.py``) and caches answers in ``.codewright/releases.json`` for six hours. A new
release since the last run is reported as a reason to re-scan, because it can make existing
code obsolete (a deprecation) or unlock a better API (a new capability card applies).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from packaging.version import InvalidVersion, Version

from . import cards as card_store
from .index import PackageIndex
from .index import resolve as resolve_index
from .models import Stack

TTL = 6 * 3600


def _cache_path(root: str | Path) -> Path:
    return Path(root) / ".codewright" / "releases.json"


def _load_cache(root: str | Path | None, index: PackageIndex) -> dict:
    """Cached answers, as long as they came from the same index (switching index starts afresh)."""
    if root is None or not _cache_path(root).exists():
        return {}
    try:
        data = json.loads(_cache_path(root).read_text())
    except json.JSONDecodeError:
        return {}
    if data.get("_index", PackageIndex("https://pypi.org/simple", "").display) != index.display:
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _save_cache(root: str | Path | None, cache: dict, index: PackageIndex) -> None:
    if root is None:
        return
    from .feedback import ensure_dir

    ensure_dir(root)
    p = _cache_path(root)
    p.write_text(json.dumps({"_index": index.display, **cache}, indent=2))


def latest(
    name: str, cache: dict | None = None, timeout: float = 8.0, index: PackageIndex | None = None
) -> dict | None:
    """{"version", "released", "requires_python"} for the newest non-prerelease, or None offline."""
    from .index import latest_release

    cache = cache if cache is not None else {}
    hit = cache.get(name)
    if hit and time.time() - hit.get("fetched", 0) < TTL:
        return hit
    try:
        found = latest_release(index or resolve_index(), name, timeout)
    except Exception:
        return hit  # stale beats nothing
    if found is None:
        return hit
    out = {**found, "fetched": time.time()}
    cache[name] = out
    return out


def prefetch(names: list[str], cache: dict, timeout: float = 8.0, index: PackageIndex | None = None) -> None:
    """Look up many packages at once (8 at a time), filling `cache`; one slow package no longer
    holds up the rest, so a repo with 60 dependencies takes seconds, not minutes."""
    todo = [n for n in dict.fromkeys(names) if not (cache.get(n) and time.time() - cache[n].get("fetched", 0) < TTL)]
    if not todo:
        return
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n: latest(n, cache, timeout, index), todo))


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


def check(
    stack: Stack,
    root: str | Path | None = None,
    names: list[str] | None = None,
    graph=None,
    on_row: Callable[[dict], None] | None = None,
) -> list[dict]:
    """Newer releases per dependency, the cards an upgrade unlocks and, with the repo's code graph,
    an upgrade preview: findings that would appear or go away at the latest version.

    `on_row(row)` is called as each dependency's lookup finishes, so a UI can show rows while the rest are
    still being asked. The returned list is sorted by how big the bump is."""
    index = resolve_index(root)
    cache = _load_cache(root, index)
    previous = {k: v.get("version") for k, v in cache.items()}
    cards = card_store.load()
    names = names or sorted(stack.dependencies)
    libraries = {c.library for c in cards if c.library and c.intent != card_store.REFERENCE}
    matches: list | None = None  # detected on first use: only an upgrade preview needs it

    def row_for(name: str, info: dict | None) -> dict:
        nonlocal matches
        dep = stack.dependencies.get(name)
        installed = dep.version if dep else None
        if info is None:
            return {
                "name": name,
                "installed": installed,
                "latest": None,
                "bump": "unknown",
                "new_since_last_check": False,
                "unlocks": [],
            }
        unlocks = [
            {"card": c.id, "title": c.title, "topic": c.topic, "reference": c.intent == card_store.REFERENCE}
            for c in cards
            if c.library == name
            and c.versions not in ("*", "")
            and card_store.version_matches(info["version"], c.versions)
            and not card_store.version_matches(installed, c.versions)
        ]
        preview = None
        if graph is not None and installed and installed != info["version"] and name in libraries:
            if matches is None:
                from .intents import detect

                matches = detect(graph)
            preview = upgrade_preview(graph, stack, name, info["version"], matches, cards)
        return {
            "name": name,
            "installed": installed,
            "latest": info["version"],
            "released": info["released"],
            "bump": bump(installed, info["version"]),
            "new_since_last_check": previous.get(name) not in (None, info["version"]),
            "unlocks": unlocks,
            "preview": preview,
        }

    out = []
    todo = [n for n in dict.fromkeys(names)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(latest, n, cache, 8.0, index): n for n in todo}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                info = fut.result()
            except Exception:
                info = None
            row = row_for(name, info)
            out.append(row)
            if on_row:
                on_row(row)
    _save_cache(root, cache, index)
    order = {"major": 0, "minor": 1, "patch": 2, "unknown": 3, "current": 4}
    return sorted(out, key=lambda r: (order[r["bump"]], r["name"]))


def upgrade_preview(graph, stack: Stack, name: str, version: str, matches=None, cards=None) -> dict:
    """What the repo's own code would show if `name` were at `version`: findings that appear and disappear.

    Re-runs card matching on the code already scanned with only that one version changed; no files
    are re-parsed and nothing is installed.
    """
    import copy

    from .intents import detect
    from .suggest import make_findings

    cards = cards if cards is not None else card_store.load()
    matches = matches if matches is not None else detect(graph)
    upgraded = copy.deepcopy(stack)
    dep = upgraded.dependencies.get(name)
    if dep is None:
        return {"new": [], "resolved": []}
    dep.version = version

    def by_card(st):
        return {(f.card_id, f.file, f.function): f for f in make_findings(matches, st, graph, cards)}

    now, then = by_card(stack), by_card(upgraded)
    row = lambda f: {"card": f.card_id, "title": f.title, "file": f.file, "line": f.line, "autofix": bool(f.patch)}  # noqa: E731
    return {
        "new": [row(f) for k, f in sorted(then.items()) if k not in now],
        "resolved": [row(f) for k, f in sorted(now.items()) if k not in then],
    }


def latest_versions(names: list[str], fallback: dict[str, str], root: str | Path | None = None) -> dict[str, str]:
    """Exact versions for a starter repo: live from the package index, else the bundled fallback."""
    index = resolve_index(root)
    cache = _load_cache(root, index)
    prefetch(names, cache, timeout=5.0, index=index)
    out = {}
    for n in names:
        info = latest(n, cache, timeout=5.0, index=index)
        out[n] = info["version"] if info else fallback.get(n, "")
    _save_cache(root, cache, index)
    return out
