"""Core data types shared by every stage of the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


@dataclass
class Dependency:
    name: str  # normalised (PEP 503) name
    version: str | None  # exact installed/locked version when known
    specifier: str | None = None  # declared range, e.g. ">=2.0"
    source: str = ""  # file it was read from
    extras: set[str] = field(default_factory=set)  # requested extras, e.g. {"mcp"} for google-adk[mcp]


@dataclass
class Stack:
    root: str
    python: str | None = None
    dependencies: dict[str, Dependency] = field(default_factory=dict)
    docker_images: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def has(self, name: str) -> bool:
        return normalise(name) in self.dependencies

    def version_of(self, name: str) -> str | None:
        dep = self.dependencies.get(normalise(name))
        return dep.version if dep else None


def normalise(name: str) -> str:
    import re

    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass
class Function:
    qualname: str
    name: str
    file: str  # path relative to repo root
    start_line: int  # 1-based, inclusive (includes decorators)
    end_line: int
    def_line: int  # line of the `def` keyword
    decorators: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)  # dotted call names as written
    params: list[str] = field(default_factory=list)
    is_async: bool = False


@dataclass
class Module:
    file: str
    imports: dict[str, str] = field(default_factory=dict)  # local alias -> fully dotted target
    functions: list[Function] = field(default_factory=list)
    has_main_guard: bool = False


class ProofStatus(str, Enum):
    VERIFIED = "verified"  # tests pass before and after the patch
    BROKE_TESTS = "broke-tests"  # tests pass before but fail after
    UNVERIFIED = "unverified"  # no patch, no tests, or tests already failing


@dataclass
class IntentMatch:
    intent: str
    function: Function
    line: int
    evidence: str  # short human-readable reason
    data: dict[str, Any] = field(default_factory=dict)  # detector-specific details


@dataclass
class Card:
    """What a library version offers for an intent. intent "reference" marks a knowledge-only entry:
    no detector produces it, so it is never a finding, but it is searchable and feeds release "unlocks"."""

    id: str
    intent: str
    title: str
    symbol: str  # e.g. "functools.lru_cache"
    kind: str  # "installed" | "stdlib" | "wellknown"
    library: str | None  # package that provides it (None for stdlib)
    versions: str  # specifier the card applies to, e.g. ">=3.9" (python for stdlib)
    suggestion: str
    gain: str
    effort: int  # 1 (trivial) .. 5 (large)
    impact: int  # 1 .. 5
    risk: str
    when_not: str
    threshold: str
    sources: list[str]
    snippet: str = ""
    snippet_lang: str = "python"
    requires_any: list[str] = field(default_factory=list)  # at least one must be installed
    match: dict[str, Any] = field(default_factory=dict)  # constraints on IntentMatch.data (None = absent)
    offer_uninstalled: bool = False  # may be suggested as a new dependency
    autofix: bool = False  # apply the detector's byte-span edits as the patch
    topic: str = ""  # feature area the Knowledge base groups by, e.g. "Sessions, state and memory"
    # Applies only when none of these is in the lockfile (e.g. an extra's package that was never installed).
    unless_installed: list[str] = field(default_factory=list)
    extra: str = ""  # with unless_installed: the `library[extra]` that would install them


@dataclass
class Finding:
    id: str
    intent: str
    file: str
    line: int
    function: str
    title: str
    code_today: str
    suggestion: str
    symbol: str  # suggested API, e.g. "functools.lru_cache"
    api: str  # symbol + version it was checked against, e.g. "functools.lru_cache (Python 3.12)"
    gain: str
    effort: int
    impact: int
    risk: str
    when_not: str
    threshold: str
    sources: list[str]
    evidence: str
    snippet: str = ""
    snippet_lang: str = "python"
    patch: str | None = None  # unified diff, applicable with `patch -p1`
    confidence: float = 0.5
    hot_path: bool = False
    symbol_check: str = "unchecked"  # "ok" | "missing" | "unchecked"
    proof: ProofStatus = ProofStatus.UNVERIFIED
    proof_detail: str = ""
    score: float = 0.0
    rejected_reason: str | None = None
    span: tuple[int, int] = (0, 0)  # enclosing function's first and last line
    status: str | None = None  # feedback: accepted | rejected | muted
    card_id: str = ""
    covered: bool = False  # a test reaches this function: run during proof, else by the call graph
    reached_by: list[str] = field(default_factory=list)  # test ids that ran it during proof (first few)
    perf: dict[str, Any] | None = None  # measured before/after timing of the function (proof with --bench)

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        d = asdict(self)
        d["proof"] = self.proof.value
        return d
