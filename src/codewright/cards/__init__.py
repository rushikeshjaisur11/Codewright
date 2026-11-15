"""Capability cards: what each library version offers for each intent."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from ..models import Card, IntentMatch, Stack
from ..stack import LOCKFILES, python_version

REFERENCE = "reference"  # intent of knowledge-only cards

DATA = Path(__file__).parent / "data"
_FIELDS = {f.name for f in fields(Card)}


def load(extra_dirs: list[Path] | None = None) -> list[Card]:
    cards: list[Card] = []
    for directory in [DATA, *(extra_dirs or [])]:
        for path in sorted(Path(directory).glob("*.yaml")):
            for raw in yaml.safe_load(path.read_text()) or []:
                unknown = set(raw) - _FIELDS
                if unknown:
                    raise ValueError(f"{path.name}: card {raw.get('id')} has unknown keys {sorted(unknown)}")
                cards.append(Card(**raw))
    return cards


def version_matches(version: str | None, spec: str) -> bool:
    if spec in ("*", ""):
        return True
    if version is None:
        return False
    try:
        return Version(version) in SpecifierSet(spec)
    except (InvalidVersion, InvalidSpecifier):
        return False


def _data_matches(card: Card, match: IntentMatch) -> bool:
    for key, want in card.match.items():
        have = match.data.get(key)
        if want is None:
            if have is not None:
                return False
        elif have != want:
            return False
    return True


def applicable(card: Card, match: IntentMatch, stack: Stack) -> tuple[bool, str, str]:
    """Return (applies, resolved_kind, api_label).

    resolved_kind is "installed", "stdlib", "platform" or "wellknown" and decides
    the preference order: what you already run beats what you would have to add.
    """
    if card.intent != match.intent or not _data_matches(card, match):
        return False, "", ""
    if card.requires_any and not any(stack.has(r) for r in card.requires_any):
        return False, "", ""
    if card.unless_installed and not _missing_from_lockfile(card, stack):
        return False, "", ""
    if card.kind == "practice":  # architecture and conventions: apply to any stack
        return True, "practice", card.symbol
    if card.kind == "stdlib":
        py = python_version(stack)
        return version_matches(py, card.versions), "stdlib", f"{card.symbol} (Python {py})"
    if card.kind == "platform":
        found = next(r for r in card.requires_any if stack.has(r)) if card.requires_any else None
        label = (
            f"{card.symbol} (via {found} {stack.version_of(found) or ''})".replace(" )", ")") if found else card.symbol
        )
        return True, "platform", label
    lib = card.library or ""
    if stack.has(lib):
        installed = stack.version_of(lib)
        if installed is None:
            # Declared but not locked: we cannot check the version, so only the
            # declared specifier can tell us anything.
            dep = stack.dependencies[lib]
            ok = card.versions == "*" or _specifier_overlaps(dep.specifier, card.versions)
            return ok, "installed", f"{card.symbol} ({lib} {dep.specifier or 'unpinned'})"
        return version_matches(installed, card.versions), "installed", f"{card.symbol} ({lib} {installed})"
    # Not installed: only cards marked offer_uninstalled may suggest adding a dependency.
    if not card.offer_uninstalled:
        return False, "", ""
    return True, "wellknown", f"{card.symbol} ({lib} {card.versions}, not installed)"


def _missing_from_lockfile(card: Card, stack: Stack) -> bool:
    """True only when a lockfile lists every installed package and none of `unless_installed` is there.

    A requirements file or pyproject.toml lists direct dependencies only, so a package
    missing from it may still be installed transitively: that is not evidence.
    """
    dep = stack.dependencies.get(card.library or "")
    if dep is None or dep.source not in LOCKFILES:
        return False
    if card.extra and card.extra in dep.extras:
        return False
    return not any(stack.has(name) for name in card.unless_installed)


def _specifier_overlaps(declared: str | None, card_spec: str) -> bool:
    if not declared:
        return False
    try:
        lows = [s.version for s in SpecifierSet(declared) if s.operator in (">=", "==", "~=", ">")]
        return any(Version(v) in SpecifierSet(card_spec) for v in lows)
    except (InvalidVersion, InvalidSpecifier):
        return False


PREFERENCE = {"installed": 0, "stdlib": 1, "practice": 1, "platform": 2, "wellknown": 3}


def best_cards(match: IntentMatch, stack: Stack, cards: list[Card]) -> list[tuple[Card, str, str]]:
    """Applicable cards ordered by preference: installed, stdlib, platform, then well-known."""
    hits = []
    for card in cards:
        ok, kind, label = applicable(card, match, stack)
        if ok:
            hits.append((card, kind, label))
    hits.sort(key=lambda h: (PREFERENCE[h[1]], h[0].effort))
    return hits
