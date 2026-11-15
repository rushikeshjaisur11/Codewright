"""PR mode: limit a review to the lines a branch changed."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .models import Finding

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_lines(root: str | Path, base: str) -> dict[str, set[int]]:
    """Map of file -> changed line numbers (new side) between `base` and the working tree."""
    proc = subprocess.run(
        ["git", "diff", "--relative", "--unified=0", "--no-color", base, "--", "*.py"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"git diff {base} failed")
    out: dict[str, set[int]] = {}
    current = None
    for line in proc.stdout.splitlines():
        if line.startswith("+++ "):
            path = line[4:]
            current = None if path == "/dev/null" else path.removeprefix("b/")
            if current:
                out.setdefault(current, set())
        elif current and (m := _HUNK.match(line)):
            start, count = int(m.group(1)), int(m.group(2) or 1)
            out[current].update(range(start, start + max(count, 1)))
    # Untracked files count as entirely changed.
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "*.py"], cwd=root, capture_output=True, text=True
    ).stdout.split()
    for path in untracked:
        out[path] = {-1}
    return out


def in_scope(f: Finding, span: tuple[int, int], changed: dict[str, set[int]]) -> bool:
    lines = changed.get(f.file)
    if not lines:
        return False
    if -1 in lines:
        return True
    start, end = span
    return any(start <= ln <= end for ln in lines)
