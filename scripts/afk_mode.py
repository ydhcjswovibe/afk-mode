#!/usr/bin/env python3
"""Runtime helper facade for the afk-mode plugin."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from afk_mode_runtime import (  # noqa: E402
    CANDIDATES_PLACEHOLDER,
    CHECKED_IN_PROFILE_RELATIVE_PATHS,
    DEFAULT_PROFILE_ROOT,
    DEFAULT_RUN_ROOT,
    SUPPORTED_CAPABILITIES,
    TRUST_MODE_ASSISTIVE,
    TRUST_MODE_OBSERVE_ONLY,
    TRUST_MODE_TRUSTED,
    TRUST_MODES,
    AfkModeError,
    approve_guardrail,
    begin_run,
    bootstrap_profile,
    build_session_context,
    cleanup_run,
    default_profile_path,
    discover_repo,
    estimate_candidates,
    finish_run,
    load_run,
    open_slice,
    pretool_decision,
    record_slice,
    relative_to,
    save_patch,
    save_run,
    start_run,
    status,
    verification_result_path,
    verify_slice,
)

__all__ = [
    "CANDIDATES_PLACEHOLDER",
    "CHECKED_IN_PROFILE_RELATIVE_PATHS",
    "DEFAULT_PROFILE_ROOT",
    "DEFAULT_RUN_ROOT",
    "SUPPORTED_CAPABILITIES",
    "TRUST_MODE_ASSISTIVE",
    "TRUST_MODE_OBSERVE_ONLY",
    "TRUST_MODE_TRUSTED",
    "TRUST_MODES",
    "AfkModeError",
    "approve_guardrail",
    "begin_run",
    "bootstrap_profile",
    "build_session_context",
    "cleanup_run",
    "default_profile_path",
    "discover_repo",
    "estimate_candidates",
    "finish_run",
    "load_run",
    "open_slice",
    "parse_args",
    "pretool_decision",
    "record_slice",
    "relative_to",
    "save_patch",
    "save_run",
    "start_run",
    "status",
    "main",
    "verification_result_path",
    "verify_slice",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Runtime helper for the afk-mode plugin.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover_parser = subparsers.add_parser("discover", help="Inspect the current repo.")
    discover_parser.add_argument("--cwd", default=".", help="Working directory to inspect.")
    discover_parser.add_argument(
        "--profile",
        help="Optional explicit repo profile path to overlay for this command only.",
    )

    begin_parser = subparsers.add_parser(
        "begin-run",
        help="Auto-start a run when safe, otherwise return a structured blocker.",
    )
    begin_parser.add_argument("--cwd", default=".", help="Working directory to inspect.")
    begin_parser.add_argument("--budget", required=True, help="User-provided runtime budget.")
    begin_parser.add_argument(
        "--profile",
        help="Optional explicit repo profile path to overlay for this command only.",
    )
    begin_parser.add_argument(
        "--ack-dirty-head-baseline",
        action="store_true",
        help="Confirm that slices should branch from committed HEAD when the repo is dirty.",
    )
    begin_parser.add_argument(
        "--allow-fallback-write",
        action="store_true",
        help="Explicitly allow afk-mode fallback writes when the repo lacks checked-in write policy.",
    )
    begin_parser.add_argument(
        "--run-root",
        default=str(DEFAULT_RUN_ROOT),
        help="Root directory for afk run artifacts.",
    )

    start_parser = subparsers.add_parser("start-run", help="Create a afk run directory.")
    start_parser.add_argument("--cwd", default=".", help="Working directory to inspect.")
    start_parser.add_argument("--budget", required=True, help="User-provided runtime budget.")
    start_parser.add_argument(
        "--profile",
        help="Optional explicit repo profile path to overlay for this command only.",
    )
    start_parser.add_argument(
        "--ack-dirty-head-baseline",
        action="store_true",
        help="Confirm that slices should branch from committed HEAD when the repo is dirty.",
    )
    start_parser.add_argument(
        "--allow-fallback-write",
        action="store_true",
        help="Explicitly allow afk-mode fallback writes when the repo lacks checked-in write policy.",
    )
    start_parser.add_argument(
        "--run-root",
        default=str(DEFAULT_RUN_ROOT),
        help="Root directory for afk run artifacts.",
    )

    bootstrap_parser = subparsers.add_parser(
        "bootstrap-profile",
        help="Write a minimal repo profile overlay from current discovery signals.",
    )
    bootstrap_parser.add_argument("--cwd", default=".", help="Working directory to inspect.")
    bootstrap_parser.add_argument("--output", help="Optional output path for the generated profile.")
    bootstrap_parser.add_argument(
        "--allow-isolated-write",
        action="store_true",
        help="Deprecated bootstrap hint flag. Checked-in profiles must still declare verify.commands and isolated_write explicitly after review.",
    )
    bootstrap_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing output profile.",
    )

    approve_parser = subparsers.add_parser(
        "approve-guardrail",
        help="Approve an ask-first guardrail for a running afk run.",
    )
    approve_parser.add_argument("--run-dir", required=True)
    approve_parser.add_argument("--rule-id")
    approve_parser.add_argument("--approved-command", dest="approved_command")
    approve_parser.add_argument("--reason", help="Optional audit note for the approval.")

    status_parser = subparsers.add_parser("status", help="Show run status.")
    status_parser.add_argument("--run-dir", required=True)

    estimate_parser = subparsers.add_parser(
        "estimate-candidates",
        help="Estimate ranked candidate slices using repo baselines and local telemetry.",
    )
    estimate_parser.add_argument("--run-dir", required=True)

    open_parser = subparsers.add_parser("open-slice", help="Create a slice worktree and branch.")
    open_parser.add_argument("--run-dir", required=True)
    open_parser.add_argument("--slice-id", required=True)
    open_parser.add_argument("--ordinal", type=int, required=True)
    open_parser.add_argument("--slug")

    verify_parser = subparsers.add_parser(
        "verify-slice",
        help="Run the repo-owned verification route for one slice and store proof artifacts.",
    )
    verify_parser.add_argument("--run-dir", required=True)
    verify_parser.add_argument("--slice-id", required=True)
    verify_parser.add_argument("--worktree")

    record_parser = subparsers.add_parser("record-slice", help="Record one slice result.")
    record_parser.add_argument("--run-dir", required=True)
    record_parser.add_argument("--slice-id", required=True)
    record_parser.add_argument("--status", required=True, choices=("success", "failed", "skipped"))
    record_parser.add_argument("--summary", required=True)
    record_parser.add_argument("--branch")
    record_parser.add_argument("--commit")
    record_parser.add_argument("--worktree")
    record_parser.add_argument("--patch")
    record_parser.add_argument("--log")
    record_parser.add_argument("--verification", action="append", default=[])

    finish_parser = subparsers.add_parser("finish-run", help="Mark a run complete.")
    finish_parser.add_argument("--run-dir", required=True)
    finish_parser.add_argument(
        "--status",
        required=True,
        choices=("completed", "stopped", "failed", "aborted"),
    )
    finish_parser.add_argument("--summary", required=True)

    patch_parser = subparsers.add_parser("save-patch", help="Write a binary git patch.")
    patch_parser.add_argument("--run-dir", required=True)
    patch_parser.add_argument("--repo-root", required=True)
    patch_parser.add_argument("--output", required=True)
    patch_parser.add_argument("--include-untracked", action="store_true")

    cleanup_parser = subparsers.add_parser("cleanup-run", help="Remove closed run worktrees.")
    cleanup_parser.add_argument("--run-dir", required=True)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "discover":
            payload = discover_repo(
                Path(args.cwd),
                profile_path=Path(args.profile) if getattr(args, "profile", None) else None,
            )
        elif args.command == "begin-run":
            payload = begin_run(
                Path(args.cwd),
                args.budget,
                Path(args.run_root),
                ack_dirty_head_baseline=args.ack_dirty_head_baseline,
                allow_fallback_write=args.allow_fallback_write,
                profile_path=Path(args.profile) if getattr(args, "profile", None) else None,
            )
        elif args.command == "start-run":
            payload = start_run(
                Path(args.cwd),
                args.budget,
                Path(args.run_root),
                ack_dirty_head_baseline=args.ack_dirty_head_baseline,
                allow_fallback_write=args.allow_fallback_write,
                profile_path=Path(args.profile) if getattr(args, "profile", None) else None,
            )
        elif args.command == "bootstrap-profile":
            payload = bootstrap_profile(
                Path(args.cwd),
                output=Path(args.output) if args.output else None,
                allow_isolated_write=args.allow_isolated_write,
                force=args.force,
            )
        elif args.command == "approve-guardrail":
            payload = approve_guardrail(
                Path(args.run_dir),
                args.approved_command,
                reason=args.reason,
                rule_id=args.rule_id,
            )
        elif args.command == "status":
            payload = status(Path(args.run_dir))
        elif args.command == "estimate-candidates":
            payload = estimate_candidates(Path(args.run_dir))
        elif args.command == "open-slice":
            payload = open_slice(Path(args.run_dir), args.slice_id, args.ordinal, args.slug)
        elif args.command == "verify-slice":
            payload = verify_slice(
                Path(args.run_dir),
                args.slice_id,
                worktree=args.worktree,
            )
        elif args.command == "record-slice":
            payload = record_slice(
                Path(args.run_dir),
                args.slice_id,
                args.status,
                args.summary,
                args.branch,
                args.commit,
                args.worktree,
                args.patch,
                args.log,
                args.verification,
            )
        elif args.command == "finish-run":
            payload = finish_run(Path(args.run_dir), args.status, args.summary)
        elif args.command == "save-patch":
            payload = save_patch(
                Path(args.repo_root),
                Path(args.output),
                args.include_untracked,
                Path(args.run_dir) if args.run_dir else None,
            )
        elif args.command == "cleanup-run":
            payload = cleanup_run(Path(args.run_dir))
        else:
            raise AfkModeError(f"Unsupported command: {args.command}")
    except AfkModeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
