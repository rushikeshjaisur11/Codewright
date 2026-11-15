"""Design mode: requirements -> candidate architectures, capacity math, decision records, starter repo.

The interview and the math run in code, so a proposal always names components,
numbers and trade-offs. Every number that rests on an assumption says so, and
the user can override it. A model (when configured) only drafts the data model
and API outline from the free-text description.

The chosen design is saved as the *intended architecture* in
``.codewright/design.json``; ``drift()`` compares it with the live Map.
"""

from __future__ import annotations

import io
import json
import math
import re
import textwrap
import time
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .llm import LLMConfig, complete

# --------------------------------------------------------------------------- interview


@dataclass
class Question:
    id: str
    text: str
    kind: str  # text | number | size | choice | multi
    options: list[str] = field(default_factory=list)
    default: object = None
    hint: str = ""
    when: str = ""  # only ask when this workload flag is set


QUESTIONS = [
    Question(
        "idea",
        "What are you building? Describe it in a sentence or two.",
        "text",
        hint="e.g. A multi-tenant NL2SQL service that turns analysts' questions into BigQuery SQL",
    ),
    Question("name", "What should the project be called?", "text", default="my-service"),
    Question(
        "users",
        "How many daily active users do you expect in 12 months?",
        "number",
        default=1000,
        hint="e.g. 5k, 200000",
    ),
    Question(
        "peak_rps",
        "Do you know the peak requests per second? Say 'skip' to derive it from users.",
        "number",
        default=None,
        hint="e.g. 120, or skip",
    ),
    Question(
        "data_gb", "How much data will you store after the first year?", "size", default=10, hint="e.g. 20 GB, 1.5 TB"
    ),
    Question(
        "latency_ms",
        "What p95 latency do user-facing requests need?",
        "number",
        default=300,
        hint="in milliseconds, e.g. 200",
    ),
    Question(
        "availability",
        "What availability target?",
        "choice",
        options=["99%", "99.9%", "99.95%", "99.99%"],
        default="99.9%",
    ),
    Question(
        "workload",
        "Which of these does it need?",
        "multi",
        options=[
            "background jobs",
            "LLM / agents",
            "vector search",
            "realtime updates",
            "file uploads",
            "scheduled jobs",
            "multi-tenant",
        ],
        default=[],
    ),
    Question("tenants", "How many tenants in 12 months?", "number", default=10, when="multi-tenant"),
    Question("cloud", "Which cloud?", "choice", options=["GCP", "AWS", "Azure", "Any / self-hosted"], default="GCP"),
    Question("team", "How many engineers will build and run it?", "number", default=3),
    Question(
        "compliance",
        "Any compliance constraints?",
        "multi",
        options=["none", "PII", "GDPR", "HIPAA", "PCI DSS", "data residency"],
        default=["none"],
    ),
]
QMAP = {q.id: q for q in QUESTIONS}

_NUM = re.compile(r"(\d+(?:[.,]\d+)?)\s*([kmb]|thousand|million|billion)?\b", re.I)
_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*(kb|mb|gb|tb|pb)?", re.I)


def parse_number(raw: str) -> float | None:
    if raw.strip().lower() in ("skip", "dont know", "don't know", "unknown", "?", "-", ""):
        return None
    m = _NUM.search(raw.replace(",", ""))
    if not m:
        raise ValueError("Please give a number, like 500 or 20k.")
    v = float(m.group(1))
    mult = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9}
    return v * mult.get((m.group(2) or "").lower(), 1)


def parse_size_gb(raw: str) -> float:
    m = _SIZE.search(raw.replace(",", ""))
    if not m:
        raise ValueError("Please give a size, like 20 GB or 1.5 TB.")
    v = float(m.group(1))
    return v * {"kb": 1e-6, "mb": 1e-3, "gb": 1, "tb": 1e3, "pb": 1e6}[(m.group(2) or "gb").lower()]


def parse_answer(q: Question, raw) -> object:
    if isinstance(raw, (int, float)) and q.kind in ("number", "size"):
        return float(raw)
    if isinstance(raw, list) and q.kind == "multi":
        return [o for o in q.options if o in raw]
    raw = str(raw).strip()
    if q.kind == "text":
        if not raw:
            if q.default is not None:
                return q.default
            raise ValueError("Please describe it in a few words.")
        return raw
    if q.kind == "number":
        return parse_number(raw)
    if q.kind == "size":
        return parse_size_gb(raw)
    low = raw.lower()
    if q.kind == "choice":
        for o in q.options:
            if low == o.lower() or low in o.lower() or o.lower().split()[0] in low:
                return o
        raise ValueError(f"Pick one of: {', '.join(q.options)}.")
    picked = [o for o in q.options if o.split()[0].lower() in low or o.lower() in low]
    if "none" in low or "no " in f"{low} ":
        return ["none"] if "none" in q.options else []
    return picked


@dataclass
class Session:
    id: str
    answers: dict = field(default_factory=dict)
    overrides: dict = field(default_factory=dict)  # assumption overrides
    created: float = field(default_factory=time.time)

    def workload(self) -> list[str]:
        return list(self.answers.get("workload") or [])

    def applicable(self) -> list[Question]:
        return [q for q in QUESTIONS if not q.when or q.when in self.workload()]

    def next_question(self) -> Question | None:
        return next((q for q in self.applicable() if q.id not in self.answers), None)

    def answer(self, qid: str, raw) -> None:
        q = QMAP[qid]
        self.answers[qid] = parse_answer(q, raw)

    def value(self, qid: str):
        v = self.answers.get(qid)
        return QMAP[qid].default if v is None else v

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- capacity math

ASSUMPTIONS = {
    "requests_per_user_per_day": (50, "API requests each daily active user makes per day"),
    "peak_factor": (4.0, "peak hour traffic as a multiple of the daily average"),
    "write_ratio": (0.2, "share of requests that write to the database"),
    "rps_per_instance": (
        150,
        "requests/s one async Python instance (2 vCPU) serves for I/O-bound work; load-test to confirm",
    ),
    "db_pool_per_instance": (10, "database connections each app instance keeps open"),
    "data_growth_per_year": (1.0, "extra data each year as a multiple of year-one data (1.0 = linear growth)"),
    "llm_calls_per_user_per_day": (5, "model calls per daily active user per day"),
    "tokens_per_llm_call": (3000, "input + output tokens per model call"),
    "usd_per_million_tokens": (
        0.0,
        "your blended model price per 1M tokens; 0 = not set (check your provider's pricing)",
    ),
    "jobs_per_request": (0.1, "background jobs created per API request"),
}


def assumptions(s: Session) -> dict[str, float]:
    return {k: float(s.overrides.get(k, v)) for k, (v, _) in ASSUMPTIONS.items()}


def capacity(s: Session) -> dict:
    a = assumptions(s)
    users = float(s.value("users"))
    avg_rps = users * a["requests_per_user_per_day"] / 86400
    derived_peak = avg_rps * a["peak_factor"]
    peak = float(s.answers.get("peak_rps") or derived_peak)
    avail = s.value("availability")
    ha = avail != "99%"
    instances = max(2 if ha else 1, math.ceil(peak / a["rps_per_instance"]) + (1 if ha else 0))
    writes = peak * a["write_ratio"]
    data1 = float(s.value("data_gb"))
    data3 = data1 * (1 + 2 * a["data_growth_per_year"])
    conns = instances * a["db_pool_per_instance"]
    jobs = peak * a["jobs_per_request"] if "background jobs" in s.workload() else 0.0
    llm_calls = users * a["llm_calls_per_user_per_day"] if "LLM / agents" in s.workload() else 0.0
    tokens_month = llm_calls * a["tokens_per_llm_call"] * 30
    llm_cost = tokens_month / 1e6 * a["usd_per_million_tokens"] if a["usd_per_million_tokens"] else None
    downtime_min = {"99%": 438, "99.9%": 43.8, "99.95%": 21.9, "99.99%": 4.4}[avail]
    rows = [
        (
            "Average load",
            f"{avg_rps:,.1f} req/s",
            f"{users:,.0f} users × {a['requests_per_user_per_day']:g} req/day ÷ 86,400 s",
        ),
        (
            "Peak load",
            f"{peak:,.1f} req/s",
            "given" if s.answers.get("peak_rps") else f"average × peak factor {a['peak_factor']:g}",
        ),
        (
            "App instances",
            f"{instances}",
            f"ceil(peak ÷ {a['rps_per_instance']:g} req/s per instance)" + (" + 1 spare for HA" if ha else ""),
        ),
        ("DB writes at peak", f"{writes:,.1f} /s", f"peak × write ratio {a['write_ratio']:g}"),
        ("DB connections", f"{conns:.0f}", f"{instances} instances × pool {a['db_pool_per_instance']:g}"),
        (
            "Data, year 1 → 3",
            f"{data1:,.1f} GB → {data3:,.1f} GB",
            f"growth {a['data_growth_per_year']:g}× year-one data per year",
        ),
        ("Downtime budget", f"{downtime_min} min/month", f"{avail} availability"),
    ]
    if jobs:
        rows.append(
            ("Background jobs", f"{jobs:,.1f} /s at peak", f"peak × {a['jobs_per_request']:g} jobs per request")
        )
    if llm_calls:
        rows.append(
            ("Model calls", f"{llm_calls:,.0f} /day", f"{users:,.0f} users × {a['llm_calls_per_user_per_day']:g}")
        )
        rows.append(
            ("Model tokens", f"{tokens_month / 1e6:,.1f} M /month", f"calls × {a['tokens_per_llm_call']:g} tokens × 30")
        )
        rows.append(
            (
                "Model cost",
                f"${llm_cost:,.0f} /month" if llm_cost is not None else "set a price per 1M tokens",
                "tokens × your price" if llm_cost is not None else "assumption usd_per_million_tokens is 0",
            )
        )
    return {
        "avg_rps": avg_rps,
        "peak_rps": peak,
        "instances": instances,
        "writes_per_s": writes,
        "connections": conns,
        "data_gb_y1": data1,
        "data_gb_y3": data3,
        "jobs_per_s": jobs,
        "llm_calls_per_day": llm_calls,
        "tokens_per_month": tokens_month,
        "llm_cost_usd_month": llm_cost,
        "downtime_min_month": downtime_min,
        "ha": ha,
        "rows": [{"metric": m, "value": v, "how": h} for m, v, h in rows],
        "assumptions": [
            {"key": k, "value": assumptions(s)[k], "default": d, "meaning": t, "overridden": k in s.overrides}
            for k, (d, t) in ASSUMPTIONS.items()
        ],
    }


# --------------------------------------------------------------------------- technology


CLOUD = {
    "GCP": {
        "run": "Cloud Run",
        "db": "Cloud SQL for PostgreSQL",
        "cache": "Memorystore for Redis",
        "queue": "Cloud Tasks",
        "bus": "Pub/Sub",
        "llm": "Vertex AI (Gemini)",
        "storage": "Cloud Storage",
        "vector": "Vertex AI Vector Search",
        "pooler": "Cloud SQL Auth Proxy + PgBouncer",
        "sched": "Cloud Scheduler",
    },
    "AWS": {
        "run": "ECS on Fargate",
        "db": "Amazon RDS for PostgreSQL",
        "cache": "ElastiCache for Redis",
        "queue": "Amazon SQS",
        "bus": "Amazon SNS + SQS",
        "llm": "Amazon Bedrock",
        "storage": "Amazon S3",
        "vector": "OpenSearch vector search",
        "pooler": "RDS Proxy",
        "sched": "EventBridge Scheduler",
    },
    "Azure": {
        "run": "Azure Container Apps",
        "db": "Azure Database for PostgreSQL",
        "cache": "Azure Cache for Redis",
        "queue": "Azure Service Bus queues",
        "bus": "Azure Service Bus topics",
        "llm": "Azure OpenAI",
        "storage": "Azure Blob Storage",
        "vector": "Azure AI Search",
        "pooler": "built-in PgBouncer",
        "sched": "Container Apps jobs",
    },
    "Any / self-hosted": {
        "run": "Docker containers",
        "db": "PostgreSQL",
        "cache": "Redis",
        "queue": "PostgreSQL SKIP LOCKED queue",
        "bus": "NATS or Kafka",
        "llm": "LiteLLM gateway",
        "storage": "S3-compatible storage",
        "vector": "Qdrant",
        "pooler": "PgBouncer",
        "sched": "cron container",
    },
}

# Bundled fallbacks, used only when PyPI can't be reached. Shown as "fallback" in the UI.
FALLBACK_VERSIONS = {
    "fastapi": "0.163.0",
    "uvicorn": "0.54.0",
    "sqlalchemy": "2.1.8",
    "psycopg": "3.3.9",
    "pydantic-settings": "2.16.3",
    "redis": "8.1.0",
    "httpx": "0.28.1",
    "tenacity": "9.2.1",
    "google-adk": "2.14.1",
    "google-genai": "2.34.0",  # newest google-adk 2.14.1 accepts (<3)
    "pgvector": "0.5.1",
    "pytest": "9.2.0",
    "httpx2": "2.16.0",
    "alembic": "1.20.1",
    "litellm": "1.109.1",
    "boto3": "1.44.3",
    "google-cloud-tasks": "2.26.0",
}


def libraries(s: Session) -> list[str]:
    libs = [
        "fastapi",
        "uvicorn",
        "sqlalchemy",
        "psycopg",
        "alembic",
        "pydantic-settings",
        "httpx",
        "tenacity",
        "pytest",
        "httpx2",  # Starlette's TestClient prefers it; plain httpx is deprecated there
    ]
    w = s.workload()
    cap_needs_cache = _needs_cache(s, capacity(s))
    if cap_needs_cache or "realtime updates" in w:
        libs.append("redis")
    if "LLM / agents" in w:
        cloud = s.value("cloud")
        libs += ["google-adk"] if cloud == "GCP" else ["litellm"]
    if "vector search" in w:
        libs.append("pgvector")
    if "background jobs" in w and s.value("cloud") == "GCP":
        libs.append("google-cloud-tasks")
    return libs


def _needs_cache(s: Session, cap: dict) -> bool:
    return cap["peak_rps"] > 300 or float(s.value("latency_ms")) < 100


# --------------------------------------------------------------------------- candidates


@dataclass
class Component:
    id: str
    label: str
    kind: str  # service | worker | database | cache | queue | llm | storage | vector | ingress | scheduler
    tech: str
    why: str = ""


@dataclass
class Candidate:
    id: str
    name: str
    summary: str
    components: list[Component]
    edges: list[tuple[str, str, str]]
    pros: list[str]
    cons: list[str]
    flips_when: list[str]
    fit: int  # 0..100
    recommended: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["edges"] = [{"source": a, "target": b, "label": lab} for a, b, lab in self.edges]
        return d


def candidates(s: Session, versions: dict[str, str]) -> list[Candidate]:
    cap = capacity(s)
    c = CLOUD[s.value("cloud")]
    w = s.workload()
    team = float(s.value("team"))
    peak = cap["peak_rps"]
    v = lambda lib: f"{lib} {versions.get(lib, '')}".strip()  # noqa: E731
    cache = _needs_cache(s, cap)
    llm = "LLM / agents" in w
    jobs = "background jobs" in w or "scheduled jobs" in w
    vector = "vector search" in w
    pooler_needed = cap["connections"] > 80

    def base_components() -> list[Component]:
        comps = [
            Component("clients", "Clients", "ingress", "HTTPS", "users and other services"),
            Component(
                "api",
                "API service",
                "service",
                f"{v('fastapi')} on {c['run']}",
                f"{cap['instances']} instance(s) for {peak:,.0f} req/s at peak",
            ),
            Component(
                "db",
                "Primary database",
                "database",
                f"{c['db']} via {v('sqlalchemy')} + {v('psycopg')}",
                f"{cap['data_gb_y3']:,.0f} GB by year 3 and {cap['writes_per_s']:,.0f} writes/s fit one primary",
            ),
        ]
        if pooler_needed:
            comps.append(
                Component(
                    "pooler",
                    "Connection pooler",
                    "service",
                    c["pooler"],
                    f"{cap['connections']:.0f} app connections exceed PostgreSQL's default max_connections of 100",
                )
            )
        if cache:
            comps.append(
                Component(
                    "cache",
                    "Cache",
                    "cache",
                    f"{c['cache']} ({v('redis')})",
                    f"p95 target {s.value('latency_ms'):g} ms at {peak:,.0f} req/s",
                )
            )
        if vector:
            comps.append(
                Component(
                    "vector",
                    "Vector index",
                    "vector",
                    f"pgvector ({v('pgvector')}) in the primary database",
                    "no extra system until roughly 10M vectors or strict low-latency ANN at high QPS",
                )
            )
        if llm:
            comps.append(
                Component(
                    "llm",
                    "Model provider",
                    "llm",
                    f"{c['llm']} via {v('google-adk') if 'google-adk' in versions else v('litellm')}",
                    f"{cap['llm_calls_per_day']:,.0f} calls/day",
                )
            )
        if "file uploads" in w:
            comps.append(
                Component(
                    "blob", "Object storage", "storage", c["storage"], "uploads go straight to storage via signed URLs"
                )
            )
        return comps

    def base_edges(comps: list[Component]) -> list[tuple[str, str, str]]:
        ids = {x.id for x in comps}
        e = [("clients", "api", "HTTPS")]
        e.append(("api", "pooler" if "pooler" in ids else "db", "SQL"))
        if "pooler" in ids:
            e.append(("pooler", "db", "SQL"))
        if "cache" in ids:
            e.append(("api", "cache", "reads/writes"))
        if "llm" in ids:
            e.append(("api", "llm", "prompts"))
        if "blob" in ids:
            e.append(("clients", "blob", "signed upload"))
        return e

    out: list[Candidate] = []

    # A. modular monolith -------------------------------------------------------
    comps = base_components()
    if jobs:
        comps.append(
            Component(
                "queue",
                "Job queue",
                "queue",
                "PostgreSQL FOR UPDATE SKIP LOCKED table",
                f"{cap['jobs_per_s']:,.1f} jobs/s fits in the primary database",
            )
        )
    edges = base_edges(comps) + ([("api", "queue", "enqueue")] if jobs else [])
    out.append(
        Candidate(
            "monolith",
            "Modular monolith",
            "One deployable service with clear internal modules, one PostgreSQL database"
            + (", and jobs in a SKIP LOCKED table" if jobs else "")
            + ".",
            comps,
            edges,
            pros=[
                "Fewest moving parts: one deploy, one database, one place to debug",
                f"Fits {peak:,.0f} req/s with {cap['instances']} instance(s)",
                f"Easy for a team of {team:g} to own end to end",
            ],
            cons=[
                "Long-running work (model calls, jobs) shares CPU and memory with the request path",
                "All modules deploy together",
            ],
            flips_when=[
                f"Peak traffic passes ~2,000 req/s (today {peak:,.0f})",
                "Background work needs to scale separately from the API",
                f"More than ~8 engineers ship to it every day (today {team:g})",
            ],
            fit=0,
        )
    )

    # B. API + worker -----------------------------------------------------------
    comps = base_components()
    queue_tech = (
        c["queue"]
        if cap["jobs_per_s"] > 50 or s.value("cloud") != "Any / self-hosted"
        else "PostgreSQL SKIP LOCKED queue"
    )
    comps += [
        Component(
            "queue",
            "Job queue",
            "queue",
            queue_tech,
            f"{max(cap['jobs_per_s'], 0.1):,.1f} jobs/s with retries and dead-lettering",
        ),
        Component(
            "worker",
            "Worker service",
            "worker",
            f"Python worker on {c['run']} ({v('tenacity')} for retries)",
            "runs jobs" + (" and model calls" if llm else "") + " outside the request path",
        ),
    ]
    if "scheduled jobs" in w:
        comps.append(
            Component("sched", "Scheduler", "scheduler", c["sched"], "triggers periodic jobs through the queue")
        )
    edges = [
        *base_edges(comps),
        ("api", "queue", "enqueue"),
        ("queue", "worker", "deliver"),
        ("worker", "pooler" if pooler_needed else "db", "SQL"),
    ]
    if llm:
        edges.append(("worker", "llm", "prompts"))
    if "scheduled jobs" in w:
        edges.append(("sched", "queue", "trigger"))
    out.append(
        Candidate(
            "api-worker",
            "API + worker",
            "The API stays fast while a separate worker runs jobs"
            + (" and model calls" if llm else "")
            + f" from {queue_tech}.",
            comps,
            edges,
            pros=[
                "Slow or bursty work can't hurt API latency",
                "Worker scales on queue depth, API on request rate",
                "Retries and dead letters come from the queue, not hand-written loops",
            ],
            cons=[
                "Two services to deploy and monitor",
                "Results of async work need a status endpoint or a push channel",
            ],
            flips_when=[
                "Several teams need to release parts independently",
                f"Peak traffic passes ~5,000 req/s (today {peak:,.0f})",
            ],
            fit=0,
        )
    )

    # C. event-driven services ----------------------------------------------------
    comps = base_components()
    comps = [x for x in comps if x.id != "api"] + [
        Component(
            "gateway",
            "API gateway",
            "service",
            f"{v('fastapi')} BFF on {c['run']}",
            "routes and authenticates requests",
        ),
        Component("svc_a", "Domain service A", "service", f"{v('fastapi')} on {c['run']}", "owns its own schema"),
        Component("svc_b", "Domain service B", "service", f"{v('fastapi')} on {c['run']}", "owns its own schema"),
        Component("bus", "Event bus", "queue", c["bus"], "services react to each other's events"),
    ]
    edges = [
        ("clients", "gateway", "HTTPS"),
        ("gateway", "svc_a", "HTTP"),
        ("gateway", "svc_b", "HTTP"),
        ("svc_a", "bus", "publish"),
        ("bus", "svc_b", "subscribe"),
        ("svc_a", "db", "SQL"),
        ("svc_b", "db", "SQL"),
    ]
    if cache:
        edges.append(("gateway", "cache", "reads"))
    if llm:
        edges.append(("svc_b", "llm", "prompts"))
    out.append(
        Candidate(
            "services",
            "Event-driven services",
            "Separate services per domain that communicate through an event bus.",
            comps,
            edges,
            pros=["Teams deploy independently", "Each service scales and fails on its own"],
            cons=[
                "Distributed tracing, schema versioning and eventual consistency become daily work",
                f"Operational load is high for a team of {team:g}",
                f"Built for far more than {peak:,.0f} req/s",
            ],
            flips_when=["Worth it once ~3+ teams own separate domains, or one domain's load is 10× the others"],
            fit=0,
        )
    )

    # fit scores -------------------------------------------------------------------
    for cand in out:
        score = 60
        if cand.id == "monolith":
            score += 25 if peak < 2000 and team <= 8 else -20
            score -= 25 if (llm or "background jobs" in w) else 0
        elif cand.id == "api-worker":
            score += 25 if (llm or jobs) else -5
            score += 5 if peak < 5000 else -10
        else:
            score += 25 if (peak > 5000 or team > 20) else -35
        cand.fit = max(0, min(100, score))
    max(out, key=lambda x: x.fit).recommended = True
    return out


# --------------------------------------------------------------------------- critic for designs


def design_critic(cands: list[Candidate]) -> list[Candidate]:
    """Drop generic proposals: each must name components with technology, trade-offs and a flip point."""
    keep = []
    for cand in cands:
        if all(x.tech for x in cand.components) and cand.pros and cand.cons and cand.flips_when:
            keep.append(cand)
    return keep


# --------------------------------------------------------------------------- decision records


def decisions(s: Session, cand: Candidate, cap: dict) -> list[dict]:
    c = CLOUD[s.value("cloud")]
    comps = {x.id: x for x in cand.components}
    out = [
        {
            "id": "0001",
            "title": f"Architecture style: {cand.name}",
            "context": f"Peak {cap['peak_rps']:,.0f} req/s, {cap['data_gb_y3']:,.0f} GB by year 3, "
            f"team of {s.value('team'):g}, "
            f"{s.value('availability')} availability.",
            "decision": cand.summary,
            "alternatives": [
                x for x in ("Modular monolith", "API + worker", "Event-driven services") if x != cand.name
            ],
            "consequences": cand.pros + cand.cons,
            "revisit_when": cand.flips_when,
        },
        {
            "id": "0002",
            "title": f"Primary database: {c['db']}",
            "context": f"{cap['writes_per_s']:,.0f} writes/s at peak, {cap['data_gb_y3']:,.0f} GB by year 3, "
            "relational data with transactions.",
            "decision": f"Use {comps['db'].tech}. Migrations with Alembic.",
            "alternatives": [
                "A document database (weaker joins and constraints)",
                "A distributed SQL database (more cost and latency now)",
            ],
            "consequences": [
                "One primary handles writes; add read replicas before sharding",
                "Jobs and vectors can live in the same database while volumes are small",
            ],
            "revisit_when": ["Writes pass a few thousand per second sustained", "Data passes ~2 TB on one primary"],
        },
        {
            "id": "0003",
            "title": f"Hosting: {c['run']}",
            "context": f"{cap['instances']} instance(s) at peak, "
            + ("high availability needed." if cap["ha"] else "single instance acceptable."),
            "decision": f"Run containers on {c['run']} with at least {2 if cap['ha'] else 1} instance(s).",
            "alternatives": ["Kubernetes (more control, much more to operate)", "VMs (manual scaling and patching)"],
            "consequences": [
                "Scale-to-zero or autoscaling keeps idle cost low",
                "Cold starts add latency to the first request",
            ],
            "revisit_when": ["You need GPUs, sidecars or long-lived connections the platform doesn't support"],
        },
    ]
    n = 4
    if "cache" in comps:
        out.append(
            {
                "id": f"{n:04d}",
                "title": "Add a cache",
                "context": comps["cache"].why,
                "decision": f"Use {comps['cache'].tech} for hot reads and sessions.",
                "alternatives": ["In-process lru_cache (not shared between instances)"],
                "consequences": ["Invalidation rules must be written per key", "One more managed service"],
                "revisit_when": ["Hit rate stays under ~50%: the cache isn't paying for itself"],
            }
        )
        n += 1
    if "queue" in comps:
        out.append(
            {
                "id": f"{n:04d}",
                "title": f"Background work: {comps['queue'].tech}",
                "context": comps["queue"].why,
                "decision": f"Enqueue jobs to {comps['queue'].tech}"
                + ("; a worker service runs them." if "worker" in comps else "."),
                "alternatives": ["Threads inside the API process (lost on restart)", "Celery (another broker to run)"],
                "consequences": ["Jobs must be idempotent: the queue delivers at least once"],
                "revisit_when": ["Job volume or fan-out outgrows the chosen queue"],
            }
        )
        n += 1
    if "llm" in comps:
        out.append(
            {
                "id": f"{n:04d}",
                "title": f"Model access: {comps['llm'].tech}",
                "context": comps["llm"].why,
                "decision": f"Call models through {comps['llm'].tech}; keep prompts versioned in the repo; "
                "cache static prompt prefixes.",
                "alternatives": ["Direct provider SDKs in each service (harder to switch or audit)"],
                "consequences": [
                    "Token spend is the main variable cost; track it per tenant",
                    "Retries need budgets so outages don't multiply spend",
                ],
                "revisit_when": [
                    "A cheaper model meets your eval bar",
                    "Latency targets need streaming or a smaller model",
                ],
            }
        )
        n += 1
    if "vector" in comps:
        out.append(
            {
                "id": f"{n:04d}",
                "title": "Vector search in PostgreSQL (pgvector)",
                "context": comps["vector"].why,
                "decision": "Store embeddings in pgvector with an HNSW index next to the rows they describe.",
                "alternatives": [c["vector"], "Qdrant"],
                "consequences": [
                    "Filters and joins work in plain SQL",
                    "Index build time and memory grow with vector count",
                ],
                "revisit_when": ["Roughly 10M+ vectors, or p95 search latency misses target at your QPS"],
            }
        )
        n += 1
    comp_flags = [x for x in s.value("compliance") if x != "none"]
    if comp_flags:
        out.append(
            {
                "id": f"{n:04d}",
                "title": "Compliance: " + ", ".join(comp_flags),
                "context": "Stated constraints: " + ", ".join(comp_flags) + ".",
                "decision": "Keep regulated data in the primary database only, encrypted at rest; "
                "no regulated data in logs, "
                "caches or prompts unless redacted; region pinned"
                + (" to the required residency." if "data residency" in comp_flags else "."),
                "alternatives": [],
                "consequences": ["Model calls must go through a redaction step for regulated fields"],
                "revisit_when": ["A new data category or region is added"],
            }
        )
    return out


def adr_markdown(d: dict) -> str:
    lst = lambda xs: "\n".join(f"- {x}" for x in xs) or "- None"  # noqa: E731
    return (
        f"# ADR {d['id']}: {d['title']}\n\nStatus: Accepted\n\n## Context\n\n{d['context']}\n\n"
        f"## Decision\n\n{d['decision']}\n\n## Alternatives considered\n\n{lst(d['alternatives'])}\n\n"
        f"## Consequences\n\n{lst(d['consequences'])}\n\n## Revisit when\n\n{lst(d['revisit_when'])}\n"
    )


# --------------------------------------------------------------------------- data model / API


def data_model(s: Session, cfg: LLMConfig | None) -> dict:
    idea = s.value("idea") or ""
    if cfg is not None:
        prompt = (
            f"Project: {idea}\nWorkload: {', '.join(s.workload()) or 'plain API'}\n"
            'Return only JSON: {"entities":[{"name":str,"fields":[str]}],'
            '"endpoints":[{"method":str,"path":str,"summary":str}]}. '
            "At most 6 entities and 10 endpoints. Field format: 'name: type'."
        )
        text = complete(cfg, "You draft concise data models and REST APIs.", [{"role": "user", "content": prompt}], 900)
        if text:
            m = re.search(r"\{.*\}", text, re.S)
            if m:
                try:
                    data = json.loads(m.group(0))
                    data["source"] = "model draft — review before use"
                    return data
                except json.JSONDecodeError:
                    pass
    multi = "multi-tenant" in s.workload()
    entities = []
    if multi:
        entities.append(
            {"name": "Tenant", "fields": ["id: uuid", "name: text", "plan: text", "created_at: timestamptz"]}
        )
    entities.append(
        {
            "name": "User",
            "fields": [
                "id: uuid",
                *(["tenant_id: uuid -> Tenant"] if multi else []),
                "email: citext unique",
                "created_at: timestamptz",
            ],
        }
    )
    if "background jobs" in s.workload():
        entities.append(
            {
                "name": "Job",
                "fields": [
                    "id: bigserial",
                    "kind: text",
                    "payload: jsonb",
                    "status: text (pending|running|done|failed)",
                    "attempts: int",
                    "run_after: timestamptz",
                ],
            }
        )
    if "vector search" in s.workload():
        entities.append(
            {
                "name": "Document",
                "fields": [
                    "id: uuid",
                    *(["tenant_id: uuid -> Tenant"] if multi else []),
                    "content: text",
                    "embedding: vector(768)",
                ],
            }
        )
    if "file uploads" in s.workload():
        entities.append(
            {
                "name": "Upload",
                "fields": [
                    "id: uuid",
                    "owner_id: uuid -> User",
                    "object_key: text",
                    "size_bytes: bigint",
                    "created_at: timestamptz",
                ],
            }
        )
    endpoints = [
        {"method": "GET", "path": "/healthz", "summary": "Liveness and readiness"},
        {"method": "GET", "path": "/v1/me", "summary": "Current user"},
    ]
    if "background jobs" in s.workload():
        endpoints += [
            {"method": "POST", "path": "/v1/jobs", "summary": "Enqueue a job"},
            {"method": "GET", "path": "/v1/jobs/{id}", "summary": "Job status"},
        ]
    if "vector search" in s.workload():
        endpoints.append({"method": "POST", "path": "/v1/search", "summary": "Semantic search"})
    if "file uploads" in s.workload():
        endpoints.append({"method": "POST", "path": "/v1/uploads", "summary": "Get a signed upload URL"})
    return {
        "entities": entities,
        "endpoints": endpoints,
        "source": "skeleton from your workload answers; add your domain entities (configure a model to draft them)",
    }


# --------------------------------------------------------------------------- full proposal


def propose(
    s: Session, cfg: LLMConfig | None = None, root: str | Path | None = None, live_versions: bool = True
) -> dict:
    from .watcher import latest_versions

    libs = libraries(s)
    versions = (
        latest_versions(libs, FALLBACK_VERSIONS, root)
        if live_versions
        else {lib: FALLBACK_VERSIONS.get(lib, "") for lib in libs}
    )
    version_source = {
        lib: "pypi" if live_versions and versions.get(lib) not in (None, "", FALLBACK_VERSIONS.get(lib)) else "fallback"
        for lib in libs
    }
    cap = capacity(s)
    cands = design_critic(candidates(s, versions))
    chosen = next(c for c in cands if c.recommended)
    return {
        "session": s.to_dict(),
        "capacity": cap,
        "versions": versions,
        "version_source": version_source,
        "candidates": [c.to_dict() for c in cands],
        "recommended": chosen.id,
        "decisions": decisions(s, chosen, cap),
        "data_model": data_model(s, cfg),
    }


def decisions_for(s: Session, proposal: dict, candidate_id: str) -> list[dict]:
    cand = _candidate(proposal, candidate_id)
    return decisions(s, cand, proposal["capacity"])


def _candidate(proposal: dict, cid: str) -> Candidate:
    raw = next(c for c in proposal["candidates"] if c["id"] == cid)
    return Candidate(
        raw["id"],
        raw["name"],
        raw["summary"],
        [Component(**x) for x in raw["components"]],
        [(e["source"], e["target"], e["label"]) for e in raw["edges"]],
        raw["pros"],
        raw["cons"],
        raw["flips_when"],
        raw["fit"],
        raw["recommended"],
    )


def design_doc(s: Session, proposal: dict, cid: str) -> str:
    cand = _candidate(proposal, cid)
    cap = proposal["capacity"]
    out = [
        f"# {s.value('name')}: design\n",
        f"{s.value('idea')}\n",
        f"## Architecture: {cand.name}\n",
        cand.summary + "\n",
        "| Component | Technology | Why |",
        "| --- | --- | --- |",
    ]
    out += [f"| {x.label} | {x.tech} | {x.why} |" for x in cand.components]
    out += ["", "## Capacity\n", "| Metric | Value | How |", "| --- | --- | --- |"]
    out += [f"| {r['metric']} | {r['value']} | {r['how']} |" for r in cap["rows"]]
    out += (
        [
            "",
            "Assumptions: " + "; ".join(f"{a['key']} = {a['value']:g}" for a in cap["assumptions"]) + "\n",
            "## Trade-offs\n",
        ]
        + [f"- + {p}" for p in cand.pros]
        + [f"- − {c}" for c in cand.cons]
    )
    out += ["", "## Revisit when\n"] + [f"- {f}" for f in cand.flips_when]
    dm = proposal["data_model"]
    out += ["", "## Data model\n"] + [f"- **{e['name']}**: " + ", ".join(e["fields"]) for e in dm["entities"]]
    out += ["", "## API outline\n"] + [f"- `{e['method']} {e['path']}` — {e['summary']}" for e in dm["endpoints"]]
    out += ["", "## Diagram\n", "```mermaid", mermaid(cand), "```", ""]
    return "\n".join(out)


def mermaid(cand: Candidate) -> str:
    shapes = {
        "database": ("[(", ")]"),
        "cache": ("[(", ")]"),
        "vector": ("[(", ")]"),
        "queue": ("[[", "]]"),
        "ingress": ("([", "])"),
        "llm": ("{{", "}}"),
        "storage": ("[(", ")]"),
    }
    lines = ["flowchart LR"]
    for x in cand.components:
        left, right = shapes.get(x.kind, ("[", "]"))
        lines.append(f'    {x.id}{left}"{x.label}<br/><small>{x.tech}</small>"{right}')
    lines += [f"    {a} -- {lab} --> {b}" for a, b, lab in cand.edges]
    return "\n".join(lines)


# --------------------------------------------------------------------------- starter repo


def starter_repo(s: Session, proposal: dict, cid: str) -> bytes:
    cand = _candidate(proposal, cid)
    v = proposal["versions"]
    name = re.sub(r"[^a-z0-9-]+", "-", str(s.value("name")).lower()).strip("-") or "my-service"
    pkg = name.replace("-", "_")
    ids = {x.id for x in cand.components}
    dev_libs = ("pytest", "httpx2")
    deps = [f"{lib}=={v[lib]}" if v.get(lib) else lib for lib in v if lib not in dev_libs]
    dev = [f"{lib}=={v.get(lib) or FALLBACK_VERSIONS[lib]}" for lib in dev_libs]
    deps = [d.replace("psycopg==", "psycopg[binary]==") if d.startswith("psycopg==") else d for d in deps]
    deps = [d.replace("uvicorn==", "uvicorn[standard]==") if d.startswith("uvicorn==") else d for d in deps]
    files: dict[str, str] = {}
    dep_lines = "\n".join(f'    "{d}",' for d in deps)  # outside dedent: it would skew the indentation
    files["pyproject.toml"] = (
        f'[project]\nname = "{name}"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n'
        f"dependencies = [\n{dep_lines}\n]\n\n"
        f"[project.optional-dependencies]\ndev = [{', '.join(repr(d).replace(chr(39), chr(34)) for d in dev)}]\n\n"
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
    )
    files[f"{pkg}/__init__.py"] = ""
    files[f"{pkg}/settings.py"] = textwrap.dedent("""\
        from pydantic_settings import BaseSettings, SettingsConfigDict


        class Settings(BaseSettings):
            model_config = SettingsConfigDict(env_file=".env")

            database_url: str = "postgresql+psycopg://app:app@localhost:5432/app"
            redis_url: str = "redis://localhost:6379/0"
            db_pool_size: int = 10


        settings = Settings()
        """)
    files[f"{pkg}/db.py"] = textwrap.dedent("""\
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from .settings import settings

        # One engine per process: its pool is reused by every request.
        engine = create_engine(settings.database_url, pool_size=settings.db_pool_size, pool_pre_ping=True)
        SessionLocal = sessionmaker(engine, expire_on_commit=False)


        def get_session():
            with SessionLocal() as session:
                yield session
        """)
    cache_on = "cache" in ids
    main = [
        "from contextlib import asynccontextmanager",
        "",
        "from fastapi import FastAPI",
        "from sqlalchemy import text",
        "",
        "from .db import engine",
        *(["from .settings import settings"] if cache_on else []),
        "",
        "",
        "@asynccontextmanager",
        "async def lifespan(app: FastAPI):",
        *(
            ["    import redis.asyncio as redis", "", "    app.state.redis = redis.from_url(settings.redis_url)"]
            if cache_on
            else []
        ),
        "    yield",
        *(["    await app.state.redis.aclose()"] if cache_on else []),
        "    engine.dispose()",
        "",
        "",
        f'app = FastAPI(title="{s.value("name")}", lifespan=lifespan)',
        "",
        "",
        '@app.get("/healthz")',
        "def healthz():",
        '    return {"status": "ok"}',
        "",
        "",
        '@app.get("/readyz")',
        "def readyz():",
        "    with engine.connect() as conn:",
        '        conn.execute(text("select 1"))',
        '    return {"status": "ready"}',
        "",
    ]
    files[f"{pkg}/main.py"] = "\n".join(main)
    if "worker" in ids or "queue" in ids:
        files[f"{pkg}/worker.py"] = textwrap.dedent("""\
            \"\"\"Job worker: claims jobs with FOR UPDATE SKIP LOCKED so several workers never double-process.\"\"\"

            import time

            from sqlalchemy import text

            from .db import engine

            CLAIM = text(\"\"\"
                UPDATE jobs SET status = 'running', attempts = attempts + 1
                WHERE id IN (
                    SELECT id FROM jobs
                    WHERE status = 'pending' AND run_after <= now()
                    ORDER BY id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 10
                )
                RETURNING id, kind, payload
            \"\"\")


            def handle(kind: str, payload: dict) -> None:
                raise NotImplementedError(kind)


            def run_forever(idle_seconds: float = 2.0) -> None:
                while True:
                    with engine.begin() as conn:
                        jobs = conn.execute(CLAIM).fetchall()
                    for job in jobs:
                        handle(job.kind, job.payload)
                        with engine.begin() as conn:
                            conn.execute(text("UPDATE jobs SET status = 'done' WHERE id = :id"), {"id": job.id})
                    if not jobs:
                        time.sleep(idle_seconds)


            if __name__ == "__main__":
                run_forever()
            """)
    files["tests/test_health.py"] = textwrap.dedent(f"""\
        from fastapi.testclient import TestClient

        from {pkg}.main import app


        def test_healthz():
            assert TestClient(app).get("/healthz").json() == {{"status": "ok"}}
        """)
    files["Dockerfile"] = textwrap.dedent(f"""\
        FROM python:3.12-slim
        WORKDIR /app
        COPY pyproject.toml .
        COPY {pkg} {pkg}
        RUN pip install --no-cache-dir .
        CMD ["uvicorn", "{pkg}.main:app", "--host", "0.0.0.0", "--port", "8080"]
        """)
    services = [
        "  db:\n    image: postgres:17\n    environment:\n      POSTGRES_USER: app\n      POSTGRES_PASSWORD: app\n"
        '      POSTGRES_DB: app\n    ports: ["5432:5432"]'
    ]
    if "cache" in ids:
        services.append('  redis:\n    image: redis:7\n    ports: ["6379:6379"]')
    services.append(
        "  api:\n    build: .\n    environment:\n      DATABASE_URL: postgresql+psycopg://app:app@db:5432/app\n"
        '    ports: ["8080:8080"]\n    depends_on: [db]'
    )
    if "worker" in ids:
        services.append(
            f"  worker:\n    build: .\n    command: python -m {pkg}.worker\n    environment:\n"
            f"      DATABASE_URL: postgresql+psycopg://app:app@db:5432/app\n    depends_on: [db]"
        )
    files["docker-compose.yml"] = "services:\n" + "\n".join(services) + "\n"
    files["README.md"] = (
        f"# {s.value('name')}\n\n{s.value('idea')}\n\nGenerated by Codewright Design mode. "
        f"See docs/design.md and docs/adr/.\n\n```bash\ndocker compose up\n```\n"
    )
    files["docs/design.md"] = design_doc(s, proposal, cid)
    for d in decisions_for(s, proposal, cid):
        slug = re.sub(r"[^a-z0-9]+", "-", d["title"].lower()).strip("-")[:50]
        files[f"docs/adr/{d['id']}-{slug}.md"] = adr_markdown(d)
    files[".codewright/design.json"] = json.dumps(intended(s, proposal, cid), indent=2)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, body in files.items():
            z.writestr(f"{name}/{path}", body)
    return buf.getvalue()


# --------------------------------------------------------------------------- intended architecture + drift

KIND_TO_MAP = {
    "database": "database",
    "cache": "cache",
    "queue": "queue",
    "llm": "llm",
    "vector": "database",
    "storage": "storage",
    "ingress": "ingress",
}


def intended(s: Session, proposal: dict, cid: str) -> dict:
    cand = _candidate(proposal, cid)
    return {
        "name": s.value("name"),
        "candidate": cand.id,
        "style": cand.name,
        "saved_at": time.time(),
        "components": [asdict(x) for x in cand.components],
        "expected_kinds": sorted({KIND_TO_MAP[x.kind] for x in cand.components if x.kind in KIND_TO_MAP}),
        # Decisions as checks: the async API shares one httpx client instead of blocking requests calls.
        "rules": [{"forbid_import": "requests", "reason": "the async API uses one shared httpx client"}],
    }


def save_intended(root: str | Path, data: dict) -> Path:
    from .feedback import ensure_dir

    p = ensure_dir(root) / "design.json"
    p.write_text(json.dumps(data, indent=2))
    return p


def load_intended(root: str | Path) -> dict | None:
    p = Path(root) / ".codewright" / "design.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


def drift(intent: dict, system_view, graph) -> dict:
    """Compare the intended architecture with the live system map and the import rules."""
    code_kinds: dict[str, list[str]] = {}
    for n in system_view.nodes:
        if n.kind not in ("component", "http"):
            code_kinds.setdefault(n.kind, []).append(n.label)
    expected = set(intent.get("expected_kinds", []))
    unplanned = [{"kind": k, "systems": v} for k, v in sorted(code_kinds.items()) if k not in expected]
    missing = [
        {"kind": k, "components": [c["label"] for c in intent["components"] if KIND_TO_MAP.get(c["kind"]) == k]}
        for k in sorted(expected - code_kinds.keys())
    ]
    violations = []
    for rule in intent.get("rules", []):
        bad = rule.get("forbid_import")
        if not bad:
            continue
        for path, pf in graph.files.items():
            for target in pf.module.imports.values():
                if target == bad or target.startswith(bad + "."):
                    violations.append(
                        {
                            "rule": f"forbid import {bad}",
                            "reason": rule.get("reason", ""),
                            "file": path,
                            "import": target,
                        }
                    )
                    break
    return {
        "design": intent.get("style"),
        "unplanned": unplanned,
        "not_built": missing,
        "violations": violations,
        "ok": not (unplanned or violations),
    }
