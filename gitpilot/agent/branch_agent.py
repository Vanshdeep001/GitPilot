"""Pillar 2 — Branch Management.

Scans branches, classifies stale ones, and recommends deletion. It NEVER
deletes from ``scan``; deletion happens only via ``execute_delete`` after a
human approves, and never for a protected branch under any condition.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from gitpilot.config.parser import GitPilotConfig
from gitpilot.config.safety import cap_confidence, is_protected_branch
from gitpilot.github.client import GitHubClient
from gitpilot.github.models import BranchReport

logger = logging.getLogger("gitpilot.branch")


class BranchAgent:
    def __init__(self, github: GitHubClient, config: GitPilotConfig, db=None, llm=None):
        self.github = github
        self.config = config
        self.db = db
        self.llm = llm

    # ------------------------------------------------------------------
    def scan(self) -> list[BranchReport]:
        """Runs periodically (every 6 hours). Recommend only — never deletes."""
        reports: list[BranchReport] = []
        for branch in self.github.get_branches():
            name = branch.name
            if is_protected_branch(name, self.config.branches.protected):
                reports.append(BranchReport(name=name, classification="release",
                                            protected=True, recommendation="protected — never auto-delete"))
                continue

            age_days = self._branch_age_days(branch)
            if age_days <= self.config.branches.stale_days:
                reports.append(BranchReport(name=name, age_days=age_days,
                                            classification="active", recommendation="keep"))
                continue

            report = self._classify_stale(branch, age_days)
            reports.append(report)

            if report.classification == "stale" and self.db is not None:
                self.db.add_pending_approval(
                    action_type="delete_branch", target=name,
                    analysis=report.model_dump(), risk_level=report.risk_level,
                )
                if self.config.branches.warn_before_delete:
                    logger.info("Stale branch flagged (warn before delete): %s", name)
        return reports

    # ------------------------------------------------------------------
    def _classify_stale(self, branch, age_days: int) -> BranchReport:
        name = branch.name
        if self.llm is None:
            return BranchReport(name=name, age_days=age_days, classification="uncertain",
                                confidence=0, recommendation="review manually", risk_level="low")

        from gitpilot.llm.prompts import BRANCH_CLASSIFICATION_PROMPT, SYSTEM_ADVISOR, render
        from gitpilot.llm.resolver import AllProvidersExhausted

        try:
            open_prs = self.github.get_open_prs_for_branch(name)
            last_commit = ""
            try:
                last_commit = branch.commit.commit.message.splitlines()[0]
            except Exception:  # noqa: BLE001
                pass
            prompt = render(
                BRANCH_CLASSIFICATION_PROMPT,
                branch_name=name, age_days=age_days, last_commit=last_commit,
                has_open_pr=bool(open_prs),
                pr_status=(open_prs[0].state if open_prs else "none"),
                commits_ahead="unknown",
            )
            text, provider, conf = self.llm.call(prompt, system=SYSTEM_ADVISOR)
            parsed = _parse_json(text)
            classification = parsed.get("classification", "uncertain")
            confidence = cap_confidence(parsed.get("confidence", conf))
            if self.db is not None:
                self.db.update_branch_classification(name, classification, confidence)
            risk = "moderate" if classification == "stale" else "low"
            rec = "recommend deletion (needs approval)" if classification == "stale" else "keep"
            return BranchReport(name=name, age_days=age_days, classification=classification,
                                confidence=confidence, recommendation=rec, risk_level=risk)
        except AllProvidersExhausted:
            logger.warning("LLM exhausted classifying %s", name)
            return BranchReport(name=name, age_days=age_days, classification="uncertain",
                                recommendation="review manually — AI unavailable", risk_level="low")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Classification failed for %s: %s", name, exc)
            return BranchReport(name=name, age_days=age_days, classification="uncertain",
                                recommendation="review manually", risk_level="low")

    def _branch_age_days(self, branch) -> int:
        # Prefer locally tracked last_push; fall back to the commit date.
        activity = self.db.get_branch_activity(branch.name) if self.db else None
        last_push = activity.get("last_push") if activity else None
        try:
            if last_push:
                dt = datetime.fromisoformat(last_push)
            else:
                dt = branch.commit.commit.author.date
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0, (datetime.now(timezone.utc) - dt).days)
        except Exception:  # noqa: BLE001
            return 0

    # ------------------------------------------------------------------
    def execute_delete(self, branch_name: str) -> bool:
        """Called ONLY after a human runs 'gitpilot approve <action_id>'."""
        if is_protected_branch(branch_name, self.config.branches.protected):
            logger.error("Refusing to delete protected branch: %s", branch_name)
            if self.db is not None:
                self.db.log_audit(action="delete_branch", target=branch_name,
                                  status="refused", approved_by="human",
                                  reason="protected branch — never deletable")
            return False
        try:
            ref = self.github.repo.get_git_ref(f"heads/{branch_name}")
            ref.delete()
            if self.db is not None:
                self.db.log_audit(action="delete_branch", target=branch_name,
                                  status="executed", approved_by="human", outcome="deleted")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to delete %s: %s", branch_name, exc)
            if self.db is not None:
                self.db.log_audit(action="delete_branch", target=branch_name,
                                  status="failed", approved_by="human", outcome=str(exc))
            return False


def _parse_json(text: str) -> dict:
    try:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
    except Exception:  # noqa: BLE001
        pass
    return {}
