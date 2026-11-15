"""Reports: terminal (rich), Markdown and JSON."""

from __future__ import annotations

import json

from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from .engine import ReviewResult
from .models import Finding, ProofStatus

PROOF_STYLE = {ProofStatus.VERIFIED: "green", ProofStatus.UNVERIFIED: "yellow", ProofStatus.BROKE_TESTS: "red"}


def to_json(result: ReviewResult) -> str:
    return json.dumps(
        {
            "root": result.stack.root,
            "python": result.stack.python,
            "dependencies": {k: d.version or d.specifier for k, d in sorted(result.stack.dependencies.items())},
            "files_scanned": len(result.graph.files),
            "intent_matches": result.total_matches,
            "findings": [f.to_dict() for f in result.findings],
            "rejected": [{"id": f.id, "title": f.title, "file": f.file, "reason": f.rejected_reason}
                         for f in result.rejected],
        },
        indent=2,
    )


def to_markdown(result: ReviewResult) -> str:
    out = [f"# Codewright review\n",
           f"{len(result.findings)} findings from {len(result.graph.files)} files "
           f"({result.total_matches} intent matches, {len(result.rejected)} dropped by the critic).\n"]
    for i, f in enumerate(result.findings, 1):
        out.append(f"## {i}. {f.title}\n")
        out.append(f"`{f.file}:{f.line}` in `{f.function}` · score {f.score} · proof: **{f.proof.value}**"
                   f"{' · hot path' if f.hot_path else ''}\n")
        out.append(f"- **Found:** {f.evidence}")
        out.append(f"- **Instead, use:** {f.suggestion}")
        out.append(f"- **API:** {f.api} (symbol check: {f.symbol_check})")
        out.append(f"- **Gain:** {f.gain}")
        out.append(f"- **Effort:** {f.effort}/5 · **Risk:** {f.risk}")
        out.append(f"- **When not to:** {f.when_not}")
        out.append(f"- **Scale threshold:** {f.threshold}")
        out.append(f"- **Proof:** {f.proof_detail}")
        out.append("- **Sources:** " + ", ".join(f"<{s}>" for s in f.sources) + "\n")
        if f.patch:
            out.append(f"```diff\n{f.patch.rstrip()}\n```\n")
        elif f.snippet:
            out.append(f"```{f.snippet_lang}\n{f.snippet.rstrip()}\n```\n")
    return "\n".join(out)


def print_terminal(result: ReviewResult, console: Console | None = None) -> None:
    console = console or Console()
    deps = ", ".join(f"{k} {d.version or d.specifier or ''}".strip()
                     for k, d in sorted(result.stack.dependencies.items())[:12])
    console.print(Panel.fit(
        f"[bold]{result.stack.root}[/]\n"
        f"Python {result.stack.python or 'unknown'} · {len(result.stack.dependencies)} dependencies"
        f"{' · ' + deps if deps else ''}\n"
        f"{len(result.graph.files)} files · {result.total_matches} intent matches · "
        f"{len(result.findings)} shown · {len(result.rejected)} dropped by the critic",
        title="Codewright review",
    ))
    if not result.findings:
        console.print("[green]No findings.[/] Nothing in this repo matched a capability card with proof-ready advice.")
        return

    table = Table(show_lines=False)
    for col in ("#", "Finding", "Where", "Proof", "Score"):
        table.add_column(col)
    for i, f in enumerate(result.findings, 1):
        table.add_row(str(i), f.title, f"{f.file}:{f.line}",
                      f"[{PROOF_STYLE[f.proof]}]{f.proof.value}[/]", f"{f.score}")
    console.print(table)
    for i, f in enumerate(result.findings, 1):
        console.print(_panel(i, f))


def _panel(i: int, f: Finding) -> Panel:
    body = Markdown(
        f"**Found:** {f.evidence}\n\n"
        f"**Instead, use:** {f.suggestion}\n\n"
        f"**API:** {f.api} · symbol check: {f.symbol_check}\n\n"
        f"**Gain:** {f.gain}\n\n"
        f"**Effort** {f.effort}/5 · **Risk:** {f.risk}\n\n"
        f"**When not to:** {f.when_not}\n\n"
        f"**Scale threshold:** {f.threshold}\n\n"
        f"**Proof:** {f.proof_detail}\n\n"
        "**Sources:** " + " · ".join(f.sources)
    )
    parts = [body]
    if f.patch:
        parts.append(Syntax(f.patch, "diff", theme="ansi_dark", word_wrap=True))
    elif f.snippet:
        parts.append(Syntax(f.snippet, f.snippet_lang, theme="ansi_dark", word_wrap=True))
    return Panel(Group(*parts), title=f"{i}. {f.title}", subtitle=f"{f.file}:{f.line} · {f.function}")


def to_pr_comment(result: ReviewResult) -> str:
    """Compact GitHub PR comment: one collapsible block per finding, changed code only."""
    scope = f" on lines changed since `{result.base}`" if result.base else ""
    if not result.findings:
        return f"### Codewright\n\nNo findings{scope}. "\
               f"{len(result.rejected)} candidate(s) were dropped by the critic.\n"
    out = [f"### Codewright: {len(result.findings)} suggestion(s){scope}\n",
           "| # | Suggestion | Where | Proof |", "| --- | --- | --- | --- |"]
    for i, f in enumerate(result.findings, 1):
        out.append(f"| {i} | {f.title} | `{f.file}:{f.line}` | {f.proof.value} |")
    out.append("")
    for i, f in enumerate(result.findings, 1):
        out.append(f"<details><summary><b>{i}. {f.title}</b> — <code>{f.file}:{f.line}</code></summary>\n")
        out.append(f"**Found:** {f.evidence}  \n**Instead:** {f.suggestion}  \n**API:** {f.api}  \n"
                   f"**Gain:** {f.gain}  \n**When not to:** {f.when_not}  \n**Proof:** {f.proof_detail}\n")
        if f.patch:
            out.append(f"```diff\n{f.patch.rstrip()}\n```")
        out.append("Sources: " + " · ".join(f"[{j + 1}]({s})" for j, s in enumerate(f.sources)))
        out.append("</details>\n")
    out.append("<sub>Codewright suggests; humans decide. Nothing is merged automatically.</sub>")
    return "\n".join(out)
