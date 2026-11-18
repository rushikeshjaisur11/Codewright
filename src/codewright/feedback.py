"""Feedback loop: accepted, rejected and muted findings, stored in the repo.

State lives in ``.codewright/feedback.json`` so a team shares it through git.
Accept/reject rates per intent tune the ranking (Laplace-smoothed), mute rules
hide an intent everywhere or under a path prefix, and the acceptance rate is the
headline metric shown in the UI.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .models import Finding

GITIGNORE = (
    "# Local caches; feedback.json, design.json and config.json are meant to be committed.\nsnapshots/\nreleases.json\n"
    "review-state.json\nlast-review.json\ncache/\n"
)


def ensure_dir(root) -> Path:
    """Create .codewright/ with a .gitignore that keeps caches local and shared state committed."""
    d = Path(root) / ".codewright"
    d.mkdir(parents=True, exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text(GITIGNORE)
    else:  # repos set up by an older version: add the caches added since
        have = gi.read_text()
        missing = [x for x in GITIGNORE.splitlines() if x and not x.startswith("#") and x not in have.splitlines()]
        if missing:
            gi.write_text(have.rstrip("\n") + "\n" + "\n".join(missing) + "\n")
    return d


STATUSES = ("accepted", "rejected", "muted")
HALF_LIFE_DAYS = 180  # a verdict this old counts half as much as one made today
MIN_VERDICTS = 3  # accepted + rejected before an intent gets an insight


def area(path: str) -> str:
    """The part of the repo a file belongs to, for per-area verdicts: its top two folders."""
    parts = path.split("/")[:-1]
    return "/".join(parts[:2])


_STOP = {"the", "a", "an", "to", "of", "is", "it", "this", "that", "for", "and", "in", "not", "we", "our", "too"}


def top_reasons(verdicts: list, limit: int = 3) -> list[dict]:
    """Rejection reasons grouped loosely (case, punctuation and filler words ignored)."""
    import re

    groups: dict[str, list[str]] = {}
    for v in verdicts:
        reason = (v.reason or "").strip()
        if not reason:
            continue
        key = " ".join(w for w in re.findall(r"[a-z0-9']+", reason.lower()) if w not in _STOP)
        groups.setdefault(key or reason.lower(), []).append(reason)
    ranked = sorted(groups.values(), key=lambda g: (-len(g), g[0]))
    return [{"reason": g[0], "count": len(g)} for g in ranked[:limit]]


@dataclass
class Verdict:
    status: str
    intent: str
    file: str
    title: str
    reason: str = ""
    at: float = field(default_factory=time.time)


@dataclass
class MuteRule:
    intent: str  # "*" = every intent
    path: str = ""  # path prefix; "" = everywhere
    reason: str = ""


@dataclass
class Feedback:
    verdicts: dict[str, Verdict] = field(default_factory=dict)
    mutes: list[MuteRule] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)  # free-text team rules, e.g. "no new dependencies"

    # -------------------------------------------------------------- persistence
    @staticmethod
    def path(root: str | Path) -> Path:
        return Path(root) / ".codewright" / "feedback.json"

    @classmethod
    def load(cls, root: str | Path) -> Feedback:
        p = cls.path(root)
        if not p.exists():
            return cls()
        try:
            raw = json.loads(p.read_text())
        except json.JSONDecodeError:
            return cls()
        return cls(
            verdicts={k: Verdict(**v) for k, v in raw.get("verdicts", {}).items()},
            mutes=[MuteRule(**m) for m in raw.get("mutes", [])],
            constraints=list(raw.get("constraints", [])),
        )

    def save(self, root: str | Path) -> None:
        p = self.path(root)
        ensure_dir(root)
        p.write_text(
            json.dumps(
                {
                    "verdicts": {k: asdict(v) for k, v in self.verdicts.items()},
                    "mutes": [asdict(m) for m in self.mutes],
                    "constraints": self.constraints,
                },
                indent=2,
            )
        )

    # -------------------------------------------------------------- updates
    def record(self, finding: Finding, status: str, reason: str = "") -> None:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        self.verdicts[finding.id] = Verdict(status, finding.intent, finding.file, finding.title, reason)

    def clear(self, finding_id: str) -> None:
        self.verdicts.pop(finding_id, None)

    def mute(self, intent: str, path: str = "", reason: str = "") -> None:
        if not any(m.intent == intent and m.path == path for m in self.mutes):
            self.mutes.append(MuteRule(intent, path, reason))

    def unmute(self, intent: str, path: str = "") -> None:
        self.mutes = [m for m in self.mutes if not (m.intent == intent and m.path == path)]

    # -------------------------------------------------------------- queries
    def is_muted(self, f: Finding) -> bool:
        if (v := self.verdicts.get(f.id)) and v.status == "muted":
            return True
        return any((m.intent in ("*", f.intent)) and f.file.startswith(m.path) for m in self.mutes)

    def status_of(self, f: Finding) -> str | None:
        v = self.verdicts.get(f.id)
        return v.status if v else None

    def intent_weight(self, intent: str) -> float:
        """Multiplier in roughly [0.33, 1.67]; 1.0 with no history."""
        return self._weight([v for v in self.verdicts.values() if v.intent == intent])

    def weight_for(self, f: Finding, now: float | None = None) -> float:
        """The intent's weight, refined by verdicts in the same area of the repo when there are enough.

        Recent verdicts count more (half weight after HALF_LIFE_DAYS), so a team that changed its mind
        isn't held to what it said a year ago.
        """
        now = now or time.time()
        mine = [v for v in self.verdicts.values() if v.intent == f.intent]
        overall = self._weight(mine, now)
        local = [v for v in mine if area(v.file) == area(f.file)]
        if len(local) >= 2 and len(local) < len(mine):
            return round((overall + 2 * self._weight(local, now)) / 3, 3)
        return overall

    @staticmethod
    def _weight(verdicts: list[Verdict], now: float | None = None) -> float:
        acc = rej = 0.0
        for v in verdicts:
            w = 0.5 ** (max(0.0, (now - v.at)) / 86400 / HALF_LIFE_DAYS) if now else 1.0
            if v.status == "accepted":
                acc += w
            elif v.status == "rejected":
                rej += w
        rate = (acc + 1) / (acc + rej + 2)
        return round(0.33 + 1.34 * rate, 3)

    def insights(self, titles: dict[str, str] | None = None) -> list[dict]:
        """What the team's verdicts say: checks to mute (everywhere or in one area), checks it trusts,
        and the reasons it gives most often. `titles` maps intent -> a readable name."""
        titles = titles or {}
        out: list[dict] = []
        by_intent: dict[str, list[Verdict]] = {}
        for v in self.verdicts.values():
            by_intent.setdefault(v.intent, []).append(v)
        muted = {(m.intent, m.path) for m in self.mutes}
        for intent, vs in sorted(by_intent.items()):
            acc = [v for v in vs if v.status == "accepted"]
            rej = [v for v in vs if v.status == "rejected"]
            decided = len(acc) + len(rej)
            name = titles.get(intent) or next((v.title for v in vs), intent)
            if decided >= MIN_VERDICTS and len(rej) / decided >= 0.7:
                areas = {}
                for v in rej:
                    areas[area(v.file)] = areas.get(area(v.file), 0) + 1
                top_area, n = max(areas.items(), key=lambda kv: (kv[1], kv[0]))
                accepted_there = sum(1 for v in acc if area(v.file) == top_area)
                scoped = len(areas) > 1 and n / len(rej) >= 0.75 and top_area and not accepted_there
                path = f"{top_area}/" if scoped else ""
                if (intent, path) in muted or (intent, "") in muted:
                    continue
                out.append(
                    {
                        "kind": "mute",
                        "intent": intent,
                        "title": name,
                        "path": path,
                        "accepted": len(acc),
                        "rejected": len(rej),
                        "rate": round(len(acc) / decided, 2),
                        "reasons": top_reasons(rej),
                        "message": f'Your team rejected {len(rej)} of {decided} "{name}" findings'
                        + (f", {n} of them under `{path}`" if scoped else "")
                        + (f". Mute it under `{path}`?" if scoped else ". Mute it?"),
                    }
                )
            elif decided >= MIN_VERDICTS and len(acc) / decided >= 0.8:
                out.append(
                    {
                        "kind": "trusted",
                        "intent": intent,
                        "title": name,
                        "path": "",
                        "accepted": len(acc),
                        "rejected": len(rej),
                        "rate": round(len(acc) / decided, 2),
                        "reasons": [],
                        "message": f'Your team accepted {len(acc)} of {decided} "{name}" findings: they rank higher '
                        "now. Consider failing CI on new ones (`codewright review --base ... -f sarif`).",
                    }
                )
        reasons = top_reasons([v for v in self.verdicts.values() if v.status == "rejected"], 3)
        if reasons:
            out.append(
                {
                    "kind": "reasons",
                    "intent": "*",
                    "title": "Why findings get rejected",
                    "path": "",
                    "accepted": 0,
                    "rejected": sum(r["count"] for r in reasons),
                    "rate": None,
                    "reasons": reasons,
                    "message": "Most common rejection reasons: "
                    + "; ".join(f'"{r["reason"]}" ({r["count"]})' for r in reasons),
                }
            )
        order = {"mute": 0, "trusted": 1, "reasons": 2}
        return sorted(out, key=lambda i: (order[i["kind"]], -(i["rejected"] + i["accepted"])))

    def stats(self) -> dict:
        acc = sum(1 for v in self.verdicts.values() if v.status == "accepted")
        rej = sum(1 for v in self.verdicts.values() if v.status == "rejected")
        per_intent: dict[str, dict[str, int]] = {}
        for v in self.verdicts.values():
            per_intent.setdefault(v.intent, {"accepted": 0, "rejected": 0, "muted": 0})[v.status] += 1
        return {
            "accepted": acc,
            "rejected": rej,
            "acceptance_rate": round(acc / (acc + rej), 3) if acc + rej else None,
            "per_intent": per_intent,
            "mutes": [asdict(m) for m in self.mutes],
            "constraints": self.constraints,
            "insights": self.insights(),
        }
