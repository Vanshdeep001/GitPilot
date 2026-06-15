"""Rich formatting helpers. Use these everywhere — never plain print()."""

from __future__ import annotations

import sys

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# Windows consoles default to cp1252, which cannot encode the box-drawing,
# block, emoji, and em-dash characters used below. Force UTF-8 so output never
# crashes with UnicodeEncodeError when piped or run on a legacy code page.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

console = Console()


def success(msg: str) -> None: console.print(f"[bold green]✓[/] {msg}")
def error(msg: str) -> None:   console.print(f"[bold red]✗[/] {msg}")
def warning(msg: str) -> None: console.print(f"[bold yellow]⚠[/] {msg}")
def info(msg: str) -> None:    console.print(f"[bold blue]ℹ[/] {msg}")
def agent(msg: str) -> None:   console.print(f"[bold purple]◆[/] {msg}")
def blocked(msg: str) -> None: console.print(f"[bold red]\U0001f6ab[/] {msg}")
def pending(msg: str) -> None: console.print(f"[bold cyan]⏳[/] {msg}")


RISK_COLORS = {
    "very-low": "bright_green", "low": "green",
    "moderate": "yellow", "medium": "yellow", "high": "red",
    "very-high": "bold red", "critical": "bold red on dark_red",
}


def risk_badge(level: str) -> str:
    color = RISK_COLORS.get(level, "white")
    return f"[{color}] {level.upper()} [/]"


def confidence_bar(score: int) -> str:
    """Visual confidence bar — always caps at 95%."""
    score = min(score, 95)
    filled = score // 10
    bar = "█" * filled + "░" * (10 - filled)
    color = "green" if score > 80 else "yellow" if score > 60 else "red"
    return f"[{color}]{bar}[/] {score}%"


def operation_panel(analysis) -> Panel:
    """Main display for Git Operations Intelligence — a rich bordered panel."""
    lines = [
        f"[bold]Command:[/]    {analysis.command}",
        f"[bold]Risk:[/]       {risk_badge(analysis.risk_level)}",
        f"[bold]Will work:[/]  {analysis.will_succeed}",
        f"[bold]Confidence:[/] {confidence_bar(analysis.confidence)}",
    ]
    if analysis.prediction:
        lines += ["", "[bold]What will happen:[/]", analysis.prediction]
    if analysis.warnings:
        lines += ["", "[bold yellow]⚠ Warnings:[/]"] + [f"  • {w}" for w in analysis.warnings]
    if analysis.recommended_steps:
        lines += ["", "[bold]Recommended steps:[/]"] + \
                 [f"  {i}. {s}" for i, s in enumerate(analysis.recommended_steps, 1)]
    if analysis.alternatives:
        lines += ["", "[bold]Alternatives:[/]"] + [f"  • {a}" for a in analysis.alternatives]
    if analysis.educational_note:
        lines += ["", f"[dim]ℹ {analysis.educational_note}[/]"]
    return Panel("\n".join(lines), title="GitPilot — Operation Analysis",
                 border_style=RISK_COLORS.get(analysis.risk_level, "white"), box=box.ROUNDED)


def pr_table(prs: list) -> Table:
    table = Table(title="Open PRs", box=box.SIMPLE_HEAVY)
    table.add_column("PR", style="cyan")
    table.add_column("Recommendation")
    table.add_column("Blocking / Warnings")
    table.add_column("Confidence")
    for rec in prs:
        notes = "; ".join(rec.blocking_reasons + rec.warnings) or "-"
        table.add_row(f"#{rec.pr_number}", rec.recommendation, notes, confidence_bar(rec.confidence))
    return table


def branch_table(branches: list) -> Table:
    table = Table(title="Branch Health", box=box.SIMPLE_HEAVY)
    table.add_column("Branch", style="cyan")
    table.add_column("Age (days)")
    table.add_column("Classification")
    table.add_column("Risk")
    table.add_column("Recommendation")
    for b in branches:
        table.add_row(b.name, str(b.age_days), b.classification,
                      risk_badge(b.risk_level), b.recommendation)
    return table


def conflict_panel(analysis) -> Panel:
    """Conflict analysis — always includes the 'SUGGESTION ONLY' label."""
    lines = ["[bold red]SUGGESTION ONLY — Human review and implementation required[/]", "",
             f"Overall risk: {risk_badge(analysis.overall_risk)}",
             f"Confidence: {confidence_bar(analysis.overall_confidence)}",
             f"Estimated human time: {analysis.estimated_human_time}", ""]
    for f in analysis.per_file_analysis:
        lines += [f"[bold cyan]{f.file_path}[/] ({f.language}) — {f.classification}, "
                  f"risk {f.risk}", f"  {f.explanation}"]
        if f.human_review_notes:
            lines.append(f"  [yellow]Verify:[/] {f.human_review_notes}")
    return Panel("\n".join(lines), title=f"Conflict Analysis — PR #{analysis.pr_number}",
                 border_style="yellow", box=box.ROUNDED)


def release_panel(readiness) -> Panel:
    lines = [f"[bold]Readiness:[/] {readiness.readiness}",
             f"[bold]Current version:[/] {readiness.current_version}",
             f"[bold]Suggested version:[/] {readiness.suggested_version or 'n/a'}",
             f"[bold]Confidence:[/] {confidence_bar(readiness.confidence)}"]
    if readiness.blocking_issues:
        lines += ["", "[bold red]Blocking issues:[/]"] + [f"  • {i}" for i in readiness.blocking_issues]
    if readiness.warnings:
        lines += ["", "[bold yellow]Warnings:[/]"] + [f"  • {w}" for w in readiness.warnings]
    if readiness.changelog_draft:
        lines += ["", "[bold]Changelog draft:[/]", readiness.changelog_draft]
    return Panel("\n".join(lines), title="Release Readiness", border_style="blue", box=box.ROUNDED)


def approval_prompt(action_type: str, risk: str) -> bool:
    """Tiered confirmation by risk level. Returns True if approved."""
    import typer

    if risk in ("high", "very-high", "critical"):
        phrase = "yes, i understand"
        answer = typer.prompt(f'[{action_type}] Type "{phrase}" to approve', default="")
        return answer.strip().lower() == phrase
    if risk in ("moderate", "medium"):
        answer = typer.prompt(f'[{action_type}] Type "yes" to approve', default="")
        return answer.strip().lower() == "yes"
    return typer.confirm(f"[{action_type}] Approve?", default=False)
