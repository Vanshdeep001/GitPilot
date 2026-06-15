"""Pydantic models for GitHub data and GitPilot recommendations.

These are plain data containers passed between the GitHub client, the agents,
and the display layer. They never execute anything.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from gitpilot.config.safety import cap_confidence


class PRInfo(BaseModel):
    number: int
    title: str
    description: str = ""
    head_branch: str = ""
    base_branch: str = ""
    labels: list[str] = Field(default_factory=list)
    draft: bool = False
    mergeable: bool | None = None        # None = GitHub still computing
    mergeable_state: str = "unknown"     # clean | dirty | blocked | behind | ...
    ci_status: str = "unknown"           # success | failure | pending | unknown
    review_count: int = 0
    approved_reviews: int = 0
    days_open: int = 0
    files_changed: list[str] = Field(default_factory=list)


class PRRecommendation(BaseModel):
    pr_number: int
    recommendation: str                  # ready-to-merge | needs-review | blocked | needs-attention
    blocking_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    summary: str = ""
    confidence: int = 0
    has_conflicts: bool = False
    llm_provider: str = ""

    def model_post_init(self, __context) -> None:  # type: ignore[override]
        object.__setattr__(self, "confidence", cap_confidence(self.confidence))


class BranchReport(BaseModel):
    name: str
    age_days: int = 0
    classification: str = "uncertain"    # stale | active | release | uncertain
    confidence: int = 0
    recommendation: str = ""
    risk_level: str = "low"
    protected: bool = False

    def model_post_init(self, __context) -> None:  # type: ignore[override]
        object.__setattr__(self, "confidence", cap_confidence(self.confidence))


class ConflictHunk(BaseModel):
    ours: str = ""
    theirs: str = ""
    context_before: str = ""
    context_after: str = ""


class FileConflictAnalysis(BaseModel):
    file_path: str
    language: str = "unknown"
    explanation: str = ""
    classification: str = "unknown"      # formatting | logic | dependency | merge-artifact | api-change | unknown
    risk: str = "low"
    suggested_resolution: str = ""       # always "SUGGESTION ONLY"
    human_review_notes: str = ""
    alternatives: list[str] = Field(default_factory=list)
    confidence: int = 0

    def model_post_init(self, __context) -> None:  # type: ignore[override]
        object.__setattr__(self, "confidence", cap_confidence(self.confidence))


class ConflictAnalysis(BaseModel):
    pr_number: int
    files_analyzed: list[str] = Field(default_factory=list)
    per_file_analysis: list[FileConflictAnalysis] = Field(default_factory=list)
    overall_risk: str = "low"            # low | medium | high | critical
    overall_confidence: int = 0
    estimated_human_time: str = "unknown"
    suggestions_posted: bool = False

    def model_post_init(self, __context) -> None:  # type: ignore[override]
        object.__setattr__(self, "overall_confidence", cap_confidence(self.overall_confidence))


class RepoState(BaseModel):
    current_branch: str = ""
    remote_status: str = "unknown"
    ahead_count: int = 0
    behind_count: int = 0
    staged_files: list[str] = Field(default_factory=list)
    unstaged_files: list[str] = Field(default_factory=list)
    untracked_count: int = 0
    open_prs: list[str] = Field(default_factory=list)
    is_protected_branch: bool = False
    recent_commits: list[str] = Field(default_factory=list)
    merge_in_progress: bool = False
    rebase_in_progress: bool = False
    conflict_files: list[str] = Field(default_factory=list)


class GitOpAnalysis(BaseModel):
    command: str
    prediction: str = ""
    risk_level: str = "low"              # very-low | low | moderate | high | very-high | critical
    will_succeed: str = "uncertain"      # yes | no | uncertain
    warnings: list[str] = Field(default_factory=list)
    recommended_steps: list[str] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    confidence: int = 0
    educational_note: str = ""
    requires_approval: bool = False
    llm_provider: str = ""

    def model_post_init(self, __context) -> None:  # type: ignore[override]
        object.__setattr__(self, "confidence", cap_confidence(self.confidence))


class VersionSuggestion(BaseModel):
    suggested_version: str
    reasoning: str = ""
    confidence: int = 0

    def model_post_init(self, __context) -> None:  # type: ignore[override]
        object.__setattr__(self, "confidence", cap_confidence(self.confidence))


class ReleaseReadiness(BaseModel):
    readiness: str = "not-ready"         # ready | needs-attention | not-ready
    suggested_version: str = ""
    current_version: str = ""
    blocking_issues: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    changelog_draft: str = ""
    confidence: int = 0

    def model_post_init(self, __context) -> None:  # type: ignore[override]
        object.__setattr__(self, "confidence", cap_confidence(self.confidence))


class MergeResult(BaseModel):
    pr_number: int
    merged: bool
    message: str = ""
    sha: str = ""
