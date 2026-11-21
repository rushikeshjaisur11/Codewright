"""What was already reviewed, so reopening a repo only re-checks what changed, and which files are worth
spending a test run on.

Proof (running the tests against each patch) is the slow part of a review. This module keeps, in
``.codewright/review-state.json``:

- a fingerprint per Python file (mtime and size, with a content hash so a ``touch`` is not a change);
- the proof result of each finding, valid only while the finding's file, the files it imports, and the
  tests are all unchanged.

``plan`` then answers "which files should I verify?": the model picks from a compact table of candidates
when one is set up, or a ranking by impact and what changed when it is not. The user confirms the list;
nothing is chosen or run without them.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .graph import CodeGraph, is_test_file, iter_python_files
from .llm import LLMConfig, complete_json
from .llm import status as llm_status
from .models import Finding, ProofStatus

VERSION = 1
MAX_DEPS = 200  # files hashed into one finding's validity key


def state_path(root: str | Path) -> Path:
    return Path(root) / ".codewright" / "review-state.json"


def _load(root: str | Path) -> dict:
    try:
        data = json.loads(state_path(root).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if data.get("version") == VERSION else {}


def _save(root: str | Path, data: dict) -> None:
    from .feedback import ensure_dir

    ensure_dir(root)
    tmp = state_path(root).with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": VERSION, **data}))
    tmp.replace(state_path(root))


def fingerprints(root: str | Path, known: dict[str, dict] | None = None) -> dict[str, dict]:
    """{relative path: {"m": mtime_ns, "s": size, "h": sha1}}. A file whose mtime and size match `known` is
    not read again, so scanning a big repo that has not changed is a stat per file."""
    root = Path(root).resolve()
    known = known or {}
    out: dict[str, dict] = {}
    for p in iter_python_files(root):
        rel = p.relative_to(root).as_posix()
        try:
            st = p.stat()
        except OSError:
            continue
        old = known.get(rel)
        if old and old["m"] == st.st_mtime_ns and old["s"] == st.st_size:
            out[rel] = old
            continue
        try:
            digest = hashlib.sha1(p.read_bytes()).hexdigest()
        except OSError:
            continue
        out[rel] = {"m": st.st_mtime_ns, "s": st.st_size, "h": digest}
    return out


class ReviewState:
    """The on-disk record of the last review, loaded once per review and written when it finishes."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        data = _load(self.root)
        self.files: dict[str, dict] = data.get("files", {})  # fingerprints at the last save
        self.reviewed: dict[str, str] = data.get("reviewed", {})  # file -> hash when it was last reviewed
        self.proofs: dict[str, dict] = data.get("proofs", {})  # finding id -> cached proof
        self.stale: list[str] = data.get("stale", [])  # edited since proof last ran on them
        self.at: float | None = data.get("at")
        self.now = fingerprints(self.root, self.files)

    # ----------------------------------------------------------------- what changed
    def changed(self) -> list[str]:
        """Files new, edited or removed since they were last reviewed (everything, before the first review)."""
        out = {p for p, fp in self.now.items() if self.reviewed.get(p) != fp["h"]}
        out |= {p for p in self.reviewed if p not in self.now}
        return sorted(out)

    def _hash(self, path: str) -> str:
        fp = self.now.get(path)
        return fp["h"] if fp else ""

    def tests_hash(self) -> str:
        h = hashlib.sha1()
        for p in sorted(self.now):
            if is_test_file(p):
                h.update(f"{p}:{self._hash(p)};".encode())
        return h.hexdigest()

    def deps_hash(self, graph: CodeGraph, file: str) -> str:
        """The file, plus everything it imports (transitively), so a change below it invalidates the proof."""
        seen, todo = {file}, [file]
        while todo and len(seen) < MAX_DEPS:
            pf = graph.files.get(todo.pop())
            if pf is None or graph.resolver is None:
                continue
            for t in graph.resolver.imported_files(pf):
                if t not in seen:
                    seen.add(t)
                    todo.append(t)
        h = hashlib.sha1()
        for p in sorted(seen):
            h.update(f"{p}:{self._hash(p)};".encode())
        return h.hexdigest()

    # ----------------------------------------------------------------- proof cache
    def key(self, f: Finding, graph: CodeGraph, tests: str) -> str:
        patch = hashlib.sha1((f.patch or "").encode()).hexdigest()
        return f"{self.deps_hash(graph, f.file)}:{tests}:{patch}"

    def reuse(self, findings: list[Finding], graph: CodeGraph) -> set[str]:
        """Copy a cached proof onto each finding whose file, imports, tests and patch are unchanged;
        returns the ids that were filled in."""
        tests = self.tests_hash()
        hit = set()
        for f in findings:
            c = self.proofs.get(f.id)
            if not c or c.get("key") != self.key(f, graph, tests):
                continue
            f.proof = ProofStatus(c["proof"])
            f.proof_detail = c.get("detail", "")
            f.reached_by = c.get("reached_by", [])
            f.perf = c.get("perf")
            f.covered = c.get("covered", f.covered)
            f.proof_cached = True
            hit.add(f.id)
        return hit

    def save(self, findings: list[Finding], graph: CodeGraph, *, proved_files: set[str]) -> None:
        """Record this review. `proved_files` is the files whose tests ran against their patches this time."""
        tests = self.tests_hash()
        ids = {f.id for f in findings}
        keep = {fid: c for fid, c in self.proofs.items() if fid in ids}
        for f in findings:
            if f.patch and f.file in proved_files and not f.proof_cached:
                keep[f.id] = {
                    "key": self.key(f, graph, tests),
                    "proof": f.proof.value,
                    "detail": f.proof_detail,
                    "reached_by": f.reached_by,
                    "perf": f.perf,
                    "covered": f.covered,
                }
        # Edited since the tests last ran on them: carried across reviews until a proof run covers them.
        stale = set(self.stale)
        if self.reviewed:
            stale |= set(self.changed())
        stale = {p for p in stale if p in self.now} - proved_files
        _save(
            self.root,
            {
                "files": self.now,
                "reviewed": {p: fp["h"] for p, fp in self.now.items()},
                "stale": sorted(stale),
                "proofs": keep,
                "at": time.time(),
            },
        )
        self.stale = sorted(stale)

    # ----------------------------------------------------------------- summary for the UI
    def summary(self, findings: list[Finding]) -> dict:
        unproven = [f for f in findings if f.patch and f.proof is ProofStatus.UNVERIFIED]
        stale = sorted({*self.stale, *self.changed()} & set(self.now)) if self.reviewed else []
        return {
            "changed": stale[:200],
            "changed_count": len(stale),
            "never_reviewed": not self.reviewed,
            "unproven": len(unproven),
            "unproven_files": len({f.file for f in unproven}),
            "last_proof": self.at,
        }


# --------------------------------------------------------------------------- which files to verify


def candidates(findings: list[Finding], changed: set[str]) -> list[dict]:
    """One row per file with something to verify: its unproven patches, ranked by impact and by whether it changed."""
    rows: dict[str, dict] = {}
    for f in findings:
        if not f.patch or f.proof is not ProofStatus.UNVERIFIED:
            continue
        r = rows.setdefault(
            f.file,
            {
                "file": f.file,
                "findings": 0,
                "impact": 0,
                "hot": 0,
                "covered": 0,
                "intents": set(),
                "changed": f.file in changed,
            },
        )
        r["findings"] += 1
        r["impact"] += f.impact
        r["hot"] += int(f.hot_path)
        r["covered"] += int(f.covered)
        r["intents"].add(f.intent)
    for r in rows.values():
        r["intents"] = sorted(r["intents"])[:4]
        r["score"] = (3 if r["changed"] else 0) + r["impact"] + 2 * r["hot"] + r["covered"]
    return sorted(rows.values(), key=lambda r: (-r["score"], r["file"]))


def _reason(r: dict) -> str:
    bits = []
    if r["changed"]:
        bits.append("changed since the last review")
    if r["hot"]:
        bits.append("on a hot path")
    if r["covered"]:
        bits.append("tests reach it")
    bits.append(f"{r['findings']} unverified patch{'es' if r['findings'] != 1 else ''}")
    return ", ".join(bits)


SYSTEM = (
    "You choose which source files of a Python repository are worth spending a test run on, because each file "
    "chosen means running the repository's test suite against that file's suggested patches. Prefer files that "
    "changed, sit on hot paths, are reached by tests (so the run can prove something) and carry high-impact "
    "patches; skip the rest. List them best first, each with a one-sentence reason. Use only paths from the table."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"file": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["file", "reason"],
            },
        }
    },
    "required": ["files"],
}


def plan(findings: list[Finding], changed: list[str], root: str | Path, limit: int = 12, use_ai: bool = True) -> dict:
    """Suggested files to verify. Asks the configured model when there is one; ranks them itself otherwise.
    Every row says why it was picked, and the user decides what runs."""
    rows = candidates(findings, set(changed))
    by_file = {r["file"]: r for r in rows}
    cfg = LLMConfig.load(root)
    st = llm_status(cfg)
    picked: list[tuple[str, str]] = []
    source, note = "ranking", st["reason"] or ""
    if use_ai and rows and st["available"]:
        table = "\n".join(
            f"{r['file']} | patches {r['findings']} | impact {r['impact']} | hot {r['hot']} "
            f"| tests reach {r['covered']} | changed {'yes' if r['changed'] else 'no'} | {', '.join(r['intents'])}"
            for r in rows[:150]
        )
        header = "file | unverified patches | impact | hot-path functions | tests reach | changed | kinds"
        ask = f"Choose up to {limit} files.\n\n{header}\n{table}"
        data = complete_json(
            cfg, SYSTEM, [{"role": "user", "content": ask}], SCHEMA, max_tokens=2000, purpose="choose files to verify"
        )
        for item in [d for d in (data or {}).get("files", []) if isinstance(d, dict)]:
            f = item.get("file")
            if f in by_file and f not in {p for p, _ in picked}:
                picked.append((f, str(item.get("reason") or "")[:200]))
        if picked:
            source, note = "ai", cfg.model
        else:
            note = "the model's answer could not be used; ranked by impact instead"
    if not picked:
        picked = [(r["file"], "") for r in rows[:limit]]
    chosen = {p for p, _ in picked}
    files = [
        {**_public(by_file[p]), "reason": why or _reason(by_file[p]), "selected": True} for p, why in picked[:limit]
    ]
    files += [{**_public(r), "reason": _reason(r), "selected": False} for r in rows if r["file"] not in chosen][:200]
    return {"source": source, "note": note, "files": files, "total": len(rows), "changed": len(changed)}


def _public(r: dict) -> dict:
    return {k: r[k] for k in ("file", "findings", "impact", "hot", "covered", "intents", "changed")}
