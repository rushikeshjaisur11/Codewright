"""The critic: a suggestion that can't name a file, an API version and a gain is never shown.

It also checks every suggested symbol against the target environment, so an API
that doesn't exist in the installed version is dropped before anyone sees it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .models import Finding, ProofStatus

REQUIRED = ("file", "api", "gain", "risk", "when_not", "threshold")

_CHECK_SCRIPT = r"""
import importlib, json, sys
out = {}
for sym in json.loads(sys.argv[1]):
    parts = sym.split(".")
    status = "unchecked"
    for i in range(len(parts), 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:i]))
        except Exception:
            continue
        try:
            for attr in parts[i:]:
                obj = getattr(obj, attr)
            status = "ok"
        except AttributeError:
            status = "missing"
        break
    if status == "unchecked":
        import builtins
        obj = builtins
        try:
            for attr in parts:
                obj = getattr(obj, attr)
            status = "ok"
        except AttributeError:
            pass
    out[sym] = status
print(json.dumps(out))
"""


def target_python(root: str | Path) -> str:
    """Prefer the repo's own virtualenv so symbols are checked against what it installs."""
    root = Path(root)
    for candidate in (".venv/bin/python", "venv/bin/python", ".venv/Scripts/python.exe"):
        if (root / candidate).exists():
            return str(root / candidate)
    return sys.executable


def check_symbols(findings: list[Finding], python: str, symbols: dict[str, str]) -> None:
    """symbols maps finding id -> dotted symbol (only for importable, dotted symbols)."""
    wanted = sorted(set(symbols.values()))
    if not wanted:
        return
    try:
        proc = subprocess.run(
            [python, "-c", _CHECK_SCRIPT, json.dumps(wanted)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        result = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        result = {}
    for f in findings:
        sym = symbols.get(f.id)
        if sym:
            f.symbol_check = result.get(sym, "unchecked")


def is_dotted_symbol(symbol: str) -> bool:
    return all(part.isidentifier() for part in symbol.split(".")) and "." in symbol


def review(findings: list[Finding]) -> tuple[list[Finding], list[Finding]]:
    kept, rejected = [], []
    for f in findings:
        reason = _reason(f)
        if reason:
            f.rejected_reason = reason
            rejected.append(f)
        else:
            kept.append(f)
    return kept, rejected


def _reason(f: Finding) -> str | None:
    for name in REQUIRED:
        if not str(getattr(f, name) or "").strip():
            return f"missing {name}"
    if f.line <= 0:
        return "missing line"
    if not (1 <= f.effort <= 6):
        return "missing effort"
    if not f.sources:
        return "no source cited"
    if f.symbol_check == "missing":
        return "suggested API does not exist in the installed version"
    if f.proof == ProofStatus.BROKE_TESTS:
        return "patch breaks the test suite"
    return None
