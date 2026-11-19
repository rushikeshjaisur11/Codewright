"""Team rules: knowledge cards that also find code, so a convention the team taught becomes a review finding.

A card in ``.codewright/cards/`` with a ``rule`` is checked against every function:

    rule:
      calls: [requests.get, requests.post]   # resolved through the file's imports; a bare name matches any module
      missing_kwarg: timeout                 # optional: only calls without this keyword argument
      in_async: true                         # optional: only inside `async def`
      paths: ["services/**"]                 # optional: only in these files (same patterns as the scan scope)

Each match is a finding with the card's advice, ranked and filtered like the built-in ones.
"""

from __future__ import annotations

import re

from .graph import CodeGraph, is_test_file, text
from .models import Card, IntentMatch


def intent_of(card_id: str) -> str:
    return f"team:{card_id}"


def _wanted(name: str, calls: list[str]) -> bool:
    return any(name == c or (("." not in c) and name.rsplit(".", 1)[-1] == c) or name.endswith("." + c) for c in calls)


def detect(graph: CodeGraph, cards: list[Card]) -> list[IntentMatch]:
    from . import scanscope
    from .intents.detectors import calls_in, line

    rules = [(c, c.rule) for c in cards if isinstance(getattr(c, "rule", None), dict) and c.rule.get("calls")]
    out: list[IntentMatch] = []
    for card, rule in rules:
        calls = [str(x).strip() for x in rule.get("calls", []) if str(x).strip()]
        words = {c.rsplit(".", 1)[-1].encode() for c in calls}
        kw = str(rule.get("missing_kwarg") or "").strip()
        paths = [str(p) for p in rule.get("paths") or []]
        for pf in graph.files.values():
            if is_test_file(pf.path) or not any(w in pf.source for w in words):
                continue  # the file never names the call: no tree needed
            if paths and not scanscope.included(pf.path, {"include": paths, "exclude": []}):
                continue
            for fn in pf.module.functions:
                if rule.get("in_async") and not fn.is_async:
                    continue
                if not any(_wanted(c, calls) or c.rsplit(".", 1)[-1].encode() in words for c in fn.calls):
                    continue
                node = pf.nodes.get(fn.qualname)
                if node is None:
                    continue
                for call, name in calls_in(node, pf):
                    if not _wanted(name, calls):
                        continue
                    args = call.child_by_field_name("arguments")
                    given = {
                        text(a.child_by_field_name("name"))
                        for a in (args.named_children if args is not None else [])
                        if a.type == "keyword_argument"
                    }
                    if kw and (kw in given or re.search(r"\*\*", text(args) if args is not None else "")):
                        continue
                    why = f"`{text(call.child_by_field_name('function'))}()`"
                    why += f" without `{kw}=`" if kw else ""
                    why += " inside `async def`" if rule.get("in_async") else ""
                    out.append(IntentMatch(intent_of(card.id), fn, line(call), f"{why} (team rule {card.id})", {}))
                    break  # one finding per function and rule
    return out
