# Codewright

Codewright is a code and architecture advisor. It reads your exact stack and library versions, finds code that rebuilds what a library, framework or platform already does, and suggests the change with a diff, measured proof and a clear "when not to".

A suggestion that can't name a file, an API version and a gain is never shown.

This is **v0**. It covers the Review mode for Python repos. Map and Design modes come in v1.

## Quick start

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/codewright review path/to/repo            # ranked findings in the terminal
.venv/bin/codewright review path/to/repo -f markdown -o report.md
.venv/bin/codewright review path/to/repo -f json     # for CI or coding agents
.venv/bin/codewright stack path/to/repo              # what versions it thinks you run
.venv/bin/codewright graph path/to/repo              # functions, call edges, entry points
```

Try it on the bundled sample: `codewright review tests/fixtures/sample_app`.

## How a run works

| Step | Module | What it does |
| --- | --- | --- |
| 1. Stack discovery | `stack.py` | Reads `uv.lock`, `poetry.lock`, `Pipfile.lock`, `requirements*.txt`, `pyproject.toml`, `.python-version` and Dockerfiles. Lockfile versions beat declared ranges. |
| 2. Code graph | `graph.py` | Its own tree-sitter indexer: imports, functions, decorators, call edges and entry points (routes, CLI commands, `__main__`). Anything reachable from an entry point is on the hot path. |
| 3. Intent detection | `intents/detectors.py` | Labels functions with the engineering intent they implement by hand. |
| 4. Capability cards | `cards/` | YAML cards per library and version: the API, gain, effort, risk, "when not to", scale threshold and sources. Preference order: already installed, then stdlib, then platform, then a new dependency. |
| 5. Suggestion | `suggest.py` | Picks the best card and writes the smallest safe diff when the shape allows it. |
| 6. Critic | `critic.py` | Drops findings missing a file, an API version, the gain, effort, risk, threshold, "when not to" or a source. Checks the suggested symbol exists in the repo's own interpreter (`.venv` by default). |
| 7. Proof | `proof.py` | Copies the repo to a temp dir, runs `pytest` before and after the patch. Verified only if both pass; a patch that breaks tests is dropped. Runs locally: code never leaves the machine. |
| 8. Ranking and report | `rank.py`, `report.py` | Impact (×1.5 on the hot path) × confidence ÷ effort; top 10 by default. |

## Intents in v0

| Intent | Detects | Card(s) |
| --- | --- | --- |
| `memo-cache` | Module-level dict used as a memo cache | `functools.lru_cache`, with an auto-patch |
| `retry` | Loop with try/except and sleep | `tenacity.retry` |
| `poll-queue` | `while True` loop polling with `SELECT` and sleep | Postgres `FOR UPDATE SKIP LOCKED` + `LISTEN/NOTIFY` |
| `connection-per-request` | Route handler that opens a DB engine, connection or HTTP client | SQLAlchemy engine reuse, psycopg pool, asyncpg pool, shared httpx client, shared requests Session |
| `agent-orchestrator` | Hand-written ADK agent driver loops; `SequentialAgent`/`ParallelAgent`/`LoopAgent` on ADK 2.x | ADK 2.0 graph workflows, or `SequentialAgent` on ADK 1.x |

The plan targets 30 intents in v0, chosen from the most frequent ones in real repos.

## Adding a capability card

Drop a YAML file in a directory and pass `--cards that/dir`. The fields are defined by `Card` in `src/codewright/models.py`; `src/codewright/cards/data/` has examples. Rules:

- `versions` is a PEP 440 specifier checked against the installed version, or the Python version for `kind: stdlib`.
- `match` constrains detector data. For example, `library: psycopg` applies the card only to psycopg connects. `null` means the key must be absent.
- `offer_uninstalled: true` allows the card to suggest adding a new dependency.
- Every card needs `sources` and a `when_not`, or the critic drops its findings.

## Development

```bash
.venv/bin/pytest -q
```
