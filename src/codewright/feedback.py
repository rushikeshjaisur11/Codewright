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
)


def ensure_dir(root) -> Path:
    """Create .codewright/ with a .gitignore that keeps caches local and shared state committed."""
    d = Path(root) / ".codewright"
    d.mkdir(parents=True, exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text(GITIGNORE)
    return d


STATUSES = ("accepted", "rejected", "muted")


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
        acc = sum(1 for v in self.verdicts.values() if v.intent == intent and v.status == "accepted")
        rej = sum(1 for v in self.verdicts.values() if v.intent == intent and v.status == "rejected")
        rate = (acc + 1) / (acc + rej + 2)
        return round(0.33 + 1.34 * rate, 3)

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
        }
