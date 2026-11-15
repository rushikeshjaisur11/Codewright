# Codewright

[![CI](https://github.com/rushikeshjaisur11/codewright/actions/workflows/ci.yml/badge.svg)](https://github.com/rushikeshjaisur11/codewright/actions/workflows/ci.yml)
![Python 3.11–3.13](https://img.shields.io/badge/python-3.11%E2%80%933.13-3776ab)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue)

Codewright is a code and architecture advisor for Python repos. It reads your exact stack and library versions, finds code that rebuilds what a library, the standard library or the platform already does, and suggests the change with a diff, test-based proof and a clear "when not to".

A suggestion that can't name a file, an API version and a gain is never shown.

It has three modes that share one engine:

- **Review** improves existing code: ranked "instead of X, use Y" findings, each with a patch where it can be made mechanically.
- **Map** shows the current architecture as live diagrams linked to the code, with findings overlaid and a code chat that cites files and lines.
- **Design** drafts the architecture for a new project from a short interview, with capacity math in code, decision records and a starter repo. The design you choose becomes the target that Map and Review check for drift.

![Review: findings beside the open suggestion, with its diff, proof and sources](docs/screenshots/review.png)

## Quick start

```bash
uv venv && uv pip install -e ".[dev]"      # add [llm] for model-written chat answers
.venv/bin/codewright serve path/to/repo    # web app on http://127.0.0.1:8765
```

Everything runs locally and the code is never uploaded. A model is optional (see [Models](#models)).

## Web app

| Page | What it does |
| --- | --- |
| Overview | Findings, proof rate, acceptance rate, hot-path count, stack, drift and upgrades at a glance |
| Review | Filterable findings with evidence, the suggested API and version, gain, risk, "when not to", scale threshold, proof, diff and sources. Accept, reject (with a reason), mute an intent, copy or apply a patch, PR mode and PR-comment export |
| Map | System, module and request-flow diagrams generated from the code graph, laid out with ELK (layered, orthogonal edges). Hover a box to trace its connections, double-click to drill in (breadcrumbs take you back), press `/` to find a node, filter by kind, switch direction. Every box links to its files and lines; overlays show findings, fan-in hotspots and untested code. Monorepos (`packages/*`, `src/` layouts) are split into their real packages. Mermaid export, drift panel and "changes this week" |
| Code chat | Questions about the code, answered with citations you can click open |
| Design | Interview → three candidate architectures with fit scores, a diagram, components with exact versions from PyPI, editable capacity assumptions, decision records, data model and API outline. Download the design doc or a starter repo, or save the design as the repo's intended architecture |
| Releases | Newer versions of each dependency and the capability cards an upgrade would unlock |
| Knowledge base | Every card, searchable and grouped by library and topic. With a repo open, each says whether it fits the locked version, needs an upgrade or isn't a dependency. Cards with a detector become findings; reference entries are for looking things up |
| Settings | Model, offline mode, team constraints, mute rules, MCP and CI snippets |
| Repo switcher | Press `⌘K` / `Ctrl+K`: switch between open repos (branch, findings, last review), reopen recent ones (remembered in `~/.codewright/recent.json`) or browse this machine's folders to open a new one |

| Overview | Map |
| --- | --- |
| ![Overview: findings, proof rate, acceptance rate and stack](docs/screenshots/overview.png) | ![Map: the system view generated from the code graph](docs/screenshots/map.png) |
| **Design** | **Repo switcher (⌘K)** |
| ![Design: three candidate architectures and the recommended one's diagram](docs/screenshots/design.png) | ![Repo switcher: open and recent repos with branch and findings](docs/screenshots/switcher.png) |
| **Light theme** | |
| ![Review in the light theme](docs/screenshots/review-light.png) | |

## Command line

```bash
codewright review .                         # ranked findings in the terminal (runs proof by default)
codewright review . -j 8                    # run up to 8 patched test suites at once
codewright review . -f markdown -o report.md
codewright review . --base origin/main -f pr -o comment.md   # PR mode: changed code only
codewright check .                          # exit 1 when code drifts from .codewright/design.json
codewright map . --level flow               # Mermaid diagram of a request flow
codewright watch . --rescan                 # new dependency releases; re-review when one appears
codewright feedback <finding-id> rejected --reason "not worth the churn"
codewright stack .                          # what versions it thinks you run
codewright graph .                          # functions, call edges and entry points
codewright mcp                              # MCP server on stdio for coding agents
```

### In CI

```yaml
- run: pip install "codewright @ git+https://github.com/rushikeshjaisur11/codewright"
- run: codewright review . --base origin/${{ github.base_ref }} -f pr -o codewright.md
- run: codewright check .
```

Post `codewright.md` as a PR comment with your usual comment action. Codewright suggests; it never merges.

### For coding agents

```json
{ "mcpServers": { "codewright": { "command": "codewright", "args": ["mcp"] } } }
```

Tools: `review`, `review_markdown`, `explain_finding`, `record_feedback`, `architecture_map`, `ask_code`, `check_drift`, `dependency_releases`, `search_knowledge` (what the installed version of a library offers for a task, before writing code against it).

## How a review works

| Step | Module | What it does |
| --- | --- | --- |
| 1. Stack discovery | `stack.py` | Reads `uv.lock`, `poetry.lock`, `Pipfile.lock`, `requirements*.txt`, `pyproject.toml`, `.python-version` and Dockerfiles. Lockfile versions beat declared ranges |
| 2. Code graph | `graph.py` | Its own tree-sitter indexer: imports, classes, functions, decorators, call edges and entry points. Calls resolve through imports (relative imports and package re-exports included), enclosing scopes, `self`/`super()` and base classes; a call on an object of unknown type is matched by method name only when the name isn't one builtin types already have (`d.items()` never links to your `items`). Code reachable from an entry point is on the hot path |
| 3. Intent detection | `intents/` | Labels functions with the engineering intent they implement by hand (31 intents) |
| 4. Capability cards | `cards/data/*.yaml` | Per library and version: the API, gain, effort, risk, "when not to", scale threshold and sources. Preference: already installed, then stdlib, then platform, then a new dependency |
| 5. Suggestion | `suggest.py` | Picks the best card and writes the smallest safe diff when the code's shape allows it |
| 6. Critic | `critic.py` | Drops findings missing a file, an API version, a gain, effort, risk, threshold, "when not to" or a source, and checks that the suggested symbol exists in the repo's own interpreter. A symbol whose library isn't installed there, or whose module needs an optional extra, counts as unchecked, not missing |
| 7. Proof | `proof.py` | Runs `pytest` in a temp copy before and after the patch. The baseline run records which tests actually execute each function (`sys.monitoring` on 3.12+, a profile hook on 3.11). **Verified** only when the suite passes both times *and* a test ran the changed function, and the finding names that test. Patched runs go in parallel (`--jobs`, default: CPUs up to 4); one that fails in parallel is re-run alone before the patch is blamed. The copy's `src/` folders go first on the path, so an editable install can't make the tests run the unpatched original. A patch that breaks tests is dropped |
| 8. Feedback and ranking | `feedback.py`, `rank.py` | Impact (×1.5 on the hot path) × confidence × team acceptance for that intent ÷ effort |

### Intents

| Intent | Suggests | Auto-patch |
| --- | --- | --- |
| Memo cache dict | `functools.lru_cache` | yes |
| `time.sleep` in `async def` | `await asyncio.sleep` | yes |
| Durations with `time.time()` | `time.perf_counter()` | yes |
| `datetime.utcnow()` | `datetime.now(timezone.utc)` | yes |
| Pydantic v1 methods on v2 | `model_validate`, `model_validate_json`… | yes |
| Retry loop with sleep | `tenacity` | |
| Blocking `requests` in `async def` | `httpx.AsyncClient` | |
| Connection or client per request | SQLAlchemy engine reuse, psycopg/asyncpg pools, shared httpx client or requests Session | |
| Polling loop over a jobs table | Postgres `FOR UPDATE SKIP LOCKED` + `LISTEN/NOTIFY` | |
| Google ADK agents (15 intents) | See [Google ADK](#google-adk) | 2 of them |
| `@app.on_event` | FastAPI lifespan | |
| `session.query()` on SQLAlchemy 2.x | `select()` | |
| Manual counting, grouping, thread fan-out | `Counter`, `defaultdict`, `ThreadPoolExecutor` | |
| `os.system` | `subprocess.run` with an argument list | |
| `+=` string building in a loop | `"".join()` | |

Add your own cards: put YAML files in a folder and pass `--cards that/folder`. Each card needs `sources` and a `when_not`, or the critic drops its findings.

### Google ADK

For agents built on [google-adk](https://pypi.org/project/google-adk/), Codewright carries a knowledge base of 69 cards covering ADK 1.0 to 2.14: 30 with a detector and 39 reference entries.

Every card's lowest supported version is the first release that has the API. It was found by checking the source of one wheel per minor release on PyPI (55 releases). Every symbol was imported from google-adk 2.14.1, and every source was opened. The detectors were run on Google's own [adk-samples](https://github.com/google/adk-samples), and three false positives found there were fixed. The findings that remain there are all genuine. One of them: the `deep-search` sample builds its instruction with an f-string that reads today's date once, at import.

| Found in your code | Suggests | Versions | Auto-patch |
| --- | --- | --- | --- |
| Sync `Runner.run` inside `async def` | `async for` over `Runner.run_async` | 1.0+ | yes |
| Agent instruction f-string that calls something at import | An instruction function, or `{key}` state placeholders | 1.0+ | yes |
| `SequentialAgent`/`ParallelAgent`/`LoopAgent`, or a hand-written driver loop | `Workflow` graphs on 2.x, `SequentialAgent` on 1.x | 1.0+ | |
| In-memory session, artifact or memory service in a module that serves requests | `DatabaseSessionService`, `GcsArtifactService`, `VertexAiMemoryBankService` | 1.0+ / 1.5+ | |
| `session.state[k] = v` on a session from the session service | `state_delta=` on `run_async`, or `EventActions(state_delta=...)` | 1.0+ | |
| A tool writing to a module-level dict | `tool_context.state` with `user:`/`app:`/`temp:` scopes | 1.0+ | |
| `json.loads(event.content.parts[0].text)` on the agent's reply | `output_schema=` + `output_key=` | 1.0+ | |
| A tool that runs another agent through its own Runner | `AgentTool` | 1.0+ | |
| A hand-written MCP client | `McpToolset` (or `MCPToolset` before 1.13) | 1.0+ | |
| `session.events[-n:]` | `EventsCompactionConfig` (1.16+), `ContextFilterPlugin` (1.15) | 1.15+ | |
| A web route that builds the Runner or agent per request | Build once at startup | 1.0+ | |
| A web route that drives a Runner by hand | `get_fast_api_app` | 1.5+ | |
| A tool polling a job in a sleep loop | `LongRunningFunctionTool` + resumability | 1.0+ | |
| A retry loop around an agent run | `Gemini(retry_options=HttpRetryOptions(...))` | 1.9+ | |
| Deprecated APIs: `global_instruction`, `Runner(plugins=)`, `VertexAiCodeExecutor`, `save_live_audio`, `ApiRegistry` | Their replacements, only from the version that deprecates them | 1.14+ | |
| An import that needs an extra the lockfile lacks: `mcp`, `db`, `gcp`, `extensions`, `a2a` | The `google-adk[extra]` to add. 2.0 moved MCP, SQLAlchemy and Cloud clients out of the base install | 2.0+ | |

The reference entries cover the rest of ADK, grouped by topic:
- **Agents and orchestration:** sub-agents, custom agents, Workflow routes, human input and retries, planners, A2A.
- **Tools and MCP:** function tools, confirmation, OpenAPI, built-in tools and their limits, code execution, Skills, MCP Toolbox.
- **Sessions, state and memory:** state templating, memory tools, SQLite sessions, rewind, artifacts.
- **Callbacks and plugins:** callbacks, error callbacks, plugins, reflect-and-retry, logging.
- **Runtime, context and apps:** `App`, context caching, resumability, `RunConfig` limits and streaming, cancellation, live streaming.
- **Models, serving, evaluation and upgrading:** LiteLLM and Claude, the dev UI, Cloud Run and Agent Engine deployment, evaluation, OpenTelemetry tracing, and what changes in 2.x.

Reference entries never become findings. They show in the Knowledge base with whether they fit the repo's locked version, and appear on the Releases page as what an upgrade unlocks. Coding agents can query them through the `search_knowledge` MCP tool (for example "persist sessions" with `path` set) before writing ADK code.

## State in your repo

Codewright keeps its state in `.codewright/`:

| File | Purpose | Commit it? |
| --- | --- | --- |
| `feedback.json` | Accepted, rejected and muted findings, mute rules, team constraints | yes, so the team shares it |
| `design.json` | The intended architecture from Design mode, checked by `codewright check` | yes |
| `config.json` | Model and offline mode | yes |
| `snapshots/`, `releases.json` | Map history and PyPI cache | no (ignored automatically) |

## Models

Review, the critic, proof, Map and Design's capacity math are deterministic and never call a model. A model only writes code-chat answers and drafts Design's data model; without one, chat answers are built from the code graph and still cite files and lines.

Models go through LiteLLM (`pip install "codewright[llm]"`). The default is `vertex_ai/gemini-2.5-flash`; set `CODEWRIGHT_MODEL` or change it in Settings. `CODEWRIGHT_OFFLINE=1` (or the Settings switch) guarantees no model calls.

## Development

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .   # lint and format (CI runs both)
.venv/bin/pytest -q                       # Python tests
cd web && npm install && npm run build    # rebuilds the UI into src/codewright/web
npm run dev                               # UI dev server, proxies /api to codewright serve on :8765
```

CI (`.github/workflows/ci.yml`) runs ruff, the tests on Python 3.11, 3.12 and 3.13, and the UI build on every pull request.

The UI follows one small design system: neutral charcoal greys in the dark theme, with blue kept for links, focus and selection (a white and link-blue light theme), Reddit Sans with JetBrains Mono (bundled, so nothing loads from the network), sharp corners and pill buttons. Colour marks state only (proof, diff lines, findings).
