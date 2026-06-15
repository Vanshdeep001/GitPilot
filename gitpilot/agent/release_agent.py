"""Pillar 4 — Release Intelligence.

Assesses release readiness, drafts a changelog, and suggests a version. It
NEVER creates tags or releases and NEVER pushes tags. A human approves via
``gitpilot approve <id>`` and confirms the actual version.
"""

from __future__ import annotations

import logging

from gitpilot.config.parser import GitPilotConfig
from gitpilot.config.safety import cap_confidence
from gitpilot.github.client import GitHubClient
from gitpilot.github.models import ReleaseReadiness, VersionSuggestion

logger = logging.getLogger("gitpilot.release")


class ReleaseAgent:
    def __init__(self, github: GitHubClient, config: GitPilotConfig, db=None, llm=None):
        self.github = github
        self.config = config
        self.db = db
        self.llm = llm

    # ------------------------------------------------------------------
    def assess_readiness(self) -> ReleaseReadiness:
        """Evaluate readiness — report only. NEVER creates a release/tag."""
        current = self.github.get_latest_release_tag() or "0.0.0"
        try:
            merged_prs = self._merged_prs_since(current)
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not fetch merged PRs: %s", exc)
            merged_prs = []

        changelog = self._changelog_from_prs(merged_prs)
        readiness = ReleaseReadiness(current_version=current, changelog_draft=changelog)

        if self.llm is None:
            readiness.readiness = "needs-attention"
            readiness.warnings = ["AI unavailable — assess release manually"]
            return readiness

        from gitpilot.llm.prompts import RELEASE_READINESS_PROMPT, SYSTEM_ADVISOR, render
        from gitpilot.llm.resolver import AllProvidersExhausted

        try:
            prompt = render(
                RELEASE_READINESS_PROMPT,
                current_version=current,
                merged_prs=", ".join(p["title"] for p in merged_prs[:50]) or "none",
                ci_status="unknown", open_critical_prs="unknown",
                days_since_release="unknown", changelog_draft=changelog,
            )
            text, provider, conf = self.llm.call(prompt, system=SYSTEM_ADVISOR)
            fields = _split_labeled(text)
            readiness.readiness = (fields.get("READINESS", "needs-attention").strip().lower()
                                   or "needs-attention")
            readiness.suggested_version = fields.get("SUGGESTED_VERSION", "").strip()
            readiness.blocking_issues = _as_list(fields.get("BLOCKING_ISSUES", ""))
            readiness.warnings = _as_list(fields.get("WARNINGS", ""))
            readiness.confidence = cap_confidence(_extract_int(fields.get("CONFIDENCE", ""), conf))
            if self.db is not None:
                self.db.log_audit(action="assess_release", target=current, status="recommended",
                                  recommendation=readiness.readiness,
                                  confidence=readiness.confidence, llm_provider=provider)
        except AllProvidersExhausted:
            readiness.readiness = "needs-attention"
            readiness.warnings.append("All LLM providers exhausted — assess manually")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Release assessment failed: %s", exc)
            readiness.readiness = "needs-attention"
        return readiness

    # ------------------------------------------------------------------
    def generate_changelog(self, since_tag: str) -> str:
        """Collect merged PRs since a tag and group them. For human editing."""
        prs = self._merged_prs_since(since_tag)
        return self._changelog_from_prs(prs)

    def suggest_version(self, changelog: str, current: str) -> VersionSuggestion:
        """Suggest the next semver. Human enters the actual version."""
        # Heuristic fallback; LLM refines via assess_readiness when available.
        bump = "patch"
        lc = changelog.lower()
        if "breaking" in lc:
            bump = "major"
        elif "feature" in lc or "feat" in lc:
            bump = "minor"
        suggested = _bump_semver(current, bump)
        return VersionSuggestion(
            suggested_version=suggested,
            reasoning=f"{bump} bump inferred from changelog contents",
            confidence=60,
        )

    # ------------------------------------------------------------------
    def _merged_prs_since(self, tag: str) -> list[dict]:
        # TODO: compare commits since the tag's date for precise filtering.
        prs: list[dict] = []
        try:
            for pr in self.github.repo.get_pulls(state="closed", sort="updated", direction="desc"):
                if pr.merged:
                    prs.append({"title": pr.title or "", "labels": [l.name for l in pr.get_labels()]})
                if len(prs) >= 100:
                    break
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to collect merged PRs: %s", exc)
        return prs

    def _changelog_from_prs(self, prs: list[dict]) -> str:
        groups = {"Breaking Changes": [], "Features": [], "Bug Fixes": [], "Other": []}
        for pr in prs:
            labels = [l.lower() for l in pr.get("labels", [])]
            title = pr.get("title", "")
            if any("break" in l for l in labels) or "breaking" in title.lower():
                groups["Breaking Changes"].append(title)
            elif any(l in ("feature", "enhancement") for l in labels) or title.lower().startswith("feat"):
                groups["Features"].append(title)
            elif any(l in ("bug", "fix") for l in labels) or title.lower().startswith("fix"):
                groups["Bug Fixes"].append(title)
            else:
                groups["Other"].append(title)
        out = []
        for section, items in groups.items():
            if items:
                out.append(f"### {section}")
                out += [f"- {i}" for i in items]
                out.append("")
        return "\n".join(out) if out else "_No merged PRs found since last release._"


def _bump_semver(version: str, bump: str) -> str:
    nums = version.lstrip("v").split(".")
    try:
        major, minor, patch = (int(nums[0]), int(nums[1]), int(nums[2]))
    except (IndexError, ValueError):
        major, minor, patch = 0, 1, 0
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def _split_labeled(text: str) -> dict[str, str]:
    labels = ["READINESS", "SUGGESTED_VERSION", "BLOCKING_ISSUES", "WARNINGS",
              "CONFIDENCE", "CHANGELOG_CATEGORIES"]
    out: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        stripped = line.strip().lstrip("0123456789. ")
        matched = next((lbl for lbl in labels if stripped.upper().startswith(lbl)), None)
        if matched:
            current = matched
            out[current] = stripped[len(matched):].lstrip(": ").strip()
        elif current:
            out[current] += "\n" + line
    return out


def _as_list(text: str) -> list[str]:
    return [l.strip("-• ").strip() for l in (text or "").splitlines() if l.strip()]


def _extract_int(text: str, fallback: int) -> int:
    import re
    m = re.search(r"\d+", text or "")
    return int(m.group()) if m else fallback
