"""Which folders a review reads: include and exclude patterns kept in ``.codewright/config.json``.

On a big repo much of the Python is not the team's: vendored packages, generated clients, migrations,
notebooks exported to scripts. Leaving them out makes every scan faster and the findings, maps and agent
architecture about the code that matters. The patterns are committed with the repo, so the team shares them.

    {"scan": {"include": ["services/api", "libs/core"], "exclude": ["**/migrations", "vendor", "*_pb2.py"]}}

A pattern without wildcards is a folder or file (and everything under it); with `*` or `?` it is matched
against the path relative to the repo, where `*` also crosses `/`. With no `include`, everything is included.
"""

from __future__ import annotations

import fnmatch
import json
import os
from collections import Counter
from pathlib import Path

from .stack import SKIP_DIRS


def _config_path(root: str | Path) -> Path:
    return Path(root) / ".codewright" / "config.json"


_CACHE: dict[str, tuple[float, dict]] = {}


def load(root: str | Path) -> dict:
    """{"include": [...], "exclude": [...]} (cached until the config file changes)."""
    p = _config_path(root)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {"include": [], "exclude": []}
    hit = _CACHE.get(str(p))
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        scan = data.get("scan") if isinstance(data, dict) else None
    except (OSError, ValueError):
        scan = None
    scan = scan if isinstance(scan, dict) else {}
    out = {
        "include": [str(x).strip().strip("/") for x in scan.get("include", []) if str(x).strip().strip("/")],
        "exclude": [str(x).strip().strip("/") for x in scan.get("exclude", []) if str(x).strip().strip("/")],
    }
    _CACHE[str(p)] = (mtime, out)
    return out


def save(root: str | Path, include: list[str], exclude: list[str]) -> dict:
    """Write the patterns, keeping whatever else the repo's config holds."""
    from .feedback import ensure_dir

    ensure_dir(root)
    p = _config_path(root)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    clean = lambda xs: [x.strip().strip("/") for x in xs if x and x.strip().strip("/")]  # noqa: E731
    data["scan"] = {"include": clean(include), "exclude": clean(exclude)}
    p.write_text(json.dumps(data, indent=2) + "\n")
    _CACHE.pop(str(p), None)
    return load(root)


def _matches(rel: str, pattern: str) -> bool:
    """`vendor` or `services/api`: that path and everything under it. With wildcards, the pattern is tried on the
    file and on each folder above it (`*` crosses `/`; a leading `**/` also matches at the top)."""
    if not any(c in pattern for c in "*?["):
        return rel == pattern or rel.startswith(pattern + "/")
    parts = rel.split("/")
    prefixes = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    pats = [pattern] + ([pattern[3:]] if pattern.startswith("**/") else [])
    return any(fnmatch.fnmatchcase(pre, pat) for pre in prefixes for pat in pats)


def included(rel: str, scope: dict) -> bool:
    """Is the file at `rel` (posix, relative to the repo) part of the review?"""
    if scope["include"] and not any(_matches(rel, p) for p in scope["include"]):
        return False
    return not any(_matches(rel, p) for p in scope["exclude"])


def all_python_files(root: Path):
    """Every Python file outside tool folders (.git, venvs, node_modules...), before the scope applies."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield Path(dirpath) / name


def summary(root: str | Path, depth: int = 2) -> dict:
    """The folders (to `depth` levels) with how many Python files each holds and how many the scope keeps:
    what the Settings page shows so the patterns can be chosen by looking."""
    root = Path(root).resolve()
    scope = load(root)
    total, kept = Counter(), Counter()
    all_n = kept_n = 0
    for p in all_python_files(root):
        rel = p.relative_to(root).as_posix()
        parts = rel.split("/")[:-1]
        keep = included(rel, scope)
        all_n += 1
        kept_n += keep
        for d in range(1, min(depth, len(parts)) + 1):
            key = "/".join(parts[:d])
            total[key] += 1
            kept[key] += keep
    folders = [{"path": k, "files": v, "kept": kept[k], "depth": k.count("/") + 1} for k, v in sorted(total.items())]
    return {**scope, "folders": folders, "total": all_n, "kept": kept_n}
