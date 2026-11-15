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

The web app only answers requests addressed to `localhost`, and refuses requests sent from other websites, so a page you have open can't read your code through it. To reach it from another machine, bind it with `--host 0.0.0.0` and name each host it's reached by with `--allow-host`.

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
codewright review . -f sarif -o codewright.sarif   # for GitHub code scanning and other SARIF viewers
codewright review . --base origin/main -f pr -o comment.md   # PR mode: changed code only
codewright check .                          # exit 1 when code drifts from .codewright/design.json
codewright map . --level flow               # Mermaid diagram of a request flow
codewright watch . --rescan                 # new dependency releases; re-review when one appears
codewright feedback <finding-id> rejected --reason "not worth the churn"
codewright stack .                          # what versions it thinks you run
codewright graph .                          # functions, call edges and entry points
codewright mcp                              # MCP server on stdio for coding agents
codewright cards verify --python .venv/bin/python --links   # every card's API exists in the installed version; sources resolve
```

### In CI

The repository is also a GitHub Action. On pull requests it reviews only the lines the PR changed, shows findings as code-scanning alerts on the diff, and writes a summary with the suggested patches to the job page:

```yaml
on: pull_request
permissions:
  contents: read
  security-events: write   # to upload code-scanning alerts
jobs:
  codewright:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: rushikeshjaisur11/codewright@main
        # with: { proof: "true" }   # also run the tests before and after each patch
```

Or call the CLI yourself:

```yaml
- run: pip install "codewright @ git+https://github.com/rushikeshjaisur11/codewright"
- run: codewright review . --base origin/${{ github.base_ref }} -f pr -o codewright.md --sarif-output codewright.sarif
- run: codewright check .
```

Codewright suggests; it never merges.

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
| 3. Intent detection | `intents/` | Labels functions with the engineering intent they implement by hand (34 intents) |
| 4. Capability cards | `cards/data/*.yaml` | Per library and version: the API, gain, effort, risk, "when not to", scale threshold and sources. Preference: already installed, then stdlib, then platform, then a new dependency |
| 5. Suggestion | `suggest.py` | Picks the best card and writes the smallest safe diff when the code's shape allows it |
| 6. Critic | `critic.py` | Drops findings missing a file, an API version, a gain, effort, risk, threshold, "when not to" or a source, and checks that the suggested symbol exists in the repo's own interpreter. A symbol whose library isn't installed there, or whose module needs an optional extra, counts as unchecked, not missing |
| 7. Proof | `proof.py`, `proof_env.py` | Runs `pytest` in a sandbox copy of the repo (with its `.git`, so tests that call git work) before and after the patch. The tests run with the repo's own dependencies: `--python`, else the repo's `.venv`, else an environment built once from `uv.lock` or `requirements*.txt` with uv and cached in `~/.codewright/envs` (nothing is written into the repo). The baseline run records which tests actually execute each function (`sys.monitoring` on 3.12+, a profile hook on 3.11). **Verified** only when the tests pass both times *and* a test ran the changed function, and the finding names that test. Each patch then runs just the test files that run the function or import its module, directly or through other modules (`--full-suite` runs everything), in parallel (`--jobs`); a run that fails in parallel is re-run alone before the patch is blamed. When the baseline fails, the finding says why: a failing test, tests that can't import the project, or no tests collected. A patch that breaks tests is dropped |
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
| Google ADK 2.x agents (18 intents) | See [Google ADK](#google-adk) | 3 of them |
| `@app.on_event` | FastAPI lifespan | |
| `session.query()` on SQLAlchemy 2.x | `select()` | |
| Manual counting, grouping, thread fan-out | `Counter`, `defaultdict`, `ThreadPoolExecutor` | |
| `os.system` | `subprocess.run` with an argument list | yes, for a plain command whose result is unused |
| `+=` string building in a loop | `"".join()` | |

Add your own cards: put YAML files in a folder and pass `--cards that/folder`. Each card needs `sources` and a `when_not`, or the critic drops its findings.

`codewright cards verify` checks every card: required fields, a detector for its intent, and examples that compile. With `--python` it also checks each card's API exists in an interpreter that has the library installed, and with `--links` that its sources still resolve. CI runs it weekly against the latest google-adk (`.github/workflows/cards.yml`), so a renamed API or a dead link turns the job red.

### Google ADK

For agents built on [google-adk](https://pypi.org/project/google-adk/), Codewright carries a knowledge base of 68 cards for **ADK 2.x**: 30 with a detector and 38 reference entries. ADK 1.x is not covered. It has had no release since 1.39.1 (August 2026), so on a 1.x repo the only ADK suggestion is the upgrade, with what breaks on the way.

A card whose lowest version is above 2.0 starts at the first 2.x release that has the API. That was found by checking the source of every 2.x minor release on PyPI. Every symbol was imported from google-adk 2.14.1, and every source was opened. Claims about behaviour were run against the real package: for example, that a `session.state` write on a fetched session is lost while `state_delta` persists. The detectors were run on Google's [adk-samples](https://github.com/google/adk-samples); the false positives found there were fixed, and the findings that remain are genuine. One of them: the `deep-search` sample builds its instruction with an f-string that reads today's date once, at import.

| Found in your code | Suggests | Auto-patch |
| --- | --- | --- |
| Any ADK app on google-adk 1.x | Upgrade to 2.x: extras to add, the event schema change, what to re-test | |
| Sync `Runner.run` inside `async def` | `async for` over `Runner.run_async` | yes |
| Agent instruction f-string that calls something at import | An instruction function, or `{key}` state placeholders | yes |
| `SequentialAgent`/`ParallelAgent`/`LoopAgent`, or a hand-written driver loop | `Workflow` graphs | |
| In-memory session, artifact or memory service in a module that serves requests | `DatabaseSessionService`, `GcsArtifactService`, `VertexAiMemoryBankService` | |
| `session.state[k] = v` on a session from the session service | `state_delta=` on `run_async`, or `EventActions(state_delta=...)` | |
| `session.events.append(...)` | Yield the event; 2.x's graph engine must emit it | |
| A tool catching `BaseException` or a bare `except:` without re-raising | Specific exceptions plus a node `RetryConfig`; `BaseException` swallows 2.x's pause for human input | |
| A tool writing to a module-level dict | `tool_context.state` with `user:`/`app:`/`temp:` scopes | |
| `json.loads(event.content.parts[0].text)` on the agent's reply | `output_schema=` + `output_key=` | |
| A tool that runs another agent through its own Runner | `AgentTool` | |
| A hand-written MCP client | `McpToolset` | |
| `session.events[-n:]` | `EventsCompactionConfig` | |
| A web route that builds the Runner or agent per request | Build once at startup | |
| A web route that drives a Runner by hand | `get_fast_api_app` | |
| A tool polling a job in a sleep loop | `LongRunningFunctionTool` + resumability | |
| A retry loop around an agent run | `Gemini(retry_options=HttpRetryOptions(...))` | |
| Deprecated APIs: `global_instruction`, `Runner(plugins=)`, `VertexAiCodeExecutor` (2.12+), `save_live_audio`, `ApiRegistry` (2.7.1+) | Their replacements | `save_live_audio` |
| An import that needs an extra the lockfile lacks: `mcp`, `db`, `gcp`, `extensions`, `a2a` | The `google-adk[extra]` to add | |

The reference entries cover the rest of ADK 2.x, grouped by topic:
- **Agents and orchestration:** sub-agents, custom agents, Workflow routes, human input and retries, planners, A2A.
- **Tools and MCP:** function tools, confirmation, OpenAPI, built-in tools and their limits, code execution, Skills, MCP Toolbox.
- **Sessions, state and memory:** state templating, memory tools, SQLite sessions, rewind, artifacts.
- **Callbacks and plugins:** callbacks, error callbacks, plugins, reflect-and-retry, logging.
- **Runtime, context and apps:** `App`, context caching, resumability, `RunConfig` limits and streaming, cancellation (2.11+), live streaming.
- **Models, serving and evaluation:** LiteLLM and Claude, the dev UI, Cloud Run and Agent Engine deployment, evaluation, OpenTelemetry tracing.

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
