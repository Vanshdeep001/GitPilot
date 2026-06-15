"""Event routing loop.

Polls the SQLite ``events`` table and routes each event to the right agent.
Agents recommend; they do not execute without human approval. The loop NEVER
crashes the daemon — every iteration is wrapped so an error is logged and the
loop continues.
"""

from __future__ import annotations

import json
import logging
import time

from gitpilot.config.parser import GitPilotConfig
from gitpilot.db.queue import Database
from gitpilot.github.client import GitHubClient, get_credential, load_llm_keys, GITHUB_TOKEN_KEY
from gitpilot.llm.resolver import LLMResolver

logger = logging.getLogger("gitpilot.orchestrator")

POLL_INTERVAL_SECONDS = 5


class Orchestrator:
    def __init__(self, config: GitPilotConfig | None = None, db: Database | None = None):
        self.config = config or GitPilotConfig.load()
        self.db = db or Database()
        token = get_credential(GITHUB_TOKEN_KEY)
        self.github = GitHubClient(token=token, repo_full_name=self.config.repo)
        self.llm = LLMResolver(load_llm_keys())

        from gitpilot.agent.branch_agent import BranchAgent
        from gitpilot.agent.conflict_agent import ConflictAgent
        from gitpilot.agent.pr_agent import PRAgent

        self.pr_agent = PRAgent(self.github, self.config, db=self.db, llm=self.llm)
        self.branch_agent = BranchAgent(self.github, self.config, db=self.db, llm=self.llm)
        self.conflict_agent = ConflictAgent(self.github, self.config, db=self.db, llm=self.llm)

    # ------------------------------------------------------------------
    def run(self) -> None:
        logger.info("Orchestrator polling every %ss", POLL_INTERVAL_SECONDS)
        while True:
            try:
                event = self.db.get_next_pending_event()
                if event:
                    self.db.mark_processing(event["id"])
                    self.route(event)
                    self.db.mark_done(event["id"])
                else:
                    time.sleep(POLL_INTERVAL_SECONDS)
            except Exception as exc:  # noqa: BLE001 — never crash the daemon
                logger.error("Orchestrator error: %s", exc)
                time.sleep(POLL_INTERVAL_SECONDS)

    # ------------------------------------------------------------------
    def route(self, event: dict) -> None:
        event_type = event["event_type"]
        try:
            payload = json.loads(event["payload"])
        except (json.JSONDecodeError, TypeError):
            logger.warning("Skipping event %s with invalid payload", event.get("id"))
            return

        if event_type == "pull_request":
            self._handle_pull_request(payload)
        elif event_type == "check_suite" and payload.get("action") == "completed":
            ref = payload.get("check_suite", {}).get("head_branch")
            if ref:
                for pr in self.github.get_open_prs_for_branch(ref):
                    self.pr_agent.evaluate_pr(pr.number)
        elif event_type == "pull_request_review":
            number = payload.get("pull_request", {}).get("number")
            if number:
                self.pr_agent.evaluate_pr(number)
        elif event_type == "push":
            ref = payload.get("ref", "")
            head = payload.get("head_commit") or {}
            if ref.startswith("refs/heads/") and head.get("timestamp"):
                self.db.update_branch_last_push(ref.replace("refs/heads/", ""), head["timestamp"])

    def _handle_pull_request(self, payload: dict) -> None:
        action = payload.get("action")
        number = payload.get("pull_request", {}).get("number")
        if not number:
            return
        if action in ("opened", "synchronize", "reopened"):
            rec = self.pr_agent.evaluate_pr(number)
            if rec.has_conflicts and self.config.conflicts.enabled:
                self.conflict_agent.analyze(number)
        elif action in ("labeled", "unlabeled"):
            self.pr_agent.evaluate_pr(number)
