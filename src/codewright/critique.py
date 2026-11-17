"""Proposal review: judge a plan (an RFC, a design doc, an agent's plan) before any code is written.

Pasted text is checked against what the repository already is:

* **dependencies**: a library the plan adds when the repo already has one for the job, one the
  standard library covers, a version that differs from the lockfile, and the team's "no new
  dependencies" constraint;
* **reuse**: "write a retry helper / cache / queue poller" when the installed stack already
  offers it (from the knowledge base, at the locked versions);
* **code it touches**: files and functions it names that don't exist, and ones with many
  callers and no test (risky to change without one);
* **architecture**: a dependency between named modules that `.codewright/rules.yaml` forbids,
  and infrastructure the saved design (``design.json``) doesn't have;
* **scale**: SQLite or in-process state with several workers or high request rates;
* **completeness**: a schema change without a migration, no test step, no rollback.

Each point is ``blocker``, ``concern`` or ``ok``, with the evidence (file, card, rule) behind it.
Deterministic and offline; the same plan gets the same review.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import cards as card_store
from . import deps as deps_mod
from . import knowledge
from . import rules as rules_mod
from .graph import CodeGraph, is_test_file
from .models import Stack, normalise

BUILD_VERBS = re.compile(
    r"\b(write|implement|build|create|add|roll|hand[- ]?roll|custom|our own|own|wrap|wrapper|helper|introduce)\b", re.I
)
INFRA = {
    "queue": r"\b(kafka|rabbitmq|sqs|pub/?sub|nats|celery|rq|message queue|task queue|redis queue)\b",
    "cache": r"\b(redis|memcached|cache layer|caching layer)\b",
    "database": r"\b(postgres(?:ql)?|mysql|mongodb|mongo|dynamodb|cassandra|elasticsearch|opensearch|pgvector"
    r"|qdrant)\b",
    "storage": r"\b(s3|gcs|blob storage|object storage|minio)\b",
    "llm": r"\b(openai|anthropic|claude|gemini|llm|gpt-?\d)\b",
}
_PATH = re.compile(r"(?<![\w/.-])((?:[\w-]+/)+[\w-]+\.py|[\w-]+\.py)\b")
_DOTTED = re.compile(r"`([A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)+)`")
_CALL = re.compile(r"`([A-Za-z_]\w*)\(\)?`|`([A-Za-z_]\w*)\(")
_VERSIONED = re.compile(r"\b([A-Za-z][\w.-]*[A-Za-z0-9])\s+v?(\d+(?:\.\d+){0,2})\b")
_RATE = re.compile(r"(\d[\d,.]*)\s*(k|m)?\s*(?:rps|req(?:uest)?s?\s*(?:/|per)\s*s(?:ec(?:ond)?)?|qps)", re.I)


@dataclass
class Point:
    level: str  # blocker | concern | ok
    topic: str  # dependencies | reuse | code | architecture | scale | completeness
    title: str
    detail: str
    evidence: list[str] = field(default_factory=list)
    quote: str = ""  # the sentence of the plan it is about

    def to_dict(self) -> dict:
        return asdict(self)


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n\s*[-*\d.)]+\s+|\n{2,}|\n(?=#)", text)
    return [p.strip(" -*#\t\n") for p in parts if p and p.strip(" -*#\t\n")]


def _known_libraries(stack: Stack, cards) -> set[str]:
    names = {normalise(n) for n in stack.dependencies}
    names |= {c.library for c in cards if c.library}
    for group in deps_mod.OVERLAPS.values():
        names |= set(group)
    names |= set(deps_mod.STDLIB)
    return {n for n in names if len(n) > 2}


def _mentions(text: str, name: str) -> bool:
    forms = {name, name.replace("-", "_"), name.replace("-", " ")}
    return any(re.search(rf"(?<![\w.-]){re.escape(f)}(?![\w-])", text, re.I) for f in forms)


def _group_of(name: str) -> str | None:
    return next((job for job, members in deps_mod.OVERLAPS.items() if name in members), None)


def review(
    text: str,
    root: str | Path,
    graph: CodeGraph,
    stack: Stack,
    *,
    constraints: list[str] | None = None,
) -> dict:
    root = Path(root)
    cards = card_store.load()
    points: list[Point] = []
    sents = sentences(text)
    lowered = text.lower()

    points += _dependencies(text, sents, stack, cards, constraints or [])
    points += _reuse(sents, stack, cards)
    points += _code(text, sents, graph)
    points += _architecture(text, sents, root, graph)
    points += _scale(lowered, sents, stack)
    points += _completeness(lowered)

    weights = {"blocker": 30, "concern": 10, "ok": 0}
    score = max(0, 100 - sum(weights[p.level] for p in points))
    blockers = sum(1 for p in points if p.level == "blocker")
    concerns = sum(1 for p in points if p.level == "concern")
    if blockers:
        verdict = "revise"
        headline = f"{blockers} blocker(s) to resolve before building this"
    elif concerns > 2:
        verdict = "revise"
        headline = f"{concerns} concerns: worth a revision before building"
    else:
        verdict = "go"
        headline = "Fits the codebase" + (f", with {concerns} point(s) to keep in mind" if concerns else "")
    order = {"blocker": 0, "concern": 1, "ok": 2}
    points.sort(key=lambda p: (order[p.level], p.topic))
    return {
        "verdict": verdict,
        "score": score,
        "headline": headline,
        "points": [p.to_dict() for p in points],
        "counts": {"blocker": blockers, "concern": concerns, "ok": sum(1 for p in points if p.level == "ok")},
    }


# --------------------------------------------------------------------------- dependencies


def _sentence_with(sents: list[str], name: str) -> str:
    return next((s for s in sents if _mentions(s, name)), "")


def _dependencies(text, sents, stack: Stack, cards, constraints: list[str]) -> list[Point]:
    out: list[Point] = []
    no_new = any(re.search(r"no new (dependenc|librar|packages)", c, re.I) for c in constraints)
    used = {normalise(n) for n in stack.dependencies}
    for name in sorted(_known_libraries(stack, cards)):
        if not _mentions(text, name):
            continue
        quote = _sentence_with(sents, name)
        if name in used:
            m = next((m for m in _VERSIONED.finditer(quote) if normalise(m.group(1)) == name), None)
            have = stack.version_of(name)
            if m and have and not have.startswith(m.group(2)) and not m.group(2).startswith(have.split(".")[0] + "."):
                want_major, have_major = m.group(2).split(".")[0], have.split(".")[0]
                if want_major != have_major:
                    out.append(
                        Point(
                            "concern",
                            "dependencies",
                            f"Plan assumes {name} {m.group(2)}; the lockfile has {have}",
                            f"APIs differ between major versions: write it for {have}, or make the upgrade its own "
                            f"step first (`codewright migrate list`).",
                            [f"lockfile: {name}=={have}"],
                            quote,
                        )
                    )
            continue
        if name in deps_mod.STDLIB:
            since, repl, _ = deps_mod.STDLIB[name]
            out.append(
                Point(
                    "concern",
                    "dependencies",
                    f"`{name}` isn't needed: use `{repl}`",
                    f"`{repl}` is in the standard library since Python {since}.",
                    [f"standard library: {repl}"],
                    quote,
                )
            )
            continue
        job = _group_of(name)
        siblings = [s for s in deps_mod.OVERLAPS.get(job, []) if s in used and s != name] if job else []
        if not (
            siblings or no_new or re.search(r"\b(add|install|introduce|use|adopt|switch to|pull in)\b", quote, re.I)
        ):
            continue
        if siblings:
            out.append(
                Point(
                    "blocker" if no_new else "concern",
                    "dependencies",
                    f"Plan adds `{name}`; the repo already uses `{siblings[0]}` for {job.lower()}",
                    f"Two libraries for one job means two sets of timeouts, errors and mocks. Use `{siblings[0]}` "
                    "unless the plan says why it can't do this.",
                    [f"dependency: {siblings[0]}=={stack.version_of(siblings[0]) or '?'}"],
                    quote,
                )
            )
        elif no_new:
            out.append(
                Point(
                    "blocker",
                    "dependencies",
                    f"Plan adds a new dependency, `{name}`",
                    "The team's constraints say no new dependencies (Settings → constraints).",
                    ["constraint: no new dependencies"],
                    quote,
                )
            )
    return out


# --------------------------------------------------------------------------- reuse


def _reuse(sents: list[str], stack: Stack, cards) -> list[Point]:
    out: list[Point] = []
    seen: set[str] = set()
    usable = [c for c in cards if c.kind in ("library", "stdlib", "installed", "platform") and c.intent != "reference"]
    for s in sents:
        if not BUILD_VERBS.search(s) or len(s) < 12:
            continue
        hits = knowledge.search(s, usable, stack, only_fitting=True, limit=5)
        best = next((h for h in hits if h["score"] >= 4.0 and h["id"] not in seen and _about(s, h)), None)
        if best is None:
            continue
        seen.add(best["id"])
        where = best["fit"]["detail"] or "installed"
        out.append(
            Point(
                "concern",
                "reuse",
                f"Already available: {best['title']}",
                f"`{best['symbol']}` ({where}) does this. {best['suggestion']}",
                [f"card: {best['id']}", *best["sources"][:1]],
                s,
            )
        )
    return out


_GENERIC = {"use", "add", "write", "instead", "new", "own", "helper", "python", "code", "call", "function", "build"}


def _about(sentence: str, card: dict) -> bool:
    """The sentence names what the card is about: two words shared with its title, symbol or intent, or
    every word of its intent (a shared prefix of 4+ letters counts, so "memoize" meets "memo"), not just
    words from its body."""
    from .words import tokens

    mine = set(tokens(sentence)) - _GENERIC
    theirs = set(tokens(f"{card['title']} {card['symbol'].replace('.', ' ')} {card['intent'].replace('-', ' ')}"))
    theirs -= _GENERIC

    def near(a: str, b: str) -> bool:
        return a == b or (min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a)))

    if sum(1 for a in mine if any(near(a, b) for b in theirs)) >= 2:
        return True
    intent = set(tokens(card["intent"].replace("-", " "))) - _GENERIC  # "retry", "memo cache": the intent itself
    return bool(intent) and all(any(near(a, b) for a in mine) for b in intent)


# --------------------------------------------------------------------------- code it touches


def _fan_in(graph: CodeGraph) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for caller, callees in graph.edges.items():
        if is_test_file(caller.partition("::")[0]):
            continue
        for c in callees:
            out[c] += 1
    return out


def _code(text: str, sents: list[str], graph: CodeGraph) -> list[Point]:
    out: list[Point] = []
    files = set(graph.files)
    named = sorted({m.group(1) for m in _PATH.finditer(text)})
    for p in named:
        hits = [f for f in files if f == p or f.endswith("/" + p)]
        if hits:
            continue
        creating = re.search(rf"\b(new|create|add|write|introduce)\b[^.\n]*{re.escape(p)}", text, re.I)
        if not creating:
            out.append(
                Point(
                    "concern",
                    "code",
                    f"`{p}` doesn't exist",
                    "The plan refers to it as existing code. If it's new, say so; if it was renamed, point at the "
                    "current file.",
                    [],
                    _sentence_with(sents, p.rsplit("/", 1)[-1]),
                )
            )
    fan_in = _fan_in(graph)
    tested = graph.test_reachable()
    by_name: dict[str, list[str]] = defaultdict(list)
    for pf, fn in graph.functions():
        if not is_test_file(pf.path):
            by_name[fn.name].append(graph.key(fn))
    funcs = {a or b for a, b in _CALL.findall(text)}
    for name in sorted(funcs):
        keys = by_name.get(name, [])
        if not keys:
            continue
        key = max(keys, key=lambda k: fan_in.get(k, 0))
        callers = fan_in.get(key, 0)
        if callers >= 5 and key not in tested:
            out.append(
                Point(
                    "concern",
                    "code",
                    f"`{name}` has {callers} callers and no test",
                    f"Changing `{key}` affects every caller; add a test that pins today's behaviour first.",
                    [key],
                    _sentence_with(sents, name),
                )
            )
        elif callers >= 5:
            out.append(
                Point(
                    "ok",
                    "code",
                    f"`{name}` has {callers} callers and is tested",
                    "Run its tests after the change; keep its signature or update every caller in the same change.",
                    [key],
                    _sentence_with(sents, name),
                )
            )
    return out


# --------------------------------------------------------------------------- architecture


def _module_of(graph: CodeGraph, ref: str) -> str | None:
    if ref.endswith(".py"):
        return next((f for f in graph.files if f == ref or f.endswith("/" + ref)), None)
    tail = ref.replace(".", "/")
    return next((f for f in graph.files if f.endswith(f"{tail}.py") or f.endswith(f"{tail}/__init__.py")), None)


def _architecture(text: str, sents: list[str], root: Path, graph: CodeGraph) -> list[Point]:
    out: list[Point] = []
    try:
        rules = rules_mod.load(root)
    except ValueError:
        rules = []
    from .archmap import module_name

    if rules:
        for s in sents:
            refs = [m.group(1) for m in _PATH.finditer(s)] + _DOTTED.findall(s)
            mods = [(r, _module_of(graph, r)) for r in refs]
            mods = [(r, f) for r, f in mods if f]
            for i, (_ra, a) in enumerate(mods):
                for _rb, b in mods[i + 1 :]:
                    for src, dst in ((a, b), (b, a)):
                        site = rules_mod.ImportSite(src, 1, module_name(dst), dst)
                        for rule in rules:
                            if rule.kind == "layers":
                                vs = rules_mod._layers(rule, [site])
                            elif rule.kind == "forbid-import":
                                vs = rules_mod._forbid(rule, [site])
                            elif rule.kind == "only-in":
                                vs = rules_mod._only_in(rule, [site])
                            else:
                                vs = []
                            for v in vs:
                                out.append(
                                    Point(
                                        "concern",
                                        "architecture",
                                        f"If `{src}` uses `{dst}`, rule `{rule.id}` fails",
                                        f"{v.message}. {v.fix}",
                                        [f"rule: {rule.id} ({rule.source})"],
                                        s,
                                    )
                                )
        # Libraries the rules restrict, used where the plan puts them.
        for rule in rules:
            if rule.kind != "only-in":
                continue
            for s in sents:
                libs = [i for i in rule.imports if "/" not in i and _mentions(s, i)]
                refs = [_module_of(graph, m.group(1)) for m in _PATH.finditer(s)]
                for lib in libs:
                    for f in filter(None, refs):
                        if not rules_mod.path_matches(f, rule.paths):
                            out.append(
                                Point(
                                    "concern",
                                    "architecture",
                                    f"`{lib}` in `{f}` breaks rule `{rule.id}`",
                                    f"`{lib}` may only be used from {', '.join(rule.paths)}. {rule.reason}".strip(),
                                    [f"rule: {rule.id}"],
                                    s,
                                )
                            )
    from .design import load_intended

    intent = load_intended(root)
    if intent:
        expected = set(intent.get("expected_kinds", []))
        for kind, pattern in INFRA.items():
            m = re.search(pattern, text, re.I)
            if m and kind not in expected:
                out.append(
                    Point(
                        "concern",
                        "architecture",
                        f"The saved design has no {kind}; the plan adds {m.group(0)}",
                        f"The {intent.get('style', 'saved')} design (`.codewright/design.json`) was chosen without a "
                        f"{kind}. Either the plan should work without one, or update the design first so "
                        "`codewright check` keeps matching.",
                        ["design.json"],
                        _sentence_with(sents, m.group(0)),
                    )
                )
    return out


# --------------------------------------------------------------------------- scale


def _rate(text: str) -> float | None:
    best = None
    for m in _RATE.finditer(text):
        try:
            n = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        n *= {"k": 1e3, "m": 1e6}.get((m.group(2) or "").lower(), 1)
        best = max(best or 0, n)
    return best


def _scale(lowered: str, sents: list[str], stack: Stack) -> list[Point]:
    out: list[Point] = []
    rate = _rate(lowered)
    many = re.search(
        r"\b(replicas?|instances|workers|horizontal(ly)? scal|autoscal|multiple pods|several pods)\b", lowered
    )
    if "sqlite" in lowered and ((rate and rate > 50) or many):
        out.append(
            Point(
                "concern",
                "scale",
                "SQLite with several writers or high load",
                "SQLite allows one writer at a time and lives on one machine's disk: with "
                + (f"~{int(rate)} requests/s" if rate else "several workers or replicas")
                + " use a server database (Postgres) instead.",
                ["https://www.sqlite.org/whentouse.html"],
                _sentence_with(sents, "sqlite"),
            )
        )
    in_proc = re.search(
        r"\b(in[- ]memory|module[- ]level dict|global dict|in[- ]process) (cache|store|state|dict|queue|session)",
        lowered,
    )
    if in_proc and many:
        out.append(
            Point(
                "concern",
                "scale",
                f"{in_proc.group(0).capitalize()} with several workers",
                "Each worker or replica gets its own copy: users see different data depending on which one answers. "
                "Use a shared store (Redis, the database) or pin it to one process.",
                [],
                next((s for s in sents if in_proc.group(0) in s.lower()), ""),
            )
        )
    if rate and rate >= 500 and any(stack.has(x) for x in ("flask", "django")) and "async" not in lowered:
        out.append(
            Point(
                "concern",
                "scale",
                f"{int(rate)} requests/s on a synchronous framework",
                "Each request holds a worker thread for its whole duration: size workers × threads from the latency "
                "(rate × seconds per request), or move the I/O-bound path to async.",
                [],
                "",
            )
        )
    return out


# --------------------------------------------------------------------------- completeness


def _completeness(lowered: str) -> list[Point]:
    out: list[Point] = []
    schema = re.search(
        r"\b(new (column|table|field)|add(ing)? (a )?(column|table|index)|schema change|alter table)\b", lowered
    )
    if schema and not re.search(r"\b(migration|alembic|migrate|backfill)\b", lowered):
        out.append(
            Point(
                "concern",
                "completeness",
                "Schema change without a migration",
                "Say how the schema changes in production (an Alembic/Django migration), whether old rows need a "
                "backfill, and whether old code can run against the new schema during the deploy.",
                [],
                "",
            )
        )
    if not re.search(r"\b(tests?|pytest|unit test|integration test|verify|verification)\b", lowered):
        out.append(
            Point(
                "concern",
                "completeness",
                "No test step",
                "Name the tests that prove it works (new ones, and existing ones that must still pass).",
                [],
                "",
            )
        )
    if re.search(r"\b(deploy|roll ?out|ship|production)\b", lowered) and not re.search(
        r"\b(rollback|roll back|revert|feature flag|flag|canary|kill switch)\b", lowered
    ):
        out.append(
            Point(
                "concern",
                "completeness",
                "No way back",
                "The plan ships to production but doesn't say how to undo it: a feature flag, a revert, or a "
                "reversible migration.",
                [],
                "",
            )
        )
    return out
