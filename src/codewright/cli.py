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
    sarif = "sarif"


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
    proof: bool | None = typer.Option(
        None, "--proof/--no-proof", help="Run the tests before and after each patch (default: on; off with --watch)."
    ),
    fmt: Format = typer.Option(Format.terminal, "--format", "-f", help="Output format."),
    output: Path | None = typer.Option(None, "--output", "-o", help="Write the report to a file."),
    sarif_output: Path | None = typer.Option(
        None, "--sarif-output", help="Also write SARIF (for GitHub code scanning) to this file, from the same run."
    ),
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
    watch: bool = typer.Option(
        False, "--watch", "-w", help="Keep running: re-review on every save and print new and resolved findings."
    ),
    interval: float = typer.Option(1.0, "--interval", min=0.2, help="With --watch: seconds between checks for saves."),
) -> None:
    """Review a repo: ranked, proven 'instead of X, use Y' findings."""
    from .engine import review as run_review
    from .report import print_terminal, to_json, to_markdown, to_pr_comment, to_sarif

    if watch:
        _watch(path, interval, proof=bool(proof), python=python, cards=cards or None, base=base)
        return
    proof = True if proof is None else proof

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
    if sarif_output:
        sarif_output.write_text(to_sarif(result))
    if fmt is Format.terminal and output is None:
        print_terminal(result, console)
        return
    text = {Format.json: to_json, Format.pr: to_pr_comment, Format.sarif: to_sarif}.get(fmt, to_markdown)(result)
    if output:
        output.write_text(text)
        console.print(f"Wrote {output}")
    else:
        print(text)


def _watch(path: Path, interval: float, **opts) -> None:
    """`review --watch`: a review on every save. Proof is off unless --proof is given explicitly."""
    import time

    from .engine import review as run_review
    from .service import diff_findings, snapshot

    def run():
        return run_review(
            path,
            run_proof=opts["proof"],
            top=None,
            python=opts["python"],
            extra_cards=opts["cards"],
            base=opts["base"],
        )

    seen: dict = {}
    snap: dict = {}
    console.print(f"Watching [bold]{path.resolve()}[/] for saves (Ctrl+C to stop)")
    try:
        while True:
            current = snapshot(path)
            if current != snap:
                changed = sorted(
                    {p for p in current.keys() ^ snap.keys()}
                    | {p for p in current if snap.get(p) not in (None, current[p])}
                )
                with _clean_errors():
                    seen, new, gone = diff_findings(seen, run().findings)
                stamp = time.strftime("%H:%M:%S")
                what = f"{len(changed)} file(s) changed" if snap else "first review"
                console.print(f"[dim]{stamp}[/] {what}: {len(seen)} finding(s)")
                for f in new:
                    console.print(
                        f"  [green]+[/] {f.file}:{f.line}  {f.title}" + ("  [blue](auto-patch)[/]" if f.patch else "")
                    )
                for f in gone:
                    console.print(f"  [dim]- resolved: {f.file}:{f.line}  {f.title}[/]")
                snap = current
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("Stopped.")


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


mcp_app = typer.Typer(
    help="MCP server for coding agents: local (stdio) or one shared server for a team (--http).",
    invoke_without_command=True,
)
app.add_typer(mcp_app, name="mcp")


@mcp_app.callback()
def mcp(
    ctx: typer.Context,
    http: bool = typer.Option(False, "--http", help="Serve over Streamable HTTP at /mcp instead of stdio."),
    host: str = typer.Option("127.0.0.1", help="Address to bind with --http (0.0.0.0 to serve a team)."),
    port: int = typer.Option(8765, help="Port for --http."),
    root: list[Path] = typer.Option([], "--root", help="Repository the shared server serves (repeatable)."),
    token: list[str] = typer.Option(
        [], "--token", help="Accepted bearer token (repeatable). Or CODEWRIGHT_MCP_TOKENS."
    ),
    git_host: list[str] = typer.Option(
        [], "--git-host", help="Git host callers may name (repeatable). Default: github.com, gitlab.com, bitbucket.org."
    ),
    no_git: bool = typer.Option(False, "--no-git", help="Don't clone git URLs; only --root repos and review_files."),
    allow_proof: bool = typer.Option(
        False, "--allow-proof", help="Let callers run proof (it runs repos' tests and installs their dependencies)."
    ),
    public_host: list[str] = typer.Option(
        [], "--public-host", help="Domain the server is reached at (repeatable); other Host headers are refused."
    ),
    no_auth: bool = typer.Option(False, "--no-auth", help="Serve --http on a non-local address without tokens."),
    sso_issuer: str = typer.Option(
        "", "--sso-issuer", help="Sign in through your identity provider: its OIDC issuer URL (Okta, Entra ID...)."
    ),
    sso_audience: str = typer.Option(
        "", "--sso-audience", help="Audience access tokens must name (default: --public-url)."
    ),
    public_url: str = typer.Option(
        "", "--public-url", help="This server's public /mcp URL (needed with --sso-issuer)."
    ),
    sso_scope: list[str] = typer.Option([], "--sso-scope", help="Scope every token must carry (repeatable)."),
    allowed_domain: list[str] = typer.Option(
        [], "--allowed-domain", help="Only users with an email in this domain (repeatable)."
    ),
) -> None:
    """Run the MCP server: on stdio for one agent, or with --http as a shared server for a team."""
    if ctx.invoked_subcommand is not None:
        return
    import os

    from .mcp_server import is_loopback, main
    from .remote import DEFAULT_GIT_HOSTS, Policy

    if not http:
        main()
        return
    tokens = [*token, *[t for t in os.environ.get("CODEWRIGHT_MCP_TOKENS", "").split(",") if t.strip()]]
    tokens = [t.strip() for t in tokens]
    if any(len(t) < 16 for t in tokens):
        console.print("[red]Tokens must be at least 16 characters; `codewright mcp token` makes one.[/]")
        raise typer.Exit(2)
    sso = None
    if sso_issuer:
        from .sso import SSOVerifier

        if not public_url.startswith("https://") and not is_loopback(host):
            console.print(
                "[red]--sso-issuer needs --public-url, the https URL clients use, e.g. https://cw.example.com/mcp[/]"
            )
            raise typer.Exit(2)
        public_url = public_url or f"http://{host}:{port}/mcp"
        sso = SSOVerifier(sso_issuer, sso_audience or public_url, list(allowed_domain), tokens)
    if not tokens and not sso and not is_loopback(host) and not no_auth:
        console.print(
            f"[red]Refusing to serve {host} without a token.[/] Pass --token (make one with `codewright mcp token`) "
            "or set CODEWRIGHT_MCP_TOKENS; --no-auth only if something in front of it authenticates."
        )
        raise typer.Exit(2)
    roots = [r.expanduser().resolve() for r in root]
    for r in roots:
        if not r.is_dir():
            console.print(f"[red]not a folder: {r}[/]")
            raise typer.Exit(2)
    hosts = () if no_git else tuple(h.lower() for h in git_host) or DEFAULT_GIT_HOSTS
    policy = Policy(shared=True, roots=roots, git_hosts=hosts, allow_proof=allow_proof)
    where = f"http://{host}:{port}/mcp"
    guard = "sign-in via " + sso_issuer if sso else ("token required" if tokens else "no token")
    console.print(f"Codewright MCP on [bold]{where}[/] ({guard}{' + tokens' if sso and tokens else ''})")
    console.print(
        f"  repos: {', '.join(r.name for r in roots) or 'none'}; git: {', '.join(hosts) or 'off'}; "
        f"proof: {'on' if allow_proof else 'off'}"
    )
    console.print("  connect an agent: `codewright mcp config --url <public URL>`")
    main(
        http=True,
        host=host,
        port=port,
        tokens=tokens,
        policy=policy,
        public_hosts=public_host or None,
        sso=sso,
        public_url=public_url,
        required_scopes=list(sso_scope),
    )


@mcp_app.command("token")
def mcp_token() -> None:
    """Print a new random bearer token for --token / CODEWRIGHT_MCP_TOKENS."""
    import secrets

    print(secrets.token_urlsafe(32))


@mcp_app.command("config")
def mcp_config(
    url: str = typer.Option(
        "",
        "--url",
        help="Shared server URL, e.g. https://codewright.example.com/mcp. Without it: the local stdio setup.",
    ),
    token_env: str = typer.Option("CODEWRIGHT_TOKEN", help="Environment variable that holds your token."),
) -> None:
    """Print copy-paste setup for Claude Code, Cursor, VS Code and other MCP clients."""
    from .mcp_setup import client_configs

    for title, text in client_configs(url, token_env):
        console.print(f"[bold]{title}[/]")
        print(text)
        print()


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
    unused: bool = typer.Option(False, "--unused", help="Also draw modules the running system never loads."),
) -> None:
    """Print the architecture map as Mermaid."""
    from .archmap import ArchMap

    m = ArchMap(graph_mod.build(path), include_unused=unused)
    try:
        view = m.view(level, target)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from None
    print(view.mermaid())


@app.command()
def agents(
    path: Path = typer.Argument(Path("."), exists=True, file_okay=False),
    mermaid: bool = typer.Option(False, "--mermaid", help="Print the agent architecture as Mermaid instead."),
) -> None:
    """Files that need a model, and the agent architecture (agents, tools, models, who runs them)."""
    from .aimap import AiMap
    from .archmap import Edge, Node, View

    m = AiMap(graph_mod.build(path))
    arch = m.architecture()
    if mermaid:
        keep = {"id", "label", "kind", "files", "detail"}
        view = View(
            "ai",
            "Agents",
            [Node(**{k: v for k, v in n.items() if k in keep}) for n in arch["nodes"]],
            [Edge(e["source"], e["target"], e["label"]) for e in arch["edges"]],
        )
        print(view.mermaid())
        return
    files = m.files()
    if not files:
        console.print("No LLM SDK or agent framework is used in this repo.")
        return
    console.print(f"[bold]{len(files)} files use AI[/]")
    for f in files:
        what = ", ".join(f.roles)
        extra = (f.agents and f" agents: {', '.join(f.agents)}") or (f.tools and f" tools: {', '.join(f.tools)}") or ""
        console.print(f"  {f.file}  [dim]{what}{extra}[/]")
    for a in m.agents.values():
        console.print(f"[bold]{a.name}[/] [dim]{a.framework} · {a.file}:{a.line}[/]")
        if a.model:
            console.print(f"  model: {a.model}")
        for child in a.children:
            console.print(f"  delegates to: {m.agents[child].name}")
        for t in a.tools:
            console.print(f"  tool: {t['label']}")


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


cards_app = typer.Typer(help="Inspect and verify the knowledge base (capability and reference cards).")
app.add_typer(cards_app, name="cards")


@cards_app.command("verify")
def cards_verify(
    python: str | None = typer.Option(
        None, "--python", help="Interpreter with the libraries installed: each card's symbol must exist there."
    ),
    library: str = typer.Option("", "--library", help="Only cards for this library (e.g. google-adk)."),
    links: bool = typer.Option(False, "--links", help="Also check that every source URL resolves."),
    cards: list[Path] = typer.Option([], "--cards", help="Extra directory of card YAML files to include."),
) -> None:
    """Check every card's shape, and optionally its symbol against an installed library and its links."""
    from .cardcheck import verify

    with console.status("Verifying cards..."):
        report = verify(extra_dirs=cards or None, library=library, python=python, links=links)
    for line in report.skipped:
        console.print(f"[dim]skip[/] {line}")
    for line in report.problems:
        console.print(f"[red]FAIL[/] {line}")
    summary = f"{report.checked} cards checked, {len(report.problems)} problem(s), {len(report.skipped)} skipped"
    console.print(("[green]OK[/] " if report.ok else "[red]FAILED[/] ") + summary)
    if not report.ok:
        raise typer.Exit(1)


index_app = typer.Typer(
    help="The package index for release lookups and environment builds: PyPI by default, or your own "
    "(Artifactory, Nexus, devpi...).",
    invoke_without_command=True,
)
app.add_typer(index_app, name="index")
REPO_OPT = typer.Option(
    None, "--repo", help="Save for this repo (committed, so no credentials) instead of this machine."
)


@index_app.callback()
def index_show(ctx: typer.Context, path: Path = typer.Option(Path("."), "--path", help="Repo to resolve for.")) -> None:
    """Show which index Codewright uses here, and where that setting comes from."""
    if ctx.invoked_subcommand is not None:
        return
    from . import index as idx

    current = idx.resolve(path)
    console.print(f"{current.display}  [dim]({current.source})[/]")


@index_app.command("set")
def index_set(
    url: str = typer.Argument(
        ..., help="Simple API URL, e.g. https://artifactory.example.com/artifactory/api/pypi/pypi/simple"
    ),
    repo: Path | None = REPO_OPT,
    test: bool = typer.Option(True, help="Look a package up on the index before saving."),
) -> None:
    """Use your own index. Credentials: in the URL (machine setting only), ~/.netrc, or CODEWRIGHT_INDEX_TOKEN."""
    from . import index as idx

    if err := idx.validate(url):
        console.print(f"[red]{err}[/]")
        raise typer.Exit(2)
    if test:
        result = idx.check(idx.PackageIndex(url, "new"))
        console.print(("[green]OK[/] " if result["ok"] else "[red]FAILED[/] ") + result["detail"])
        if not result["ok"]:
            console.print("Not saved. Fix the URL or credentials, or pass --no-test to save anyway.")
            raise typer.Exit(1)
    try:
        idx.save(url, root=repo)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from None
    console.print(f"Saved for {'repo ' + str(repo) if repo else 'this machine'}: {idx.redact(url)}")


@index_app.command("reset")
def index_reset(repo: Path | None = REPO_OPT) -> None:
    """Remove the setting (for this machine, or a repo with --repo); with none left, PyPI is used."""
    from . import index as idx

    idx.save("", root=repo)
    console.print(f"Now using {idx.resolve(repo).display} ({idx.resolve(repo).source})")


@index_app.command("test")
def index_test(
    package: str = typer.Argument("pip", help="Package to look up."),
    path: Path = typer.Option(Path("."), "--path", help="Repo to resolve the index for."),
) -> None:
    """Check that Codewright can read the configured index."""
    from . import index as idx

    result = idx.check(idx.resolve(path), package)
    console.print(("[green]OK[/] " if result["ok"] else "[red]FAILED[/] ") + result["detail"])
    if not result["ok"]:
        raise typer.Exit(1)


@app.command()
def version() -> None:
    """Print the Codewright version."""
    print(__version__)


if __name__ == "__main__":
    app()
