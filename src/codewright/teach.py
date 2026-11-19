"""Teach Codewright: add cards to the repo's own knowledge base, by hand or drafted by the model.

A taught card is a `reference` card (knowledge, not a finding: no detector produces it) unless an existing
intent is named, in which case it competes with the built-in cards for that intent's findings. Cards are
written to ``.codewright/cards/<id>.yaml``, committed with the repo so the whole team gets them, and used by
the Knowledge page, code chat, the MCP `search_knowledge` tool and the release watcher.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from . import cards as card_store
from .models import Card

REQUIRED = ("title", "suggestion")
TEXT_FIELDS = (
    "title",
    "topic",
    "symbol",
    "library",
    "versions",
    "suggestion",
    "gain",
    "risk",
    "when_not",
    "threshold",
    "snippet",
    "snippet_lang",
    "intent",
    "kind",
)


def slug(title: str, library: str = "") -> str:
    base = re.sub(r"[^a-z0-9]+", "-", f"{library} {title}".lower()).strip("-")[:60]
    return f"team.{base or 'card'}"


def normalise(data: dict) -> dict:
    """A complete card dict from what a form or the model gave; raises ValueError on what cannot be fixed."""
    missing = [k for k in REQUIRED if not str(data.get(k) or "").strip()]
    if missing:
        raise ValueError(f"a card needs {' and '.join(missing)}")
    library = str(data.get("library") or "").strip() or None
    from . import intents

    rule = _rule(data.get("rule"))
    intent = str(data.get("intent") or card_store.REFERENCE).strip()
    if rule:
        intent = "team"  # set to team:<id> below, once the id is known
    elif intent != card_store.REFERENCE and intent not in intents.detectors.all_intents():
        raise ValueError(f"unknown intent {intent!r}: leave it empty for a knowledge card")
    sources = data.get("sources") or []
    if isinstance(sources, str):
        sources = [s.strip() for s in re.split(r"[\n,]+", sources) if s.strip()]
    out = {
        "id": str(data.get("id") or "").strip() or slug(str(data["title"]), library or ""),
        "intent": intent,
        "topic": str(data.get("topic") or "Team knowledge").strip(),
        "title": str(data["title"]).strip(),
        "symbol": str(data.get("symbol") or "").strip(),
        "kind": str(data.get("kind") or ("library" if library else "stdlib")),
        "library": library,
        "versions": str(data.get("versions") or "*").strip() or "*",
        "suggestion": str(data["suggestion"]).strip(),
        "gain": str(data.get("gain") or "").strip(),
        "effort": max(1, min(5, int(data.get("effort") or 2))),
        "impact": max(1, min(5, int(data.get("impact") or 2))),
        "risk": str(data.get("risk") or "").strip(),
        "when_not": str(data.get("when_not") or "").strip(),
        "threshold": str(data.get("threshold") or "").strip(),
        "sources": [str(s) for s in sources][:10],
    }
    if rule:  # a check: its findings need what every finding shows, so fill what the team left out
        from .teamrules import intent_of

        out["intent"] = intent_of(out["id"])
        out["kind"] = "practice"
        out["rule"] = rule
        out["symbol"] = out["symbol"] or ", ".join(rule["calls"])
        out["gain"] = out["gain"] or "Follows a convention this team agreed on."
        out["risk"] = out["risk"] or "Low: the team rule says how to write it instead."
        out["when_not"] = out["when_not"] or "Where the team has agreed an exception; mute the rule for that path."
        out["threshold"] = out["threshold"] or "Always, by team agreement."
        out["sources"] = out["sources"] or [f"Team convention: .codewright/cards/{out['id']}.yaml"]
    if str(data.get("snippet") or "").strip():
        out["snippet"] = str(data["snippet"]).rstrip() + "\n"
        out["snippet_lang"] = str(data.get("snippet_lang") or "python")
    if not re.fullmatch(r"[\w.\-]+", out["id"]):
        raise ValueError("a card id may use letters, digits, '.', '-' and '_' only")
    Card(**out)  # every field the loader expects
    return out


def _rule(raw) -> dict:
    """A clean rule from the form or YAML ({} when there is none)."""
    if not isinstance(raw, dict):
        return {}
    calls = raw.get("calls") or []
    if isinstance(calls, str):
        calls = re.split(r"[\s,]+", calls)
    calls = [c.strip() for c in calls if c and re.fullmatch(r"[A-Za-z_][\w.]*", c.strip())]
    if not calls:
        return {}
    out: dict = {"calls": calls[:20]}
    if str(raw.get("missing_kwarg") or "").strip():
        kw = str(raw["missing_kwarg"]).strip()
        if not re.fullmatch(r"[A-Za-z_]\w*", kw):
            raise ValueError("the keyword argument must be a Python name")
        out["missing_kwarg"] = kw
    if raw.get("in_async"):
        out["in_async"] = True
    paths = raw.get("paths") or []
    if isinstance(paths, str):
        paths = [p for p in re.split(r"[\n,]+", paths) if p.strip()]
    if paths:
        out["paths"] = [p.strip() for p in paths][:20]
    return out


def save(root: str | Path, data: dict) -> dict:
    card = normalise(data)
    builtin = {c.id for c in card_store.load()}
    if card["id"] in builtin:
        raise ValueError(f"{card['id']} is a built-in card: give yours another id")
    d = card_store.repo_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{card['id']}.yaml").write_text(
        "# Added from Codewright's Knowledge page. Edit freely; commit it to share with the team.\n"
        + yaml.safe_dump([card], sort_keys=False, allow_unicode=True, width=110)
    )
    return card


def delete(root: str | Path, card_id: str) -> None:
    if not re.fullmatch(r"[\w.\-]+", card_id):
        raise ValueError("bad card id")
    p = card_store.repo_dir(root) / f"{card_id}.yaml"
    if not p.is_file():
        raise FileNotFoundError(card_id)
    p.unlink()


DRAFT_SYSTEM = (
    "You write one knowledge card for a code-review tool's knowledge base. A card teaches one specific, "
    "checkable practice for a library version: what to do instead, why (the gain), the risk, when not to, and "
    "sources. Ground it in the notes you are given; do not invent APIs. If the notes don't support a field, "
    "leave it empty. Reply with JSON only, with these keys: title, topic, library, versions (a PEP 440 "
    "specifier such as '>=2.0', or '*'), symbol (the dotted API it is about, if any), suggestion, gain, risk, "
    "when_not, threshold, sources (list of URLs from the notes), snippet (short Python, may be empty)."
)


def draft(
    root: str | Path,
    *,
    topic: str,
    library: str = "",
    notes: str = "",
    base: Card | None = None,
    installed: str | None = None,
) -> dict:
    """Card fields drafted by the configured model, for the user to edit before saving (nothing is saved)."""
    from .llm import LLMConfig, complete
    from .llm import status as llm_status

    cfg = LLMConfig.load(root)
    st = llm_status(cfg)
    if not st["available"]:
        raise RuntimeError(f"no model to draft with: {st['reason']}. Set one up in Settings, or fill the form by hand")
    parts = [f"Topic: {topic or (base.title if base else '')}"]
    if library:
        parts.append(f"Library: {library}" + (f" (this repo has {installed})" if installed else ""))
    if base is not None:
        parts.append(
            "Go deeper than this existing card: write a related card about a more specific or more advanced "
            f"practice it does not cover.\nExisting card: {base.title}\n{base.suggestion}\nGain: {base.gain}\n"
            f"Sources: {', '.join(base.sources)}"
        )
    if notes.strip():
        parts.append("Notes and documentation excerpts:\n" + notes.strip()[:16000])
    reply = complete(
        cfg, DRAFT_SYSTEM, [{"role": "user", "content": "\n\n".join(parts)}], max_tokens=1400, purpose="draft a card"
    )
    if reply is None:
        from .llm import last_error

        raise RuntimeError(last_error() or "the model did not answer; check the model in Settings")
    m = re.search(r"\{.*\}", reply, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        data = {}
    out = {k: data.get(k, "") for k in (*TEXT_FIELDS, "sources") if k in data}
    if library and not out.get("library"):
        out["library"] = library
    out["model"] = cfg.model
    return out
