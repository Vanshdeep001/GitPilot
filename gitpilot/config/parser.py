"""Configuration models and .gitpilot.yml loader.

User config tunes behavior but can NEVER weaken the hardcoded rules in
``gitpilot.config.safety``. Where a field overlaps a safety rule, the safety
rule always wins (enforced by the agents, not by silently mutating config).
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

CONFIG_FILENAME = ".gitpilot.yml"


class MergeConfig(BaseModel):
    enabled: bool = True
    min_reviews: int = 1
    require_ci: bool = True          # always True for protected branches regardless
    ci_checks: list[str] = Field(default_factory=list)  # empty = all checks must pass
    strategy: str = "squash"         # merge | squash | rebase
    delete_branch_after: bool = True
    # Note: BLOCKED_LABELS from safety.py always apply — cannot be overridden.


class BranchConfig(BaseModel):
    enabled: bool = True
    stale_days: int = 30
    warn_before_delete: bool = True  # always True regardless
    warning_days: int = 3
    protected: list[str] = Field(default_factory=list)  # safety.py patterns always included


class ConflictConfig(BaseModel):
    enabled: bool = True
    # auto_resolve intentionally REMOVED — conflicts are NEVER auto-applied.
    # GitPilot only analyzes and suggests — the human implements.
    max_file_size_kb: int = 100
    post_suggestion_as_comment: bool = True


class GitOpsConfig(BaseModel):
    enabled: bool = True
    intercept_commands: bool = True
    educational_mode: bool = False   # explains Git internals when True
    approval_timeout_seconds: int = 30


class ReleaseConfig(BaseModel):
    enabled: bool = True
    changelog_format: str = "conventional"  # conventional | keepachangelog | simple
    auto_suggest_version: bool = True


class NotificationsConfig(BaseModel):
    slack_webhook: str = ""
    github_comments: bool = True
    terminal: bool = True


class GitPilotConfig(BaseModel):
    repo: str
    merge: MergeConfig = Field(default_factory=MergeConfig)
    branches: BranchConfig = Field(default_factory=BranchConfig)
    conflicts: ConflictConfig = Field(default_factory=ConflictConfig)
    git_ops: GitOpsConfig = Field(default_factory=GitOpsConfig)
    release: ReleaseConfig = Field(default_factory=ReleaseConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)

    # ------------------------------------------------------------------
    # Load / save helpers
    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path | None = None) -> "GitPilotConfig":
        """Load config from .gitpilot.yml (defaults to the current directory)."""
        config_path = _resolve_path(path)
        if not config_path.exists():
            raise FileNotFoundError(
                f"No {CONFIG_FILENAME} found at {config_path}. Run 'gitpilot init' first."
            )
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)

    def save(self, path: str | Path | None = None) -> Path:
        """Write config to .gitpilot.yml. Never contains secrets/tokens."""
        config_path = _resolve_path(path)
        config_path.write_text(
            yaml.safe_dump(self.model_dump(), sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )
        return config_path


def _resolve_path(path: str | Path | None) -> Path:
    if path is None:
        return Path.cwd() / CONFIG_FILENAME
    p = Path(path)
    return p / CONFIG_FILENAME if p.is_dir() else p


def config_exists(path: str | Path | None = None) -> bool:
    return _resolve_path(path).exists()
