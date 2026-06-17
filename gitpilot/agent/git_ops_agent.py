"""Pillar 5 — Git Operations Intelligence Engine.

Intercepts risky git commands and advises like a senior engineer. Every
operation follows: Analyze → Explain → Recommend → Request Approval → Execute
→ Report. GitPilot advises; the human decides.

Activated by ``gitpilot watch`` (installs git hooks) and available directly via
``gitpilot explain "<command>"``.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

from gitpilot.config.parser import GitPilotConfig
from gitpilot.config.safety import (
    cap_confidence,
    is_protected_branch,
    normalize_command,
    requires_approval,
)
from gitpilot.github.models import GitOpAnalysis, RepoState

logger = logging.getLogger("gitpilot.gitops")


class GitOpsAgent:
    def __init__(self, config: GitPilotConfig | None = None, repo_path: str | None = None,
                 github=None, db=None, llm=None):
        self.config = config
        self.repo_path = repo_path or os.getcwd()
        self.github = github
        self.db = db
        self.llm = llm

    # ------------------------------------------------------------------
    # Repo state
    # ------------------------------------------------------------------
    def collect_repo_state(self) -> RepoState:
        """Gather full repository context before any analysis (read-only)."""
        try:
            from git import Repo
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("GitPython not installed. Run 'pip install -e .'") from exc

        repo = Repo(self.repo_path, search_parent_directories=True)
        git_dir = Path(repo.git_dir)
        state = RepoState()

        try:
            state.current_branch = repo.active_branch.name
        except Exception:  # noqa: BLE001 — detached HEAD
            state.current_branch = "(detached HEAD)"

        extra = self.config.branches.protected if self.config else []
        state.is_protected_branch = is_protected_branch(state.current_branch, extra)

        # ahead/behind vs upstream
        try:
            tracking = repo.active_branch.tracking_branch()
            if tracking:
                ahead = sum(1 for _ in repo.iter_commits(f"{tracking.name}..{state.current_branch}"))
                behind = sum(1 for _ in repo.iter_commits(f"{state.current_branch}..{tracking.name}"))
                state.ahead_count, state.behind_count = ahead, behind
                state.remote_status = f"{ahead} ahead, {behind} behind {tracking.name}"
            else:
                state.remote_status = "no upstream tracking branch"
        except Exception as exc:  # noqa: BLE001
            state.remote_status = f"unknown ({exc})"

        # working tree status
        try:
            state.staged_files = [d.a_path for d in repo.index.diff("HEAD")]
        except Exception:  # noqa: BLE001
            state.staged_files = []
        state.unstaged_files = [d.a_path for d in repo.index.diff(None)]
        state.untracked_count = len(repo.untracked_files)

        # in-progress operations
        state.merge_in_progress = (git_dir / "MERGE_HEAD").exists()
        state.rebase_in_progress = (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()
        state.conflict_files = [
            path for path, blobs in repo.index.unmerged_blobs().items()
        ] if hasattr(repo.index, "unmerged_blobs") else []

        # recent commits (hash + subject only)
        try:
            state.recent_commits = [
                f"{c.hexsha[:7]} {c.message.splitlines()[0]}"
                for c in list(repo.iter_commits(max_count=5))
            ]
        except Exception:  # noqa: BLE001
            state.recent_commits = []

        # open PRs (best-effort; never fatal)
        if self.github is not None:
            try:
                state.open_prs = [f"#{pr.number} {pr.title}" for pr in self.github.get_open_prs()]
            except Exception:  # noqa: BLE001
                state.open_prs = []
        return state

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------
    def analyze_command(self, command: str) -> GitOpAnalysis:
        """Main entry point. Routes to a specific interceptor, then the LLM."""
        normalized = normalize_command(command)
        try:
            state = self.collect_repo_state()
        except Exception as exc:  # noqa: BLE001
            logger.error("Repo state collection failed: %s", exc)
            return GitOpAnalysis(
                command=normalized,
                prediction=f"Could not analyze — repo state unavailable: {exc}",
                risk_level="high", will_succeed="uncertain",
                warnings=["Repository state could not be read. Aborting analysis."],
                requires_approval=requires_approval(normalized),
            )

        analysis = self._route(normalized, state)
        analysis = self._enrich_with_llm(normalized, state, analysis)
        analysis.requires_approval = requires_approval(normalized)
        return analysis

    def _route(self, command: str, state: RepoState) -> GitOpAnalysis:
        if "push --force" in command or "push -f" in command:
            return self.intercept_force_push(command, state)
        if command.startswith("git push") or command.startswith("push"):
            return self.intercept_push(command, state)
        if "reset" in command:
            return self.intercept_reset(command, state)
        if "rebase" in command:
            return self.intercept_rebase(command, state)
        if "branch -D" in command or "branch -d" in command:
            return self.intercept_branch_delete(command, state)
        if "merge" in command:
            return self.intercept_merge(command, state)
        if "cherry-pick" in command:
            return self.intercept_cherry_pick(command, state)
        if "revert" in command:
            return self.intercept_revert(command, state)
        return self.general_analysis(command, state)

    # -- interceptors (deterministic risk seeds; LLM adds prose) --------
    def intercept_push(self, command: str, state: RepoState) -> GitOpAnalysis:
        warnings = []
        risk = "low"
        if state.behind_count > 0:
            warnings.append(f"Branch is {state.behind_count} commits behind remote — pull first.")
            risk = "moderate"
        if state.is_protected_branch:
            warnings.append("Pushing to a protected branch.")
            risk = "high"
        return GitOpAnalysis(command=command, risk_level=risk, warnings=warnings,
                             recommended_steps=["git fetch", "git pull --rebase"] if state.behind_count else [])

    def intercept_force_push(self, command: str, state: RepoState) -> GitOpAnalysis:
        risk = "critical" if state.is_protected_branch else "very-high"
        return GitOpAnalysis(
            command=command, risk_level=risk, will_succeed="yes",
            warnings=["Force push overwrites remote history — collaborators may lose work.",
                      "This rewrites commits already pushed to the remote."],
            recommended_steps=["Coordinate with your team first",
                               "Prefer --force-with-lease over --force"],
            alternatives=["git push --force-with-lease"],
            educational_note="Force push moves the remote ref to your local ref, discarding "
                             "remote commits not in your history.",
        )

    def intercept_reset(self, command: str, state: RepoState) -> GitOpAnalysis:
        hard = "--hard" in command
        risk = "critical" if hard else "high"
        warnings = []
        if hard and (state.staged_files or state.unstaged_files):
            warnings.append(f"{len(state.staged_files)} staged + {len(state.unstaged_files)} "
                            "unstaged changes will be permanently lost.")
        return GitOpAnalysis(
            command=command, risk_level=risk, warnings=warnings,
            recommended_steps=["git stash (save current work)",
                               "git branch backup-before-reset (safety net)"],
            alternatives=["git reset --soft <ref> (keeps changes staged — much safer)"]
            if hard else [],
            educational_note="reset moves HEAD; --hard also overwrites the index and working tree.",
        )

    def intercept_rebase(self, command: str, state: RepoState) -> GitOpAnalysis:
        warnings = ["Rebase rewrites history — commits get new SHAs."]
        if state.rebase_in_progress:
            warnings.append("A rebase is already in progress (continue or abort it first).")
        return GitOpAnalysis(
            command=command, risk_level="high", warnings=warnings,
            alternatives=["git merge (preserves history, no SHA rewrite)"],
            educational_note="rebase replays your commits on top of another base, creating new commits.",
        )

    def intercept_branch_delete(self, command: str, state: RepoState) -> GitOpAnalysis:
        branch = command.split()[-1] if command.split() else ""
        extra = self.config.branches.protected if self.config else []
        if is_protected_branch(branch, extra):
            return GitOpAnalysis(
                command=command, risk_level="critical", will_succeed="no",
                warnings=[f"'{branch}' is a protected branch — GitPilot blocks this deletion."],
                recommended_steps=["Protected branches cannot be deleted via GitPilot."],
            )
        return GitOpAnalysis(
            command=command, risk_level="high",
            warnings=["Force delete discards unmerged commits if any exist."],
            recommended_steps=["Verify the branch is merged: git branch --merged",
                               "Use 'git branch -d' (lowercase) to refuse unmerged deletes."],
        )

    def intercept_merge(self, command: str, state: RepoState) -> GitOpAnalysis:
        risk = "high" if state.is_protected_branch else "moderate"
        warnings = []
        if state.is_protected_branch:
            warnings.append("Merging into a protected branch — requires approval.")
        return GitOpAnalysis(
            command=command, risk_level=risk, warnings=warnings,
            alternatives=["git rebase (linear history)"] ,
            educational_note="merge combines histories, creating a merge commit unless fast-forward.",
        )

    def intercept_cherry_pick(self, command: str, state: RepoState) -> GitOpAnalysis:
        return GitOpAnalysis(
            command=command, risk_level="moderate",
            warnings=["Cherry-pick duplicates a commit onto the current branch (new SHA)."],
            educational_note="cherry-pick applies the diff of a specific commit as a new commit.",
        )

    def intercept_revert(self, command: str, state: RepoState) -> GitOpAnalysis:
        return GitOpAnalysis(
            command=command, risk_level="moderate",
            warnings=["Revert creates a new commit that undoes a previous one."],
            educational_note="revert is safe history-wise: it adds an inverse commit rather than removing one.",
        )

    def general_analysis(self, command: str, state: RepoState) -> GitOpAnalysis:
        return GitOpAnalysis(command=command, risk_level="low")

    # ------------------------------------------------------------------
    def _enrich_with_llm(self, command: str, state: RepoState, seed: GitOpAnalysis) -> GitOpAnalysis:
        if self.llm is None:
            seed.confidence = cap_confidence(seed.confidence or 60)
            return seed
        from gitpilot.llm.prompts import GIT_OPERATION_ANALYSIS_PROMPT, SYSTEM_ADVISOR, render
        from gitpilot.llm.resolver import AllProvidersExhausted

        try:
            prompt = render(
                GIT_OPERATION_ANALYSIS_PROMPT,
                command=command, current_branch=state.current_branch,
                remote_status=state.remote_status,
                ahead_behind=f"{state.ahead_count} ahead / {state.behind_count} behind",
                staged_changes=", ".join(state.staged_files) or "none",
                unstaged_changes=", ".join(state.unstaged_files) or "none",
                untracked_files=state.untracked_count,
                open_prs="; ".join(state.open_prs) or "none",
                is_protected=state.is_protected_branch,
                recent_commits="; ".join(state.recent_commits) or "none",
            )
            text, provider, conf = self.llm.call(prompt, system=SYSTEM_ADVISOR)
            self._merge_llm_fields(seed, text)
            seed.llm_provider = provider
            seed.confidence = cap_confidence(conf)
        except AllProvidersExhausted:
            seed.warnings.append("AI advice unavailable — all providers exhausted.")
            seed.confidence = cap_confidence(seed.confidence or 50)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM enrichment failed: %s", exc)
            seed.confidence = cap_confidence(seed.confidence or 50)
        return seed

    @staticmethod
    def _merge_llm_fields(seed: GitOpAnalysis, text: str) -> None:
        """Parse the labeled LLM response into the structured analysis fields.

        Deterministic safety values from the interceptor (risk level, the core
        warnings/steps) take precedence; the LLM only fills gaps and adds prose.
        """
        fields = _split_labeled(text)
        seed.prediction = fields.get("PREDICTION", "").strip() or seed.prediction
        will = fields.get("WILL_SUCCEED", "").strip().lower()
        if will in ("yes", "no", "uncertain"):
            seed.will_succeed = will
        for w in _as_list(fields.get("WARNINGS", "")):
            if w and w not in seed.warnings:
                seed.warnings.append(w)
        if not seed.recommended_steps:
            seed.recommended_steps = _as_list(fields.get("RECOMMENDED_STEPS", ""))
        if not seed.alternatives:
            seed.alternatives = _as_list(fields.get("ALTERNATIVES", ""))
        if not seed.educational_note:
            seed.educational_note = fields.get("EDUCATIONAL_NOTE", "").strip()

    # ------------------------------------------------------------------
    # Approval (typed confirmations for high-risk ops)
    # ------------------------------------------------------------------
    def request_approval(self, analysis: GitOpAnalysis) -> bool:
        """Prompt the human. Force push & critical require a typed phrase."""
        import typer

        normalized = normalize_command(analysis.command)
        if analysis.will_succeed == "no" and analysis.risk_level == "critical":
            # Hard-blocked operations (e.g. protected branch delete).
            return False

        if "push --force" in normalized or "push -f" in normalized:
            phrase = "yes, force push"
            answer = typer.prompt(f'Type "{phrase}" to proceed (anything else cancels)', default="")
            approved = answer.strip().lower() == phrase
        elif analysis.risk_level in ("critical", "very-high"):
            phrase = "yes, i understand the risk"
            answer = typer.prompt(f'Type "{phrase}" to proceed (anything else cancels)', default="")
            approved = answer.strip().lower() == phrase
        else:
            approved = typer.confirm("Proceed with original command?", default=False)

        if self.db is not None:
            self.db.log_audit(
                action="git_op", target=normalized,
                status="approved" if approved else "rejected",
                approved_by="human" if approved else None,
                risk_level=analysis.risk_level, confidence=analysis.confidence,
                llm_provider=analysis.llm_provider,
            )
        return approved

    def educational_explain(self, command: str, analysis: GitOpAnalysis) -> str:
        """Extra plain-language explanation when educational_mode is on."""
        if self.llm is None:
            return analysis.educational_note or ""
        from gitpilot.llm.prompts import EDUCATIONAL_EXPLAIN_PROMPT, SYSTEM_ADVISOR, render
        try:
            text, _, _ = self.llm.call(
                render(EDUCATIONAL_EXPLAIN_PROMPT, command=command), system=SYSTEM_ADVISOR
            )
            return text.strip()
        except Exception:  # noqa: BLE001
            return analysis.educational_note or ""

    # ------------------------------------------------------------------
    # Hook installation
    # ------------------------------------------------------------------
    def install_hooks(self, repo_path: str | None = None) -> list[str]:
        """Install pre-push and pre-commit hooks. Backs up existing hooks."""
        repo_path = repo_path or self.repo_path
        hooks_dir = Path(repo_path) / ".git" / "hooks"
        if not hooks_dir.parent.exists():
            raise RuntimeError(f"Not a git repository: {repo_path}")
        hooks_dir.mkdir(parents=True, exist_ok=True)

        installed = []
        for hook in ("pre-push", "pre-commit"):
            target = hooks_dir / hook
            if target.exists():
                backup = target.with_suffix(".backup")
                if not backup.exists():
                    target.replace(backup)
            script = f'#!/bin/sh\n# Installed by GitPilot\ngitpilot _hook {hook} "$@"\n'
            target.write_text(script, encoding="utf-8", newline="\n")
            target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
            installed.append(str(target))
        return installed


def _split_labeled(text: str) -> dict[str, str]:
    labels = ["PREDICTION", "RISK_LEVEL", "WILL_SUCCEED", "WARNINGS",
              "RECOMMENDED_STEPS", "ALTERNATIVES", "CONFIDENCE", "EDUCATIONAL_NOTE"]
    out: dict[str, str] = {}
    current = None
    for line in (text or "").splitlines():
        stripped = line.strip().lstrip("0123456789. ")
        matched = next((lbl for lbl in labels if stripped.upper().startswith(lbl)), None)
        if matched:
            current = matched
            out[current] = stripped[len(matched):].lstrip(": ").strip()
        elif current:
            out[current] += "\n" + line
    return out


def _as_list(text: str) -> list[str]:
    items: list[str] = []
    for line in (text or "").splitlines():
        item = line.strip().strip("-•*").strip()
        if item and item.lower() not in ("none", "[]", "n/a"):
            items.append(item)
    return items
