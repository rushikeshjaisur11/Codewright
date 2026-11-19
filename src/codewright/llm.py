"""The optional model: which one Codewright may call, and the call itself.

Everything in Codewright works without a model: detectors, cards, critic and
proof are deterministic, Design mode runs its interview and capacity math in
code, and code chat falls back to an extractive answer with citations. A model,
when configured, only writes the chat answers and drafts Design's data model.

Claude, OpenAI-compatible servers (OpenAI, Ollama, LM Studio, OpenRouter, Azure)
and Gemini are called over plain HTTPS, so there is nothing to install for them.
Vertex AI and anything else go through LiteLLM (``pip install 'codewright[llm]'``).

The provider, key and model are set on the Settings page and stored for this
machine in ``~/.codewright/settings.json`` (mode 0600); the key never goes into a
repo and is never sent back to the browser. Environment variables win over the
file, so CI and servers can pin their configuration:

  CODEWRIGHT_AI_PROVIDER   anthropic | openai | gemini | litellm
  CODEWRIGHT_AI_KEY        the API key (ANTHROPIC_API_KEY, OPENAI_API_KEY and GEMINI_API_KEY also work)
  CODEWRIGHT_AI_MODEL      the model name
  CODEWRIGHT_AI_BASE_URL   an OpenAI-compatible server, e.g. http://localhost:11434/v1
  CODEWRIGHT_MODEL         older form, a LiteLLM id such as "anthropic/claude-haiku-5-5"
  CODEWRIGHT_OFFLINE       "1" to never call a model (also a per-repo switch in Settings)
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODEL = "vertex_ai/gemini-2.5-flash"  # the LiteLLM default, kept for setups that predate the Settings page


@dataclass(frozen=True)
class ProviderSpec:
    label: str
    model: str  # default model
    needs_key: bool
    key_env: tuple[str, ...]  # the provider's usual key variables; one of them alone turns the provider on
    base_url: str = ""


PROVIDERS: dict[str, ProviderSpec] = {
    "anthropic": ProviderSpec(
        "Claude (Anthropic API)", "claude-haiku-5-5", True, ("ANTHROPIC_API_KEY",), "https://api.anthropic.com"
    ),
    "openai": ProviderSpec(
        "OpenAI or any OpenAI-compatible server (Ollama, LM Studio, OpenRouter, Azure)",
        "gpt-4o-mini",
        False,  # a local server needs an address but no key
        ("OPENAI_API_KEY",),
        "https://api.openai.com/v1",
    ),
    "gemini": ProviderSpec(
        "Gemini (Google AI Studio API key)",
        "gemini-2.5-flash",
        True,
        ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "https://generativelanguage.googleapis.com/v1beta",
    ),
    "litellm": ProviderSpec("Vertex AI or another provider, through LiteLLM", DEFAULT_MODEL, False, ()),
}
FIELDS = ("provider", "api_key", "model", "base_url")
ENV = {
    "provider": "CODEWRIGHT_AI_PROVIDER",
    "api_key": "CODEWRIGHT_AI_KEY",
    "model": "CODEWRIGHT_AI_MODEL",
    "base_url": "CODEWRIGHT_AI_BASE_URL",
}
# Credentials LiteLLM reads from the environment, by the prefix of the model id.
_LITELLM_ENV = {
    "vertex_ai": ("GOOGLE_APPLICATION_CREDENTIALS", "VERTEXAI_PROJECT", "GOOGLE_CLOUD_PROJECT"),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "ollama": (),  # local, no key
}
_TRUE = ("1", "true", "True")
OLLAMA_URL = "http://localhost:11434/v1"


class LLMError(RuntimeError):
    """A model call that failed, with a message fit to show the user."""


# --------------------------------------------------------------------------- stored settings


def settings_path() -> Path:
    return Path(os.environ.get("CODEWRIGHT_HOME", "~/.codewright")).expanduser() / "settings.json"


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def stored() -> dict:
    ai = _read(settings_path()).get("ai")
    return ai if isinstance(ai, dict) else {}


def save_settings(values: dict) -> dict:
    """Merge fields into this machine's settings. An empty api_key keeps the stored one; None removes it."""
    cur = stored()
    for k in FIELDS:
        if k not in values:
            continue
        v = values[k]
        if k == "api_key" and v == "":
            continue
        if v is None or v == "":
            cur.pop(k, None)
        else:
            cur[k] = str(v).strip()
    if cur.get("provider") not in PROVIDERS:
        cur.pop("provider", None)
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = _read(p)
    data["ai"] = cur
    tmp = p.with_suffix(".tmp")
    # Created 0600 from the start: the file holds an API key.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    return cur


def _from_model_id(model: str) -> dict:
    """An older LiteLLM-style id ("anthropic/claude-…", "ollama/llama3") as provider settings."""
    prefix, _, name = model.partition("/")
    if name and prefix in ("anthropic", "openai", "gemini"):
        return {"provider": prefix, "model": name}
    if name and prefix == "ollama":
        return {"provider": "openai", "model": name, "base_url": OLLAMA_URL}
    return {"provider": "litellm", "model": model}


def effective(root: str | Path | None = None) -> dict:
    """Saved settings overlaid with the environment; `_source` says where each field came from.

    Order: CODEWRIGHT_AI_* variables, then the Settings page, then the older CODEWRIGHT_MODEL
    (or the repo's `.codewright/config.json`), then a provider's usual key variable on its own.
    """
    env = os.environ.get
    cfg = {k: v for k, v in stored().items() if k in FIELDS and v}
    src = {k: "saved" for k in cfg}
    for k, name in ENV.items():
        if env(name):
            cfg[k], src[k] = env(name), "environment"
    if cfg.get("provider") not in PROVIDERS:
        cfg.pop("provider", None)
        src.pop("provider", None)
    if not cfg.get("provider"):
        repo = _read(Path(root) / ".codewright" / "config.json") if root is not None else {}
        legacy, where = (env("CODEWRIGHT_MODEL"), "environment")
        if not legacy:
            legacy, where = (repo.get("codewright_model"), "repo")
        if legacy:
            for k, v in _from_model_id(str(legacy)).items():
                if k == "provider" or not cfg.get(k):
                    cfg[k], src[k] = v, where
        else:
            for pid, spec in PROVIDERS.items():
                if any(env(v) for v in spec.key_env):
                    cfg["provider"], src["provider"] = pid, "environment"
                    break
            else:
                if any(env(v) for v in _LITELLM_ENV["vertex_ai"]) and _has_litellm():
                    cfg["provider"], src["provider"] = "litellm", "environment"
    pid = cfg.get("provider")
    if pid in PROVIDERS:
        if not cfg.get("api_key"):
            for v in PROVIDERS[pid].key_env:
                if env(v):
                    cfg["api_key"], src["api_key"] = env(v), "environment"
                    break
        cfg.setdefault("model", PROVIDERS[pid].model)
    cfg["_source"] = src
    return cfg


def _has_litellm() -> bool:
    import importlib.util

    return importlib.util.find_spec("litellm") is not None


def _offline(root: str | Path | None) -> tuple[bool, bool]:
    """(offline, set by the environment)."""
    if os.environ.get("CODEWRIGHT_OFFLINE"):
        return os.environ["CODEWRIGHT_OFFLINE"] in _TRUE, True
    if root is None:
        return False, False
    return str(_read(Path(root) / ".codewright" / "config.json").get("codewright_offline", "")) in _TRUE, False


# --------------------------------------------------------------------------- configuration


@dataclass
class LLMConfig:
    model: str = ""
    offline: bool = False
    provider: str = ""
    api_key: str = field(default="", repr=False)
    base_url: str = ""

    @classmethod
    def load(cls, root: str | Path | None = None) -> LLMConfig:
        cfg = effective(root)
        return cls(
            cfg.get("model", ""),
            _offline(root)[0],
            cfg.get("provider", ""),
            cfg.get("api_key", ""),
            cfg.get("base_url", ""),
        )

    def save(self, root: str | Path) -> None:
        """Write the per-repo offline switch, keeping whatever else the repo's config holds."""
        from .feedback import ensure_dir

        p = ensure_dir(root) / "config.json"
        data = _read(p)
        data["codewright_offline"] = self.offline
        p.write_text(json.dumps(data, indent=2))


def _missing(cfg: LLMConfig) -> str:
    """Why this configuration can't reach a model, or "" when it can be tried."""
    if cfg.provider not in PROVIDERS:
        return "no model is set up (choose a provider in Settings)"
    spec = PROVIDERS[cfg.provider]
    if cfg.provider == "litellm":
        if not _has_litellm():
            return "litellm is not installed (pip install 'codewright[llm]')"
        prefix = cfg.model.split("/", 1)[0] if "/" in cfg.model else "openai"
        needed = _LITELLM_ENV.get(prefix)
        if needed and not cfg.api_key and not any(os.environ.get(k) for k in needed):
            return f"no credentials for {prefix} (set one of {', '.join(needed)})"
        return ""
    if spec.needs_key and not cfg.api_key:
        return f"no API key for {spec.label.split(' (')[0]}"
    if cfg.provider == "openai" and not (cfg.api_key or cfg.base_url):
        return "no API key or server address for the OpenAI-compatible provider"
    return ""


def status(cfg: LLMConfig) -> dict:
    """Whether a model call can be attempted, and why not."""
    reason = "offline mode is on" if cfg.offline else _missing(cfg)
    return {"available": not reason, "model": cfg.model, "provider": cfg.provider, "reason": reason}


def public(root: str | Path | None = None) -> dict:
    """What the browser may see: everything except the key itself."""
    cfg = effective(root)
    src = cfg.pop("_source")
    key = cfg.pop("api_key", "")
    llm = LLMConfig(cfg.get("model", ""), False, cfg.get("provider", ""), key, cfg.get("base_url", ""))
    reason = _missing(llm)
    return {
        "provider": llm.provider,
        "model": llm.model,
        "base_url": llm.base_url,
        "has_key": bool(key),
        "key_hint": f"…{key[-4:]}" if len(key) >= 12 else "",
        "configured": not reason,
        "reason": reason,
        "from_environment": sorted(k for k, v in src.items() if v == "environment"),
        "from_repo": sorted(k for k, v in src.items() if v == "repo"),
        "litellm_installed": _has_litellm(),
        "providers": [
            {"id": pid, "label": s.label, "model": s.model, "needs_key": s.needs_key} for pid, s in PROVIDERS.items()
        ],
    }


# --------------------------------------------------------------------------- the call


def _post(url: str, body: dict, headers: dict, who: str, timeout: int) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "codewright", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        try:
            err = json.loads(detail).get("error")
            detail = err.get("message", detail) if isinstance(err, dict) else (err or detail)
        except (ValueError, AttributeError):
            pass
        raise LLMError(f"{who} answered HTTP {e.code}: {detail}") from None
    except (urllib.error.URLError, OSError) as e:
        raise LLMError(f"cannot reach {url.split('/v1')[0]}: {getattr(e, 'reason', e)}") from None
    except ValueError:
        raise LLMError(f"{who} did not answer with JSON; check the server address") from None


# --------------------------------------------------------------------------- usage and budget

DEFAULT_DAILY_CALLS = 200
_USAGE_LOCK = __import__("threading").Lock()
_LAST_ERROR: list[str] = [""]


def _usage_path() -> Path:
    return settings_path().with_name("usage.json")


def usage() -> dict:
    """Today's model calls and tokens on this machine, and the daily call budget."""
    import time as _t

    today = _t.strftime("%Y-%m-%d")
    data = _read(_usage_path())
    day = data.get(today) if isinstance(data.get(today), dict) else {}
    budget = _read(settings_path()).get("ai_budget", {})
    limit = int(budget.get("daily_calls", DEFAULT_DAILY_CALLS)) if isinstance(budget, dict) else DEFAULT_DAILY_CALLS
    return {
        "date": today,
        "calls": int(day.get("calls", 0)),
        "input_tokens": int(day.get("input_tokens", 0)),
        "output_tokens": int(day.get("output_tokens", 0)),
        "daily_calls": limit,
    }


def set_budget(daily_calls: int) -> dict:
    p = settings_path()
    data = _read(p)
    data["ai_budget"] = {"daily_calls": max(0, int(daily_calls))}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))
    return usage()


def _record(input_tokens: int, output_tokens: int) -> dict:
    import time as _t

    with _USAGE_LOCK:
        p = _usage_path()
        data = _read(p)
        today = _t.strftime("%Y-%m-%d")
        day = data.get(today) if isinstance(data.get(today), dict) else {}
        day = {
            "calls": int(day.get("calls", 0)) + 1,
            "input_tokens": int(day.get("input_tokens", 0)) + input_tokens,
            "output_tokens": int(day.get("output_tokens", 0)) + output_tokens,
        }
        keep = {k: v for k, v in data.items() if k >= _t.strftime("%Y-%m-%d", _t.localtime(_t.time() - 30 * 86400))}
        keep[today] = day
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(keep, indent=1))
        return day


def last_error() -> str:
    """Why the last `complete` returned None (empty when it did not)."""
    return _LAST_ERROR[0]


def _tokens(pid: str, r) -> tuple[int, int]:
    try:
        if pid == "litellm":
            u = getattr(r, "usage", None)
            return int(getattr(u, "prompt_tokens", 0) or 0), int(getattr(u, "completion_tokens", 0) or 0)
        if pid == "anthropic":
            u = r.get("usage") or {}
            return int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))
        if pid == "gemini":
            u = r.get("usageMetadata") or {}
            return int(u.get("promptTokenCount", 0)), int(u.get("candidatesTokenCount", 0))
        u = r.get("usage") or {}
        return int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))
    except (TypeError, ValueError, AttributeError):
        return 0, 0


def generate(
    cfg: LLMConfig, system: str, messages: list[dict], max_tokens: int = 1200, timeout: int = 90, purpose: str = ""
) -> str:
    """One chat completion, within the daily call budget and counted in the usage. Raises LLMError with the
    reason when the model can't be reached, refuses, or the budget is used up."""
    import logging
    import time as _t

    if reason := _missing(cfg):
        raise LLMError(reason)
    u = usage()
    if u["daily_calls"] and u["calls"] >= u["daily_calls"]:
        raise LLMError(
            f"today's AI budget of {u['daily_calls']} model calls is used up; "
            "raise it in Settings or wait until tomorrow"
        )
    t0 = _t.perf_counter()
    text_out, raw = _generate(cfg, system, messages, max_tokens, timeout)
    tin, tout = _tokens(cfg.provider, raw)
    day = _record(tin, tout)
    logging.getLogger("codewright").info(
        "%-18s Model call%s: %s, %s in / %s out tokens, %.1fs (today: %s of %s calls)",
        "ai",
        f" ({purpose})" if purpose else "",
        cfg.model,
        tin or "?",
        tout or "?",
        _t.perf_counter() - t0,
        day["calls"],
        u["daily_calls"] or "unlimited",
    )
    from . import activity

    activity.note(
        "ai",
        f"Model call{f' ({purpose})' if purpose else ''}: {cfg.model}, {tin or '?'} in / {tout or '?'} out tokens"
        f" · today {day['calls']}/{u['daily_calls'] or '∞'} calls",
    )
    return text_out


def _generate(cfg: LLMConfig, system: str, messages: list[dict], max_tokens: int, timeout: int) -> tuple[str, object]:
    pid = cfg.provider
    msgs = [{"role": m["role"], "content": m["content"]} for m in messages if m.get("content")]
    if pid == "litellm":
        import litellm

        try:
            resp = litellm.completion(
                model=cfg.model,
                messages=[{"role": "system", "content": system}, *msgs],
                max_tokens=max_tokens,
                temperature=0.2,
                timeout=timeout,
                **({"api_key": cfg.api_key} if cfg.api_key else {}),
            )
            return (resp.choices[0].message.content or "").strip(), resp
        except Exception as e:  # LiteLLM raises a different class per provider
            raise LLMError(str(e)[:400]) from None
    base = (cfg.base_url if pid == "openai" and cfg.base_url else PROVIDERS[pid].base_url).rstrip("/")
    if pid == "anthropic":
        r = _post(
            f"{base}/v1/messages",
            {"model": cfg.model, "max_tokens": max_tokens, "system": system, "messages": msgs},
            {"x-api-key": cfg.api_key, "anthropic-version": "2023-06-01"},
            "Claude",
            timeout,
        )
        if r.get("stop_reason") == "refusal":
            raise LLMError("the model declined to answer")
        return "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text").strip(), r
    if pid == "gemini":
        r = _post(
            f"{base}/models/{cfg.model}:generateContent",
            {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [
                    {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                    for m in msgs
                ],
                "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.2},
            },
            {"x-goog-api-key": cfg.api_key},
            "Gemini",
            timeout,
        )
        parts = ((r.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts).strip(), r
    r = _post(
        f"{base}/chat/completions",
        {"model": cfg.model, "messages": [{"role": "system", "content": system}, *msgs], "max_tokens": max_tokens},
        {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {},
        "the server",
        timeout,
    )
    return (((r.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip(), r


def complete(
    cfg: LLMConfig, system: str, messages: list[dict], max_tokens: int = 1200, purpose: str = ""
) -> str | None:
    """One chat completion, or None when no model is available or the call fails (callers fall back;
    `last_error()` says why)."""
    st = status(cfg)
    if not st["available"]:
        _LAST_ERROR[0] = st["reason"]
        return None
    try:
        out = generate(cfg, system, messages, max_tokens, purpose=purpose) or None
        _LAST_ERROR[0] = "" if out else "the model sent an empty answer"
        return out
    except LLMError as e:
        _LAST_ERROR[0] = str(e)
        return None


def test(cfg: LLMConfig) -> dict:
    """One tiny request, so a wrong key or model name shows up in Settings and not in the middle of a chat."""
    t0 = time.time()
    try:
        reply = generate(cfg, "You answer in one word.", [{"role": "user", "content": "Reply with: ok"}], 64, 30)
    except LLMError as e:
        return {"ok": False, "model": cfg.model, "error": str(e)}
    if not reply:
        return {"ok": False, "model": cfg.model, "error": "the model answered with nothing"}
    return {"ok": True, "model": cfg.model, "reply": reply[:80], "ms": int((time.time() - t0) * 1000)}
