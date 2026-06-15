"""Pillar 1 — PR Intelligence.

Evaluates PR readiness and returns a recommendation. It NEVER merges from
``evaluate_pr``; merges happen only via ``execute_merge`` after a human runs
``gitpilot approve <id>``.
"""

from __future__ import annotations

import logging

from gitpilot.config.parser import GitPilotConfig
from gitpilot.config.safety import cap_confidence, has_blocked_label, is_protected_branch
from gitpilot.github.client import GitHubClient
from gitpilot.github.models import MergeResult, PRInfo, PRRecommendation

logger = logging.getLogger("gitpilot.pr")


class PRAgent:
    def __init__(self, github: GitHubClient, config: GitPilotConfig, db=None, llm=None):
        self.github = github
        self.config = config
        self.db = db
        self.llm = llm

    # ------------------------------------------------------------------
    def evaluate_pr(self, pr_number: int) -> PRRecommendation:
        """Evaluate readiness — recommend only. NEVER calls execute_merge()."""
        try:
            info = self._fetch_pr_info(pr_number)
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not fetch PR #%s: %s", pr_number, exc)
            return PRRecommendation(
                pr_number=pr_number,
                recommendation="needs-attention",
                blocking_reasons=[f"Could not fetch PR data: {exc}"],
                confidence=0,
            )

        rec = self._apply_rules(info)

        # Natural-language summary via LLM (best-effort, never blocks the rules).
        if self.llm is not None:
            rec = self._add_llm_summary(info, rec)

        # Post comment + log to pending_approvals when ready.
        if self.config.notifications.github_comments and rec.summary:
            self.github.post_comment(pr_number, _format_comment(rec))

        if self.db is not None and rec.recommendation == "ready-to-merge":
            self.db.add_pending_approval(
                action_type="merge_pr",
                target=f"PR #{pr_number}",
                analysis=rec.model_dump(),
                risk_level="moderate",
            )
        return rec

    # ------------------------------------------------------------------
    def _apply_rules(self, info: PRInfo) -> PRRecommendation:
        """Deterministic safety check order (safety rules win over LLM)."""
        reasons: list[str] = []
        warnings: list[str] = []
        has_conflicts = info.mergeable is False or info.mergeable_state == "dirty"

        if info.draft:
            return PRRecommendation(
                pr_number=info.number, recommendation="needs-attention",
                blocking_reasons=["PR is a draft"], has_conflicts=has_conflicts, confidence=90,
            )

        if has_blocked_label(info.labels):
            blocked = [l for l in info.labels if has_blocked_label([l])]
            return PRRecommendation(
                pr_number=info.number, recommendation="blocked",
                blocking_reasons=[f"Blocked label(s): {', '.join(blocked)}"],
                has_conflicts=has_conflicts, confidence=90,
            )

        if has_conflicts:
            return PRRecommendation(
                pr_number=info.number, recommendation="blocked",
                blocking_reasons=["Merge conflicts must be resolved by a human"],
                has_conflicts=True, confidence=85,
            )

        if info.ci_status == "failure":
            return PRRecommendation(
                pr_number=info.number, recommendation="blocked",
                blocking_reasons=["CI is failing"], confidence=90,
            )

        if info.ci_status == "pending":
            return PRRecommendation(
                pr_number=info.number, recommendation="needs-attention",
                warnings=["CI still running — re-evaluate when complete"], confidence=80,
            )

        if info.approved_reviews < self.config.merge.min_reviews:
            need = self.config.merge.min_reviews - info.approved_reviews
            return PRRecommendation(
                pr_number=info.number, recommendation="needs-review",
                blocking_reasons=[f"Needs {need} more approving review(s)"], confidence=85,
            )

        base_protected = is_protected_branch(info.base_branch, self.config.branches.protected)
        if base_protected:
            warnings.append("Base branch is protected — human approval required to merge")

        return PRRecommendation(
            pr_number=info.number, recommendation="ready-to-merge",
            blocking_reasons=reasons, warnings=warnings, confidence=85,
        )

    # ------------------------------------------------------------------
    def _fetch_pr_info(self, pr_number: int) -> PRInfo:
        pr = self.github.get_pr(pr_number)
        labels = [l.name for l in pr.get_labels()]
        # CI status via combined status / check-runs on the head sha.
        ci_status = _combined_ci_status(pr)
        reviews = list(pr.get_reviews())
        approved = sum(1 for r in reviews if r.state == "APPROVED")
        days_open = max(0, (pr.created_at and (pr.updated_at - pr.created_at).days) or 0)
        return PRInfo(
            number=pr.number,
            title=pr.title or "",
            description=pr.body or "",
            head_branch=pr.head.ref,
            base_branch=pr.base.ref,
            labels=labels,
            draft=bool(pr.draft),
            mergeable=pr.mergeable,
            mergeable_state=pr.mergeable_state or "unknown",
            ci_status=ci_status,
            review_count=len(reviews),
            approved_reviews=approved,
            days_open=days_open,
            files_changed=[f.filename for f in pr.get_files()],
        )

    def _add_llm_summary(self, info: PRInfo, rec: PRRecommendation) -> PRRecommendation:
        from gitpilot.llm.prompts import PR_EVALUATION_PROMPT, SYSTEM_ADVISOR, render
        from gitpilot.llm.resolver import AllProvidersExhausted

        try:
            prompt = render(
                PR_EVALUATION_PROMPT,
                title=info.title, description=info.description[:2000],
                files_changed=", ".join(info.files_changed[:30]),
                ci_status=info.ci_status, review_count=info.review_count,
                labels=", ".join(info.labels), head_branch=info.head_branch,
                base_branch=info.base_branch,
                is_protected=is_protected_branch(info.base_branch, self.config.branches.protected),
                days_open=info.days_open,
                conflict_status="conflicts" if rec.has_conflicts else "clean",
            )
            text, provider, conf = self.llm.call(prompt, system=SYSTEM_ADVISOR)
            rec.summary = text.strip()
            rec.llm_provider = provider
            rec.confidence = cap_confidence(min(rec.confidence or conf, conf))
        except AllProvidersExhausted:
            logger.warning("LLM exhausted for PR #%s summary", info.number)
            rec.warnings.append("AI summary unavailable — all providers exhausted")
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM summary failed for PR #%s: %s", info.number, exc)
        return rec

    # ------------------------------------------------------------------
    def execute_merge(self, pr_number: int, strategy: str | None = None) -> MergeResult:
        """Called ONLY after a human runs 'gitpilot approve <action_id>'."""
        strategy = strategy or self.config.merge.strategy
        try:
            pr = self.github.get_pr(pr_number)
            result = pr.merge(merge_method=strategy)
            if self.db is not None:
                self.db.log_audit(
                    action="merge_pr", target=f"PR #{pr_number}", status="executed",
                    approved_by="human", outcome="merged",
                )
            return MergeResult(pr_number=pr_number, merged=bool(result.merged),
                               message=result.message or "", sha=result.sha or "")
        except Exception as exc:  # noqa: BLE001
            logger.error("Merge of #%s failed: %s", pr_number, exc)
            if self.db is not None:
                self.db.log_audit(
                    action="merge_pr", target=f"PR #{pr_number}", status="failed",
                    approved_by="human", outcome=str(exc),
                )
            return MergeResult(pr_number=pr_number, merged=False, message=str(exc))


def _combined_ci_status(pr) -> str:
    try:
        commit = pr.get_commits().reversed[0]
        state = commit.get_combined_status().state  # success | failure | pending | error
        if state in ("success", "failure", "pending"):
            return state
        if state == "error":
            return "failure"
        return "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _format_comment(rec: PRRecommendation) -> str:
    lines = [f"### GitPilot — PR recommendation: `{rec.recommendation}`", ""]
    if rec.summary:
        lines += [rec.summary, ""]
    if rec.blocking_reasons:
        lines += ["**Blocking:**"] + [f"- {r}" for r in rec.blocking_reasons] + [""]
    if rec.warnings:
        lines += ["**Warnings:**"] + [f"- {w}" for w in rec.warnings] + [""]
    lines.append(f"_Confidence: {rec.confidence}% — GitPilot recommends; a human decides._")
    return "\n".join(lines)
