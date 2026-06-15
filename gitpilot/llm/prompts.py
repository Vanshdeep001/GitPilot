"""LLM prompt templates.

Rules for every prompt here:
- Always ask for a confidence score (0 to MAX_CONFIDENCE — never 100%).
- Always ask the model to surface uncertainty explicitly.
- Always ask for alternatives.
- Never ask the LLM to make a final decision — only recommendations.

Templates use ``str.format`` placeholders. ``MAX_CONFIDENCE`` is the only value
baked in at import time; everything else is filled by the caller at render time.
"""

from __future__ import annotations

from gitpilot.config.safety import MAX_CONFIDENCE

# A short system prompt shared by all calls to reinforce the advisory stance.
SYSTEM_ADVISOR = (
    "You are GitPilot, a senior Git operations advisor. You recommend and explain; "
    "you never make final decisions and never claim certainty. Always cap confidence "
    f"at {MAX_CONFIDENCE} and never report 100%. If you are unsure, say so explicitly."
)

CONFLICT_ANALYSIS_PROMPT = """\
You are an expert software engineer helping a developer understand a Git merge conflict.
You are NOT resolving the conflict. You are SUGGESTING for human review only.

File: {file_path}
Language: {language}

Context before conflict:
{context_before}

Our version (HEAD / target branch):
{ours}

Their version (incoming branch):
{theirs}

Context after conflict:
{context_after}

Respond with ALL of these fields:
1. EXPLANATION: Plain English — why does this conflict exist? (2-3 sentences)
2. CLASSIFICATION: One of [formatting / logic / dependency / merge-artifact / api-change / unknown]
3. RISK: One of [low / medium / high / critical]
4. SUGGESTED_RESOLUTION: Code suggestion — label this clearly as "SUGGESTION ONLY — Human review required"
5. CONFIDENCE: Integer 0 to {max_confidence} — never higher
6. HUMAN_REVIEW_NOTES: What must the developer verify before accepting this suggestion?
7. ALTERNATIVES: At least one other resolution approach

You are providing a suggestion. The developer makes the final decision.
"""

BRANCH_CLASSIFICATION_PROMPT = """\
You are a Git repository analyst. Classify this branch based on its metadata.

Branch name: {branch_name}
Age (days since last push): {age_days}
Last commit message: {last_commit}
Has open PR: {has_open_pr}
PR status: {pr_status}
Commits ahead of main: {commits_ahead}

Classify as exactly ONE of:
- stale: abandoned branch, safe to recommend deletion
- active: recent work, do not recommend deletion
- release: appears to be a release/version branch — must be protected
- uncertain: cannot determine — escalate to human

Respond in JSON only:
{{
  "classification": "<stale|active|release|uncertain>",
  "confidence": <integer 0-{max_confidence}>,
  "reasoning": "<one sentence>"
}}
"""

GIT_OPERATION_ANALYSIS_PROMPT = """\
You are a senior Git engineer advising a developer BEFORE they run a Git command.
You are advising — the developer decides whether to proceed.

Command: {command}
Current branch: {current_branch}
Remote status: {remote_status}
Ahead/behind: {ahead_behind}
Staged changes: {staged_changes}
Unstaged changes: {unstaged_changes}
Untracked files: {untracked_files}
Open PRs: {open_prs}
Protected branch: {is_protected}
Recent commits: {recent_commits}

Respond with ALL of these fields:
1. PREDICTION: What will happen if this command runs (be specific)
2. RISK_LEVEL: One of [very-low / low / moderate / high / very-high / critical]
3. WILL_SUCCEED: yes / no / uncertain
4. WARNINGS: Specific risks as a list (empty list if none)
5. RECOMMENDED_STEPS: Ordered list of safer steps to achieve the same goal
6. ALTERNATIVES: Safer alternative commands
7. CONFIDENCE: Integer 0-{max_confidence}
8. EDUCATIONAL_NOTE: One sentence — what Git does internally with this command

You are advising. The human decides.
"""

PR_EVALUATION_PROMPT = """\
You are a senior engineer reviewing whether a PR is ready to merge.
You are recommending — a human makes the final merge decision.

PR title: {title}
PR description: {description}
Files changed: {files_changed}
CI status: {ci_status}
Review count: {review_count}
Labels: {labels}
Branch: {head_branch} to {base_branch}
Base branch protected: {is_protected}
Days open: {days_open}
Conflict status: {conflict_status}

Respond with ALL of these fields:
1. RECOMMENDATION: One of [ready-to-merge / needs-review / blocked / needs-attention]
2. BLOCKING_REASONS: Specific reasons blocking merge (empty list if ready)
3. WARNINGS: Non-blocking concerns
4. CONFIDENCE: Integer 0-{max_confidence}
5. SUMMARY: One sentence for the developer

You are recommending. A human approves the merge.
"""

RELEASE_READINESS_PROMPT = """\
You are a release engineer assessing if a codebase is ready to release.

Current version: {current_version}
Merged PRs since last release: {merged_prs}
CI status on main: {ci_status}
Open critical PRs: {open_critical_prs}
Days since last release: {days_since_release}
Changelog draft: {changelog_draft}

Respond with:
1. READINESS: One of [ready / needs-attention / not-ready]
2. SUGGESTED_VERSION: Semantic version suggestion (patch/minor/major reasoning)
3. BLOCKING_ISSUES: List of issues that must be resolved before release
4. WARNINGS: Non-blocking concerns
5. CONFIDENCE: Integer 0-{max_confidence}
6. CHANGELOG_CATEGORIES: Categorize the PRs into Features / Bug Fixes / Breaking Changes / Other
"""

EDUCATIONAL_EXPLAIN_PROMPT = """\
A developer is about to run this Git command: {command}

Explain, in plain language and without recommending any dangerous shortcuts:
1. What Git is doing internally (object model, refs) for this command.
2. Why this command exists and its intended use case.
3. Common mistakes developers make with it.
4. When to use it vs. when to avoid it.
5. A link to the relevant official Git documentation.

Keep it concise and educational. Never suggest skipping safety steps.
"""


def render(template: str, **kwargs) -> str:
    """Render a prompt template, injecting MAX_CONFIDENCE automatically.

    Callers pass the template-specific fields; ``max_confidence`` is supplied
    here so no template can accidentally request a higher cap.
    """
    kwargs.setdefault("max_confidence", MAX_CONFIDENCE)
    return template.format(**kwargs)
