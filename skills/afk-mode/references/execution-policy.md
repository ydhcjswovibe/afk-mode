# AFK Mode Execution Policy

## Truth Source Order

Use the current repository only.

Preferred order:

1. current contract docs such as `SPEC.md` or `docs/SPEC.md`
2. forward-looking docs such as `*mvp*.md`, roadmap, or requirements docs
3. repo-local workflow state under `.codex/work/*/STATUS.json`
4. repo-local routing docs such as `AGENTS.md` when they materially change the
   execution path

Do not invent scope beyond what those sources support.

## Candidate Ranking

Prefer slices that are:

- explicitly implied by present or near-term design docs
- small enough to verify in one bounded pass
- machine-verifiable with focused tests or compile checks
- unlikely to require secrets, credentials, or manual browser interaction
- unlikely to conflict with already-dirty user work

Avoid slices that are:

- cross-repo
- daemon or scheduler work outside the current repo
- broad refactors without a crisp verification surface
- blocked on unclear product intent
- already closed by recent work items

## Isolation Policy

- Never write on the active user branch.
- Use a separate temporary worktree and branch for each slice.
- Branch name format: `afk/<run-id>/<ordinal>-<slug>`.
- Successful slices may be committed only after verification passes.
- Failed slices must be archived as patches, then the disposable worktree may
  be removed.

## Failure Policy

- Preserve each failed slice as a patch artifact.
- Record the failure reason in `run.json`.
- Continue to the next viable slice while time remains.
- Stop early if every remaining candidate depends on the failed slice.

## Dirty Repo Handling

If the active repo has uncommitted changes:

- call that out explicitly
- explain that isolated slice branches will start from committed `HEAD`
- ask whether to continue or stop after candidate ranking

Do not silently branch from a stale baseline when the user's uncommitted work
materially changes likely candidate files.
