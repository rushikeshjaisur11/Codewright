"""Command-line interface: `codewright review`, `codewright stack`, `codewright graph`."""

from __future__ import annotations

import contextlib
import json
from enum import Enum
from pathlib import Path

import typer
from rich.console import Console

from . import __version__
from . import graph as graph_mod
from . import stack as stack_mod

app = typer.Typer(help="Codewright: version-aware, proven 'instead of X, use Y' suggestions.", no_args_is_help=True)
console = Console()


class Format(str, Enum):
    terminal = "terminal"
    markdown = "markdown"
    json = "json"
    pr = "pr"


@contextlib.contextmanager
def _clean_errors():
    """Expected failures (a bad git ref, a missing folder) print one line and exit 2, not a traceback."""
    try:
        yield
    except (RuntimeError, FileNotFoundError, ValueError) as exc:
        console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(2) from None


@app.command()
def review(
    path: Path = typer.Argument(Path("."), exists=True, file_okay=False, help="Repository to review."),
    top: int = typer.Option(10, help="Show at most this many findings (0 = all)."),
    proof: bool = typer.Option(True, "--proof/--no-proof", help="Run the test suite before and after each patch."),
    fmt: Format = typer.Option(Format.terminal, "--format", "-f", help="Output format."),
    output: Path | None = typer.Option(None, "--output", "-o", help="Write the report to a file."),
    python: str | None = typer.Option(None, help="Interpreter for symbol checks and tests (default: repo .venv)."),
    cards: list[Path] = typer.Option([], "--cards", help="Extra directory of capability card YAML files."),
    base: str | None = typer.Option(None, "--base", help="PR mode: only review code changed since this git ref."),
    jobs: int | None = typer.Option(
        None, "--jobs", "-j", min=1, help="Patched test runs to run at once (default: CPUs, at most 4)."
    ),
    full_suite: bool = typer.Option(
        False, "--full-suite", help="Run every test for each patch, not just the files that run or import it."
    ),
    build_env: bool = typer.Option(
        True,
        "--build-env/--no-build-env",
        help="Without a repo .venv, build (and cache) an environment from uv.lock or requirements to run the tests.",
    ),
) -> None:
    """Review a repo: ranked, proven 'instead of X, use Y' findings."""
    from .engine import review as run_review
    from .report import print_terminal, to_json, to_markdown, to_pr_comment

    with console.status("Scanning..."), _clean_errors():
        result = run_review(
            path,
            run_proof=proof,
            top=top or None,
            python=python,
            extra_cards=cards or None,
            base=base,
            proof_jobs=jobs,
            full_suite=full_suite,
            build_env=build_env,
        )
    if fmt is Format.terminal and output is None:
        print_terminal(result, console)
        return
    text = {Format.json: to_json, Format.pr: to_pr_comment}.get(fmt, to_markdown)(result)
    if output:
        output.write_text(text)
        console.print(f"Wrote {output}")
    else:
        print(text)


@app.command()
def stack(path: Path = typer.Argument(Path("."), exists=True, file_okay=False)) -> None:
    """Show the discovered stack: Python version, dependencies and where each came from."""
    st = stack_mod.discover(path)
    console.print(f"Python: {st.python or 'unknown'}   sources: {', '.join(st.sources) or 'none'}")
    if st.docker_images:
        console.print(f"Docker images: {', '.join(st.docker_images)}")
    for name, dep in sorted(st.dependencies.items()):
        console.print(f"  {name:32} {dep.version or '-':14} {dep.specifier or '':18} [dim]{dep.source}[/]")


@app.command()
def graph(
    path: Path = typer.Argument(Path("."), exists=True, file_okay=False),
    as_json: bool = typer.Option(False, "--json", help="Print the graph as JSON."),
) -> None:
    """Show the code graph: functions, call edges and entry points."""
    g = graph_mod.build(path)
    hot = g.hot_functions()
    if as_json:
        print(
            json.dumps(
                {
                    "files": sorted(g.files),
                    "functions": [g.key(fn) for _, fn in g.functions()],
                    "edges": {k: sorted(v) for k, v in sorted(g.edges.items())},
                    "entry_points": sorted(g.entry_points),
                },
                indent=2,
            )
        )
        return
    console.print(
        f"{len(g.files)} files, {sum(1 for _ in g.functions())} functions, "
        f"{sum(len(v) for v in g.edges.values())} call edges, {len(g.entry_points)} entry points"
    )
    for _, fn in g.functions():
        key = g.key(fn)
        tag = " [green]entry[/]" if key in g.entry_points else (" [yellow]hot[/]" if key in hot else "")
        console.print(f"  {key}{tag}")


@app.command()
def serve(
    path: Path = typer.Argument(Path("."), exists=True, file_okay=False, help="Repository to open first."),
    host: str = typer.Option("127.0.0.1", help="Interface to bind. Keep 127.0.0.1 unless you trust the network."),
    port: int = typer.Option(8765, help="Port."),
    allow_host: list[str] = typer.Option(
        [], "--allow-host", help="Host name the browser may use to reach the app (repeatable). Default: localhost only."
    ),
) -> None:
    """Start the web app: Review, Map, Design and code chat in the browser."""
    import uvicorn

    from .server import LOOPBACK_HOSTS, create_app

    hosts = list(LOOPBACK_HOSTS) + list(allow_host)
    if host not in ("127.0.0.1", "localhost", "::1") and not allow_host:
        console.print(
            f"[yellow]Bound to {host}: only requests addressed to localhost are served. "
            "Pass --allow-host <name> for each name other machines use to reach it.[/]"
        )
    console.print(f"Codewright on [bold]http://{host}:{port}[/]  (repo: {path.resolve()})")
    uvicorn.run(create_app([str(path)], allowed_hosts=hosts), host=host, port=port, log_level="warning")


@app.command()
def mcp() -> None:
    """Run the MCP server on stdio, for coding agents."""
    from .mcp_server import main

    main()


@app.command()
def check(path: Path = typer.Argument(Path("."), exists=True, file_okay=False)) -> None:
    """Decisions as CI checks: exit 1 when the code drifts from .codewright/design.json."""
    from .service import Workspace

    ws = Workspace(path.resolve())
    d = ws.drift()
    if d is None:
        console.print("No intended architecture in .codewright/design.json; nothing to check.")
        raise typer.Exit(0)
    if d["ok"]:
        console.print(f"[green]OK[/] code matches the {d['design']} design.")
        raise typer.Exit(0)
    console.print(f"[red]Drift[/] from the {d['design']} design:")
    for u in d["unplanned"]:
        console.print(f"  unplanned {u['kind']}: {', '.join(u['systems'])}")
    for v in d["violations"]:
        console.print(f"  {v['file']}: {v['rule']} ({v['reason']})")
    for m in d["not_built"]:
        console.print(f"  [dim]not built yet: {', '.join(m['components'])}[/]")
    raise typer.Exit(1)


@app.command()
def watch(
    path: Path = typer.Argument(Path("."), exists=True, file_okay=False),
    rescan: bool = typer.Option(False, help="Re-run the review when a dependency has a new release."),
) -> None:
    """Release watcher: newer dependency versions and the cards an upgrade unlocks."""
    from . import watcher

    st = stack_mod.discover(path)
    rows = watcher.check(st, path)
    for r in rows:
        if r["bump"] in ("current",):
            continue
        new = " [yellow]NEW[/]" if r["new_since_last_check"] else ""
        unlock = ("  unlocks: " + "; ".join(u["title"] for u in r["unlocks"])) if r["unlocks"] else ""
        console.print(
            f"  {r['name']:28} {r['installed'] or '-':12} -> {r['latest'] or 'offline':12} {r['bump']}{new}{unlock}"
        )
    if rescan and any(r["new_since_last_check"] for r in rows):
        from .engine import review as run_review
        from .report import print_terminal

        print_terminal(run_review(path), console)


@app.command("map")
def map_cmd(
    path: Path = typer.Argument(Path("."), exists=True, file_okay=False),
    level: str = typer.Option("system", help="system | module | flow"),
    target: str = typer.Option("", help="Component (module level) or entry point id (flow level)."),
) -> None:
    """Print the architecture map as Mermaid."""
    from .archmap import ArchMap

    m = ArchMap(graph_mod.build(path))
    try:
        view = m.view(level, target)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from None
    print(view.mermaid())


@app.command()
def feedback(
    finding_id: str,
    status: str = typer.Argument(..., help="accepted | rejected | muted | clear"),
    path: Path = typer.Option(Path("."), exists=True, file_okay=False),
    reason: str = typer.Option("", help="Why (helps the team and the ranking)."),
) -> None:
    """Record a verdict on a finding; it tunes ranking for that intent in this repo."""
    from .service import Workspace

    ws = Workspace(path.resolve())
    try:
        stats = ws.feedback(finding_id, status, reason)
    except KeyError:
        console.print(f"No finding {finding_id}. Run `codewright review` to see ids.")
        raise typer.Exit(1) from None
    rate = stats["acceptance_rate"]
    console.print(f"Recorded. Acceptance rate: {'-' if rate is None else f'{rate:.0%}'}")


@app.command()
def version() -> None:
    """Print the Codewright version."""
    print(__version__)


if __name__ == "__main__":
    app()
