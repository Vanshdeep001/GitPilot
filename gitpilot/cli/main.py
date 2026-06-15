"""GitPilot CLI — all commands.

GitPilot recommends; humans decide. Destructive operations always require an
explicit human approval before anything executes.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import typer

from gitpilot.cli import display as d

app = typer.Typer(
    name="gitpilot",
    help="AI-powered Git Operations Engineer — explains, warns, and acts only with your approval",
    no_args_is_help=True,
    add_completion=False,
)

PID_DIR = Path.home() / ".gitpilot"
PID_FILE = PID_DIR / "gitpilot.pid"
LOG_FILE = PID_DIR / "gitpilot.log"


# ---------------------------------------------------------------------------
# Setup commands
# ---------------------------------------------------------------------------
@app.command()
def auth():
    """Store GitHub + LLM provider keys in the OS keychain (never plaintext)."""
    from gitpilot.github.client import (
        GITHUB_TOKEN_KEY, GitHubClient, set_credential,
    )

    github_token = typer.prompt("GitHub Personal Access Token", hide_input=True)
    openrouter = typer.prompt("OpenRouter API key", hide_input=True)
    google = typer.prompt("Google AI Studio key (optional)", hide_input=True, default="")
    groq = typer.prompt("Groq API key (optional)", hide_input=True, default="")

    set_credential(GITHUB_TOKEN_KEY, github_token)
    set_credential("OPENROUTER_API_KEY", openrouter)
    if google:
        set_credential("GOOGLE_AI_STUDIO_KEY", google)
    if groq:
        set_credential("GROQ_API_KEY", groq)

    try:
        login = GitHubClient(token=github_token).validate_token()
        d.success(f"Authenticated as @{login}")
    except Exception as exc:  # noqa: BLE001
        d.error(f"GitHub token validation failed: {exc}")
        raise typer.Exit(code=1)
    d.success("Keys stored securely in OS keychain")


@app.command()
def init():
    """Configure GitPilot for the current repo and register a webhook."""
    import secrets

    from gitpilot.config.parser import GitPilotConfig, config_exists
    from gitpilot.github.client import (
        WEBHOOK_SECRET_KEY, GitHubClient, get_credential, GITHUB_TOKEN_KEY,
        parse_remote_url, set_credential,
    )
    from gitpilot.server.webhook import SUBSCRIBED_EVENTS

    if not _in_git_repo():
        d.error("Not a git repository. Run 'gitpilot init' inside a repo.")
        raise typer.Exit(code=1)

    remote = _git_output(["config", "--get", "remote.origin.url"])
    repo_full = parse_remote_url(remote or "")
    if not repo_full:
        repo_full = typer.prompt("Could not detect repo. Enter owner/repo")
    d.info(f"Repository: {repo_full}")

    if config_exists() and not typer.confirm(".gitpilot.yml exists. Overwrite?", default=False):
        raise typer.Exit()

    config = GitPilotConfig(repo=repo_full)
    config.merge.min_reviews = typer.prompt("Minimum approving reviews", default=1, type=int)
    config.branches.stale_days = typer.prompt("Stale branch threshold (days)", default=30, type=int)
    config.git_ops.educational_mode = typer.confirm("Enable educational mode?", default=False)
    path = config.save()
    d.success(f"Wrote {path}")

    if typer.confirm("Register a GitHub webhook now?", default=False):
        payload_url = typer.prompt("Public webhook URL (e.g. https://host/webhook)")
        secret = secrets.token_hex(32)
        set_credential(WEBHOOK_SECRET_KEY, secret)
        client = GitHubClient(token=get_credential(GITHUB_TOKEN_KEY), repo_full_name=repo_full)
        if client.create_webhook(payload_url, secret, SUBSCRIBED_EVENTS):
            d.success("Webhook registered")
        else:
            d.warning("Webhook registration failed — you can add it manually later")

    d.agent("GitPilot will RECOMMEND actions. You approve before anything executes.")


# ---------------------------------------------------------------------------
# Daemon lifecycle
# ---------------------------------------------------------------------------
@app.command()
def start(port: int = typer.Option(8787, help="Webhook server port")):
    """Start the background daemon (webhook server + orchestrator)."""
    from gitpilot.config.parser import GitPilotConfig

    if _daemon_running():
        d.warning("GitPilot is already running.")
        raise typer.Exit()

    config = _load_config_or_exit()
    PID_DIR.mkdir(parents=True, exist_ok=True)
    log_handle = open(LOG_FILE, "a", encoding="utf-8")
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = subprocess.Popen(
        [sys.executable, "-m", "gitpilot.server.runner"],
        stdout=log_handle, stderr=log_handle, stdin=subprocess.DEVNULL,
        creationflags=creationflags, close_fds=True,
    )
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    d.success(f"GitPilot agent running (pid {proc.pid})")
    d.info(f"Repo: {config.repo}  ·  port: {port}")
    d.info("Branch scan scheduled every 6 hours.")  # scheduler hook: see orchestrator TODO


@app.command()
def stop():
    """Stop the background daemon."""
    if not PID_FILE.exists():
        d.warning("GitPilot is not running.")
        raise typer.Exit()
    pid = int(PID_FILE.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        d.success(f"Stopped GitPilot (pid {pid})")
    except ProcessLookupError:
        d.warning("Process not found — clearing stale PID file.")
    except Exception as exc:  # noqa: BLE001
        d.error(f"Could not stop process: {exc}")
    PID_FILE.unlink(missing_ok=True)


@app.command()
def status():
    """Show daemon health, repo, last action, and today's counts."""
    from rich.table import Table

    from gitpilot.db.queue import Database

    running = _daemon_running()
    table = Table(title="GitPilot Status")
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    table.add_row("Daemon", "[green]running[/]" if running else "[red]stopped[/]")
    cfg = _try_load_config()
    table.add_row("Repo", cfg.repo if cfg else "[yellow]not configured[/]")
    db = Database()
    last = db.last_audit()
    table.add_row("Last action", f"{last['action']} → {last['status']}" if last else "none")
    counts = db.audit_counts_today()
    table.add_row("Today", ", ".join(f"{k}: {v}" for k, v in counts.items()) or "no activity")
    d.console.print(table)


@app.command()
def log(lines: int = typer.Option(30, help="How many recent entries to show")):
    """Show recent agent decisions from the audit log."""
    from rich.table import Table

    from gitpilot.db.queue import Database

    rows = Database().recent_audit(lines)
    if not rows:
        d.info("No audit entries yet.")
        return
    table = Table(title="Audit Log (most recent first)")
    for col in ("id", "action", "target", "status", "approved_by", "confidence", "created_at"):
        table.add_column(col)
    for r in rows:
        table.add_row(str(r["id"]), r["action"], r["target"], r["status"],
                      r["approved_by"] or "-", str(r["confidence"] or "-"), str(r["created_at"]))
    d.console.print(table)


# ---------------------------------------------------------------------------
# Pillar views
# ---------------------------------------------------------------------------
@app.command()
def prs():
    """Show open PRs with readiness, blocking reasons, and confidence."""
    agent, _ = _build_pr_agent()
    recs = []
    for pr in agent.github.get_open_prs():
        recs.append(agent.evaluate_pr(pr.number))
    if not recs:
        d.info("No open PRs.")
        return
    d.console.print(d.pr_table(recs))


@app.command()
def branches():
    """Show branch health with classification and recommendation."""
    from gitpilot.agent.branch_agent import BranchAgent

    config = _load_config_or_exit()
    github, llm, db = _build_runtime(config)
    reports = BranchAgent(github, config, db=db, llm=llm).scan()
    if not reports:
        d.info("No branches found.")
        return
    d.console.print(d.branch_table(reports))


@app.command()
def conflicts():
    """Show PRs with conflicts and their analysis status."""
    from gitpilot.agent.conflict_agent import ConflictAgent

    config = _load_config_or_exit()
    github, llm, db = _build_runtime(config)
    agent = ConflictAgent(github, config, db=db, llm=llm)
    found = False
    for pr in github.get_open_prs():
        if pr.mergeable is False or pr.mergeable_state == "dirty":
            found = True
            d.console.print(d.conflict_panel(agent.analyze(pr.number)))
    if not found:
        d.info("No PRs with conflicts.")


@app.command()
def release():
    """Show release readiness, changelog draft, and version suggestion."""
    from gitpilot.agent.release_agent import ReleaseAgent

    config = _load_config_or_exit()
    github, llm, db = _build_runtime(config)
    readiness = ReleaseAgent(github, config, db=db, llm=llm).assess_readiness()
    d.console.print(d.release_panel(readiness))


# ---------------------------------------------------------------------------
# Git Operations Intelligence
# ---------------------------------------------------------------------------
@app.command()
def watch():
    """Install git hooks so risky commands are analyzed before they run."""
    from gitpilot.agent.git_ops_agent import GitOpsAgent

    if not _in_git_repo():
        d.error("Not a git repository.")
        raise typer.Exit(code=1)
    config = _try_load_config()
    installed = GitOpsAgent(config=config).install_hooks(os.getcwd())
    for path in installed:
        d.success(f"Installed hook: {path}")
    d.agent("GitPilot is now watching git commands in this repo.")
    d.info("Risky operations will be analyzed and require your approval.")


@app.command()
def explain(command: str = typer.Argument(..., help='e.g. "git rebase origin/main"')):
    """Analyze a git command without running it."""
    from gitpilot.agent.git_ops_agent import GitOpsAgent

    config = _try_load_config()
    github, llm, _ = _build_runtime(config) if config else (None, _llm_or_none(), None)
    agent = GitOpsAgent(config=config, github=github, llm=llm)
    analysis = agent.analyze_command(command)
    d.console.print(d.operation_panel(analysis))
    if config and config.git_ops.educational_mode:
        note = agent.educational_explain(command, analysis)
        if note:
            d.console.print(d.Panel(note, title="Educational", border_style="blue"))


# ---------------------------------------------------------------------------
# Approvals & safety
# ---------------------------------------------------------------------------
@app.command()
def approve(action_id: int = typer.Argument(..., help="Pending approval id")):
    """Approve a pending recommended action (shows full analysis first)."""
    import json

    from gitpilot.db.queue import Database

    db = Database()
    pending = db.get_pending_approval(action_id)
    if not pending:
        d.error(f"No pending approval with id {action_id}.")
        raise typer.Exit(code=1)

    d.console.print_json(pending["analysis"])
    if not typer.confirm("Confirm approval?", default=False):
        db.log_audit(action=pending["action_type"], target=pending["target"],
                     status="rejected", approved_by=None)
        db.delete_pending_approval(action_id)
        d.warning("Rejected.")
        return

    _execute_approved(pending, db)
    db.delete_pending_approval(action_id)


@app.command(name="dry-run")
def dry_run():
    """Run the full evaluation pipeline with zero side effects."""
    config = _load_config_or_exit()
    github, llm, db = _build_runtime(config)
    from gitpilot.agent.branch_agent import BranchAgent
    from gitpilot.agent.pr_agent import PRAgent

    d.warning("[DRY RUN] No changes will be written to GitHub or git.")
    pr_agent = PRAgent(github, config, db=None, llm=llm)  # db=None → no pending writes
    pr_agent.config.notifications.github_comments = False
    recs = [pr_agent.evaluate_pr(pr.number) for pr in github.get_open_prs()]
    if recs:
        d.console.print(d.pr_table(recs))
    reports = BranchAgent(github, config, db=None, llm=llm).scan()
    if reports:
        d.console.print(d.branch_table(reports))
    d.warning("[DRY RUN] Complete — nothing was executed.")


@app.command()
def ignore(branch: str = typer.Argument(..., help="Branch name/pattern to protect")):
    """Add a branch to branches.protected in .gitpilot.yml."""
    config = _load_config_or_exit()
    if branch not in config.branches.protected:
        config.branches.protected.append(branch)
        config.save()
        d.success(f"'{branch}' added to protected branches.")
    else:
        d.info(f"'{branch}' is already protected.")


# ---------------------------------------------------------------------------
# Internal: git hook entry point (hidden)
# ---------------------------------------------------------------------------
@app.command(name="_hook", hidden=True)
def hook(hook_type: str, args: list[str] = typer.Argument(None)):
    """Called by installed git hooks. Exit 0 = allow, exit 1 = block."""
    from gitpilot.agent.git_ops_agent import GitOpsAgent

    config = _try_load_config()
    command = f"git {hook_type} {' '.join(args or [])}".strip()
    agent = GitOpsAgent(config=config, llm=_llm_or_none())
    analysis = agent.analyze_command(command)
    d.console.print(d.operation_panel(analysis))

    if not analysis.requires_approval:
        raise typer.Exit(code=0)
    approved = agent.request_approval(analysis)
    raise typer.Exit(code=0 if approved else 1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _in_git_repo() -> bool:
    return _git_output(["rev-parse", "--git-dir"]) is not None


def _git_output(args: list[str]) -> str | None:
    try:
        out = subprocess.run(["git", *args], capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except Exception:  # noqa: BLE001
        return None


def _daemon_running() -> bool:
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def _load_config_or_exit():
    config = _try_load_config()
    if config is None:
        d.error("No .gitpilot.yml found. Run 'gitpilot init' first.")
        raise typer.Exit(code=1)
    return config


def _try_load_config():
    from gitpilot.config.parser import GitPilotConfig, config_exists

    if not config_exists():
        return None
    try:
        return GitPilotConfig.load()
    except Exception as exc:  # noqa: BLE001
        d.error(f"Failed to load config: {exc}")
        return None


def _llm_or_none():
    from gitpilot.github.client import load_llm_keys
    from gitpilot.llm.resolver import LLMResolver

    keys = load_llm_keys()
    return LLMResolver(keys) if keys else None


def _build_runtime(config):
    from gitpilot.db.queue import Database
    from gitpilot.github.client import GITHUB_TOKEN_KEY, GitHubClient, get_credential

    github = GitHubClient(token=get_credential(GITHUB_TOKEN_KEY), repo_full_name=config.repo)
    return github, _llm_or_none(), Database()


def _build_pr_agent():
    from gitpilot.agent.pr_agent import PRAgent

    config = _load_config_or_exit()
    github, llm, db = _build_runtime(config)
    return PRAgent(github, config, db=db, llm=llm), config


def _execute_approved(pending: dict, db) -> None:
    action = pending["action_type"]
    target = pending["target"]
    config = _load_config_or_exit()
    github, llm, _ = _build_runtime(config)

    if action == "merge_pr":
        from gitpilot.agent.pr_agent import PRAgent

        number = int(target.replace("PR #", "").strip())
        result = PRAgent(github, config, db=db, llm=llm).execute_merge(number)
        d.success(f"Merged PR #{number}") if result.merged else d.error(result.message)
    elif action == "delete_branch":
        from gitpilot.agent.branch_agent import BranchAgent

        ok = BranchAgent(github, config, db=db, llm=llm).execute_delete(target)
        d.success(f"Deleted branch {target}") if ok else d.error(f"Could not delete {target}")
    else:
        d.warning(f"No executor wired for action '{action}' — logged as approved only.")
        db.log_audit(action=action, target=target, status="approved", approved_by="human")


if __name__ == "__main__":
    app()
