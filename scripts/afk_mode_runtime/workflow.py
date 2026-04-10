from __future__ import annotations

import datetime as dt
import shutil
import uuid
from pathlib import Path
from typing import Any

from .common import (
    CANDIDATES_PLACEHOLDER,
    DEFAULT_PROFILE_ROOT,
    AfkModeError,
    VERIFICATION_SOURCE_NONE,
    WRITE_MODE_FALLBACK,
    WRITE_MODE_NONE,
    WRITE_MODE_REPO_OWNED,
    default_profile_path,
    json_dump,
    checked_in_profile_candidates,
    now_utc,
    parse_duration_seconds,
    relative_to,
    run_command,
    sha256_text,
    slugify,
    write_text_atomic,
    is_within,
    TRUST_MODE_OBSERVE_ONLY,
)
from .discovery import discover_repo
from .estimation import (
    append_terminal_sample,
    estimate_one_slice,
    estimate_warning_for_slice,
    write_candidates_meta_stub,
)
from .policy import request_capability
from .proof import load_verification_result, verification_result_path
from .run_state import (
    active_runs_path,
    clear_active_run,
    elapsed_and_remaining_seconds,
    find_active_run_for_repo,
    load_run,
    register_active_run,
    save_run,
)


def _discovery_verification_route(discovery: dict[str, Any]) -> list[str]:
    route = (
        discovery.get("verification_route")
        or discovery.get("fallback_verification_route")
        or discovery.get("verification", {}).get("commands")
        or []
    )
    if route:
        return [command for command in route if isinstance(command, str) and command.strip()]
    generic_checks = discovery.get("verification", {}).get("generic_file_checks") or []
    checker_hints = {
        "python": "python3 -m py_compile <changed .py files>",
        "node": "node --check <changed .js/.mjs/.cjs files>",
        "shell": "bash -n <changed shell files>",
        "json": "python3 -c 'json.load(...)' <changed .json files>",
        "yaml": "python3 -c 'yaml.safe_load(...)' <changed .yaml/.yml files>",
    }
    route = [checker_hints[checker] for checker in generic_checks if checker in checker_hints]
    return [command for command in route if isinstance(command, str) and command.strip()]


def _blocker_payload(
    discovery: dict[str, Any],
    blocker_code: str,
    reason: str,
    next_action: str,
    *,
    required_approval_type: str | None = None,
    required_repo_policy_source: str | None = None,
    existing_run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    repo_context = discovery.get("repo_context") or {}
    return {
        "started": False,
        "blocked": True,
        "blocker_code": blocker_code,
        "reason": reason,
        "next_action": next_action,
        "recovery": {
            "existing_run_id": existing_run.get("run_id") if existing_run else None,
            "existing_run_dir": existing_run.get("run_dir") if existing_run else None,
            "existing_run_status": existing_run.get("status") if existing_run else None,
            "existing_active_slice_id": existing_run.get("active_slice_id") if existing_run else None,
            "existing_active_branch": existing_run.get("active_branch") if existing_run else None,
            "existing_active_worktree": existing_run.get("active_worktree") if existing_run else None,
            "existing_remaining_seconds": existing_run.get("remaining_seconds") if existing_run else None,
            "required_approval_type": required_approval_type,
            "required_repo_policy_source": required_repo_policy_source,
            "current_repo_policy_source": repo_context.get("policy_source"),
            "current_write_mode": discovery.get("write_mode"),
            "current_write_authorized": bool(discovery.get("write_authorized")),
            "current_verification_source": discovery.get("verification_source"),
            "required_verification_route": _discovery_verification_route(discovery),
            "suggested_profile_path": repo_context.get("suggested_profile_path"),
        },
        "discovery": discovery,
    }


def _write_unavailable_reason(discovery: dict[str, Any], profile_root: Path) -> str:
    if discovery.get("verification_source") == VERIFICATION_SOURCE_NONE:
        repo_root = Path(discovery["repo_root"])
        checked_in_targets = ", ".join(str(path) for path in checked_in_profile_candidates(repo_root))
        return (
            "AFK Mode needs a deterministic verification path before it can write. "
            "Add checked-in verify.commands or rely on an existing repo test/verify command first. "
            f"Checked-in profile locations: {checked_in_targets}. "
            f"Local overlays under {default_profile_path(repo_root, profile_root).parent} can only narrow policy now."
        )
    return request_capability(
        {
            **(discovery.get("repo_context") or {}),
            "repo_root_path": Path(discovery["repo_root"]),
            "profile_root": profile_root,
            "signals": discovery.get("signals") or {},
        },
        "isolated_write",
    )["reason"]


def ensure_repo_run(
    discovery: dict[str, Any],
    *,
    profile_root: Path = DEFAULT_PROFILE_ROOT,
    allow_fallback_write: bool = False,
) -> dict[str, Any]:
    if not discovery["git"]["is_repo"]:
        raise AfkModeError("AFK Mode can only start inside a git repository.")
    if not discovery["design_docs"]:
        raise AfkModeError("AFK Mode needs at least one credible design doc for this repo.")
    write_mode = discovery.get("write_mode") or WRITE_MODE_NONE
    if write_mode == WRITE_MODE_REPO_OWNED:
        return {
            "write_mode": WRITE_MODE_REPO_OWNED,
            "write_authorized": True,
            "verification_source": discovery.get("verification_source"),
        }
    if write_mode == WRITE_MODE_FALLBACK:
        if not allow_fallback_write:
            raise AfkModeError(
                "Fallback writes require --allow-fallback-write because this repo does not have a "
                "checked-in write-capable policy."
            )
        return {
            "write_mode": WRITE_MODE_FALLBACK,
            "write_authorized": True,
            "verification_source": discovery.get("verification_source"),
        }
    raise AfkModeError(_write_unavailable_reason(discovery, profile_root))


def begin_run(
    cwd: Path,
    budget: str,
    run_root: Path,
    ack_dirty_head_baseline: bool = False,
    allow_fallback_write: bool = False,
    profile_path: Path | None = None,
    profile_root: Path = DEFAULT_PROFILE_ROOT,
) -> dict[str, Any]:
    discovery = discover_repo(cwd, profile_path=profile_path, profile_root=profile_root)
    if not discovery["git"]["is_repo"]:
        return _blocker_payload(
            discovery,
            "not_repo",
            "AFK Mode can only start inside a git repository.",
            "Choose one nearby repo candidate and retry begin-run there.",
        )
    if not discovery["design_docs"]:
        return _blocker_payload(
            discovery,
            "missing_design_docs",
            "AFK Mode needs at least one credible design doc for this repo.",
            "Add or point to a credible design/spec document before retrying.",
        )

    existing_run = find_active_run_for_repo(Path(discovery["repo_root"]), run_root=run_root)
    if existing_run is not None:
        return _blocker_payload(
            discovery,
            "active_run_exists",
            f"Repo already has an active afk run: {existing_run['run_id']} at {existing_run['run_dir']}",
            "Resume, finish, or clean up the active run before starting another.",
            existing_run=existing_run,
        )

    write_mode = discovery.get("write_mode") or WRITE_MODE_NONE
    if write_mode == WRITE_MODE_FALLBACK and not allow_fallback_write:
        return _blocker_payload(
            discovery,
            "fallback_write_approval_required",
            (
                "Repo does not have a checked-in write-capable policy. "
                "AFK Mode can still proceed in fallback write mode with stricter built-in limits."
            ),
            "Retry begin-run with --allow-fallback-write to allow source-and-test-only changes on the run-owned branch.",
            required_approval_type="fallback_write",
            required_repo_policy_source="repo_owned",
        )
    if write_mode == WRITE_MODE_NONE:
        trust_mode = discovery.get("trust_mode", TRUST_MODE_OBSERVE_ONLY)
        next_action = (
            "Add a deterministic verification path or a checked-in repo profile, then retry begin-run."
            if trust_mode in {"observe_only", "assistive"}
            else "Update the checked-in repo profile so write admission and verification are explicit, then retry begin-run."
        )
        return _blocker_payload(
            discovery,
            "profile_required",
            _write_unavailable_reason(discovery, profile_root),
            next_action,
            required_repo_policy_source="repo_owned",
        )

    if discovery["git"]["dirty"] and not ack_dirty_head_baseline:
        return _blocker_payload(
            discovery,
            "dirty_head_ack_required",
            (
                "Repo has uncommitted changes. AFK Mode will branch slices from committed HEAD, "
                "not from your uncommitted work."
            ),
            "Retry begin-run with --ack-dirty-head-baseline if you want to proceed from committed HEAD.",
            required_approval_type="dirty_head_baseline",
        )

    try:
        started = start_run(
            cwd,
            budget,
            run_root,
            ack_dirty_head_baseline=ack_dirty_head_baseline,
            allow_fallback_write=allow_fallback_write,
            profile_path=profile_path,
            profile_root=profile_root,
        )
    except AfkModeError as exc:
        return _blocker_payload(
            discovery,
            "start_failed",
            str(exc),
            "Resolve the reported blocker and retry begin-run.",
        )

    return {
        "started": True,
        "blocked": False,
        "run": started,
        "discovery": discovery,
    }


def build_run_id(repo_name: str) -> str:
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    return f"{timestamp}-{slugify(repo_name)}-{uuid.uuid4().hex[:8]}"


def render_candidates_stub(run_id: str, discovery: dict[str, Any], budget: str) -> str:
    lines = [
        "# AFK Run Candidates",
        "",
        f"- Run ID: `{run_id}`",
        f"- Repo: `{discovery['repo_name']}`",
        f"- Budget: `{budget}`",
        f"- Trust Mode: `{discovery.get('trust_mode', TRUST_MODE_OBSERVE_ONLY)}`",
        "",
        "## Trusted Design Docs",
        "",
    ]
    design_docs = discovery.get("design_docs", [])
    if design_docs:
        for doc in design_docs:
            lines.append(
                f"- `{doc['relative_path']}` ({doc['kind']}, priority {doc['priority']})"
            )
    else:
        lines.append("- None detected")
    lines.extend(
        [
            "",
            "## Ranked Slices",
            "",
            CANDIDATES_PLACEHOLDER,
            "",
            "Use one compact block per slice:",
            "",
            "1. `slice-id`",
            "   - Why it is in scope",
            "   - Why it is safe overnight",
            "   - Expected verification",
            "   - Estimated difficulty",
            "",
            "Machine-readable estimate hints live in `candidates.meta.json`:",
            "",
            "{",
            '  "slices": [',
            '    {"slice_id": "slice-id", "size": "medium", "risk": "medium", "verify_cost": "medium"}',
            "  ]",
            "}",
            "",
        ]
    )
    return "\n".join(lines)


def write_candidates_stub(run_dir: Path, discovery: dict[str, Any], budget: str) -> str:
    content = render_candidates_stub(run_dir.name, discovery, budget)
    write_text_atomic(run_dir / "candidates.md", content)
    write_candidates_meta_stub(run_dir)
    return sha256_text(content)


def start_run(
    cwd: Path,
    budget: str,
    run_root: Path,
    ack_dirty_head_baseline: bool = False,
    allow_fallback_write: bool = False,
    profile_path: Path | None = None,
    profile_root: Path = DEFAULT_PROFILE_ROOT,
) -> dict[str, Any]:
    discovery = discover_repo(cwd, profile_path=profile_path, profile_root=profile_root)
    admission = ensure_repo_run(
        discovery,
        profile_root=profile_root,
        allow_fallback_write=allow_fallback_write,
    )
    if discovery["git"]["dirty"] and not ack_dirty_head_baseline:
        raise AfkModeError(
            "Repo has uncommitted changes. Re-run with --ack-dirty-head-baseline "
            "to confirm slices should branch from committed HEAD instead."
        )
    budget_seconds = parse_duration_seconds(budget)
    run_id = build_run_id(discovery["repo_name"])
    run_dir = run_root / run_id
    while run_dir.exists():
        run_id = build_run_id(discovery["repo_name"])
        run_dir = run_root / run_id
    for name in ("logs", "patches", "worktrees"):
        (run_dir / name).mkdir(parents=True, exist_ok=True)
    json_dump(run_dir / "discovery.json", discovery)
    candidates_stub_sha256 = write_candidates_stub(run_dir, discovery, budget)

    repo_root = Path(discovery["repo_root"])
    repo_context = dict(discovery["repo_context"] or {})
    repo_context["write_authorized"] = bool(admission["write_authorized"])
    run_payload = {
        "run_id": run_id,
        "status": "running",
        "started_at": now_utc(),
        "finished_at": None,
        "last_updated_at": now_utc(),
        "budget": budget,
        "budget_seconds": budget_seconds,
        "cwd": discovery["cwd"],
        "repo_root": discovery["repo_root"],
        "repo_name": discovery["repo_name"],
        "dirty_head_baseline_acknowledged": ack_dirty_head_baseline,
        "git_baseline": {
            "branch": discovery["git"]["branch"],
            "head": discovery["git"]["head"],
            "dirty": discovery["git"]["dirty"],
        },
        "candidates_stub_sha256": candidates_stub_sha256,
        "design_docs": discovery["design_docs"],
        "workflow": discovery["workflow"],
        "repo_candidates": discovery["repo_candidates"],
        "repo_context": repo_context,
        "policy_source": discovery.get("policy_source") or repo_context.get("policy_source") or "unknown",
        "write_mode": admission["write_mode"],
        "write_authorized": bool(admission["write_authorized"]),
        "verification_source": discovery.get("verification_source"),
        "verification_route": (
            discovery.get("verification_route")
            if discovery.get("verification_source") == "repo_owned"
            else discovery.get("fallback_verification_route") or []
        ),
        "metrics_root": str(run_root.parent / "afk-metrics"),
        "trust_mode": discovery["trust_mode"],
        "capability_verdicts": discovery["capability_verdicts"],
        "guardrail_approvals": [],
        "artifacts": {
            "discovery": "discovery.json",
            "candidates": "candidates.md",
            "candidate_metadata": "candidates.meta.json",
            "logs_dir": "logs",
            "patches_dir": "patches",
            "worktrees_dir": "worktrees",
            "active_run_pointer": relative_to(active_runs_path(run_root), run_root),
        },
        "active_slice": None,
        "completed_count": 0,
        "failed_count": 0,
        "slices": [],
        "summary": None,
    }
    save_run(run_dir, run_payload)
    try:
        register_active_run(run_root, repo_root, run_dir, run_id)
    except AfkModeError:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise
    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "repo_root": discovery["repo_root"],
        "budget": budget,
        "budget_seconds": budget_seconds,
        "dirty_head_baseline_acknowledged": ack_dirty_head_baseline,
        "trust_mode": discovery["trust_mode"],
        "write_mode": admission["write_mode"],
        "write_authorized": bool(admission["write_authorized"]),
        "verification_source": discovery.get("verification_source"),
    }


def open_slice(run_dir: Path, slice_id: str, ordinal: int, slug: str | None) -> dict[str, Any]:
    payload = load_run(run_dir)
    if payload["status"] != "running":
        raise AfkModeError(f"Cannot open a slice on a run with status '{payload['status']}'.")
    if payload.get("active_slice"):
        raise AfkModeError("Run already has an active slice. Record it before opening another.")
    _, remaining = elapsed_and_remaining_seconds(payload)
    if remaining <= 0:
        raise AfkModeError("Run budget is exhausted. Finish the run instead of opening a new slice.")

    candidates_path = run_dir / "candidates.md"
    if not candidates_path.exists():
        raise AfkModeError("Run candidates.md is missing. Rank slices before opening a worktree.")
    current_candidates = candidates_path.read_text(encoding="utf-8")
    stub_hash = payload.get("candidates_stub_sha256")
    if stub_hash:
        if sha256_text(current_candidates) == stub_hash:
            raise AfkModeError("Rank slices in candidates.md before opening a worktree.")
    elif CANDIDATES_PLACEHOLDER in current_candidates:
        raise AfkModeError("Rank slices in candidates.md before opening a worktree.")

    safe_slug = slugify(slug or slice_id)
    branch = f"afk/{payload['run_id']}/{ordinal:02d}-{safe_slug}"
    worktree = run_dir / "worktrees" / f"{ordinal:02d}-{safe_slug}"
    repo_root = Path(payload["repo_root"])
    base_ref = payload["git_baseline"]["head"]
    run_command(
        ["git", "-C", str(repo_root), "worktree", "add", "-b", branch, str(worktree), base_ref]
    )
    estimate = estimate_one_slice(
        run_dir,
        payload,
        slice_id=slice_id,
        ordinal=ordinal,
        remaining_seconds=remaining,
    )
    estimate_warning = estimate_warning_for_slice(estimate)

    payload["active_slice"] = {
        "slice_id": slice_id,
        "branch": branch,
        "worktree": str(worktree),
        "ordinal": ordinal,
        "slug": safe_slug,
        "opened_at": now_utc(),
        "remaining_seconds_at_open": remaining,
        "size": estimate["size"],
        "risk": estimate["risk"],
        "verify_cost": estimate["verify_cost"],
        "estimate_point_minutes": estimate["point_minutes"],
        "estimate_upper_minutes": estimate["upper_minutes"],
        "estimate_fit_status": estimate["fit_status"],
        "estimation_confidence": estimate["estimation_confidence"],
        "estimate_samples_used": estimate["samples_used"],
        "estimate_source": estimate["source"],
        "estimate_reason": estimate["reason"],
        "estimate_warning": estimate_warning,
    }
    save_run(run_dir, payload)
    return {
        **payload["active_slice"],
        "estimate": estimate,
        "estimate_warning": estimate_warning,
    }


def _verification_seconds(verification_result: dict[str, Any] | None) -> float:
    if verification_result is None:
        return 0.0
    total = 0.0
    for result in verification_result.get("results") or []:
        duration = result.get("duration_seconds")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            total += float(duration)
    return round(total, 3)


def record_slice(
    run_dir: Path,
    slice_id: str,
    status_name: str,
    summary: str,
    branch: str | None,
    commit: str | None,
    worktree: str | None,
    patch: str | None,
    log_path: str | None,
    verification: list[str] | None = None,
) -> dict[str, Any]:
    payload = load_run(run_dir)
    if payload["status"] != "running":
        raise AfkModeError(f"Cannot record a slice on a run with status '{payload['status']}'.")
    active = payload.get("active_slice")
    active_slice = active if active and active.get("slice_id") == slice_id else None
    existing_entry = next(
        (
            item
            for item in payload.get("slices") or []
            if isinstance(item, dict) and item.get("slice_id") == slice_id
        ),
        None,
    )
    verification_commands = list(verification or [])
    verification_result: dict[str, Any] | None = None
    if status_name == "success":
        if not branch:
            raise AfkModeError("Successful slices must record a branch name.")
        if not commit:
            raise AfkModeError("Successful slices must record a commit SHA.")
        repo_root = Path(payload["repo_root"])
        branch_check = run_command(
            ["git", "-C", str(repo_root), "show-ref", "--verify", f"refs/heads/{branch}"],
            check=False,
        )
        if branch_check.returncode != 0:
            raise AfkModeError(f"Successful slice branch was not found: {branch}")
        run_command(
            ["git", "-C", str(repo_root), "rev-parse", "--verify", f"{commit}^{{commit}}"]
        )
        ancestry_check = run_command(
            ["git", "-C", str(repo_root), "merge-base", "--is-ancestor", commit, f"refs/heads/{branch}"],
            check=False,
        )
        if ancestry_check.returncode != 0:
            raise AfkModeError(
                f"Successful slice commit {commit} is not contained in branch {branch}."
            )
        verification_result = load_verification_result(run_dir, slice_id)
        if not verification_result.get("all_passed"):
            raise AfkModeError(
                "Successful slices require a passing verification result artifact."
            )
        proof_commands = [entry.get("command") for entry in verification_result["results"]]
        if any(not isinstance(command, str) or not command.strip() for command in proof_commands):
            raise AfkModeError(
                "Verification result artifact is missing valid verification commands."
            )
        allowed_commands = verification_result.get("allowed_commands") or proof_commands
        if proof_commands != allowed_commands:
            raise AfkModeError(
                "Verification result artifact does not match the resolved verification route."
            )
        if verification_result.get("verification_source") != payload.get("verification_source"):
            raise AfkModeError(
                "Verification result artifact does not match the run verification source."
            )
        if verification_commands and verification_commands != proof_commands:
            raise AfkModeError(
                "Recorded verification commands do not match the verification result artifact."
            )
        if worktree and verification_result.get("worktree"):
            if Path(worktree).resolve() != Path(verification_result["worktree"]).resolve():
                raise AfkModeError(
                    "Recorded worktree does not match the verification result artifact."
                )
        verification_commands = list(proof_commands)
    elif status_name == "failed" and not patch and not log_path:
        raise AfkModeError("Failed slices must record a patch path or a log path.")
    else:
        verification_artifact = verification_result_path(run_dir, slice_id)
        if verification_artifact.exists():
            verification_result = load_verification_result(run_dir, slice_id)

    recorded_at = now_utc()
    opened_at = None
    if isinstance(active_slice, dict):
        opened_at = active_slice.get("opened_at")
    elif isinstance(existing_entry, dict):
        opened_at = existing_entry.get("opened_at")
    opened_at_timestamp = dt.datetime.fromisoformat(opened_at.replace("Z", "+00:00")) if isinstance(opened_at, str) and opened_at else None
    recorded_at_timestamp = dt.datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
    wall_clock_seconds = None
    if opened_at_timestamp is not None:
        wall_clock_seconds = round(
            max(0.0, (recorded_at_timestamp - opened_at_timestamp).total_seconds()),
            3,
        )
    slice_metadata = active_slice or existing_entry or {}
    entry = {
        "slice_id": slice_id,
        "status": status_name,
        "recorded_at": recorded_at,
        "opened_at": opened_at,
        "wall_clock_seconds": wall_clock_seconds,
        "verification_seconds": _verification_seconds(verification_result),
        "size": slice_metadata.get("size"),
        "risk": slice_metadata.get("risk"),
        "verify_cost": slice_metadata.get("verify_cost"),
        "estimate_point_minutes": slice_metadata.get("estimate_point_minutes"),
        "estimate_upper_minutes": slice_metadata.get("estimate_upper_minutes"),
        "estimation_confidence": slice_metadata.get("estimation_confidence"),
        "estimate_fit_status": slice_metadata.get("estimate_fit_status"),
        "estimate_source": slice_metadata.get("estimate_source"),
        "estimate_samples_used": slice_metadata.get("estimate_samples_used"),
        "estimate_reason": slice_metadata.get("estimate_reason"),
        "summary": summary,
        "branch": branch,
        "commit": commit,
        "worktree": worktree,
        "patch": relative_to(Path(patch), run_dir) if patch else None,
        "log": relative_to(Path(log_path), run_dir) if log_path else None,
        "verification": verification_commands,
        "verification_result": (
            relative_to(verification_result_path(run_dir, slice_id), run_dir)
            if verification_result is not None
            else None
        ),
    }
    slices = payload.setdefault("slices", [])
    replaced = False
    for index, existing in enumerate(slices):
        if existing.get("slice_id") == slice_id:
            slices[index] = entry
            replaced = True
            break
    if not replaced:
        slices.append(entry)

    completed = len([item for item in slices if item["status"] == "success"])
    failed = len([item for item in slices if item["status"] == "failed"])
    payload["completed_count"] = completed
    payload["failed_count"] = failed

    if active and active.get("slice_id") == slice_id:
        payload["active_slice"] = None

    save_run(run_dir, payload)
    append_terminal_sample(run_dir, payload, entry)
    return entry


def finish_run(run_dir: Path, status_name: str, summary: str) -> dict[str, Any]:
    payload = load_run(run_dir)
    if payload.get("active_slice"):
        raise AfkModeError("Cannot finish a run while an active slice is still open.")
    payload["status"] = status_name
    payload["finished_at"] = now_utc()
    payload["summary"] = summary
    save_run(run_dir, payload)
    clear_active_run(run_dir.parent, Path(payload["repo_root"]), payload["run_id"])
    return payload


def validate_patch_capture(
    run_dir: Path | None,
    repo_root: Path,
    output: Path,
    include_untracked: bool,
) -> None:
    if run_dir is None:
        raise AfkModeError(
            "save-patch requires --run-dir so the active slice worktree and patch "
            "destination can be verified."
        )
    payload = load_run(run_dir)
    active = payload.get("active_slice")
    if active is None:
        raise AfkModeError("Cannot save a patch because the run has no active slice.")
    expected_worktree = Path(active["worktree"]).resolve()
    if repo_root.resolve() != expected_worktree:
        raise AfkModeError(
            f"Patch capture must target the active slice worktree: {expected_worktree}"
        )
    patches_dir = (run_dir / "patches").resolve()
    if not is_within(output, patches_dir):
        raise AfkModeError(
            f"Patch output must stay under the run patches directory: {patches_dir}"
        )


def save_patch(
    repo_root: Path,
    output: Path,
    include_untracked: bool,
    run_dir: Path | None = None,
) -> dict[str, Any]:
    validate_patch_capture(run_dir, repo_root, output, include_untracked)
    if include_untracked:
        run_command(["git", "-C", str(repo_root), "add", "--intent-to-add", "--all"])
    diff = run_command(["git", "-C", str(repo_root), "diff", "--binary", "HEAD"]).stdout
    if not diff:
        raise AfkModeError("No changes found to save as a patch.")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(diff, encoding="utf-8")
    return {
        "output": str(output),
        "bytes": output.stat().st_size,
        "include_untracked": include_untracked,
        "run_dir": str(run_dir) if run_dir else None,
    }


def cleanup_run(run_dir: Path) -> dict[str, Any]:
    payload = load_run(run_dir)
    repo_root = Path(payload["repo_root"])
    worktrees_dir = run_dir / "worktrees"
    if not worktrees_dir.exists():
        return {"removed": [], "skipped": [], "failed": []}
    active_worktree = None
    if payload.get("active_slice"):
        active_worktree = Path(payload["active_slice"]["worktree"])

    removed: list[str] = []
    skipped: list[str] = []
    failed: list[dict[str, Any]] = []
    for worktree in sorted(worktrees_dir.iterdir()):
        if not worktree.exists():
            continue
        if active_worktree and worktree == active_worktree:
            skipped.append(str(worktree))
            continue
        result = run_command(
            ["git", "-C", str(repo_root), "worktree", "remove", "--force", str(worktree)],
            check=False,
        )
        if result.returncode == 0:
            removed.append(str(worktree))
            continue
        if worktree.exists():
            failed.append(
                {
                    "worktree": str(worktree),
                    "reason": result.stderr.strip() or result.stdout.strip() or "unknown",
                }
            )
    return {"removed": removed, "skipped": skipped, "failed": failed}
