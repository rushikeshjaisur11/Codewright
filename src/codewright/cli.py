"""Command-line interface: `codewright review`, `codewright stack`, `codewright graph`."""

from __future__ import annotations

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


@app.command()
def review(
    path: Path = typer.Argument(Path("."), exists=True, file_okay=False, help="Repository to review."),
    top: int = typer.Option(10, help="Show at most this many findings (0 = all)."),
    proof: bool = typer.Option(True, "--proof/--no-proof", help="Run the test suite before and after each patch."),
    fmt: Format = typer.Option(Format.terminal, "--format", "-f", help="Output format."),
    output: Path | None = typer.Option(None, "--output", "-o", help="Write the report to a file."),
    python: str | None = typer.Option(None, help="Interpreter for symbol checks and tests (default: repo .venv)."),
    cards: list[Path] = typer.Option([], "--cards", help="Extra directory of capability card YAML files."),
) -> None:
    """Review a repo: ranked, proven 'instead of X, use Y' findings."""
    from .engine import review as run_review
    from .report import print_terminal, to_json, to_markdown

    with console.status("Scanning..."):
        result = run_review(path, run_proof=proof, top=top or None, python=python, extra_cards=cards or None)
    if fmt is Format.terminal and output is None:
        print_terminal(result, console)
        return
    text = to_json(result) if fmt is Format.json else to_markdown(result)
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
        print(json.dumps({
            "files": sorted(g.files),
            "functions": [g.key(fn) for _, fn in g.functions()],
            "edges": {k: sorted(v) for k, v in sorted(g.edges.items())},
            "entry_points": sorted(g.entry_points),
        }, indent=2))
        return
    console.print(f"{len(g.files)} files, {sum(1 for _ in g.functions())} functions, "
                  f"{sum(len(v) for v in g.edges.values())} call edges, {len(g.entry_points)} entry points")
    for _, fn in g.functions():
        key = g.key(fn)
        tag = " [green]entry[/]" if key in g.entry_points else (" [yellow]hot[/]" if key in hot else "")
        console.print(f"  {key}{tag}")


@app.command()
def version() -> None:
    """Print the Codewright version."""
    print(__version__)


if __name__ == "__main__":
    app()
