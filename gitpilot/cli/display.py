"""Rich formatting helpers. Use these everywhere — never plain print()."""

from __future__ import annotations

import sys

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Windows consoles default to cp1252, which cannot encode the box-drawing,
# block, emoji, and em-dash characters used below. Force UTF-8 so output never
# crashes with UnicodeEncodeError when piped or run on a legacy code page.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

console = Console()

FOOTER = "[dim italic]◆ GitPilot recommends — you make the final call[/]"


# ---------------------------------------------------------------------------
# Status line helpers
# ---------------------------------------------------------------------------
def success(msg: str) -> None: console.print(f"[bold green]✓[/] {msg}")
def error(msg: str) -> None:   console.print(f"[bold red]✗[/] {msg}")
def warning(msg: str) -> None: console.print(f"[bold yellow]⚠[/] {msg}")
def info(msg: str) -> None:    console.print(f"[bold blue]ℹ[/] {msg}")
def agent(msg: str) -> None:   console.print(f"[bold magenta]◆[/] {msg}")
def blocked(msg: str) -> None: console.print(f"[bold red]\U0001f6ab[/] {msg}")
def pending(msg: str) -> None: console.print(f"[bold cyan]⏳[/] {msg}")


# ---------------------------------------------------------------------------
# Banner — shown on bare `gitpilot`, and after auth/init/start
# ---------------------------------------------------------------------------
_BANNER_ART = r"""
   ____ _ _   ____  _ _       _
  / ___(_) |_|  _ \(_) | ___ | |_
 | |  _| | __| |_) | | |/ _ \| __|
 | |_| | | |_|  __/| | | (_) | |_
  \____|_|\__|_|   |_|_|\___/ \__|
"""

_BANNER_GRADIENT = ["bright_cyan", "cyan", "blue", "blue", "bright_blue"]


def banner(subtitle: str | None = None) -> None:
    """Print the GitPilot splash — a creative welcome, Claude-Code style."""
    from gitpilot import __version__

    art_lines = _BANNER_ART.strip("\n").splitlines()
    rendered = Text()
    for i, line in enumerate(art_lines):
        color = _BANNER_GRADIENT[min(i, len(_BANNER_GRADIENT) - 1)]
        rendered.append(line + "\n", style=f"bold {color}")
    rendered.append("\n  ✈  ", style="bold bright_cyan")
    rendered.append("Your AI co-pilot for Git operations", style="bold white")
    rendered.append(f"   v{__version__}\n", style="dim")
    rendered.append(
        "  Analyze → Explain → Recommend → Approve → Execute → Report\n",
        style="dim cyan",
    )
    if subtitle:
        rendered.append(f"\n  {subtitle}\n", style="bold green")
    rendered.append(
        "\n  GitPilot recommends — you always make the final call.",
        style="dim italic",
    )
    console.print(
        Panel(rendered, box=box.HEAVY, border_style="bright_cyan", padding=(0, 2))
    )


def welcome_hint() -> None:
    """A short 'what next' shown under the banner for new users."""
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold cyan", no_wrap=True)
    table.add_column(style="white")
    table.add_row("gitpilot auth", "Store your GitHub + LLM keys (once, in the OS keychain)")
    table.add_row("gitpilot init", "Configure GitPilot for the current repo")
    table.add_row("gitpilot explain \"<cmd>\"", "Analyze a risky git command before running it")
    table.add_row("gitpilot --help", "See every command")
    console.print(Panel(table, title="[bold]Get started[/]", border_style="dim",
                        box=box.ROUNDED, padding=(1, 1)))


# ---------------------------------------------------------------------------
# Badges & bars
# ---------------------------------------------------------------------------
RISK_COLORS = {
    "very-low": "bright_green", "low": "green",
    "moderate": "yellow", "medium": "yellow", "high": "red",
    "very-high": "bold red", "critical": "bold white on red",
}

REC_STYLE = {
    "ready-to-merge": ("✅", "bold green"),
    "needs-review": ("👀", "bold yellow"),
    "blocked": ("🚫", "bold red"),
    "needs-attention": ("⚠️", "yellow"),
}

CLASS_STYLE = {
    "active": "green", "stale": "bold red",
    "release": "magenta", "uncertain": "yellow",
}

READINESS_STYLE = {
    "ready": "bold green", "needs-attention": "yellow", "not-ready": "bold red",
}


def risk_badge(level: str) -> str:
    color = RISK_COLORS.get(level, "white")
    return f"[{color}] {level.upper()} [/]"


def rec_badge(recommendation: str) -> str:
    emoji, color = REC_STYLE.get(recommendation, ("◆", "white"))
    return f"[{color}]{emoji} {recommendation}[/]"


def confidence_bar(score: int) -> str:
    """Visual confidence bar — always caps at 95%."""
    score = min(score, 95)
    filled = score // 10
    bar = "█" * filled + "░" * (10 - filled)
    color = "green" if score > 80 else "yellow" if score > 60 else "red"
    return f"[{color}]{bar}[/] [bold]{score}%[/]"


# ---------------------------------------------------------------------------
# Panels & tables
# ---------------------------------------------------------------------------
def operation_panel(analysis) -> Panel:
    """Main display for Git Operations Intelligence — a rich bordered panel."""
    head = Table.grid(padding=(0, 2))
    head.add_column(style="bold", justify="right")
    head.add_column()
    head.add_row("Command", f"[bright_white]{analysis.command}[/]")
    head.add_row("Risk", risk_badge(analysis.risk_level))
    head.add_row("Will work", _will_work(analysis.will_succeed))
    head.add_row("Confidence", confidence_bar(analysis.confidence))

    parts = [head]
    if analysis.prediction:
        parts += [Text("\nWhat will happen", style="bold"), Text(analysis.prediction)]
    if analysis.warnings:
        parts.append(_bullets("⚠️  Warnings", analysis.warnings, "yellow"))
    if analysis.recommended_steps:
        parts.append(_numbered("🛟  Recommended steps", analysis.recommended_steps))
    if analysis.alternatives:
        parts.append(_bullets("🔀  Safer alternatives", analysis.alternatives, "cyan"))
    if analysis.educational_note:
        parts.append(Text(f"\nℹ  {analysis.educational_note}", style="dim"))

    return Panel(
        Group(*parts),
        title="[bold]✈ GitPilot — Operation Analysis[/]",
        subtitle=FOOTER,
        border_style=RISK_COLORS.get(analysis.risk_level, "white"),
        box=box.ROUNDED, padding=(1, 2),
    )


def pr_table(prs: list) -> Table:
    table = Table(title="[bold]✈ Pull Request Readiness[/]", box=box.ROUNDED,
                  header_style="bold cyan", title_justify="left", expand=True)
    table.add_column("PR", style="bold cyan", no_wrap=True)
    table.add_column("Recommendation", no_wrap=True)
    table.add_column("Blocking / Warnings", ratio=2)
    table.add_column("Confidence", no_wrap=True)
    for rec in prs:
        notes = _join_notes(rec.blocking_reasons, rec.warnings)
        table.add_row(f"#{rec.pr_number}", rec_badge(rec.recommendation),
                      notes, confidence_bar(rec.confidence))
    table.caption = FOOTER
    return table


def branch_table(branches: list) -> Table:
    table = Table(title="[bold]✈ Branch Health[/]", box=box.ROUNDED,
                  header_style="bold cyan", title_justify="left", expand=True)
    table.add_column("Branch", style="bold cyan")
    table.add_column("Age", justify="right", no_wrap=True)
    table.add_column("Classification", no_wrap=True)
    table.add_column("Risk", no_wrap=True)
    table.add_column("Recommendation", ratio=2)
    for b in branches:
        cls_color = CLASS_STYLE.get(b.classification, "white")
        lock = "🔒 " if b.protected else ""
        table.add_row(f"{lock}{b.name}", f"{b.age_days}d",
                      f"[{cls_color}]{b.classification}[/]",
                      risk_badge(b.risk_level), b.recommendation)
    table.caption = FOOTER
    return table


def conflict_panel(analysis) -> Panel:
    """Conflict analysis — always includes the 'SUGGESTION ONLY' label."""
    parts: list = [
        Panel(
            "[bold]SUGGESTION ONLY[/] — human review and implementation required",
            border_style="red", box=box.HEAVY, padding=(0, 1),
        ),
        _kv_grid([
            ("Overall risk", risk_badge(analysis.overall_risk)),
            ("Confidence", confidence_bar(analysis.overall_confidence)),
            ("Est. human time", analysis.estimated_human_time),
            ("Files", str(len(analysis.per_file_analysis))),
        ]),
    ]
    if not analysis.per_file_analysis:
        parts.append(Text("\nNo analyzable conflict hunks were found.", style="dim"))
    for f in analysis.per_file_analysis:
        cls_color = CLASS_STYLE.get(f.classification, "white")
        block = Text()
        block.append(f"\n{f.file_path}", style="bold cyan")
        block.append(f"  ({f.language})  ", style="dim")
        block.append(f"{f.classification}", style=cls_color)
        block.append(f" · risk {f.risk}\n", style=RISK_COLORS.get(f.risk, "white"))
        block.append(f.explanation + "\n")
        if f.human_review_notes:
            block.append("Verify: ", style="bold yellow")
            block.append(f.human_review_notes + "\n")
        parts.append(block)

    return Panel(
        Group(*parts),
        title=f"[bold]✈ Conflict Analysis — PR #{analysis.pr_number}[/]",
        subtitle=FOOTER, border_style="yellow", box=box.ROUNDED, padding=(1, 2),
    )


def release_panel(readiness) -> Panel:
    r_color = READINESS_STYLE.get(readiness.readiness, "white")
    parts = [_kv_grid([
        ("Readiness", f"[{r_color}]{readiness.readiness.upper()}[/]"),
        ("Current version", readiness.current_version or "—"),
        ("Suggested version", f"[bold green]{readiness.suggested_version or '—'}[/]"),
        ("Confidence", confidence_bar(readiness.confidence)),
    ])]
    if readiness.blocking_issues:
        parts.append(_bullets("🚧 Blocking issues", readiness.blocking_issues, "red"))
    if readiness.warnings:
        parts.append(_bullets("⚠️  Warnings", readiness.warnings, "yellow"))
    if readiness.changelog_draft:
        parts += [Text("\nChangelog draft", style="bold"), Text(readiness.changelog_draft)]
    return Panel(Group(*parts), title="[bold]✈ Release Readiness[/]", subtitle=FOOTER,
                 border_style="blue", box=box.ROUNDED, padding=(1, 2))


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


# ---------------------------------------------------------------------------
# Internal layout helpers
# ---------------------------------------------------------------------------
def _kv_grid(rows: list[tuple[str, str]]):
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold", justify="right")
    grid.add_column()
    for k, v in rows:
        grid.add_row(k, v)
    return grid


def _bullets(title: str, items: list[str], color: str):
    t = Text()
    t.append(f"\n{title}\n", style=f"bold {color}")
    for item in items:
        t.append("  • ", style=color)
        t.append(item + "\n")
    return t


def _numbered(title: str, items: list[str]):
    t = Text()
    t.append(f"\n{title}\n", style="bold")
    for i, item in enumerate(items, 1):
        t.append(f"  {i}. ", style="bold cyan")
        t.append(item + "\n")
    return t


def _will_work(value: str) -> str:
    colors = {"yes": "green", "no": "red", "uncertain": "yellow"}
    return f"[{colors.get(value, 'white')}]{value}[/]"


def _join_notes(blocking: list[str], warnings: list[str]) -> str:
    parts = [f"[red]• {b}[/]" for b in blocking] + [f"[yellow]• {w}[/]" for w in warnings]
    return "\n".join(parts) if parts else "[dim]—[/]"
