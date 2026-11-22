"""Change coupling from git history: files that keep changing in the same commits without the code saying why.

Two files that change together in most of their commits are linked, whether or not one imports the other. When
neither imports the other (and the Python side doesn't name the other file), the link is hidden: whoever changes
one has to remember the other. Typical causes: a contract written twice (a backend route and the frontend client
that calls it, a model and a hand-written schema), copied logic, two halves of a feature split across layers,
or a value both sides have to agree on.

The measure is code-maat's: degree = shared commits / average commits of the two files. Commits that touch many
files (formatting, renames, dependency bumps) say nothing about coupling and are skipped, as are merges.
"""

from __future__ import annotations

import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

MAX_COMMITS = 3000
MAX_CHANGESET = 30  # code-maat's default: larger commits are sweeps, not features
MIN_REVS = 5
MIN_SHARED = 5
MIN_DEGREE = 0.3
SINCE = "3 years ago"

# Source files worth pairing with Python: a contract or a feature can be split across these.
SOURCE_SUFFIXES = (
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".vue",
    ".svelte",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".rb",
    ".sql",
    ".proto",
    ".graphql",
    ".gql",
    ".html",
    ".jinja",
    ".jinja2",
    ".j2",
)
NOISE_PARTS = {
    "migrations",
    "alembic",
    "versions",
    "node_modules",
    "dist",
    "build",
    "vendor",
    "locales",
    "locale",
    "i18n",
    "__snapshots__",
    "fixtures",
    "generated",
    "static",
}
NOISE_NAMES = ("settings", "config", "conftest", "__init__", "constants", "version", "_version", "types", "conf")


@dataclass
class Pair:
    a: str
    b: str
    shared: int
    revs_a: int
    revs_b: int
    degree: float
    last: int  # unix time of the latest shared commit
    subjects: list[str] = field(default_factory=list)


def history(root: str | Path, max_commits: int = MAX_COMMITS) -> tuple[list[tuple[int, str, list[str]]], str]:
    """(commits as (time, subject, files), note). The note says why there is little or no history."""
    root = Path(root)
    try:
        inside = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree", "--is-shallow-repository"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return [], "git is not available"
    if inside.returncode != 0:
        return [], "not a git repository"
    note = (
        "shallow clone: fetch the history (git fetch --unshallow) to see what changes together"
        if (inside.stdout.split()[-1:] == ["true"])
        else ""
    )
    try:
        out = subprocess.run(
            [
                "git",
                "-c",
                "core.quotepath=off",
                "log",
                "--no-merges",
                "--no-renames",
                "--name-only",
                f"--since={SINCE}",
                "-n",
                str(max_commits),
                "--format=%x00%at%x1f%s",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=120,
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return [], "git log took too long"
    commits = []
    for chunk in out.stdout.split("\0")[1:]:
        head, _, body = chunk.partition("\n")
        at, _, subject = head.partition("\x1f")
        files = [f for f in body.splitlines() if f.strip()]
        try:
            commits.append((int(at), subject.strip(), files))
        except ValueError:
            continue
    return commits, note


def _wanted(path: str) -> bool:
    if not path.endswith(SOURCE_SUFFIXES) or path.endswith((".min.js", ".d.ts")):
        return False
    parts = path.split("/")
    name = parts[-1].rsplit(".", 1)[0]
    if any(p in NOISE_PARTS for p in parts[:-1]) or name.lower() in NOISE_NAMES:
        return False
    from .graph import is_test_file

    if path.endswith(".py") and is_test_file(path):
        return False
    lowered = path.lower()
    return not any(t in lowered for t in ("/test/", "/tests/", ".test.", ".spec.", "/__tests__/", "/e2e/"))


def coupled(commits: list[tuple[int, str, list[str]]], exists) -> list[Pair]:
    """Pairs of files (at least one Python) that change together, strongest first. `exists(path)` keeps files
    that are still in the repo."""
    revs: Counter[str] = Counter()
    shared: Counter[tuple[str, str]] = Counter()
    last: dict[tuple[str, str], int] = {}
    subjects: dict[tuple[str, str], list[str]] = defaultdict(list)
    for at, subject, files in commits:
        files = sorted({f for f in files if _wanted(f)})
        if not files or len(files) > MAX_CHANGESET:
            continue
        revs.update(files)
        for i, a in enumerate(files):
            for b in files[i + 1 :]:
                if not (a.endswith(".py") or b.endswith(".py")):
                    continue
                key = (a, b)
                shared[key] += 1
                last[key] = max(last.get(key, 0), at)
                if len(subjects[key]) < 4:
                    subjects[key].append(subject)
    out = []
    for (a, b), n in shared.items():
        if n < MIN_SHARED or revs[a] < MIN_REVS or revs[b] < MIN_REVS:
            continue
        degree = n / ((revs[a] + revs[b]) / 2)
        if degree < MIN_DEGREE or not (exists(a) and exists(b)):
            continue
        out.append(Pair(a, b, n, revs[a], revs[b], round(degree, 2), last[(a, b)], subjects[(a, b)]))
    out.sort(key=lambda p: (-p.degree * p.shared, p.a, p.b))
    return out
