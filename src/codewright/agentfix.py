"""Corrections to the agent architecture that the code cannot show, kept in ``.codewright/agents.yaml``.

Static reading misses wiring that only exists at run time: agents built from a YAML file or a database, a
registry filled in a loop, an agent another service calls over HTTP. The team writes what is missing here
(by hand or from the AI page) and the architecture, the cost estimate and the review use it.

    agents:                       # agents the code builds at run time
      - name: billing
        model: gemini-2.5-flash
        file: helpdesk/specialists.py   # optional, for "open the code"
    links:                        # connections the code can't show; ends are box labels or ids
      - {from: "POST /ask", to: billing, label: runs}
      - {from: desk, to: "pricing-service", label: calls over HTTP}
    hide: [legacy_agent]          # boxes to leave out
"""

from __future__ import annotations

from pathlib import Path

import yaml

LABELS = (
    "runs",
    "runs in parallel",
    "delegates",
    "uses as tool",
    "tool",
    "model",
    "prompts",
    "calls over HTTP",
    "then",
)


def path(root: str | Path) -> Path:
    return Path(root) / ".codewright" / "agents.yaml"


def load(root: str | Path) -> dict:
    try:
        data = yaml.safe_load(path(root).read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {"agents": [], "links": [], "hide": []}
    data = data if isinstance(data, dict) else {}
    return {
        "agents": [a for a in data.get("agents") or [] if isinstance(a, dict) and a.get("name")],
        "links": [x for x in data.get("links") or [] if isinstance(x, dict) and x.get("from") and x.get("to")],
        "hide": [str(h) for h in data.get("hide") or []],
    }


def save(root: str | Path, data: dict) -> dict:
    clean = {
        "agents": [
            {
                k: v
                for k, v in a.items()
                if k in ("name", "model", "file", "line", "kind", "detail") and v not in (None, "")
            }
            for a in data.get("agents") or []
            if isinstance(a, dict) and str(a.get("name") or "").strip()
        ],
        "links": [
            {"from": str(x["from"]).strip(), "to": str(x["to"]).strip(), "label": str(x.get("label") or "runs").strip()}
            for x in data.get("links") or []
            if isinstance(x, dict) and str(x.get("from") or "").strip() and str(x.get("to") or "").strip()
        ],
        "hide": [str(h).strip() for h in data.get("hide") or [] if str(h).strip()],
    }
    from .feedback import ensure_dir

    ensure_dir(root)
    path(root).write_text(
        "# Corrections to the agent architecture (see Codewright's AI page). Commit to share with the team.\n"
        + yaml.safe_dump(clean, sort_keys=False, allow_unicode=True)
    )
    return load(root)


def apply(arch: dict, fixes: dict) -> dict:
    """Add the agents and links, drop the hidden boxes. Added items carry `manual: True`."""
    nodes = {n["id"]: n for n in arch["nodes"]}
    edges = list(arch["edges"])

    def find(ref: str) -> str | None:
        if ref in nodes:
            return ref
        exact = [i for i, n in nodes.items() if n["label"] == ref]
        if exact:
            return exact[0]
        loose = [i for i, n in nodes.items() if n["label"].lower() == ref.lower() or i.endswith(f"::{ref}")]
        return loose[0] if loose else None

    def add(name: str, kind: str = "agent", **extra) -> str:
        nid = f"manual:{name}"
        if nid not in nodes:
            nodes[nid] = {
                "id": nid,
                "label": name,
                "kind": kind,
                "detail": extra.get("detail") or "added in .codewright/agents.yaml",
                "findings": 0,
                "hotspot": 0,
                "untested": 0,
                "files": [{"file": extra["file"], "line": int(extra.get("line") or 1)}] if extra.get("file") else [],
                "manual": True,
            }
        return nid

    for a in fixes.get("agents", []):
        kind = {"workflow": "workflow", "service": "entry", "function": "function"}.get(str(a.get("kind")), "agent")
        extra = {k: v for k, v in a.items() if k in ("file", "line", "detail")}
        nid = find(str(a["name"])) or add(str(a["name"]), kind, **extra)
        if a.get("model"):
            mid = f"model:{a['model']}"
            if mid not in nodes:
                nodes[mid] = {
                    "id": mid,
                    "label": str(a["model"]),
                    "kind": "llm",
                    "detail": "model",
                    "findings": 0,
                    "hotspot": 0,
                    "untested": 0,
                    "files": [],
                }
            edges.append({"source": nid, "target": mid, "label": "model", "weight": 1, "manual": True})
    for link in fixes.get("links", []):
        src = find(str(link["from"])) or add(str(link["from"]))
        dst = find(str(link["to"])) or add(str(link["to"]))
        label = str(link.get("label") or "runs")
        if not any(e["source"] == src and e["target"] == dst and e["label"] == label for e in edges):
            edges.append({"source": src, "target": dst, "label": label, "weight": 1, "manual": True})
    hidden = {i for h in fixes.get("hide", []) if (i := find(h))}
    return {
        "nodes": [n for i, n in nodes.items() if i not in hidden],
        "edges": [e for e in edges if e["source"] not in hidden and e["target"] not in hidden],
    }
