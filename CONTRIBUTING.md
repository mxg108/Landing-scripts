# Contributing to Landing Scripts

> Conventions for working in this repository. Short by design — keep this file
> readable. If a rule needs a paragraph to justify, link out instead of expanding
> here.

This repo holds three production systems (`mass-notifications`, `qa-automation`,
`qa-automation/AI-Scoring`) that ship via different mechanisms. The rules below
exist so two-plus people can work on it without stepping on each other or
silently breaking a deployment.

---

## Branches

One branch per logical change. Branch off `main`, never directly off another
in-flight branch.

**Prefix by intent**, matching the convention in `git log`:

| Prefix | When |
|---|---|
| `feat/` | New capability (new feature, new endpoint, new template). |
| `fix/`  | Bug repair on existing behavior. |
| `chore/`| Tooling, dependencies, CI, repo hygiene. |
| `docs/` | Documentation only — no code changes. |
| `refactor/` | Code reshape with no behavior change. |

Slug should be terse and descriptive: `fix/looker-sync-targets-mass-notification`,
not `fix/maxs-bug` or `fix/issue-23`.

---

## Commits

Conventional Commits, with the project scope when it's not obvious:

```
fix(mass-notifications): Looker sync always targets Mass_Notification
feat(qa-automation): Sales Phase F — manual_yn schema + colocated FR-AI
chore: bump version strings to v3.3.1
docs: PhaseThree.md — Step 9 (PostgreSQL migration) design doc
```

Body lines explain **why**, not what (the diff already shows what). A bug-fix
commit names the symptom and the root cause; a feature commit names the
constraint or use case it solves.

One logical change per commit. If you find yourself writing "and also" in the
subject line, split it.

---

## Pull requests

- **All changes to `main` go through a PR.** No direct pushes, no exceptions.
- One reviewer approval required before merge.
- **Open early as a draft** when you want feedback or want to make work visible.
  Mark "Ready for review" when it's actually ready.
- PR body should explain the change well enough that the reviewer doesn't have
  to read the diff to understand intent. Include a **Test plan** section with a
  bulleted checklist of how the change was verified.
- Keep PRs small. A reviewer can give a useful pass on 200 lines; on 2,000 they
  rubber-stamp. If your change is naturally large, split into a stacked series
  of PRs that each compile and test on their own.

---

## Syncing your branch

If your PR sits open while `main` moves, bring `main` into your branch:

```bash
git checkout main && git pull
git checkout your-branch
git merge main
# resolve any conflicts, commit, push
```

**Do not rebase a pushed branch** that another person might have pulled. Merging
from main produces an extra merge commit; that's fine. Rewriting shared history
is not.

---

## Deployments ≠ merges

This is the most important rule for newcomers to the repo. **Merging to `main`
does not deploy anything.** Each system ships through its own pipeline:

| System | Deploy via | Notes |
|---|---|---|
| `mass-notifications` | `./push.sh mass-notifications` | Pushes to Apps Script. WebApp also needs a new deployment version via the Apps Script UI. |
| `qa-automation` (per team) | `./push.sh qa-member-support` / `qa-sales` | Apps Script, multi-team overlay. `qa-member-support` is flagged LIVE — confirm twice. |
| `qa-automation/AI-Scoring` | Railway auto-deploy on push to `main` | **Only commits inside `qa-automation/AI-Scoring/` trigger a deploy** (Railway Watch Paths). Other paths get SKIPPED; use Railway's "Deploy Latest Commit" button to force. |

`push.sh` shows the file manifest and aborts on a zero-file misconfiguration —
read it before typing `yes`. Successful pushes are logged to `.push-log`
(gitignored, local-only).

Never deploy from a dirty working tree unless you've reviewed exactly what's
dirty. The wrapper will warn but not block.

---

## AI-Scoring operator notes

### API key tiers

`qa-automation/AI-Scoring` has two key tiers, both Bearer tokens in env vars:

| Env var | Role | Reach |
|---|---|---|
| `API_KEY_MEMBER_SUPPORT`, `API_KEY_SALES` | `team` | Locked to that team's `/api/{team_id}/...` routes; only that team's Mails roster can be scored. |
| `API_KEY_PRIVILEGED` | `privileged` | Cross-team. Bypasses the path-team check and the roster check; the frontend's team-pick dialog supplies the target team when the agent isn't rostered anywhere. |

Generate a new key with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Both `.env.example` and the Railway env are the source of truth — keep them in sync.

### One-time setup

The audit log lives in a single `Score_Audit` tab on the **member_support** Google Sheet (one row per `/score` POST and per `/score/{job_id}/approve`, regardless of `target_team`). Create it once per Sheets host:

```bash
cd qa-automation/AI-Scoring
python3 scripts/init_score_audit_tab.py [--dry-run]
```

The script is idempotent — re-running it on an existing tab is a no-op.

### End-to-end smoke

`scripts/score_by_call_id.py` exercises the full Lookup-to-Score pipeline against a running backend: `/score` (Dialpad download → Gemini scoring → FR-AI write → audit row) → poll → `/score/{job_id}/approve` (Stage 1.5 → 2 → 3 → 4 → Apps Script email dispatch). **Real side effects** — writes to Sheets and sends a QA evaluation email.

```bash
AI_SCORING_API_KEY=$API_KEY_PRIVILEGED \
  python3 scripts/score_by_call_id.py \
    --team member_support \
    --call-id <dialpad-call-id> \
    --agent-email agent@hellolanding.com \
    --manager-email you@hellolanding.com
```

Pass `--score-only` to stop after scoring (skips the approve step + email). `--yes` skips the approve confirmation prompt; reserve it for CI.

### Pages

| Path | Purpose |
|---|---|
| `/score/{team}` | Upload-driven scoring — drag a recording, fill in metadata, score, approve. |
| `/lookup/{team}` | Search a Dialpad agent by email, list their calls, one-click **Score Call** per row (no upload — backend downloads via `download_recording`). |
| `/scorecard/{team}/{job_id}` | Dedicated editor for a single in-progress or completed scoring job. Reached via the **Open editor** link from `/lookup` after scoring completes. |

The API key + manager email are stored in `localStorage` (shared across tabs), so the typical flow is: enter once on first page load → reused everywhere until the browser closes or you clear storage.

---

## Substantial refactors

For any change estimated at **more than half a day** of work, write a design
doc first. The doc lives in the relevant project directory (see
`mass-notifications/PhaseTwo.md`, `qa-automation/AI-Scoring/docs/PhaseThree.md`
for examples). The flow is:

1. **Doc** — state the problem, the chosen approach, and what's explicitly out
   of scope. PR-review the doc before any code.
2. **Schema / config** — land the data-shape changes (JSON, SQL, sheet columns)
   in a separate PR with pytest fixtures asserting the new shape.
3. **Code** — implement against the locked schema, with checkpoint commits that
   each pass tests.

The doc is the contract. Skipping it on real refactors burns hours rediscovering
constraints in code review.

---

## Things to never do

- **Force-push to `main`** or to any shared branch. Use new commits to fix
  mistakes; let history reflect what actually happened.
- **Skip hooks** (`--no-verify`, `--no-gpg-sign`). If a hook fails, fix the
  underlying issue.
- **Commit secrets.** Credentials, API keys, `.env` files, Looker dashboard
  references, internal IDs. `.gitignore` already covers the known offenders;
  `git diff --staged` before every commit catches the unknown ones.
- **Run dev servers from a clone with real PII.** Static analysis is fine;
  serving traffic is not.
- **Bypass `push.sh`** with a manual `clasp push` unless you know exactly which
  rootDir would resolve. The wrapper's manifest preview and zero-file abort
  exist because direct pushes silently no-op'd for weeks once.

---

## Asking questions

If you read this file and something is still unclear, that's a documentation
bug — flag it in your PR or open a `docs/` PR fixing it. The recruit-onboarding
walkthrough (`docs/git-for-the-recruit.md`, local-only) is the place for
Git-101 explanations; this file stays a reference, not a tutorial.
