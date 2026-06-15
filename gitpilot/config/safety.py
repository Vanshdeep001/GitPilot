"""Hardcoded safety constants for GitPilot.

These values are NON-NEGOTIABLE and CANNOT be overridden by user config. They
encode the rules that keep GitPilot a trusted advisor rather than an autonomous
agent. Edit with extreme care — every constant here is a safety boundary.
"""

from __future__ import annotations

import fnmatch
import re

# ---------------------------------------------------------------------------
# Git commands that ALWAYS require explicit user approval before execution.
# Matched as substrings against a normalized command string.
# ---------------------------------------------------------------------------
ALWAYS_REQUIRE_APPROVAL: list[str] = [
    "push --force",
    "push --force-with-lease",
    "reset --hard",
    "reset --mixed",
    "reset --soft",
    "rebase",
    "revert",
    "cherry-pick",
    "branch -D",
    "tag",
    "clean -fd",
    "filter-branch",
    "commit --amend",
]

# ---------------------------------------------------------------------------
# Branch name patterns that GitPilot must NEVER touch automatically.
# Supports fnmatch-style wildcards (e.g. "release/*").
# ---------------------------------------------------------------------------
PROTECTED_BRANCH_PATTERNS: list[str] = [
    "main",
    "master",
    "develop",
    "dev",
    "release/*",
    "hotfix/*",
    "production",
    "prod",
    "staging",
]

# ---------------------------------------------------------------------------
# PR labels that ALWAYS block a merge recommendation. Compared case-insensitively.
# ---------------------------------------------------------------------------
BLOCKED_LABELS: list[str] = [
    "WIP",
    "wip",
    "do-not-merge",
    "hold",
    "blocked",
    "draft",
    "needs-rebase",
    "work-in-progress",
]

# ---------------------------------------------------------------------------
# Maximum confidence GitPilot can ever report. Never 100%.
# ---------------------------------------------------------------------------
MAX_CONFIDENCE: int = 95

# ---------------------------------------------------------------------------
# Operations that require approval — no exceptions.
# ---------------------------------------------------------------------------
DESTRUCTIVE_OPERATIONS: list[str] = [
    "merge",
    "rebase",
    "delete_branch",
    "resolve_conflict",
    "cherry_pick",
    "tag_create",
    "release_create",
    "force_push",
    "reset",
    "revert",
]

# ---------------------------------------------------------------------------
# Patterns that must NEVER be sent to external LLM providers.
# ---------------------------------------------------------------------------
SENSITIVE_PATTERNS: list[str] = [
    r"GITHUB_TOKEN",
    r"API_KEY",
    r"SECRET",
    r"PASSWORD",
    r"PRIVATE_KEY",
    r"ACCESS_TOKEN",
    r"AUTH_TOKEN",
]


# ---------------------------------------------------------------------------
# Helper functions — the single source of truth for safety decisions.
# Agents and the CLI must use these rather than re-implementing the checks.
# ---------------------------------------------------------------------------
def cap_confidence(value: int) -> int:
    """Clamp any confidence value to [0, MAX_CONFIDENCE]. Never returns 100."""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(value, MAX_CONFIDENCE))


def is_protected_branch(branch_name: str, extra_patterns: list[str] | None = None) -> bool:
    """Return True if a branch matches any protected pattern (built-in or user)."""
    if not branch_name:
        return False
    name = branch_name.strip()
    patterns = list(PROTECTED_BRANCH_PATTERNS) + list(extra_patterns or [])
    for pattern in patterns:
        if fnmatch.fnmatch(name, pattern) or name == pattern:
            return True
    return False


def has_blocked_label(labels: list[str]) -> bool:
    """Return True if any label is in BLOCKED_LABELS (case-insensitive)."""
    blocked = {label.lower() for label in BLOCKED_LABELS}
    return any((label or "").lower() in blocked for label in labels)


def normalize_command(command: str) -> str:
    """Collapse whitespace so command matching is robust to spacing/quoting."""
    return re.sub(r"\s+", " ", (command or "").strip())


def requires_approval(command: str) -> bool:
    """Return True if a git command matches ALWAYS_REQUIRE_APPROVAL."""
    normalized = normalize_command(command)
    return any(token in normalized for token in ALWAYS_REQUIRE_APPROVAL)


def contains_secret(text: str) -> str | None:
    """Return the first sensitive pattern found in text, else None."""
    for pattern in SENSITIVE_PATTERNS:
        if re.search(pattern, text or "", re.IGNORECASE):
            return pattern
    return None
