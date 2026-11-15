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
import importlib, importlib.metadata, json, sys
out = {}
dists = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
for sym in json.loads(sys.argv[1]):
    parts = sym.split(".")
    status = "unchecked"
    if dists.get(sym):
        try:
            importlib.metadata.distribution(dists[sym])
        except importlib.metadata.PackageNotFoundError:
            out[sym] = "unchecked"  # the library isn't installed here: nothing to check against
            continue
    for i in range(len(parts), 0, -1):
        mod = ".".join(parts[:i])
        try:
            obj = importlib.import_module(mod)
        except ModuleNotFoundError as exc:
            if exc.name and (mod == exc.name or mod.startswith(exc.name + ".")):
                continue  # no such module: try a shorter prefix
            status = "broken"  # the module exists but a dependency of its own is missing
            break
        except Exception:
            status = "broken"  # the module exists but fails to import, e.g. asking for an extra
            break
        try:
            for attr in parts[i:]:
                obj = getattr(obj, attr)
            status = "ok"
        except AttributeError:
            status = "missing"
        except Exception:
            status = "unchecked"  # e.g. a lazy attribute that needs an optional extra
        break
    if status == "broken":
        status = "unchecked"
    elif status == "unchecked":
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


def check_symbols(
    findings: list[Finding], python: str, symbols: dict[str, str], dists: dict[str, str] | None = None
) -> None:
    """symbols maps finding id -> dotted symbol (only for importable, dotted symbols).

    dists maps a symbol to the distribution that provides it. When that distribution isn't
    installed in `python`, the symbol is "unchecked": absent there says nothing about whether
    the API exists, and the stack's lockfile already decided the card applies.
    """
    wanted = sorted(set(symbols.values()))
    if not wanted:
        return
    try:
        proc = subprocess.run(
            [python, "-c", _CHECK_SCRIPT, json.dumps(wanted), json.dumps(dists or {})],
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
