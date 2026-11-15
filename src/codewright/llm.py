"""LLM access through LiteLLM, Vertex AI (Gemini) by default.

Everything in Codewright works without a model: detectors, cards, critic and
proof are deterministic, Design mode runs its interview and capacity math in
code, and code chat falls back to an extractive answer with citations. A model,
when configured, only improves the wording and the chat answers.

Configuration (environment, or ``.codewright/config.json`` with the same keys lowercased):
  CODEWRIGHT_MODEL     LiteLLM model id, default "vertex_ai/gemini-2.5-flash"
  CODEWRIGHT_OFFLINE   "1" to never call a model (locked-down / offline mode)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MODEL = "vertex_ai/gemini-2.5-flash"

_CREDENTIAL_ENV = {
    "vertex_ai": ("GOOGLE_APPLICATION_CREDENTIALS", "VERTEXAI_PROJECT", "GOOGLE_CLOUD_PROJECT"),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "ollama": (),  # local, no key
}


@dataclass
class LLMConfig:
    model: str = DEFAULT_MODEL
    offline: bool = False

    @classmethod
    def load(cls, root: str | Path | None = None) -> LLMConfig:
        file_cfg: dict = {}
        if root is not None:
            p = Path(root) / ".codewright" / "config.json"
            if p.exists():
                try:
                    file_cfg = json.loads(p.read_text())
                except json.JSONDecodeError:
                    file_cfg = {}
        model = os.environ.get("CODEWRIGHT_MODEL") or file_cfg.get("codewright_model") or DEFAULT_MODEL
        offline = (os.environ.get("CODEWRIGHT_OFFLINE") or str(file_cfg.get("codewright_offline", ""))) in (
            "1",
            "true",
            "True",
        )
        return cls(model, offline)

    def save(self, root: str | Path) -> None:
        from .feedback import ensure_dir

        p = ensure_dir(root) / "config.json"
        p.write_text(json.dumps({"codewright_model": self.model, "codewright_offline": self.offline}, indent=2))


def status(cfg: LLMConfig) -> dict:
    """Whether a model call can be attempted, and why not."""
    if cfg.offline:
        return {"available": False, "model": cfg.model, "reason": "offline mode is on"}
    try:
        import litellm  # noqa: F401
    except ImportError:
        return {
            "available": False,
            "model": cfg.model,
            "reason": "litellm is not installed (pip install 'codewright[llm]')",
        }
    provider = cfg.model.split("/", 1)[0] if "/" in cfg.model else "openai"
    needed = _CREDENTIAL_ENV.get(provider)
    if needed and not any(os.environ.get(k) for k in needed):
        return {
            "available": False,
            "model": cfg.model,
            "reason": f"no credentials for {provider} (set one of {', '.join(needed)})",
        }
    return {"available": True, "model": cfg.model, "reason": ""}


def complete(cfg: LLMConfig, system: str, messages: list[dict], max_tokens: int = 1200) -> str | None:
    """One chat completion, or None when no model is available or the call fails."""
    if not status(cfg)["available"]:
        return None
    import litellm

    try:
        resp = litellm.completion(
            model=cfg.model,
            messages=[{"role": "system", "content": system}, *messages],
            max_tokens=max_tokens,
            temperature=0.2,
        )
        return resp.choices[0].message.content
    except Exception:  # network, auth, quota: fall back to deterministic output
        return None
