# AFK Mode V2 Architecture

## Summary

AFK Mode v2 follows one rule above all others:

- `Harness-First, Sleep-Mode-Last-Resort`

If a repository already has workflow, verification, approval, or evidence rules,
AFK Mode must follow them. AFK Mode only supplies minimal fallback
guardrails when the repository does not provide enough structure.

## Goals

- Respect repo-owned harnesses, workflows, and validation boundaries.
- Keep overnight automation safe without requiring repo-specific hardcoding in
  the workflow itself.
- Separate platform concerns from the concrete `afk-mode` workflow.
- Require machine-readable proof for recorded success.
- Degrade safely on unfamiliar repositories instead of improvising policy.

## Non-Goals

- Replacing a repository's own harness, work-item system, or verifier process.
- Inventing workflow phases, role systems, or acceptance criteria for a repo
  that already defines them.
- Granting write authority from heuristics alone.

## Core Principle

AFK Mode does not define what counts as success for a repository.

The repository defines:

- authoritative truth documents
- workflow state and entrypoints
- validation commands and evidence rules
- approval boundaries
- high-risk areas

AFK Mode enforces:

- bounded run admission
- isolated branches and worktrees
- blocker reporting
- artifact persistence
- fallback guardrails
- proof storage and success gating

## Ownership Model

### Repository Owns Policy

The repository is the policy owner when it provides any of the following:

- checked-in harness docs such as `AGENTS.md`, role docs, verifier docs
- checked-in workflow state or work-item artifacts
- repo-local scripts or CLIs for work-item control, validation, or verification
- checked-in profile data that points at those existing assets

When those exist, AFK Mode must not replace them with global defaults.

### AFK Mode Owns Mechanism

AFK Mode owns only the shared mechanism layer:

- run registry and resume context
- budget and timeboxing
- worktree and branch isolation
- blocker protocol
- patch and log preservation
- proof artifact storage
- minimal safety hooks

## Platform Split

V2 should separate the current implementation into two layers.

### 1. Sleep Kernel

The kernel is generic platform infrastructure.

Responsibilities:

- resolve repo policy sources
- decide read/write/verify authorization
- manage active runs
- expose a structured blocker API
- manage worktrees, logs, and patches
- broker hooks
- store evidence and proof bundles

The kernel must not contain afk-mode-specific slice ranking logic.

### 2. Sleep Workflow

The workflow is the overnight automation consumer of the kernel.

Responsibilities:

- start a bounded run
- rank candidates
- open a slice
- call repo-owned workflow entrypoints when required
- request verification
- record outcomes
- finish the run

## Policy Resolution

### Source Precedence

Policy should resolve in this order:

1. repo-owned checked-in harness or checked-in profile
2. repo-owned workflow state and verifier docs
3. local overlay that can only narrow or annotate policy
4. heuristic discovery hints

### Critical Constraint

Local overlay and session overrides must never widen repo-owned write or
verification authority.

They may:

- add local notes
- narrow capabilities
- add stricter guardrails
- add operator-only metadata

They may not:

- enable writes when the checked-in repo policy denies them
- replace repo-owned verification commands
- replace repo-owned workflow entrypoints
- weaken repo-owned guardrails

## Trust Model

The current `trusted | assistive | observe_only` model is directionally useful,
but v2 should split policy provenance from write authorization.

Recommended state dimensions:

- `policy_source`: `repo_owned | local_overlay | heuristic`
- `write_authorized`: `true | false`
- `verification_source`: `repo_owned | fallback | none`

That keeps `trusted` from implying more than it really means.

## Harness-First Execution

When a repository defines a harness, AFK Mode must use it.

Examples:

- repo-owned work-item CLI
- repo-owned validator or verifier command
- repo-owned state files such as `STATUS.json`
- repo-owned evidence requirements

AFK Mode may still orchestrate the run, but it should call those entrypoints
instead of inventing a parallel process.

## Fallback Guardrails

AFK Mode only falls back to generic guardrails when the repository does not
provide enough structure.

Minimum fallback guardrails:

- current repo only
- isolated branch/worktree only
- no success without a verification route
- destructive command deny
- failure requires a patch or a log
- secrets/global config/manual setup guarded or denied

Repo-owned guardrails should override generic defaults when stricter.

## Verification And Proof

### Repo Owns Validation Policy

The repository decides:

- which commands count as validation
- which evidence is required
- whether manual checks are allowed
- what additional approvals are required for high-risk work

### AFK Mode Owns Proof Enforcement

AFK Mode must execute or record the repo-owned verification path and store
machine-readable evidence.

V2 should add:

- `verify-slice`
- `verification_result.json`
- stored stdout/stderr, exit code, and timing
- proof bundle generation at run finish

### Success Gate

`record-slice --status success` should require:

- a verification result artifact
- proof that the recorded commit belongs to the recorded branch
- proof that the verification route used is allowed by repo-owned policy

Free-form verification strings alone are insufficient.

## Guardrails

Guardrails should be repo-first and typed where possible.

V2 direction:

- repo-owned `ask_first`
- repo-owned hard-deny paths and commands
- repo-owned high-risk categories
- optional fallback generic rules when the repo is silent

String matching is acceptable as a temporary fallback, but it is not the final
shape for long-term automation.

## Blocker Protocol

Structured blockers are the right direction and should remain the admission API.

V2 blockers should include machine-usable recovery fields such as:

- existing `run_id`
- existing `run_dir`
- required approval type
- required repo policy source
- required verification route

That allows autonomous recovery instead of free-text parsing.

## Recommended Commands

The workflow-facing commands should look like this:

- `begin-run`
- `status`
- `open-slice`
- `verify-slice`
- `record-slice`
- `finish-run`
- `approve-guardrail`

`begin-run` remains the default operator entrypoint.

## Migration Priorities

### Phase 1

- freeze repo-owned `verify` and `workflow` against widening overrides
- keep `begin-run` as the default entrypoint
- keep current worktree isolation

### Phase 2

- add `verify-slice`
- require proof artifacts for success
- require failure evidence

### Phase 3

- split kernel and afk workflow
- move profile and trust resolution out of the workflow module
- expose shared hook and blocker contracts

### Phase 4

- replace loose substring guardrails with typed policies
- improve recovery APIs and inspection surfaces

## Decision Rule

If a repository already says how to work, AFK Mode follows it.

If the repository does not say enough, AFK Mode applies the minimum rules
needed to keep overnight automation safe and auditable.
