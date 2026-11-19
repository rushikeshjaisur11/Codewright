"""Detection results per file, kept between runs, so a review only looks again at files that changed.

Stored as JSON in ``.codewright/cache/detect.json`` (never pickle: a cloned repo could ship the file).
It is thrown away whenever Codewright or its detectors change, since the matches depend on them.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .models import Function, IntentMatch


def _path(root: str | Path) -> Path:
    return Path(root) / ".codewright" / "cache" / "detect.json"


def signature() -> str:
    """Changes whenever a detector's code does."""
    h = hashlib.sha1(__version__.encode())
    for p in sorted((Path(__file__).parent / "intents").glob("*.py")):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()


class Unencodable(TypeError):
    pass


def _enc(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, tuple):
        return {"__t": [_enc(x) for x in v]}
    if isinstance(v, list):
        return [_enc(x) for x in v]
    if isinstance(v, (set, frozenset)):
        return {"__s": [_enc(x) for x in sorted(v, key=repr)]}
    if isinstance(v, bytes):
        return {"__b": base64.b64encode(v).decode()}
    if isinstance(v, dict) and all(isinstance(k, str) for k in v):
        if any(k.startswith("__") for k in v):
            raise Unencodable("reserved key")
        return {k: _enc(x) for k, x in v.items()}
    raise Unencodable(type(v).__name__)  # a syntax-tree node or another live object: not cacheable


def _dec(v):
    if isinstance(v, list):
        return [_dec(x) for x in v]
    if isinstance(v, dict):
        if "__t" in v:
            return tuple(_dec(x) for x in v["__t"])
        if "__s" in v:
            return {_dec(x) for x in v["__s"]}
        if "__b" in v:
            return base64.b64decode(v["__b"])
        return {k: _dec(x) for k, x in v.items()}
    return v


def _match_to(m: IntentMatch) -> dict:
    return {"i": m.intent, "f": asdict(m.function), "l": m.line, "e": m.evidence, "d": _enc(m.data)}


def _match_from(d: dict) -> IntentMatch:
    return IntentMatch(d["i"], Function(**d["f"]), d["l"], d["e"], _dec(d["d"]))


def load(root: str | Path) -> dict[str, dict]:
    """{file: {"h": sha1, "m": [IntentMatch]}}, or {} when there is none or it is from other detectors."""
    try:
        data = json.loads(_path(root).read_text())
        if data.get("sig") != signature():
            return {}
        cache = {p: {"h": e["h"], "m": [_match_from(m) for m in e["m"]]} for p, e in data["files"].items()}
        _LOADED[str(Path(root).resolve())] = _fingerprint(cache)
        return cache
    except (OSError, ValueError, KeyError, TypeError):
        return {}


_LOADED: dict[str, str] = {}  # root -> fingerprint of the cache as last read or written


def _fingerprint(cache: dict[str, dict]) -> str:
    """Which files the cache holds at which hash: when that is unchanged, so are its matches."""
    return hashlib.sha1("".join(f"{p}:{e['h']};" for p, e in sorted(cache.items())).encode()).hexdigest()


def save(root: str | Path, cache: dict[str, dict]) -> None:
    key, fp = str(Path(root).resolve()), _fingerprint(cache)
    if _LOADED.get(key) == fp and _path(root).exists():
        return  # nothing changed since it was read: rewriting tens of MB would only cost time
    _LOADED[key] = fp
    files = {}
    for path, e in cache.items():
        try:
            files[path] = {"h": e["h"], "m": [_match_to(m) for m in e["m"]]}
        except Unencodable:
            continue  # this file is looked at again next time
    p = _path(root)
    with contextlib.suppress(OSError):
        from .feedback import ensure_dir

        ensure_dir(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"sig": signature(), "files": files}))
        tmp.replace(p)
