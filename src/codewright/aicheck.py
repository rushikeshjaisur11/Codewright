"""A second opinion from the configured model on one finding, for what tests cannot prove.

Tests verify a patch when they reach the changed code. When there is no patch, no test reaches it, or the
change is about behaviour tests do not cover, the user can ask the model to read the finding against the
code. The verdict is kept in ``.codewright/ai-checks.json`` and stays attached to the finding while its
code and patch are unchanged.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from .llm import LLMConfig, complete
from .llm import status as llm_status
from .models import Finding

VERDICTS = ("confirmed", "needs-changes", "wrong")

SYSTEM = (
    "You check a change suggested by a static analyzer for a Python repository. You get the finding, the code "
    "it is about, the library version it was checked against and, when there is one, the patch. Decide whether "
    "the suggestion is correct and safe for this code as written: does the API exist in that version, does the "
    "patch keep the behaviour (arguments, return values, errors, side effects, thread or async safety), and do "
    "the 'when not to' conditions apply here? Be concrete and refer to the code. Reply with JSON only: "
    '{"verdict": "confirmed" | "needs-changes" | "wrong", "confidence": 0.0-1.0, "reason": "...", '
    '"risks": ["..."], "test": "a test that would prove it, in a sentence"}'
)


def _path(root: str | Path) -> Path:
    return Path(root) / ".codewright" / "ai-checks.json"


def key(f: Finding | dict) -> str:
    get = f.get if isinstance(f, dict) else lambda k, d="": getattr(f, k, d)
    return hashlib.sha1(f"{get('id')}\n{get('patch') or ''}\n{get('code_today') or ''}".encode()).hexdigest()


def load(root: str | Path) -> dict[str, dict]:
    try:
        data = json.loads(_path(root).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def attach(root: str | Path, findings: list[dict]) -> None:
    """Add `ai_check` to each finding dict whose code and patch are what was checked."""
    store = load(root)
    if not store:
        return
    for f in findings:
        hit = store.get(f["id"])
        if hit and hit.get("key") == key(f):
            f["ai_check"] = hit


def factor(check: dict | None) -> float:
    """How much an AI verdict moves a finding in the ranking: up when confirmed, down when wrong."""
    if not check:
        return 1.0
    c = float(check.get("confidence", 0.5))
    return {"confirmed": 1 + 0.3 * c, "needs-changes": 1 - 0.15 * c, "wrong": 1 - 0.6 * c}.get(check["verdict"], 1.0)


def weights(root: str | Path, findings: list[Finding]) -> dict[str, float]:
    """finding id -> ranking multiplier from AI verdicts and AI-written tests, for the code and patch they saw."""
    from . import verify

    store, tests = load(root), verify.load(root)
    out = {}
    for f in findings:
        w = 1.0
        hit = store.get(f.id)
        if hit and hit.get("key") == key(f):
            w *= factor(hit)
        t = tests.get(f.id)
        if t and t.get("key") == verify._key(f):
            w *= {"verified": 1.25, "broke": 0.4}.get(t.get("verdict"), 1.0)
        if w != 1.0:
            out[f.id] = w
    return out


def check(root: str | Path, f: Finding, cfg: LLMConfig | None = None) -> dict:
    cfg = cfg or LLMConfig.load(root)
    st = llm_status(cfg)
    if not st["available"]:
        raise RuntimeError(f"no model to ask: {st['reason']}. Set one up in Settings")
    parts = [
        f"Finding: {f.title}",
        f"File: {f.file}, function `{f.function}`, line {f.line}",
        f"What was found: {f.evidence}",
        f"Suggestion: {f.suggestion}",
        f"API and version it was checked against: {f.api} (exists: {f.symbol_check})",
        f"Gain: {f.gain}",
        f"Risk: {f.risk}",
        f"When not to: {f.when_not}",
        f"Tests so far: {f.proof.value} - {f.proof_detail}",
        "Code today:\n```python\n" + (f.code_today or "")[:12000] + "\n```",
    ]
    if f.patch:
        parts.append("Patch:\n```diff\n" + f.patch[:12000] + "\n```")
    elif f.snippet:
        parts.append(f"Suggested shape:\n```{f.snippet_lang}\n" + f.snippet[:4000] + "\n```")
    reply = complete(
        cfg, SYSTEM, [{"role": "user", "content": "\n\n".join(parts)}], max_tokens=900, purpose="check a finding"
    )
    if reply is None:
        from .llm import last_error

        raise RuntimeError(last_error() or "the model did not answer; check the model in Settings")
    m = re.search(r"\{.*\}", reply, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        data = {}
    verdict = data.get("verdict") if data.get("verdict") in VERDICTS else "needs-changes"
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5
    out = {
        "key": key(f),
        "verdict": verdict,
        "confidence": confidence,
        "reason": str(data.get("reason") or reply.strip())[:2000],
        "risks": [str(r)[:300] for r in data.get("risks", []) if r][:6] if isinstance(data.get("risks"), list) else [],
        "test": str(data.get("test") or "")[:500],
        "model": cfg.model,
        "at": time.time(),
    }
    store = load(root)
    store[f.id] = out
    from .feedback import ensure_dir

    ensure_dir(root)
    _path(root).write_text(json.dumps(store, indent=1))
    return out
