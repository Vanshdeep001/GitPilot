"""GitHub API wrapper (PyGitHub) plus keyring-backed credential storage.

All tokens and API keys live ONLY in the OS keychain via ``keyring`` — never in
a file and never in ``.gitpilot.yml``. Every API call is wrapped so callers can
log and continue rather than crashing the daemon.
"""

from __future__ import annotations

import logging
import re

import keyring

logger = logging.getLogger("gitpilot.github")

# keyring "service" namespace and the credential names GitPilot stores.
KEYRING_SERVICE = "gitpilot"
GITHUB_TOKEN_KEY = "GITHUB_TOKEN"
WEBHOOK_SECRET_KEY = "WEBHOOK_SECRET"

# LLM provider key names (mirrors PROVIDERS[*]["key_env"] in llm.resolver).
LLM_KEY_NAMES = [
    "OPENROUTER_API_KEY",
    "GOOGLE_AI_STUDIO_KEY",
    "GROQ_API_KEY",
]


# ---------------------------------------------------------------------------
# Credential storage — keyring only, never plaintext files.
# ---------------------------------------------------------------------------
def set_credential(name: str, value: str) -> None:
    keyring.set_password(KEYRING_SERVICE, name, value)


def get_credential(name: str) -> str | None:
    return keyring.get_password(KEYRING_SERVICE, name)


def delete_credential(name: str) -> None:
    try:
        keyring.delete_password(KEYRING_SERVICE, name)
    except keyring.errors.PasswordDeleteError:
        pass


def load_llm_keys() -> dict:
    """Return the dict of LLM keys for ``LLMResolver`` (missing keys omitted)."""
    keys: dict[str, str] = {}
    for name in LLM_KEY_NAMES:
        value = get_credential(name)
        if value:
            keys[name] = value
    return keys


def parse_remote_url(url: str) -> str | None:
    """Extract ``owner/repo`` from an https or ssh GitHub remote URL."""
    if not url:
        return None
    url = url.strip()
    # git@github.com:owner/repo.git  /  https://github.com/owner/repo.git
    match = re.search(r"github\.com[:/]+([^/]+)/(.+?)(?:\.git)?/?$", url)
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    return None


# ---------------------------------------------------------------------------
# GitHub client wrapper
# ---------------------------------------------------------------------------
class GitHubError(Exception):
    """Wraps any failure talking to the GitHub API."""


class GitHubClient:
    """Thin wrapper over PyGitHub.

    Constructed lazily so the rest of the package imports even when PyGitHub is
    not installed (e.g. during partial scaffolding/tests).
    """

    def __init__(self, token: str | None = None, repo_full_name: str | None = None):
        self.token = token or get_credential(GITHUB_TOKEN_KEY)
        self.repo_full_name = repo_full_name
        self._gh = None
        self._repo = None

    # -- connection -----------------------------------------------------
    @property
    def gh(self):
        if self._gh is None:
            if not self.token:
                raise GitHubError("No GitHub token. Run 'gitpilot auth' first.")
            try:
                from github import Github  # imported lazily
            except ImportError as exc:  # pragma: no cover
                raise GitHubError("PyGitHub is not installed. Run 'pip install -e .'") from exc
            self._gh = Github(self.token)
        return self._gh

    @property
    def repo(self):
        if self._repo is None:
            if not self.repo_full_name:
                raise GitHubError("No repo configured. Run 'gitpilot init' first.")
            self._repo = self.gh.get_repo(self.repo_full_name)
        return self._repo

    # -- auth -----------------------------------------------------------
    def validate_token(self) -> str:
        """Validate the token via GET /user. Returns the authenticated login."""
        try:
            return self.gh.get_user().login
        except Exception as exc:  # noqa: BLE001
            raise GitHubError(f"GitHub token validation failed: {exc}") from exc

    # -- pull requests --------------------------------------------------
    def get_open_prs(self) -> list:
        try:
            return list(self.repo.get_pulls(state="open"))
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to list PRs: %s", exc)
            raise GitHubError(str(exc)) from exc

    def get_pr(self, number: int):
        try:
            return self.repo.get_pull(number)
        except Exception as exc:  # noqa: BLE001
            raise GitHubError(f"Failed to fetch PR #{number}: {exc}") from exc

    def get_open_prs_for_branch(self, branch: str) -> list:
        try:
            return [pr for pr in self.repo.get_pulls(state="open") if pr.head.ref == branch]
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to list PRs for %s: %s", branch, exc)
            return []

    def post_comment(self, pr_number: int, body: str) -> bool:
        try:
            self.repo.get_issue(pr_number).create_comment(body)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to post comment on #%s: %s", pr_number, exc)
            return False

    # -- branches -------------------------------------------------------
    def get_branches(self) -> list:
        try:
            return list(self.repo.get_branches())
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to list branches: %s", exc)
            return []

    # -- webhooks -------------------------------------------------------
    def create_webhook(self, payload_url: str, secret: str, events: list[str]) -> bool:
        try:
            self.repo.create_hook(
                name="web",
                config={
                    "url": payload_url,
                    "content_type": "json",
                    "secret": secret,
                    "insecure_ssl": "0",
                },
                events=events,
                active=True,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to create webhook: %s", exc)
            return False

    # -- releases / tags ------------------------------------------------
    def get_latest_release_tag(self) -> str | None:
        try:
            releases = list(self.repo.get_releases())
            return releases[0].tag_name if releases else None
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to fetch releases: %s", exc)
            return None
