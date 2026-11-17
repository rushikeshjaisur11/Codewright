"""What the map inspectors show when a box is clicked: everything known about one node.

One shape for both graphs (the architecture Map and the AI & agents graph), so the web UI
renders them with one component:

    {id, label, kind, summary, facts: [{label, value}], sections: [{title, items: [...]}],
     findings: [...], code: {file, start, end, text} | None, ask: "a question for Code chat"}

An item is {label, sub?, file?, line?, node?}: `node` is the id of another box in the same
graph (clicking it selects that box), else `file`/`line` open the code.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from .aimap import AiMap
from .archmap import ArchMap, module_name
from .graph import CodeGraph, ParsedFile, is_test_file
from .models import CATEGORIES, Finding

MAX_ITEMS = 40
PREVIEW_LINES = 40


# --------------------------------------------------------------------------- helpers


def _finding(f: Finding) -> dict:
    return {
        "id": f.id,
        "title": f.title,
        "category": f.category,
        "category_label": CATEGORIES.get(f.category, f.category),
        "intent": f.intent,
        "file": f.file,
        "line": f.line,
        "evidence": f.evidence,
        "proof": f.proof.value,
    }


def _findings(findings: list[Finding], keep) -> list[dict]:
    order = list(CATEGORIES)
    hits = [f for f in findings if keep(f)]
    hits.sort(key=lambda f: (order.index(f.category) if f.category in order else 9, -f.score))
    return [_finding(f) for f in hits[:MAX_ITEMS]]


def _preview(g: CodeGraph, path: str, start: int, end: int | None = None) -> dict | None:
    pf = g.files.get(path)
    if pf is None:
        return None
    lines = pf.lines
    start = max(1, start)
    end = min(len(lines), end or start + PREVIEW_LINES - 1, start + PREVIEW_LINES - 1)
    if start > len(lines):
        return None
    return {
        "file": path,
        "start": start,
        "end": end,
        "text": "".join(lines[start - 1 : end]).rstrip("\n"),
        "truncated": (end - start + 1) >= PREVIEW_LINES,
    }


def _docstring(pf: ParsedFile, qualname: str) -> str:
    node = pf.nodes.get(qualname)
    body = node.child_by_field_name("body") if node is not None else None
    if body is None or not body.named_children:
        return ""
    first = body.named_children[0]
    if first.type == "expression_statement" and first.named_children and first.named_children[0].type == "string":
        raw = first.named_children[0].text.decode(errors="replace")
        return raw.strip("\"' \n").split("\n\n")[0].strip()[:600]
    return ""


def _signature(pf: ParsedFile, qualname: str) -> str:
    node = pf.nodes.get(qualname)
    if node is None:
        return ""
    head = pf.source[node.start_byte : node.end_byte].decode(errors="replace")
    sig = head.split(":\n", 1)[0] if ":\n" in head else head.splitlines()[0]
    lines = [ln for ln in sig.splitlines() if not ln.lstrip().startswith("@")]
    return " ".join(x.strip() for x in lines)[:300]


def _callers(g: CodeGraph) -> dict[str, set[str]]:
    rev: dict[str, set[str]] = defaultdict(set)
    for caller, callees in g.edges.items():
        for c in callees:
            rev[c].add(caller)
    return rev


def _fn(g: CodeGraph, key: str):
    path, _, qual = key.partition("::")
    pf = g.files.get(path)
    if pf is None:
        return None, None
    return pf, next((f for f in pf.module.functions if f.qualname == qual), None)


def _import_line(pf: ParsedFile, prefix: str) -> int:
    for i, ln in enumerate(pf.lines, 1):
        s = ln.lstrip()
        if (s.startswith("import ") or s.startswith("from ")) and prefix in s:
            return i
    return 1


def _n(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _fn_item(key: str, node_ids: set[str] | None = None, sub: str = "") -> dict:
    path, _, qual = key.partition("::")
    item = {"label": qual, "sub": sub or path, "file": path}
    if node_ids is not None and key in node_ids:
        item["node"] = key
    return item


# --------------------------------------------------------------------------- architecture Map


def map_node(m: ArchMap, level: str, target: str, node_id: str) -> dict:
    """Details of one box on the Map (system, module or flow level)."""
    g = m.g
    try:
        view = m.view(level, target)
    except ValueError:
        view = None
    ids = {n.id for n in view.nodes} if view else set()
    node = next((n for n in view.nodes if n.id == node_id), None) if view else None
    out_edges = [e for e in (view.edges if view else []) if e.source == node_id]
    in_edges = [e for e in (view.edges if view else []) if e.target == node_id]
    rev = _callers(g)

    def links(edges, other) -> list[dict]:
        by = {n.id: n for n in view.nodes} if view else {}
        return [
            {
                "label": by[getattr(e, other)].label if getattr(e, other) in by else getattr(e, other),
                "sub": e.label + (f" · {e.weight}×" if e.weight > 1 else ""),
                "node": getattr(e, other),
            }
            for e in edges
        ]

    if node_id.startswith("c:"):  # a component on the system view
        comp = node_id[2:]
        paths = sorted(p for p in m.shown if m._comp(p) == comp)
        fns = [(p, fn) for p in paths for fn in g.files[p].module.functions]
        eps = [e for e in m.entry_points() if e["file"] in paths]
        hot = sorted(((m._fan_in.get(f"{p}::{fn.qualname}", 0), p, fn) for p, fn in fns), key=lambda x: -x[0])[:6]
        untested = [(p, fn) for p, fn in fns if not fn.name.startswith("_") and fn.name not in m._tested]
        cats = Counter(f.category for f in m.findings if f.file in paths)
        return {
            "id": node_id,
            "label": comp,
            "kind": "component",
            "summary": f"{_n(len(paths), 'module')} and {_n(len(fns), 'function')}"
            + (f", with {_n(len(eps), 'entry point')}" if eps else "")
            + ".",
            "facts": [
                {"label": "Modules", "value": str(len(paths))},
                {"label": "Functions", "value": str(len(fns))},
                {"label": "Entry points", "value": str(len(eps))},
                {"label": "Untested public functions", "value": str(len(untested))},
                {"label": "Lines of code", "value": str(sum(len(g.files[p].lines) for p in paths))},
                *[{"label": CATEGORIES[c], "value": str(n)} for c, n in cats.items()],
            ],
            "sections": [
                {"title": "Depends on", "items": links(out_edges, "target")},
                {"title": "Used by", "items": links(in_edges, "source")},
                {
                    "title": "Entry points",
                    "items": [
                        {
                            "label": e["trigger"],
                            "sub": f"{e['name']} · {e['file']}:{e['line']}",
                            "file": e["file"],
                            "line": e["line"],
                            "flow": e["id"],
                        }
                        for e in eps[:MAX_ITEMS]
                    ],
                },
                {
                    "title": "Most called functions",
                    "items": [
                        {
                            "label": fn.qualname,
                            "sub": f"called from {n} place{'s' if n != 1 else ''} · {p}",
                            "file": p,
                            "line": fn.def_line,
                        }
                        for n, p, fn in hot
                        if n
                    ],
                },
                {
                    "title": "Modules",
                    "items": [
                        {
                            "label": module_name(p).rsplit(".", 1)[-1] or p,
                            "sub": f"{p} · {len(g.files[p].module.functions)} functions",
                            "file": p,
                            "line": 1,
                        }
                        for p in paths[:MAX_ITEMS]
                    ],
                },
            ],
            "findings": _findings(m.findings, lambda f: f.file in paths),
            "code": None,
            "drill": {"level": "module", "target": comp},
            "ask": f"What does the {comp} component do, and how is it used?",
        }

    if node_id.startswith("m:"):  # a module on the module view
        mod = node_id[2:]
        path = m.modules.get(mod)
        if path is None:
            return _unknown(node_id)
        page = m.module_page(path)
        pf = g.files[path]
        eps = [e for e in m.entry_points() if e["file"] == path]
        return {
            "id": node_id,
            "label": mod,
            "kind": "module",
            "summary": page["doc"]
            or (f"{_n(len(page['functions']), 'function')} in {path}." if pf.lines else f"An empty `{path}`."),
            "facts": [
                {"label": "File", "value": path, "mono": True},
                {"label": "Functions", "value": str(len(page["functions"]))},
                {"label": "Lines", "value": str(len(pf.lines))},
                {"label": "Imported by", "value": str(len(page["imported_by"]))},
                {"label": "Untested functions", "value": str(sum(1 for f in page["functions"] if not f["tested"]))},
            ],
            "sections": [
                {
                    "title": "Functions",
                    "items": [
                        {
                            "label": f["name"],
                            "file": path,
                            "line": f["line"],
                            "sub": " · ".join(
                                x
                                for x in (
                                    "async" if f["async"] else "",
                                    f"called from {f['fan_in']}" if f["fan_in"] else "",
                                    "tested" if f["tested"] else "untested",
                                    f"{f['findings']} finding{'s' if f['findings'] != 1 else ''}"
                                    if f["findings"]
                                    else "",
                                )
                                if x
                            ),
                        }
                        for f in page["functions"][:MAX_ITEMS]
                    ],
                },
                {
                    "title": "Entry points here",
                    "items": [
                        {"label": e["trigger"], "sub": e["name"], "file": path, "line": e["line"], "flow": e["id"]}
                        for e in eps
                    ],
                },
                {
                    "title": "Imports",
                    "items": [
                        {"label": x, "file": m.modules[x], "line": 1, **({"node": f"m:{x}"} if f"m:{x}" in ids else {})}
                        for x in page["imports"]
                    ],
                },
                {
                    "title": "Imported by",
                    "items": [
                        {"label": x, "file": m.modules[x], "line": 1, **({"node": f"m:{x}"} if f"m:{x}" in ids else {})}
                        for x in page["imported_by"]
                    ],
                },
                {"title": "Libraries", "items": [{"label": x} for x in page["external"][:MAX_ITEMS]]},
            ],
            "findings": _findings(m.findings, lambda f: f.file == path),
            "code": _preview(g, path, 1),
            "drill": {"level": "flow", "target": eps[0]["id"]} if eps else None,
            "ask": f"What does {mod} do?",
        }

    if node_id.startswith("x:"):  # an external system
        label = node_id[2:]
        users: list[tuple[str, str]] = []  # (file, import target)
        for path in m.modules.values():
            for t in m._module_imports(path)[1]:
                ext = m._external_for(t)
                if ext and ext[0] == label:
                    users.append((path, t))
        kind = node.kind if node else "external"
        calls: list[dict] = []
        for path in sorted({u[0] for u in users}):
            pf = g.files[path]
            aliases = {a for a, v in pf.module.imports.items() if m._external_for(v) and m._external_for(v)[0] == label}
            for fn in pf.module.functions:
                used = [c for c in fn.calls if c.split(".")[0] in aliases]
                if used:
                    calls.append(
                        {
                            "label": fn.qualname,
                            "sub": f"{', '.join(sorted(set(used))[:3])} · {path}",
                            "file": path,
                            "line": fn.def_line,
                            **({"node": f"{path}::{fn.qualname}"} if f"{path}::{fn.qualname}" in ids else {}),
                        }
                    )
        files = sorted({u[0] for u in users})
        grouped: dict[str, list[str]] = defaultdict(list)
        for p, t in users:
            grouped[p].append(t)
        by_file = sorted((p, sorted(set(ts))) for p, ts in grouped.items())
        roots = sorted({".".join(t.split(".")[:2]) if t.startswith("google.") else t.split(".")[0] for _, t in users})
        return {
            "id": node_id,
            "label": label,
            "kind": kind,
            "summary": f"Outside this repo. Reached from {len(files)} file{'s' if len(files) != 1 else ''}"
            f" through {', '.join(roots) or 'imports'}.",
            "facts": [
                {"label": "Kind", "value": kind},
                {"label": "Files that use it", "value": str(len(files))},
                {"label": "Functions that call it", "value": str(len(calls))},
                {"label": "Imported as", "value": ", ".join(sorted({u[1] for u in users}))[:200], "mono": True},
            ],
            "sections": [
                {"title": "Connected boxes", "items": links(in_edges, "source") + links(out_edges, "target")},
                {"title": "Functions that call it", "items": calls[:MAX_ITEMS]},
                {
                    "title": "Imported in",
                    "items": [
                        {
                            "label": p,
                            "sub": ", ".join(ts),
                            "file": p,
                            "line": _import_line(g.files[p], ts[0].split(".")[0]),
                        }
                        for p, ts in by_file[:MAX_ITEMS]
                    ],
                },
            ],
            "findings": _findings(
                m.findings, lambda f: f.file in files and f.category in ("performance", "reliability", "agents")
            ),
            "code": None,
            "ask": f"Where and how does this repo use {label}?",
        }

    if node_id.endswith("::<module>"):  # a file that starts the app
        path = node_id.partition("::")[0]
        e = g.app_entries.get(path)
        if e is None:
            return _unknown(node_id)
        runs = [c for c in e.calls if not is_test_file(c.partition("::")[0])]
        return {
            "id": node_id,
            "label": e.trigger,
            "kind": "entry",
            "summary": f"{path} starts the app ({e.framework}): it " + "; ".join(e.reasons) + ".",
            "facts": [
                {"label": "Started with", "value": e.trigger, "mono": True},
                {"label": "App", "value": e.framework},
                {"label": "File", "value": f"{path}:{e.line}", "mono": True},
                {"label": "Runs", "value": _n(len(runs), "function")},
            ],
            "sections": [
                {"title": "Why it is an entry point", "items": [{"label": r} for r in e.reasons]},
                {"title": "Runs", "items": [_fn_item(c, ids) for c in runs[:MAX_ITEMS]]},
            ],
            "findings": _findings(m.findings, lambda f: f.file == path),
            "code": _preview(g, path, max(1, e.line - 3), e.line + PREVIEW_LINES - 4),
            "ask": f"What happens when the app starts from {path}?",
        }

    if "::" in node_id:  # a function on the flow view
        pf, fn = _fn(g, node_id)
        if fn is None:
            return _unknown(node_id)
        callees = sorted(c for c in g.edges.get(node_id, ()) if not is_test_file(c.partition("::")[0]))
        callers = sorted(rev.get(node_id, ()))
        ext = [n for n in (links(out_edges, "target")) if n["node"].startswith("x:")]
        doc = _docstring(pf, fn.qualname)
        return {
            "id": node_id,
            "label": fn.qualname,
            "kind": "entry" if node_id in g.entry_points else "function",
            "summary": doc or _signature(pf, fn.qualname),
            "facts": [
                {"label": "Where", "value": f"{pf.path}:{fn.def_line}", "mono": True},
                {"label": "Signature", "value": _signature(pf, fn.qualname), "mono": True},
                {"label": "Async", "value": "yes" if fn.is_async else "no"},
                {"label": "Called from", "value": f"{len(callers)} place{'s' if len(callers) != 1 else ''}"},
                {
                    "label": "Tested",
                    "value": "yes" if fn.name in m._tested or fn.name.startswith("_") else "no test calls it",
                },
                *([{"label": "Decorators", "value": ", ".join(fn.decorators), "mono": True}] if fn.decorators else []),
            ],
            "sections": [
                {"title": "Calls", "items": [_fn_item(c, ids) for c in callees[:MAX_ITEMS]]},
                {"title": "Called by", "items": [_fn_item(c, ids) for c in callers[:MAX_ITEMS]]},
                {"title": "Systems it uses", "items": ext},
            ],
            "findings": _findings(m.findings, lambda f: f.file == pf.path and f.function == fn.qualname),
            "code": _preview(g, pf.path, fn.start_line, fn.end_line),
            "ask": f"What does {fn.qualname} in {pf.path} do, step by step?",
        }
    return _unknown(node_id)


def _unknown(node_id: str) -> dict:
    return {
        "id": node_id,
        "label": node_id,
        "kind": "unknown",
        "summary": "Nothing more is known about this box.",
        "facts": [],
        "sections": [],
        "findings": [],
        "code": None,
        "ask": "",
    }


# --------------------------------------------------------------------------- AI & agents graph


def ai_node(ai: AiMap, node_id: str) -> dict:
    """Details of one box on the agent architecture graph."""
    g = ai.g
    arch = ai.architecture()
    nodes = {n["id"]: n for n in arch["nodes"]}
    n = nodes.get(node_id)
    if n is None:
        return _unknown(node_id)
    ins = [e for e in arch["edges"] if e["target"] == node_id]
    outs = [e for e in arch["edges"] if e["source"] == node_id]

    def rel(edges, other, label=None) -> list[dict]:
        return [
            {"label": nodes[e[other]]["label"], "sub": e["label"], "node": e[other]}
            for e in edges
            if (label is None or e["label"] == label) and e[other] in nodes
        ]

    findings = ai.findings
    if node_id in ai.agents:
        a = ai.agents[node_id]
        parents = rel(ins, "source", "delegates")
        return {
            "id": node_id,
            "label": a.name,
            "kind": n["kind"],
            "summary": (f"{a.framework} agent" if a.kind == "llm" else f"{a.framework} {n['detail'].split(' · ')[-1]}")
            + f", defined at {a.file}:{a.line}"
            + (f" as `{a.var}`" if a.var and a.var != a.name else "")
            + ".",
            "facts": [
                {"label": "Framework", "value": a.framework},
                {
                    "label": "Model",
                    "value": a.model or ("inherited from its parent" if a.kind == "llm" and parents else "not set"),
                    "mono": bool(a.model),
                },
                {"label": "Tools", "value": str(len(a.tools))},
                {"label": "Sub-agents", "value": str(len(a.children) + len(a.child_names))},
                {
                    "label": "Run by",
                    "value": ", ".join(x["label"] for x in rel(ins, "source", "runs")) or "nothing found in the repo",
                },
                *[{"label": k, "value": v, "mono": True} for k, v in list(a.options.items())[:8]],
            ],
            "text": {"title": "Instruction", "body": a.instruction_full} if a.instruction_full else None,
            "sections": [
                {"title": "Delegates to", "items": rel(outs, "target", "delegates")},
                {
                    "title": "Tools",
                    "items": [
                        {
                            "label": t["label"],
                            "sub": "agent used as a tool"
                            if t.get("agent")
                            else ("built-in or library tool" if t.get("builtin") else f"{t['file']}:{t['line']}"),
                            "node": t["agent"]
                            if t.get("agent")
                            else (f"tool:{t['file']}:{t['label']}" if not t.get("builtin") else f"tool:{t['label']}"),
                            "file": t["file"],
                            "line": t["line"],
                        }
                        for t in a.tools
                    ],
                },
                {"title": "Run by", "items": rel(ins, "source", "runs")},
                {"title": "Part of", "items": parents + rel(ins, "source", "uses as tool")},
                {
                    "title": "Steps",
                    "items": [
                        {
                            "label": s["name"],
                            "sub": f"{s['file']}:{s['line']}",
                            "node": f"{a.id}#{s['name']}",
                            "file": s["file"],
                            "line": s["line"],
                        }
                        for s in a.steps
                    ],
                },
            ],
            "findings": _findings(findings, lambda f: f.file == a.file),
            "code": _preview(g, a.file, a.line, a.end or a.line + 20),
            "ask": f"How does the {a.name} agent work: what does it delegate and which tools does it use?",
        }

    kind = n["kind"]
    file, line = (n["files"][0]["file"], n["files"][0]["line"]) if n["files"] else ("", 1)
    if kind == "tool":
        used_by = rel(ins, "source")
        pf = g.files.get(file)
        fn = (
            next((f for f in pf.module.functions if f.qualname == n["label"]), None)
            if pf and not n["detail"].startswith("built-in")
            else None
        )
        if fn is not None:
            doc = _docstring(pf, fn.qualname)
            return {
                "id": node_id,
                "label": n["label"],
                "kind": "tool",
                "summary": doc
                or "A repo function given to an agent as a tool. The model sees its name, parameters and docstring.",
                "facts": [
                    {"label": "Where", "value": f"{file}:{fn.def_line}", "mono": True},
                    {"label": "Signature", "value": _signature(pf, fn.qualname), "mono": True},
                    {
                        "label": "Docstring",
                        "value": "yes" if doc else "missing: the model gets no description of this tool",
                    },
                    {"label": "Async", "value": "yes" if fn.is_async else "no"},
                ],
                "sections": [{"title": "Used by", "items": used_by}],
                "findings": _findings(findings, lambda f: f.file == file and f.function == fn.qualname),
                "code": _preview(g, file, fn.start_line, fn.end_line),
                "ask": f"What does the {n['label']} tool do and which agents call it?",
            }
        return {
            "id": node_id,
            "label": n["label"],
            "kind": "tool",
            "summary": "A tool from a library or framework (not a function in this repo).",
            "facts": [{"label": "Referenced in", "value": f"{file}:{line}", "mono": True}],
            "sections": [{"title": "Used by", "items": used_by}],
            "findings": [],
            "code": _preview(g, file, max(1, line - 2), line + 6) if file else None,
            "ask": f"What does {n['label']} do in this repo's agents?",
        }

    if kind == "llm":
        users = rel(ins, "source")
        detail = n.get("detail") or ""
        provider = f" from {detail}" if detail and detail != "model" else ""
        calls = [
            c
            for c in ai.calls
            if f"model:{c.model or c.provider}" == node_id or (c.model and f"model:{c.model}" == node_id)
        ]
        return {
            "id": node_id,
            "label": n["label"],
            "kind": "llm",
            "summary": f"A model{provider}, used by {len(users)} box{'es' if len(users) != 1 else ''} in this graph.",
            "facts": [
                {"label": "Provider", "value": n.get("detail") or "unknown"},
                {"label": "Agents using it", "value": str(sum(1 for u in users if u["node"] in ai.agents))},
                {"label": "Direct calls", "value": str(len(calls))},
            ],
            "sections": [
                {"title": "Used by", "items": users},
                {
                    "title": "Call sites",
                    "items": [
                        {
                            "label": f"{c.function}",
                            "sub": f"{c.api} · {c.file}:{c.line}",
                            "file": c.file,
                            "line": c.line,
                        }
                        for c in calls
                    ],
                },
            ],
            "findings": [],
            "code": None,
            "ask": f"Where is the model {n['label']} used, and with what settings?",
        }

    # functions that call a model, entry points that run agents, LangGraph steps
    key = node_id.split(":", 1)[1] if node_id.startswith(("fn:", "run:")) else ""
    pf, fn = _fn(g, key) if key else (g.files.get(file), None)
    if fn is None and pf is not None:
        fn = next((f for f in pf.module.functions if f.def_line == line or f.qualname == n["label"]), None)
    calls = [c for c in ai.calls if fn is not None and c.file == pf.path and c.function == fn.qualname]
    rev = _callers(g)
    callers = sorted(rev.get(f"{pf.path}::{fn.qualname}", ())) if fn is not None else []
    return {
        "id": node_id,
        "label": n["label"],
        "kind": kind,
        "summary": (_docstring(pf, fn.qualname) if fn is not None else "") or n.get("detail", ""),
        "facts": [
            {"label": "Where", "value": f"{file}:{fn.def_line if fn else line}", "mono": True},
            *([{"label": "Signature", "value": _signature(pf, fn.qualname), "mono": True}] if fn is not None else []),
            *(
                [{"label": "Decorators", "value": ", ".join(fn.decorators), "mono": True}]
                if fn is not None and fn.decorators
                else []
            ),
        ],
        "sections": [
            {"title": "Runs", "items": rel(outs, "target", "runs")},
            {
                "title": "Model calls",
                "items": [
                    {
                        "label": c.api,
                        "sub": f"{c.provider} · {c.model or 'model set at runtime'}",
                        "file": c.file,
                        "line": c.line,
                    }
                    for c in calls
                ],
            },
            {
                "title": "Connections",
                "items": [x for x in rel(outs, "target") + rel(ins, "source") if x["sub"] not in ("runs",)],
            },
            {"title": "Called by", "items": [_fn_item(c) for c in callers[:MAX_ITEMS]]},
        ],
        "findings": _findings(findings, lambda f: fn is not None and f.file == pf.path and f.function == fn.qualname),
        "code": _preview(g, pf.path, fn.start_line, fn.end_line)
        if fn is not None
        else (_preview(g, file, line) if file else None),
        "ask": f"What does {n['label']} do?",
    }
