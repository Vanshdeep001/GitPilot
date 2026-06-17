# GitPilot

**AI-powered Git Operations Engineer** — explains, warns, assists, and executes only after *your* approval.

GitPilot is **not** an autonomous decision maker, **not** a code-writing agent, and **not** a
replacement for developers. It is a trusted advisor that follows one pipeline for every operation:

```
Analyze → Explain → Recommend → Request Approval → Execute → Report Outcome
```

Humans remain the final authority over all repository operations at all times.

---

## Five Pillars

1. **PR Intelligence** — evaluates PRs, recommends merges, explains what is blocking them.
2. **Branch Management** — monitors branch health, warns about stale branches, recommends cleanup.
3. **Conflict Intelligence** — analyzes conflicts, explains them, suggests resolutions for human review.
4. **Release Intelligence** — tracks release readiness, changelog generation, tag recommendations.
5. **Git Operations Intelligence Engine** — intercepts risky git commands, analyzes repo state,
   explains what will happen, and asks for approval before executing.

---

## Install

```bash
pip install -e .
```

## Usage

```bash
gitpilot auth                          # store all keys in the OS keychain
gitpilot init                          # configure repo, register webhook
gitpilot dry-run                       # see recommendations without executing

gitpilot start                         # start background daemon
gitpilot status                        # show health + today's counts
gitpilot prs                           # PR readiness table
gitpilot branches                      # branch health table
gitpilot conflicts                     # conflict analysis table
gitpilot release                       # release readiness + changelog draft
gitpilot log                           # live tail of agent decisions

gitpilot watch                         # install git hooks in current repo
gitpilot explain "git rebase main"     # analyze without running

gitpilot approve <action_id>           # approve a pending recommended action
gitpilot ignore release/v2             # protect a branch from agent

gitpilot stop                          # clean shutdown
```

---

## Safety guarantees (hardcoded, non-overridable)

- Tokens are stored only in the OS keychain via `keyring` — never in plaintext.
- Secret-like patterns are blocked before any prompt is sent to an external LLM.
- Protected branches can never be deleted automatically under any condition.
- No merge, delete, rebase, reset, or force-push executes without `approved_by='human'`.
- Confidence is hard-capped at 95% — GitPilot never reports certainty.
- Conflict suggestions are always labeled "SUGGESTION ONLY — human review required".

See [`gitpilot/config/safety.py`](gitpilot/config/safety.py) for the full, hardcoded ruleset.

> **Status:** v0.1.0 — all five pillars and the automatic webhook daemon have been
> live-tested end-to-end against a real GitHub repo (PR evaluation, branch health,
> conflict clone+merge analysis, release readiness, and the git-ops safety engine).
> Remaining refinements: the 6-hour branch-scan scheduler is not yet wired into the
> daemon loop.
