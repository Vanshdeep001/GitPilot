"""Pillar 3 — Conflict Intelligence.

CRITICAL: This pillar NEVER writes files, commits, or pushes. It analyzes and
suggests only. Every suggestion is labeled "SUGGESTION ONLY — human review and
implementation required". The conflict is never claimed to be resolved.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile

from gitpilot.config.parser import GitPilotConfig
from gitpilot.config.safety import cap_confidence
from gitpilot.github.client import GitHubClient
from gitpilot.github.models import ConflictAnalysis, ConflictHunk, FileConflictAnalysis

logger = logging.getLogger("gitpilot.conflict")

SUGGESTION_LABEL = "SUGGESTION ONLY — Human review and implementation required"

_LANG_BY_EXT = {
    ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".jsx": "JavaScript", ".java": "Java", ".go": "Go", ".rs": "Rust", ".rb": "Ruby",
    ".c": "C", ".h": "C", ".cpp": "C++", ".cs": "C#", ".php": "PHP", ".kt": "Kotlin",
    ".swift": "Swift", ".yml": "YAML", ".yaml": "YAML", ".json": "JSON", ".md": "Markdown",
    ".sql": "SQL", ".sh": "Shell",
}


class ConflictAgent:
    def __init__(self, github: GitHubClient, config: GitPilotConfig, db=None, llm=None):
        self.github = github
        self.config = config
        self.db = db
        self.llm = llm

    # ------------------------------------------------------------------
    def analyze(self, pr_number: int) -> ConflictAnalysis:
        """Analyze conflicts for a PR. Suggest only — never apply, commit, or push."""
        temp_dir = tempfile.mkdtemp(prefix="gitpilot-conflict-")
        analysis = ConflictAnalysis(pr_number=pr_number)
        try:
            conflicted = self._materialize_conflicts(pr_number, temp_dir)
            per_file: list[FileConflictAnalysis] = []
            for path, content in conflicted.items():
                size_kb = len(content.encode("utf-8")) / 1024
                if size_kb > self.config.conflicts.max_file_size_kb:
                    logger.info("Skipping large conflicted file: %s (%.1f KB)", path, size_kb)
                    continue
                hunks = self.extract_conflict_hunks(content)
                if not hunks:
                    continue
                per_file.append(self._analyze_file(path, hunks))

            analysis.files_analyzed = list(conflicted.keys())
            analysis.per_file_analysis = per_file
            analysis.overall_risk = _aggregate_risk(per_file)
            analysis.overall_confidence = cap_confidence(
                min((f.confidence for f in per_file), default=0)
            )
            analysis.estimated_human_time = _estimate_time(per_file)

            if self.config.conflicts.post_suggestion_as_comment and per_file:
                posted = self.github.post_comment(pr_number, _format_comment(analysis))
                analysis.suggestions_posted = posted
            return analysis
        except Exception as exc:  # noqa: BLE001
            logger.error("Conflict analysis failed for #%s: %s", pr_number, exc)
            return analysis
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    def extract_conflict_hunks(self, file_content: str) -> list[ConflictHunk]:
        """Parse git conflict markers into structured hunks (multiple per file)."""
        lines = file_content.splitlines()
        hunks: list[ConflictHunk] = []
        i = 0
        n = len(lines)
        while i < n:
            if lines[i].startswith("<<<<<<<"):
                ours: list[str] = []
                theirs: list[str] = []
                i += 1
                while i < n and not lines[i].startswith("======="):
                    ours.append(lines[i])
                    i += 1
                i += 1  # skip =======
                while i < n and not lines[i].startswith(">>>>>>>"):
                    theirs.append(lines[i])
                    i += 1
                start = max(0, len(ours))  # placeholder; real context computed below
                context_before = "\n".join(lines[max(0, i - len(ours) - len(theirs) - 22):])
                hunks.append(ConflictHunk(
                    ours="\n".join(ours),
                    theirs="\n".join(theirs),
                    context_before="",   # filled by _with_context
                    context_after="",
                ))
            i += 1
        return self._with_context(file_content, hunks)

    def _with_context(self, content: str, hunks: list[ConflictHunk]) -> list[ConflictHunk]:
        """Attach 20 lines of context before/after each hunk's marker block."""
        lines = content.splitlines()
        markers = [idx for idx, l in enumerate(lines) if l.startswith("<<<<<<<")]
        ends = [idx for idx, l in enumerate(lines) if l.startswith(">>>>>>>")]
        for h, start, end in zip(hunks, markers, ends):
            before = lines[max(0, start - 20):start]
            after = lines[end + 1:end + 21]
            h.context_before = "\n".join(before)
            h.context_after = "\n".join(after)
        return hunks

    # ------------------------------------------------------------------
    def _analyze_file(self, path: str, hunks: list[ConflictHunk]) -> FileConflictAnalysis:
        language = _detect_language(path)
        if self.llm is None:
            return FileConflictAnalysis(
                file_path=path, language=language,
                explanation="AI unavailable — manual review required.",
                suggested_resolution=SUGGESTION_LABEL, confidence=0,
            )

        from gitpilot.llm.prompts import CONFLICT_ANALYSIS_PROMPT, SYSTEM_ADVISOR, render
        from gitpilot.llm.resolver import AllProvidersExhausted

        primary = hunks[0]
        try:
            prompt = render(
                CONFLICT_ANALYSIS_PROMPT,
                file_path=path, language=language,
                context_before=primary.context_before, ours=primary.ours,
                theirs=primary.theirs, context_after=primary.context_after,
            )
            text, provider, conf = self.llm.call(prompt, system=SYSTEM_ADVISOR)
            return _parse_conflict_response(path, language, text, conf)
        except AllProvidersExhausted:
            return FileConflictAnalysis(
                file_path=path, language=language,
                explanation="All LLM providers exhausted — manual review required.",
                suggested_resolution=SUGGESTION_LABEL, confidence=0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Conflict analysis failed for %s: %s", path, exc)
            return FileConflictAnalysis(
                file_path=path, language=language,
                explanation=f"Analysis error: {exc}",
                suggested_resolution=SUGGESTION_LABEL, confidence=0,
            )

    def _materialize_conflicts(self, pr_number: int, temp_dir: str) -> dict[str, str]:
        """Clone into a temp dir and surface real conflict markers via a trial merge.

        Clones the repo (read-only intent), checks out the PR base branch, and
        attempts ``git merge --no-commit --no-ff`` of the head branch. Files left
        unmerged then contain genuine ``<<<<<<<`` markers, which we read back for
        hunk parsing. Nothing here is ever committed, pushed, or written outside
        ``temp_dir`` (the caller removes it in a finally block).
        """
        from git import GitCommandError, Repo

        results: dict[str, str] = {}
        try:
            pr = self.github.get_pr(pr_number)
            base, head = pr.base.ref, pr.head.ref
            full = self.github.repo_full_name
            token = getattr(self.github, "token", None)
            # Token is embedded only in the ephemeral temp-dir remote; never logged.
            url = (f"https://x-access-token:{token}@github.com/{full}.git"
                   if token else f"https://github.com/{full}.git")

            repo = Repo.clone_from(url, temp_dir)
            repo.git.checkout(base)
            try:
                repo.git.merge(f"origin/{head}", "--no-commit", "--no-ff")
            except GitCommandError:
                pass  # expected: a conflicting merge exits non-zero

            unmerged = repo.index.unmerged_blobs()
            for path in unmerged:
                fpath = os.path.join(temp_dir, path)
                if os.path.exists(fpath):
                    with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                        results[path] = fh.read()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not materialize conflicts for #%s: %s", pr_number, exc)
        return results


def _detect_language(path: str) -> str:
    return _LANG_BY_EXT.get(os.path.splitext(path)[1].lower(), "unknown")


def _parse_conflict_response(path, language, text, conf) -> FileConflictAnalysis:
    """Best-effort parse of the labeled fields the prompt requests."""
    fields = _split_labeled(text)
    return FileConflictAnalysis(
        file_path=path, language=language,
        explanation=fields.get("EXPLANATION", "").strip(),
        classification=(fields.get("CLASSIFICATION", "unknown").strip().lower() or "unknown"),
        risk=(fields.get("RISK", "low").strip().lower() or "low"),
        suggested_resolution=(fields.get("SUGGESTED_RESOLUTION", "").strip()
                              or SUGGESTION_LABEL),
        human_review_notes=fields.get("HUMAN_REVIEW_NOTES", "").strip(),
        alternatives=[a.strip("-• ").strip()
                      for a in fields.get("ALTERNATIVES", "").splitlines() if a.strip()],
        confidence=cap_confidence(_extract_int(fields.get("CONFIDENCE", ""), conf)),
    )


def _split_labeled(text: str) -> dict[str, str]:
    labels = ["EXPLANATION", "CLASSIFICATION", "RISK", "SUGGESTED_RESOLUTION",
              "CONFIDENCE", "HUMAN_REVIEW_NOTES", "ALTERNATIVES"]
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


def _extract_int(text: str, fallback: int) -> int:
    import re
    m = re.search(r"\d+", text or "")
    return int(m.group()) if m else fallback


def _aggregate_risk(per_file: list[FileConflictAnalysis]) -> str:
    order = ["low", "medium", "high", "critical"]
    worst = "low"
    for f in per_file:
        if order.index(f.risk if f.risk in order else "low") > order.index(worst):
            worst = f.risk
    return worst


def _estimate_time(per_file: list[FileConflictAnalysis]) -> str:
    if not per_file:
        return "n/a"
    minutes = sum({"low": 5, "medium": 15, "high": 30, "critical": 60}.get(f.risk, 10)
                  for f in per_file)
    return f"~{minutes} min" if minutes < 60 else f"~{minutes // 60}h {minutes % 60}m"


def _format_comment(analysis: ConflictAnalysis) -> str:
    lines = [f"### GitPilot — Conflict analysis (PR #{analysis.pr_number})",
             "", f"> **{SUGGESTION_LABEL}**", "",
             f"Overall risk: **{analysis.overall_risk}** · "
             f"Confidence: {analysis.overall_confidence}% · "
             f"Estimated human time: {analysis.estimated_human_time}", ""]
    for f in analysis.per_file_analysis:
        lines += [f"#### `{f.file_path}` ({f.language}) — {f.classification}, risk {f.risk}",
                  f.explanation, ""]
        if f.suggested_resolution:
            lines += [f"**Suggested resolution ({SUGGESTION_LABEL}):**",
                      "```", f.suggested_resolution, "```", ""]
        if f.human_review_notes:
            lines += [f"**Verify before accepting:** {f.human_review_notes}", ""]
        if f.alternatives:
            lines += ["**Alternatives:**"] + [f"- {a}" for a in f.alternatives] + [""]
    lines.append("_GitPilot suggests; a human reviews and implements. "
                 "GitPilot never resolves conflicts itself._")
    return "\n".join(lines)
