---
name: afk-mode
description: "Use when the user wants a budgeted overnight coding run in the current repository: ask for a runtime budget, inspect the repo's design docs and workflow state, rank safe implementation slices, execute each slice in an isolated temporary branch/worktree, verify successful slices, auto-commit them on slice branches, and archive failed slices as patch artifacts. Do not use for native slash-command development, daemonized background scheduling, or cross-repo planning."
---

# AFK Mode

Use this skill for explicit overnight or time-boxed autonomous work in the
current repository. This skill is bundled by a local plugin, but invocation
remains explicit through `$afk-mode` or selection via `/skills`.

Architectural direction for the plugin lives in
`references/v2-architecture.md`. Prefer that document when reasoning about
future changes to trust, policy ownership, verification, or guardrails.

## When To Use It

Use `$afk-mode` when the user wants all of the following:

- current-repo work only
- a runtime budget decided at the start of the run
- candidate slices chosen from the repo's actual design truth
- isolated execution on temporary branches/worktrees
- successful slices auto-committed
- failed slices preserved as patch artifacts, then skipped

Do not use this skill when:

- the request is to add a new native Codex slash command
- the user expects a daemon, unattended scheduler, or OS-level background job
- the request spans multiple repositories
- the repo has no credible design docs or no safe verification path
- the repo is still in `observe_only` or `assistive` mode and the user does not
  want to add a repo profile or bootstrap a reviewed local overlay

## Workflow

### 1. Ask For Budget

If the user did not already provide a runtime budget, ask one short question:

`How long should this afk run work?`

Accept simple duration answers such as `90m`, `4h`, or `6h30m`.

Once the budget is known, default to starting the run immediately. Do not stop
to narrate or wait unless the runtime reports a concrete blocker.

### 2. Begin The Run Immediately

After you have the budget, call the helper's auto-start entrypoint first:

```bash
python3 scripts/afk_mode.py begin-run --cwd "$PWD" --budget "<duration>"
```

`begin-run` does one of two things:

- starts the run immediately and returns the new `run_dir`
- returns a structured blocker with `blocker_code`, `reason`, `next_action`, and machine-usable `recovery` fields

Only stop and ask the user something when `begin-run` reports a blocker that
requires an explicit decision, such as acknowledging a dirty-head baseline or
allowing fallback write mode.

If the repo is dirty and the user confirms branching from committed `HEAD`,
retry with:

```bash
python3 scripts/afk_mode.py begin-run \
  --cwd "$PWD" \
  --budget "<duration>" \
  --ack-dirty-head-baseline
```

If the repo has no checked-in write-capable policy but `begin-run` reports
`fallback_write_approval_required`, retry with:

```bash
python3 scripts/afk_mode.py begin-run \
  --cwd "$PWD" \
  --budget "<duration>" \
  --allow-fallback-write
```

### 3. Discover Repo Truth

Use `discover` when you need more detail about why `begin-run` blocked, or when
you need extra context for slice ranking:

Run the helper script first:

```bash
python3 scripts/afk_mode.py discover --cwd "$PWD"
```

This returns JSON with:

- repo root and repo name
- detected design docs
- trust mode and repo-profile sources
- capability verdicts for discovery, workflow read, verification, and isolated writes
- git baseline and dirty-state signals
- workflow/work-item signals from repo-local `STATUS.json` roots such as
  `.codex/work/*/STATUS.json` or `.codex/work-items/*/STATUS.json`
- recent completed and open workflow tasks
- nearby repo candidates if the current directory is not a repo

If no credible design docs are found, stop and say afk mode cannot safely
derive slices for this repo.

If the current directory is not a repo, show the nearby repo candidates and
stop. Do not auto-pick one.

If `trust_mode` is `observe_only`, stop. AFK Mode can inspect the repo, but it
cannot safely open implementation slices yet.

If `trust_mode` is `assistive`, the repo may still be runnable in afk-mode
fallback write mode. Prefer checked-in repo policy first by either:

- adding a checked-in repo profile at `.codex/plugin-profile.yaml`
- adding the fallback checked-in profile at `.codex-plugin.yaml` when `.codex/`
  cannot host plugin files
- bootstrapping a reviewed local overlay:

```bash
python3 scripts/afk_mode.py bootstrap-profile \
  --cwd "$PWD"
```

Local overlays now only narrow policy or add stricter guardrails. They do not
grant write access. If no checked-in write-capable policy exists but the repo
has credible design docs plus deterministic verification, afk mode can still
run with `--allow-fallback-write`. In fallback mode, afk mode enforces its own
minimal harness:

- source-and-test changes only
- no project structural flow changes
- no merge/push/rebase/tag/cherry-pick
- local commits only on the run-owned branch

If you bootstrap a checked-in profile, treat the generated `verify.commands` and
execution details as discovery hints to review, not as auto-approved authority.
AFK Mode now keeps heuristic detection separate from repo-owned execution
authority. A checked-in profile only becomes write-capable after it explicitly
declares the verifier and `isolated_write` policy you want.

For one-off experiments or overrides, you may still pass an explicit profile
file to `discover`, `begin-run`, or `start-run` with `--profile`, but explicit
profiles also cannot widen write authority beyond the checked-in repo policy.

If the repo is dirty, do not silently ignore that fact. Tell the user that
slice execution will branch from committed `HEAD`, not from their uncommitted
changes, and ask whether to continue or stop after candidate ranking. If the
user confirms, pass the explicit acknowledgement flag when starting the run:

```bash
python3 scripts/afk_mode.py begin-run \
  --cwd "$PWD" \
  --budget "<duration>" \
  --ack-dirty-head-baseline
```

### 4. Start A Trusted Run Artifact Directory

Create a run directory under `~/.codex/afk-runs/`:

```bash
python3 scripts/afk_mode.py begin-run --cwd "$PWD" --budget "<duration>"
```

Prefer `begin-run` as the default operator entrypoint. `start-run` still exists
as the low-level command, but it now mainly serves direct/manual invocation.

The run helper can start in two modes:

- repo-owned write: a checked-in repo profile explicitly allows `isolated_write`
- fallback write: no checked-in write-capable policy exists, but the user
  explicitly allows `--allow-fallback-write`

Fallback write is stricter and is only for source-and-test changes. It does not
allow structural repo-flow changes.

If a checked-in repo profile already exists but does not allow writes, afk
mode does not bypass that decision with fallback mode. Fix the checked-in
contract instead.

This creates:

- `run.json`
- `discovery.json`
- `candidates.md`
- `candidates.meta.json`
- `logs/`
- `patches/`
- `worktrees/`

Use the returned `run_dir` for the rest of the run.
The helper also refuses to start a second active afk run for the same repo
until the earlier run is finished.

### 5. Read Design Truth And Rank Slices

Read the discovered design docs, repo profile truth order, and any repo-local
operating docs that can change execution rules.

Use the repo's design truth as the primary source of candidate slices.
For repositories that expose future-gap docs and workflow history, combine:

- present contract docs
- forward-looking MVP or roadmap docs
- recent completed work items, to avoid repeating already-closed work
- open workflow items, to prefer bounded continuation work over invented scope

Write a ranked shortlist into `candidates.md`. Keep it compact:

- slice id
- why it is in scope
- why it is safe for overnight work
- expected verification commands
- estimated difficulty

Put machine-readable estimate hints into `candidates.meta.json` next to the
ranked shortlist:

- `size: small|medium|large`
- `risk: low|medium|high`
- `verify_cost: fast|medium|slow`

For repo-local workflow systems, reuse them rather than inventing new local
state. If a repo profile points to workflow entrypoints or checked-in work-item
artifacts for non-trivial writes, follow that repo-local workflow inside each
chosen slice.

Do not leave `candidates.md` in untouched stub state. The runtime helper now
refuses to open a slice until the ranked shortlist has been written.

If you want a quick advisory ETA before opening anything:

```bash
python3 scripts/afk_mode.py estimate-candidates --run-dir "$RUN_DIR"
```

### 6. Open A Slice Worktree

Never write directly on the active user branch.

For each chosen slice:

```bash
python3 scripts/afk_mode.py open-slice \
  --run-dir "$RUN_DIR" \
  --slice-id "$SLICE_ID" \
  --ordinal 1 \
  --slug "$SLUG"
```

This creates:

- branch `afk/<run-id>/<ordinal>-<slug>`
- worktree `"$RUN_DIR/worktrees/<ordinal>-<slug>"`
- `active_slice` state inside `run.json`

Perform the implementation only in that worktree.

`open-slice` now returns an advisory estimate and warning when the slice looks
tight or over-budget. This is guidance only; it does not block admission.

The helper refuses to open a new slice when:

- the run budget is already exhausted
- `candidates.md` is still in untouched stub state
- another slice is already active

### 7. Track Progress

Use status whenever you need a concise checkpoint:

```bash
python3 scripts/afk_mode.py status --run-dir "$RUN_DIR"
```

This reports:

- elapsed and remaining budget
- active slice
- active slice estimate
- next ranked slice estimate
- completed count
- failed count
- repo baseline

### 8. Record Success

When a slice passes verification:

```bash
python3 scripts/afk_mode.py verify-slice \
  --run-dir "$RUN_DIR" \
  --slice-id "$SLICE_ID"
```

This stores a machine-readable proof artifact under `logs/verification/` for the
slice. Then record success:

```bash
python3 scripts/afk_mode.py record-slice \
  --run-dir "$RUN_DIR" \
  --slice-id "$SLICE_ID" \
  --status success \
  --branch "$BRANCH" \
  --commit "$COMMIT_SHA" \
  --worktree "$WORKTREE" \
  --summary "Short success summary"
```

The helper now rejects `--status success` unless it has:

- a real slice branch name
- a real commit SHA
- a passing `verification_result.json` proof artifact from `verify-slice`
- proof that the commit belongs to the recorded branch
- proof that the verification route matches the run's resolved verification
  source, whether that came from repo-owned verification or afk-mode fallback
  generic checks

Then remove closed worktrees owned by the run:

```bash
python3 scripts/afk_mode.py cleanup-run --run-dir "$RUN_DIR"
```

### 9. Record Failure And Continue

When a slice fails implementation or verification:

```bash
python3 scripts/afk_mode.py save-patch \
  --run-dir "$RUN_DIR" \
  --repo-root "$WORKTREE" \
  --output "$RUN_DIR/patches/$SLICE_ID.patch" \
  --include-untracked
```

The helper only allows patch capture from the current active slice worktree and
only into that run's `patches/` directory.

Then:

```bash
python3 scripts/afk_mode.py record-slice \
  --run-dir "$RUN_DIR" \
  --slice-id "$SLICE_ID" \
  --status failed \
  --branch "$BRANCH" \
  --worktree "$WORKTREE" \
  --patch "$RUN_DIR/patches/$SLICE_ID.patch" \
  --summary "Why the slice stopped"
```

Then cleanup:

```bash
python3 scripts/afk_mode.py cleanup-run --run-dir "$RUN_DIR"
```

### 10. Finish The Run

Stop when any of the following is true:

- runtime budget is exhausted
- no safe slice remains
- the repo truth is too ambiguous to continue
- the repo state becomes unsafe

Finish the run with:

```bash
python3 scripts/afk_mode.py finish-run \
  --run-dir "$RUN_DIR" \
  --status completed \
  --summary "High-signal summary of completed, failed, and skipped slices"
```

The final user-facing summary should list:

- trusted design docs
- ranked slices considered
- completed slices with branch and commit
- failed slices with patch paths
- skipped or deferred slices and why

## Safety Hooks

The plugin also installs global Codex hooks. During an active run they:

- inject run context on session start or resume
- deny obviously destructive Bash commands
- remind the session to operate in the active slice worktree
- prevent stopping with an unresolved active slice

Treat those hooks as guardrails, not perfect enforcement.

Repo profiles now declare typed guardrails under `guardrails.rules`.
Each rule has an `id`, `action`, `match_type`, and optional `approval_scope`
for `ask_first` rules.

When an `ask_first` rule blocks a command, the runtime will report the
triggered `rule_id` and required approval scope.

For `rule_for_run` approval:

```bash
python3 scripts/afk_mode.py approve-guardrail \
  --run-dir "$RUN_DIR" \
  --rule-id "<rule-id>" \
  --reason "user approved ask-first guardrail"
```

For `exact_command_once` approval:

```bash
python3 scripts/afk_mode.py approve-guardrail \
  --run-dir "$RUN_DIR" \
  --rule-id "<rule-id>" \
  --approved-command "<exact bash command>" \
  --reason "user approved ask-first guardrail"
```

Legacy local overlays may still be auto-converted at runtime, but checked-in
profiles must use `guardrails.rules`.

## Repo Profiles

Prefer checked-in repo profiles for durable support. They live at:

- `<repo>/.codex/plugin-profile.yaml`
- `<repo>/.codex-plugin.yaml` when `.codex/` is unavailable

The runtime also supports reviewed user-local overlays under
`~/.codex/repo-profiles/` for non-invasive adoption on external repos.

Profiles should stay minimal. They point at existing truth, verification, and
workflow entrypoints. They should not duplicate the repo's harness logic.
