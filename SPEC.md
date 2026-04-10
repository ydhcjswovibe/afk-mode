# AFK Mode Spec

## Summary

AFK Mode is a Codex plugin for bounded unattended repository work.
It opens a time-boxed run, works on isolated slice branches and worktrees,
records proof for successful slices, and applies repo-first guardrails.

## Goals

- Respect repository-owned workflow, verification, and approval rules.
- Keep unattended automation bounded by time, proof, and isolated write scope.
- Degrade safely on unfamiliar repositories instead of inventing authority.
- Stay mostly invisible when AFK Mode is not active.

## Non-Goals

- Replacing a repository's own harness, work-item system, or verifier.
- Granting write authority from heuristics alone.
- Hiding proof requirements behind free-form success strings.

## Core Behavior

- Discover repo truth, workflow, and verification signals.
- Start one active run per repo.
- Open work on run-owned branches and worktrees only.
- Require deterministic verification before recording success.
- Preserve logs, patches, and verification artifacts.

## Self-Maintenance Scope

When AFK Mode maintains this repository, it should prefer:

- plugin runtime code under `scripts/`
- hook entrypoints under `hooks/`
- skill and reference docs under `skills/`
- plugin metadata under `.codex-plugin/`
- repo-owned checked-in policy under `.codex/`

It should avoid inventing extra workflow structure for this repository.
If a future repo-local harness is added, AFK Mode should follow that harness.

## Verification

The checked-in verification route for this repository is the plugin-local test
suite:

- `cd scripts && python3 -m unittest test_afk_mode.py`

External mirrors or user-home integrations may be checked separately, but they
are not part of the repo-owned verification contract for this repository.

## Guardrail Intent

- Global git config changes are never acceptable as unattended repo work.
- `git push` requires explicit user approval.
- Repo-owned write scope should stay inside this repository.
