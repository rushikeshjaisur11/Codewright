"""Code chat for Map mode: answers that cite files and lines.

Retrieval is BM25 over functions (name, docstring, identifiers) and module
docstrings. With a model, the top passages go into the prompt and the answer
must cite them as [n]. Without one, the answer is built from the passages and
the call graph, so every claim is still a citation.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from .graph import CodeGraph
from .intents.detectors import is_test_file
from .llm import LLMConfig, complete

_WORD = re.compile(r"[A-Za-z][a-z0-9]*|[A-Z]+(?![a-z])|\d+")
STOP = {
    "the",
    "a",
    "an",
    "is",
    "are",
    "how",
    "does",
    "do",
    "what",
    "where",
    "which",
    "this",
    "that",
    "in",
    "of",
    "to",
    "and",
    "or",
    "for",
    "it",
    "on",
    "with",
    "repo",
    "code",
    "self",
    "return",
    "def",
    "import",
    "from",
    "none",
    "true",
    "false",
    "if",
    "else",
    "as",
    "by",
    "be",
    "i",
    "my",
    "we",
    "can",
    "when",
}


def tokens(s: str) -> list[str]:
    out = []
    for part in re.split(r"[^A-Za-z0-9]+", s):
        out.extend(w.lower() for w in _WORD.findall(part))
    return [t for t in out if t not in STOP and len(t) > 1]


@dataclass
class Passage:
    id: str
    file: str
    line: int
    end: int
    title: str
    text: str


class CodeIndex:
    def __init__(self, graph: CodeGraph):
        self.g = graph
        self.passages: list[Passage] = []
        for path, pf in graph.files.items():
            if is_test_file(path):
                continue
            lines = pf.lines
            for fn in pf.module.functions:
                body = "".join(lines[fn.start_line - 1 : fn.end_line])
                self.passages.append(
                    Passage(f"{path}::{fn.qualname}", path, fn.def_line, fn.end_line, fn.qualname, body)
                )
            head = "".join(lines[:15])
            self.passages.append(Passage(f"{path}::<module>", path, 1, min(len(lines), 15), path, head))
        self.docs = [Counter(tokens(p.title) * 3 + tokens(p.file) * 2 + tokens(p.text)) for p in self.passages]
        self.lengths = [sum(d.values()) for d in self.docs]
        self.avg = (sum(self.lengths) / len(self.lengths)) if self.lengths else 1
        df: Counter = Counter()
        for d in self.docs:
            df.update(d.keys())
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def search(self, query: str, k: int = 6) -> list[tuple[Passage, float]]:
        q = tokens(query)
        scored = []
        for p, d, ln in zip(self.passages, self.docs, self.lengths, strict=True):
            s = 0.0
            for t in q:
                if t in d:
                    tf = d[t]
                    s += self.idf.get(t, 0) * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * ln / self.avg))
            if s > 0:
                scored.append((p, s))
        scored.sort(key=lambda x: -x[1])
        return scored[:k]


SYSTEM = (
    "You answer questions about a codebase. Use only the numbered passages given. "
    "Cite every claim with the passage number in square brackets, like [2]. "
    "If the passages don't contain the answer, say what is missing. Be concise: short paragraphs or bullets."
)


def answer(index: CodeIndex, question: str, history: list[dict] | None = None, cfg: LLMConfig | None = None) -> dict:
    hits = index.search(question)
    citations = [
        {"n": i + 1, "id": p.id, "file": p.file, "line": p.line, "end": p.end, "title": p.title, "score": round(s, 2)}
        for i, (p, s) in enumerate(hits)
    ]
    if not hits and cfg is None and (topical := _topical(index, question)):
        return {"answer": topical[0], "citations": topical[1], "mode": "extractive"}
    if not hits:
        return {
            "answer": "I couldn't find code matching that question. Try naming a function, module, route or library.",
            "citations": [],
            "mode": "extractive",
        }
    if cfg is not None:
        context = "\n\n".join(
            f"[{i + 1}] {p.file}:{p.line}-{p.end} ({p.title})\n{p.text[:1800]}" for i, (p, _) in enumerate(hits)
        )
        msgs = [*list(history or [])[-6:], {"role": "user", "content": f"Passages:\n{context}\n\nQuestion: {question}"}]
        text = complete(cfg, SYSTEM, msgs)
        if text:
            return {"answer": text, "citations": citations, "mode": "llm"}
    topical = _topical(index, question)
    if topical:
        text, cites = topical
        return {"answer": text, "citations": cites, "mode": "extractive"}
    return {"answer": _extractive(index, hits), "citations": citations, "mode": "extractive"}


TOPICS = [
    (("flow", "request", "route", "endpoint", "entry", "handled", "handler"), "flow"),
    (("database", "db", "sql", "query", "queries", "postgres", "table"), "database"),
    (("http", "external", "api", "apis", "network"), "http"),
    (("model", "llm", "agent", "agents", "gemini", "prompt", "adk", "openai", "claude"), "llm"),
    (("cache", "redis"), "cache"),
    (("queue", "job", "jobs", "worker", "background"), "queue"),
]


def _topical(index: CodeIndex, question: str) -> tuple[str, list[dict]] | None:
    names = {p.title.rsplit(".", 1)[-1].lower() for p in index.passages}
    if any(
        w.lower() in names
        for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", question)
        if "_" in w or (len(w) > 3 and w.lower() not in STOP and w.lower() in names)
    ):
        return None  # the question names a function: answer about that symbol instead
    words = set(re.findall(r"[a-z]+", question.lower()))
    topic = next((t for keys, t in TOPICS if words & set(keys)), None)
    if topic is None:
        return None
    from .archmap import ArchMap

    g = index.g
    amap = ArchMap(g)
    cites: list[dict] = []

    def cite(path: str, fn) -> int:
        key = f"{path}::{fn.qualname}"
        for c in cites:
            if c["id"] == key:
                return c["n"]
        cites.append(
            {
                "n": len(cites) + 1,
                "id": key,
                "file": path,
                "line": fn.def_line,
                "end": fn.end_line,
                "title": fn.qualname,
                "score": 1.0,
            }
        )
        return len(cites)

    def fn_of(key: str):
        path, _, qual = key.partition("::")
        pf = g.files.get(path)
        return path, next((f for f in pf.module.functions if f.qualname == qual), None) if pf else None

    lines: list[str] = []
    if topic == "flow":
        eps = amap.entry_points()
        if not eps:
            return None
        lines.append(f"Requests enter through {len(eps)} entry point(s). Each chain below follows the call graph:")
        lines.append("")
        for ep in eps[:6]:
            path, fn = fn_of(ep["id"])
            if fn is None:
                continue
            chain, seen, cur = [f"`{fn.qualname}` [{cite(path, fn)}]"], {ep["id"]}, ep["id"]
            for _ in range(4):
                nxt = sorted(c for c in g.edges.get(cur, ()) if c not in seen and not is_test_file(c.split("::")[0]))
                if not nxt:
                    break
                cur = nxt[0]
                seen.add(cur)
                p2, f2 = fn_of(cur)
                if f2 is not None:
                    chain.append(f"`{f2.qualname}` [{cite(p2, f2)}]")
            view = amap.flow(ep["id"])
            ext = sorted({n.label for n in view.nodes if n.id.startswith("x:")})
            lines.append(f"- `{ep['trigger']}` → " + " → ".join(chain) + (f"; reaches {', '.join(ext)}" if ext else ""))
        return "\n".join(lines), cites

    kinds = {"database": ("database",), "http": ("http",), "llm": ("llm",), "cache": ("cache",), "queue": ("queue",)}[
        topic
    ]
    found = []
    for path, pf in g.files.items():
        if is_test_file(path):
            continue
        for fn in pf.module.functions:
            from .graph import resolve

            systems = set()
            for call in fn.calls:
                ext = amap._external_for(resolve(pf.module, call))
                if ext and ext[1] in kinds:
                    systems.add(ext[0])
            if systems:
                found.append((path, fn, sorted(systems)))
        # Module-level code, e.g. `root_agent = SequentialAgent(...)`.
        from .intents.detectors import calls_in

        systems, first = set(), None
        for node, name in calls_in(pf.root, pf):
            ext = amap._external_for(name)
            if ext and ext[1] in kinds:
                systems.add(ext[0])
                first = first or node
        if systems:
            ln = first.start_point[0] + 1
            from .models import Function

            found.append((path, Function("<module>", "<module>", path, ln, ln, ln), sorted(systems)))
    if not found:
        lines.append(f"No code calls a {topic} library directly. Imports alone are not counted.")
        return "\n".join(lines), cites
    label = {
        "database": "the database",
        "http": "external HTTP APIs",
        "llm": "models or agents",
        "cache": "a cache",
        "queue": "a queue",
    }[topic]
    lines.append(f"{len(found)} function(s) talk to {label} directly:")
    lines.append("")
    for path, fn, systems in found[:8]:
        name = "module-level code" if fn.qualname == "<module>" else f"`{fn.qualname}`"
        lines.append(f"- {name} in `{path}:{fn.def_line}` uses {', '.join(systems)} [{cite(path, fn)}]")
    return "\n".join(lines), cites


def _extractive(index: CodeIndex, hits: list[tuple[Passage, float]]) -> str:
    g = index.g
    callers: dict[str, list[str]] = {}
    for caller, callees in g.edges.items():
        for c in callees:
            callers.setdefault(c, []).append(caller)
    lines = ["Most relevant code (no model configured, so this is built from the code graph):", ""]
    for i, (p, _) in enumerate(hits[:4], 1):
        if p.title == p.file:
            lines.append(f"- Module `{p.file}` [{i}]")
            continue
        fn = next((f for f in g.files[p.file].module.functions if f.qualname == p.title), None)
        desc = f"- `{p.title}` in `{p.file}:{p.line}` [{i}]"
        if fn is not None:
            if fn.decorators:
                desc += f", exposed as `@{fn.decorators[0]}`"
            internal = [
                c
                for c in fn.calls
                if any(c.rsplit(".", 1)[-1] == x.rsplit("::", 1)[-1].rsplit(".", 1)[-1] for x in g.edges.get(p.id, ()))
            ]
            if internal:
                desc += f"; calls {', '.join(f'`{c}`' for c in internal[:4])}"
            ups = callers.get(p.id, [])
            if ups:
                names = ", ".join(f"`{u.split('::')[1]}`" for u in ups[:3])
                desc += f"; called from {names}"
            if p.id in g.entry_points:
                desc += "; this is an entry point"
        lines.append(desc)
    return "\n".join(lines)
