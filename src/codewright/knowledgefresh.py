"""Keeping the knowledge base current: which libraries have moved on since their cards were checked, what
changed (release notes), and drafts of the cards those changes call for.

Each library's cards were checked against a version: the built-in ones are recorded in
``cards/data/checked.json``, a team's own in ``.codewright/knowledge.json`` (set when a card is saved or
the team marks a library re-checked). ``freshness`` compares that with the newest release on the package
index. ``release_notes`` gathers what changed in between (GitHub releases when the project links its
repository, else the changelog links), and ``draft_updates`` has the model turn those notes into new
cards and notes on cards that may be out of date, for the team to review; nothing is saved by itself.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from collections import Counter
from pathlib import Path

from packaging.version import InvalidVersion, Version

from . import cards as card_store

BUILTIN = Path(__file__).parent / "cards" / "data" / "checked.json"


def _v(s: str | None) -> Version | None:
    try:
        return Version(str(s)) if s else None
    except InvalidVersion:
        return None


def _team_path(root: str | Path) -> Path:
    return Path(root) / ".codewright" / "knowledge.json"


def checked_versions(root: str | Path | None) -> dict[str, dict]:
    """library -> {"version", "date", "by"}: the newest version its cards were checked against."""
    out: dict[str, dict] = {}
    try:
        for lib, v in json.loads(BUILTIN.read_text()).items():
            out[lib] = {**v, "by": "built-in"}
    except (OSError, ValueError):
        pass
    if root is not None:
        try:
            for lib, v in json.loads(_team_path(root).read_text()).items():
                if lib not in out or (_v(v.get("version")) or Version("0")) >= (
                    _v(out[lib]["version"]) or Version("0")
                ):
                    out[lib] = {**v, "by": "your team"}
        except (OSError, ValueError):
            pass
    return out


def mark_checked(root: str | Path, library: str, version: str) -> dict:
    """Record that the team re-checked `library`'s cards against `version`."""
    from .feedback import ensure_dir

    if _v(version) is None:
        raise ValueError(f"not a version: {version!r}")
    ensure_dir(root)
    p = _team_path(root)
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        data = {}
    data[library] = {"version": version, "date": time.strftime("%Y-%m-%d")}
    p.write_text(json.dumps(data, indent=2) + "\n")
    return checked_versions(root)


def _bump(old: Version | None, new: Version | None) -> str:
    if old is None or new is None:
        return "unknown"
    if new <= old:
        return "current"
    if new.major != old.major:
        return "major"
    return "minor" if new.minor != old.minor else "patch"


def freshness(root: str | Path, stack=None, online: bool = True) -> list[dict]:
    """One row per library the knowledge base covers: checked version, installed, newest, how far behind."""
    from . import watcher
    from .index import resolve as resolve_index

    cards = card_store.for_repo(root)
    counts = Counter(c.library for c in cards if c.library)
    checked = checked_versions(root)
    index = resolve_index(root)
    cache = watcher._load_cache(root, index)
    if online:
        watcher.prefetch(sorted(counts), cache, index=index)
        watcher._save_cache(root, cache, index)
    rows = []
    for lib, n in counts.most_common():
        latest = (cache.get(lib) or {}).get("version")
        chk = checked.get(lib)
        installed = stack.version_of(lib) if stack is not None else None
        bump = _bump(_v(chk["version"]) if chk else None, _v(latest))
        rows.append(
            {
                "library": lib,
                "cards": n,
                "checked": chk["version"] if chk else None,
                "checked_on": chk.get("date") if chk else None,
                "checked_by": chk.get("by") if chk else None,
                "installed": installed,
                "latest": latest,
                "released": (cache.get(lib) or {}).get("released"),
                "status": "unrecorded" if chk is None else ("current" if bump == "current" else "behind"),
                "bump": bump,
                "uses": installed is not None,
            }
        )
    order = {"behind": 0, "unrecorded": 1, "current": 2}
    return sorted(rows, key=lambda r: (order[r["status"]], not r["uses"], -r["cards"]))


# --------------------------------------------------------------------------- what changed


def _get_json(url: str, timeout: float = 8.0):
    req = urllib.request.Request(url, headers={"User-Agent": "codewright", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _github_repo(urls: dict) -> str | None:
    for u in urls.values():
        m = re.match(r"https?://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/|$)", str(u))
        if m:
            return f"{m.group(1)}/{m.group(2)}"
    return None


def release_notes(library: str, since: str | None, until: str | None, limit: int = 20) -> dict:
    """What changed after `since` up to `until`: {"versions", "notes": [{version, date, body}], "links", "error"}."""
    out: dict = {
        "library": library,
        "since": since,
        "until": until,
        "versions": [],
        "notes": [],
        "links": [],
        "error": None,
    }
    lo, hi = _v(since), _v(until)
    try:
        data = _get_json(f"https://pypi.org/pypi/{library}/json")
    except Exception as exc:  # offline, a private index, or not on PyPI
        out["error"] = f"could not read {library} on PyPI ({type(exc).__name__}); paste its release notes instead"
        return out
    vs = sorted(
        (v for k in data.get("releases", {}) if (v := _v(k)) is not None and not v.is_prerelease),
        reverse=True,
    )
    out["versions"] = [str(v) for v in vs if (lo is None or v > lo) and (hi is None or v <= hi)][:50]
    urls = data.get("info", {}).get("project_urls") or {}
    out["links"] = [
        {"label": k, "url": u} for k, u in urls.items() if re.search(r"change|release|history|news|what", k, re.I)
    ][:5]
    repo = _github_repo(urls)
    if repo:
        try:
            releases = _get_json(f"https://api.github.com/repos/{repo}/releases?per_page=50")
        except Exception:
            releases = []
        for rel in releases if isinstance(releases, list) else []:
            tag = str(rel.get("tag_name") or rel.get("name") or "")
            v = _v(re.sub(r"^[^\d]*", "", tag.split("@")[-1]))
            if v is None or v.is_prerelease or (lo is not None and v <= lo) or (hi is not None and v > hi):
                continue
            out["notes"].append(
                {
                    "version": str(v),
                    "date": str(rel.get("published_at") or "")[:10],
                    "body": str(rel.get("body") or "")[:6000],
                }
            )
            if len(out["notes"]) >= limit:
                break
        if not out["links"]:
            out["links"] = [{"label": "Releases", "url": f"https://github.com/{repo}/releases"}]
    if not out["notes"]:  # no GitHub releases (or the API is out of reach): read the project's changelog page
        for link in out["links"]:
            try:
                out["notes"] = _changelog_sections(link["url"], set(out["versions"]), limit)
            except Exception:
                continue
            if out["notes"]:
                break
    return out


class _Text(__import__("html.parser").parser.HTMLParser):
    """Text of an HTML page with headings marked, enough to cut a changelog into versions."""

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav"):
            self._skip += 1
        elif tag in ("h1", "h2", "h3", "h4"):
            self.parts.append("\n\n## ")
        elif tag in ("li", "p", "br"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _changelog_sections(url: str, versions: set[str], limit: int) -> list[dict]:
    """The sections of a changelog (HTML, Markdown or plain text) whose heading names one of `versions`."""
    req = urllib.request.Request(url, headers={"User-Agent": "codewright"})
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read(6_000_000).decode("utf-8", "replace")
    if "<html" in raw[:2000].lower() or "<body" in raw.lower():
        parser = _Text()
        parser.feed(raw)
        raw = "".join(parser.parts)
    out = []
    # a heading line that starts with (or is) a version: "## 0.165.0", "Version 2.1.9 (2026-11-02)", "v1.4.0"
    heads = list(
        re.finditer(r"(?m)^(?:#+\s*|=+\s*)?(?:version\s+|v|release\s+)?(\d+\.\d+(?:\.\d+)?)\b[^\n]*$", raw, re.I)
    )
    for i, m in enumerate(heads):
        if m.group(1) not in versions:
            continue
        end = heads[i + 1].start() if i + 1 < len(heads) else len(raw)
        body = re.sub(r"\n{3,}", "\n\n", raw[m.end() : end]).strip()
        if body:
            out.append({"version": m.group(1), "date": "", "body": body[:6000]})
        if len(out) >= limit:
            break
    return out


DRAFT_SYSTEM = (
    "You keep a code-review tool's knowledge base current for one Python library. You get the cards it has now "
    "(id, title, API, versions) and the release notes since they were checked. Find what a reviewer should now "
    "know: new APIs that replace hand-written code, deprecations and removals, changed defaults or behaviour. "
    "For each, either a new card or a note on an existing card that is now out of date. Ground every item in the "
    'notes, name the version, and do not invent APIs. Reply with JSON only: {"new_cards": [{"title", "symbol", '
    '"versions" (PEP 440, e.g. ">=2.15"), "suggestion", "gain", "risk", "when_not", "sources" (URLs), "topic"}], '
    '"outdated": [{"card": <id>, "why", "change"}]}'
)


def draft_updates(root: str | Path, library: str, notes: dict, pasted: str = "") -> dict:
    """The model's proposed new cards and outdated-card notes for `library`, for the team to review."""
    from .llm import LLMConfig, complete, last_error
    from .llm import status as llm_status

    cfg = LLMConfig.load(root)
    st = llm_status(cfg)
    if not st["available"]:
        raise RuntimeError(f"no model to draft with: {st['reason']}. Set one up in Settings")
    have = [c for c in card_store.for_repo(root) if c.library == library]
    listing = "\n".join(f"- {c.id}: {c.title} [{c.symbol}] versions {c.versions}" for c in have[:120]) or "- none"
    text = "\n\n".join(f"## {n['version']} ({n['date']})\n{n['body']}" for n in notes.get("notes", []))
    if pasted.strip():
        text += "\n\n## Pasted by the team\n" + pasted.strip()
    if not text.strip():
        raise RuntimeError("no release notes to work from: paste the changelog for these versions")
    ask = (
        f"Library: {library}. Cards checked up to {notes.get('since') or 'an unrecorded version'}; newest is "
        f"{notes.get('until')}.\n\nCards now:\n{listing}\n\nRelease notes:\n{text[:30000]}"
    )
    reply = complete(cfg, DRAFT_SYSTEM, [{"role": "user", "content": ask}], max_tokens=3000, purpose="update knowledge")
    if reply is None:
        raise RuntimeError(last_error() or "the model did not answer")
    m = re.search(r"\{.*\}", reply, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        data = {}
    ids = {c.id for c in have}
    new = [
        {
            **{
                k: c.get(k, "")
                for k in ("title", "symbol", "versions", "suggestion", "gain", "risk", "when_not", "topic")
            },
            "sources": [s for s in c.get("sources", []) if isinstance(s, str)][:5],
            "library": library,
        }
        for c in data.get("new_cards", [])
        if isinstance(c, dict) and c.get("title") and c.get("suggestion")
    ][:20]
    outdated = [
        {"card": o["card"], "why": str(o.get("why", ""))[:600], "change": str(o.get("change", ""))[:600]}
        for o in data.get("outdated", [])
        if isinstance(o, dict) and o.get("card") in ids
    ][:30]
    return {"library": library, "new_cards": new, "outdated": outdated, "model": cfg.model}
