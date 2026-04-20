from __future__ import annotations
from pathlib import Path
from typing import Any

from .candidate_queue import (
    find_candidate_entry,
    inspect_candidate_plan,
    load_candidate_queue,
    plan_artifact_paths,
    refresh_candidate_queue,
    upsert_candidate_entry,
)
from .common import AfkModeError, WRITE_MODE_REPO_OWNED, now_utc, run_command, write_text_atomic
from .estimation import estimate_one_slice
from .kernel_run import cleanup_run, finish_run, save_patch
from .proof import verification_result_path, verify_slice
from .run_state import is_run_stale, load_run, save_run, status
from .workflow import open_slice, record_slice


CONTROLLER_FAILURE_LIMIT = 2
_UNSET = object()


def _controller_response(
    run_dir: Path,
    *,
    next_action: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = status(run_dir)
    payload["run_dir"] = str(run_dir)
    payload["next_action"] = next_action
    payload.setdefault("blocker_severity", "none")
    payload.setdefault("wake_operator", False)
    if extra:
        payload.update(extra)
    return payload


def _update_controller_state(
    run_dir: Path,
    payload: dict[str, Any],
    *,
    phase: str | None = None,
    controller_state: str | None = None,
    last_blocker: dict[str, Any] | None | object = _UNSET,
) -> dict[str, Any]:
    if phase is not None:
        payload["phase"] = phase
    if controller_state is not None:
        payload["controller_state"] = controller_state
    payload["heartbeat_at"] = now_utc()
    if last_blocker is not _UNSET:
        payload["last_blocker"] = last_blocker
    save_run(run_dir, payload)
    return payload


def _controller_log(run_dir: Path, slice_id: str, name: str, message: str) -> Path:
    log_dir = run_dir / "logs" / "controller"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{slice_id}-{name}.log"
    write_text_atomic(path, message.rstrip() + "\n")
    return path


def _worktree_has_changes(worktree: Path) -> bool:
    status_result = run_command(
        ["git", "-C", str(worktree), "status", "--short"],
        check=False,
    )
    return bool(status_result.stdout.strip())


def _commit_active_slice(payload: dict[str, Any]) -> tuple[str, str, Path]:
    active = payload.get("active_slice") or {}
    slice_id = str(active.get("slice_id") or "")
    branch = str(active.get("branch") or "")
    if not slice_id or not branch:
        raise AfkModeError("advance-run could not find an active slice to commit.")
    worktree = Path(str(active.get("worktree") or "")).resolve()
    if not worktree.exists():
        raise AfkModeError(f"Active slice worktree is missing: {worktree}")

    baseline_head = str((payload.get("git_baseline") or {}).get("head") or "").strip()
    current_head = run_command(["git", "-C", str(worktree), "rev-parse", "HEAD"]).stdout.strip()
    if _worktree_has_changes(worktree):
        run_command(["git", "-C", str(worktree), "add", "-A"])
        run_command(
            ["git", "-C", str(worktree), "commit", "-m", f"AFK slice: {slice_id}"],
            timeout_seconds=300,
        )
        current_head = run_command(["git", "-C", str(worktree), "rev-parse", "HEAD"]).stdout.strip()
    if current_head == baseline_head:
        raise AfkModeError("Active slice has no committed changes to verify or record.")
    return branch, current_head, worktree


def _record_controller_failure(
    run_dir: Path,
    payload: dict[str, Any],
    *,
    summary: str,
    log_path: Path | None = None,
) -> dict[str, Any]:
    active = payload.get("active_slice") or {}
    slice_id = str(active.get("slice_id") or "")
    if not slice_id:
        raise AfkModeError("Cannot record a controller failure without an active slice.")
    patch_path: str | None = None
    worktree_value = active.get("worktree")
    if isinstance(worktree_value, str) and worktree_value.strip():
        worktree = Path(worktree_value)
        if worktree.exists() and _worktree_has_changes(worktree):
            patch_target = run_dir / "patches" / f"{slice_id}.patch"
            try:
                saved = save_patch(worktree, patch_target, include_untracked=True, run_dir=run_dir)
                patch_path = str(saved["output"])
            except AfkModeError:
                patch_path = None
    if log_path is None:
        log_path = _controller_log(run_dir, slice_id, "failure", summary)
    recorded = record_slice(
        run_dir,
        slice_id,
        "failed",
        summary,
        None,
        None,
        None,
        patch_path,
        str(log_path),
        [],
    )
    refreshed = load_run(run_dir)
    refreshed["consecutive_failures"] = int(refreshed.get("consecutive_failures") or 0) + 1
    refreshed["phase"] = "cleaning"
    refreshed["controller_state"] = "recorded"
    refreshed["heartbeat_at"] = now_utc()
    save_run(run_dir, refreshed)
    cleanup_run(run_dir)
    return recorded


def _recover_stale_active_slice(run_dir: Path, payload: dict[str, Any]) -> None:
    active = payload.get("active_slice") or {}
    slice_id = str(active.get("slice_id") or "")
    if not slice_id:
        return
    summary = f"Recovered stale active slice '{slice_id}' after missing heartbeat."
    log_path = _controller_log(run_dir, slice_id, "stale", summary)
    _record_controller_failure(run_dir, payload, summary=summary, log_path=log_path)


def _candidate_terminal_statuses(queue: dict[str, Any]) -> set[str]:
    return {
        str(entry.get("slice_id"))
        for entry in queue.get("slices") or []
        if str(entry.get("status") or "") in {"done", "failed", "blocked", "skipped"}
    }


def _hard_blocker(code: str, reason: str) -> dict[str, Any]:
    return {
        "code": code,
        "reason": reason,
        "severity": "hard",
        "wake_operator": True,
    }


def _soft_stop(code: str, reason: str) -> dict[str, Any]:
    return {
        "code": code,
        "reason": reason,
        "severity": "soft",
        "wake_operator": False,
    }


def _select_next_candidate(
    run_dir: Path,
    payload: dict[str, Any],
    *,
    remaining_seconds: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    queue = refresh_candidate_queue(run_dir)
    terminal = _candidate_terminal_statuses(queue)
    eligible: list[tuple[int, int, int, float, int, str, dict[str, Any], dict[str, Any]]] = []
    dependency_waiting = False
    hard_blockers: list[dict[str, Any]] = []
    queued_count = 0
    for entry in queue.get("slices") or []:
        if str(entry.get("status") or "") != "queued":
            continue
        queued_count += 1
        slice_id = str(entry.get("slice_id") or "")
        workflow_evidence = entry.get("workflow_evidence") or {}
        if entry.get("requires_workflow_token") and not workflow_evidence.get("validated"):
            hard_blockers.append(
                _hard_blocker(
                    "workflow_evidence_required",
                    "Queued candidates require validated repo workflow evidence before AFK Mode can open them.",
                )
            )
            continue
        dependencies = [dependency for dependency in entry.get("dependencies") or [] if dependency]
        if any(dependency not in terminal for dependency in dependencies):
            dependency_waiting = True
            continue
        estimate = estimate_one_slice(
            run_dir,
            payload,
            slice_id=slice_id,
            ordinal=int(entry["ordinal"]),
            remaining_seconds=remaining_seconds,
        )
        if estimate.get("fit_status") == "over":
            continue
        fit_rank = 0 if estimate.get("fit_status") == "fit" else 1
        source_kind = str((entry.get("source") or {}).get("kind") or "")
        workflow_rank = 0 if source_kind == "workflow_item" else 1
        ready_rank = 0 if entry.get("ready_for_execution") else 1
        eligible.append(
            (
                workflow_rank,
                ready_rank,
                fit_rank,
                float(estimate["upper_minutes"]),
                int(entry["ordinal"]),
                slice_id,
                entry,
                estimate,
            )
        )
    if eligible:
        eligible.sort(key=lambda item: item[:6])
        *_, candidate, estimate = eligible[0]
        return candidate, estimate, None
    if hard_blockers:
        return None, None, hard_blockers[0]
    if dependency_waiting:
        return None, None, _soft_stop(
            "candidate_dependencies_pending",
            "Queued candidates are still waiting on dependencies before AFK Mode can continue.",
        )
    if queued_count > 0:
        return None, None, _soft_stop(
            "no_candidate_within_budget",
            "No queued candidate safely fits within the remaining budget.",
        )
    return None, None, None


def _verify_and_record_success(
    run_dir: Path,
    payload: dict[str, Any],
    *,
    summary: str,
) -> dict[str, Any]:
    active = payload.get("active_slice") or {}
    slice_id = str(active.get("slice_id") or "")
    branch, commit, worktree = _commit_active_slice(payload)
    refreshed = load_run(run_dir)
    _update_controller_state(run_dir, refreshed, phase="verifying", controller_state="committed")
    proof = verify_slice(run_dir, slice_id, str(worktree))
    refreshed = load_run(run_dir)
    _update_controller_state(run_dir, refreshed, phase="recording", controller_state="verified")
    if not proof.get("all_passed"):
        failure_log = verification_result_path(run_dir, slice_id)
        return _record_controller_failure(
            run_dir,
            load_run(run_dir),
            summary=f"Verification failed for slice '{slice_id}'. {summary}".strip(),
            log_path=failure_log,
        )
    recorded = record_slice(
        run_dir,
        slice_id,
        "success",
        summary,
        branch,
        commit,
        str(worktree),
        None,
        None,
        [],
    )
    refreshed = load_run(run_dir)
    refreshed["consecutive_failures"] = 0
    refreshed["phase"] = "cleaning"
    refreshed["controller_state"] = "recorded"
    refreshed["heartbeat_at"] = now_utc()
    refreshed["last_blocker"] = None
    save_run(run_dir, refreshed)
    cleanup_run(run_dir)
    return recorded


def advance_run(
    run_dir: Path,
    *,
    implementation_result: str | None = None,
    summary: str | None = None,
) -> dict[str, Any]:
    payload = load_run(run_dir)
    if payload.get("status") != "running":
        raise AfkModeError(
            f"Cannot advance a run with status '{payload.get('status')}'."
        )
    if implementation_result is not None and implementation_result not in {"done", "failed", "skipped"}:
        raise AfkModeError("advance-run implementation_result must be one of: done, failed, skipped.")
    if implementation_result is not None and (summary is None or not summary.strip()):
        raise AfkModeError("advance-run requires --summary when --implementation-result is set.")

    if str(payload.get("write_mode") or "") != WRITE_MODE_REPO_OWNED:
        blocker = _hard_blocker(
            "overnight_repo_owned_required",
            "advance-run only supports repo-owned write policy. Use manual primitives for fallback runs.",
        )
        _update_controller_state(
            run_dir,
            payload,
            phase="blocked",
            controller_state="blocked",
            last_blocker=blocker,
        )
        return _controller_response(
            run_dir,
            next_action="blocked",
            extra={
                "blocked": True,
                "blocker_severity": blocker["severity"],
                "wake_operator": blocker["wake_operator"],
                "blocker_code": blocker["code"],
                "reason": blocker["reason"],
            },
        )

    _update_controller_state(run_dir, payload, phase=str(payload.get("phase") or "planning"))
    payload = load_run(run_dir)

    if is_run_stale(payload):
        _recover_stale_active_slice(run_dir, payload)
        payload = load_run(run_dir)

    if payload.get("active_slice") and implementation_result is None:
        _update_controller_state(run_dir, payload, phase="implementing", controller_state="opened", last_blocker=None)
        return _controller_response(
            run_dir,
            next_action="requires_implementation",
            extra={
                "requires_implementation": True,
                "blocker_severity": "none",
                "wake_operator": False,
            },
        )

    if payload.get("active_slice") and implementation_result is not None:
        _update_controller_state(run_dir, payload, phase="recording", controller_state="implemented", last_blocker=None)
        payload = load_run(run_dir)
        if implementation_result == "done":
            _verify_and_record_success(run_dir, payload, summary=summary.strip())
        elif implementation_result == "failed":
            _record_controller_failure(run_dir, payload, summary=summary.strip())
        else:
            active = payload.get("active_slice") or {}
            slice_id = str(active.get("slice_id") or "")
            record_slice(
                run_dir,
                slice_id,
                "skipped",
                summary.strip(),
                None,
                None,
                None,
                None,
                None,
                [],
            )
            refreshed = load_run(run_dir)
            refreshed["consecutive_failures"] = 0
            refreshed["phase"] = "cleaning"
            refreshed["controller_state"] = "recorded"
            refreshed["heartbeat_at"] = now_utc()
            refreshed["last_blocker"] = None
            save_run(run_dir, refreshed)
            cleanup_run(run_dir)
        payload = load_run(run_dir)
        if int(payload.get("consecutive_failures") or 0) >= CONTROLLER_FAILURE_LIMIT:
            blocker = _hard_blocker(
                "consecutive_failures_limit",
                f"AFK Mode stopped after {payload['consecutive_failures']} consecutive failed slices.",
            )
            _update_controller_state(
                run_dir,
                payload,
                phase="blocked",
                controller_state="blocked",
                last_blocker=blocker,
            )
            return _controller_response(
                run_dir,
                next_action="blocked",
                extra={
                    "blocked": True,
                    "blocker_severity": blocker["severity"],
                    "wake_operator": blocker["wake_operator"],
                    "blocker_code": blocker["code"],
                    "reason": blocker["reason"],
                },
            )

    payload = load_run(run_dir)
    if payload.get("active_slice"):
        return _controller_response(
            run_dir,
            next_action="requires_implementation",
            extra={
                "requires_implementation": True,
                "blocker_severity": "none",
                "wake_operator": False,
            },
        )

    current_status = status(run_dir)
    remaining_seconds = int(current_status["remaining_seconds"])
    candidate, estimate, blocker = _select_next_candidate(
        run_dir,
        load_run(run_dir),
        remaining_seconds=remaining_seconds,
    )
    if candidate is None:
        queue = load_candidate_queue(run_dir)
        if blocker and blocker.get("severity") == "hard":
            _update_controller_state(
                run_dir,
                load_run(run_dir),
                phase="blocked",
                controller_state="blocked",
                last_blocker=blocker,
            )
            return _controller_response(
                run_dir,
                next_action="blocked",
                extra={
                    "blocked": True,
                    "blocker_severity": blocker["severity"],
                    "wake_operator": blocker["wake_operator"],
                    "blocker_code": blocker["code"],
                    "reason": blocker["reason"],
                },
            )
        if any(str(entry.get("status") or "") == "queued" for entry in queue.get("slices") or []):
            reason = (blocker or {}).get("reason") or "No remaining queued candidate can proceed."
            finish_run(run_dir, "stopped", reason)
            return _controller_response(
                run_dir,
                next_action="finished",
                extra={
                    "finished": True,
                    "blocker_severity": (blocker or {}).get("severity", "soft"),
                    "wake_operator": (blocker or {}).get("wake_operator", False),
                    "blocker_code": (blocker or {}).get("code"),
                    "reason": reason,
                },
            )
        finish_run(run_dir, "completed", "All queued candidates reached terminal state.")
        return _controller_response(
            run_dir,
            next_action="finished",
            extra={
                "finished": True,
                "blocker_severity": "none",
                "wake_operator": False,
            },
        )

    plan_state = inspect_candidate_plan(
        run_dir,
        str(candidate["slice_id"]),
        plan_dir=candidate.get("plan_dir"),
    )
    candidate = upsert_candidate_entry(
        run_dir,
        slice_id=str(candidate["slice_id"]),
        ordinal=int(candidate["ordinal"]),
        title=str(candidate.get("title") or candidate["slice_id"]),
        plan_dir=str(plan_state["plan_dir"]),
        plan_status=str(plan_state["plan_status"]),
        ready_for_execution=bool(plan_state["ready_for_execution"]),
        blocked_reason=None,
        blocked_at=None,
    )
    if not candidate.get("ready_for_execution"):
        plan_artifacts = plan_artifact_paths(
            run_dir,
            str(candidate["slice_id"]),
            plan_dir=candidate.get("plan_dir"),
        )
        plan_artifacts["plan_dir"].mkdir(parents=True, exist_ok=True)
        next_action = "draft_plan" if candidate.get("plan_status") == "missing" else "revise_plan"
        controller_state = "plan_required" if next_action == "draft_plan" else "plan_revision_required"
        _update_controller_state(
            run_dir,
            load_run(run_dir),
            phase="planning",
            controller_state=controller_state,
            last_blocker=None,
        )
        return _controller_response(
            run_dir,
            next_action=next_action,
            extra={
                "selected_candidate": candidate,
                "selected_estimate": estimate,
                "plan_dir": str(plan_artifacts["plan_dir"]),
                "blocker_severity": "none",
                "wake_operator": False,
            },
        )

    active = open_slice(
        run_dir,
        str(candidate["slice_id"]),
        int(candidate["ordinal"]),
        str(candidate.get("title") or candidate["slice_id"]),
    )
    return _controller_response(
        run_dir,
        next_action="requires_implementation",
        extra={
            "requires_implementation": True,
            "selected_candidate": candidate,
            "selected_estimate": estimate,
            "active_slice": active,
            "plan_dir": candidate.get("plan_dir"),
            "blocker_severity": "none",
            "wake_operator": False,
        },
    )
