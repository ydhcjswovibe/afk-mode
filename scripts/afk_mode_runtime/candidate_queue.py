from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from .common import (
    CANDIDATES_META_FILENAME,
    CANDIDATES_QUEUE_FILENAME,
    CANDIDATES_PLACEHOLDER,
    AfkModeError,
    ensure_mapping,
    json_dump,
    json_load,
    now_utc,
    slugify,
    write_text_atomic,
)


CANDIDATE_QUEUE_VERSION = 1
CANDIDATE_STATUSES = {
    "queued",
    "active",
    "done",
    "failed",
    "blocked",
    "skipped",
}
CANDIDATE_PLAN_STATUSES = {
    "missing",
    "drafted",
    "reviewed",
    "frozen",
}
DEFAULT_CANDIDATE_ESTIMATE = {
    "size": "medium",
    "risk": "medium",
    "verify_cost": "medium",
}


def candidate_queue_path(run_dir: Path) -> Path:
    return run_dir / CANDIDATES_QUEUE_FILENAME


def empty_candidate_queue() -> dict[str, Any]:
    return {
        "version": CANDIDATE_QUEUE_VERSION,
        "generated_at": now_utc(),
        "slices": [],
    }


def plan_dir_for(run_dir: Path, slice_id: str) -> Path:
    safe_slice_id = slugify(slice_id) or "slice"
    return run_dir / "plans" / safe_slice_id


def plan_artifact_paths(
    run_dir: Path,
    slice_id: str,
    *,
    plan_dir: str | Path | None = None,
) -> dict[str, Path]:
    directory = Path(plan_dir) if plan_dir is not None else plan_dir_for(run_dir, slice_id)
    if not directory.is_absolute():
        directory = run_dir / directory
    directory = directory.resolve()
    return {
        "plan_dir": directory,
        "plan": directory / "plan.json",
        "review_summary": directory / "review_summary.json",
        "frozen_plan": directory / "frozen_plan.json",
    }


def inspect_candidate_plan(
    run_dir: Path,
    slice_id: str,
    *,
    plan_dir: str | Path | None = None,
) -> dict[str, Any]:
    artifacts = plan_artifact_paths(run_dir, slice_id, plan_dir=plan_dir)
    if artifacts["frozen_plan"].exists():
        plan_status = "frozen"
    elif artifacts["review_summary"].exists():
        plan_status = "reviewed"
    elif artifacts["plan"].exists():
        plan_status = "drafted"
    else:
        plan_status = "missing"
    return {
        "plan_dir": str(artifacts["plan_dir"]),
        "plan_status": plan_status,
        "ready_for_execution": plan_status == "frozen",
        "artifacts": artifacts,
    }


def _normalize_estimate(raw: Any) -> dict[str, str]:
    estimate = ensure_mapping(raw, "candidate estimate")
    normalized = deepcopy(DEFAULT_CANDIDATE_ESTIMATE)
    for key in ("size", "risk", "verify_cost"):
        value = estimate.get(key)
        if isinstance(value, str) and value.strip():
            normalized[key] = value.strip()
    return normalized


def _normalize_dependencies(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise AfkModeError("candidate dependencies must be a list.")
    result: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise AfkModeError("candidate dependencies must only contain non-empty strings.")
        result.append(item.strip())
    return result


def _normalize_plan_status(raw: Any, *, default: str) -> str:
    plan_status = str(raw or default).strip()
    if plan_status not in CANDIDATE_PLAN_STATUSES:
        raise AfkModeError(
            "candidate plan_status must be one of: "
            + ", ".join(sorted(CANDIDATE_PLAN_STATUSES))
            + "."
        )
    return plan_status


def _normalize_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _normalize_workflow_evidence(entry: dict[str, Any], *, index: int) -> dict[str, Any]:
    evidence = ensure_mapping(
        entry.get("workflow_evidence"),
        f"candidates.json slices[{index}].workflow_evidence",
    )
    legacy_token = _normalize_optional_string(entry.get("workflow_token"))
    if not evidence and legacy_token:
        evidence = {
            "source": "legacy_workflow_token",
            "task_id": legacy_token,
            "validated": False,
        }
    source = _normalize_optional_string(evidence.get("source"))
    task_id = _normalize_optional_string(evidence.get("task_id"))
    status_path = _normalize_optional_string(evidence.get("status_path"))
    approved_at = _normalize_optional_string(evidence.get("approved_at"))
    validated = bool(evidence.get("validated"))
    if not any((source, task_id, status_path, approved_at, validated)):
        return {}
    normalized: dict[str, Any] = {
        "validated": validated,
    }
    if source is not None:
        normalized["source"] = source
    if task_id is not None:
        normalized["task_id"] = task_id
    if status_path is not None:
        normalized["status_path"] = status_path
    if approved_at is not None:
        normalized["approved_at"] = approved_at
    return normalized


def normalize_candidate_entry(raw: Any, *, index: int) -> dict[str, Any]:
    entry = ensure_mapping(raw, f"candidates.json slices[{index}]")
    slice_id = str(entry.get("slice_id") or "").strip()
    if not slice_id:
        raise AfkModeError(f"candidates.json slices[{index}].slice_id is required.")
    ordinal = entry.get("ordinal")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1:
        raise AfkModeError(f"candidates.json slices[{index}].ordinal must be a positive integer.")
    status_name = str(entry.get("status") or "queued").strip()
    if status_name not in CANDIDATE_STATUSES:
        raise AfkModeError(
            "candidates.json "
            f"slices[{index}].status must be one of: {', '.join(sorted(CANDIDATE_STATUSES))}."
        )
    title = str(entry.get("title") or slice_id).strip() or slice_id
    source = ensure_mapping(entry.get("source"), f"candidates.json slices[{index}].source")
    normalized = {
        "slice_id": slice_id,
        "ordinal": ordinal,
        "title": title,
        "source": source,
        "estimate": _normalize_estimate(entry.get("estimate")),
        "status": status_name,
        "dependencies": _normalize_dependencies(entry.get("dependencies")),
        "requires_workflow_token": bool(entry.get("requires_workflow_token")),
        "plan_status": _normalize_plan_status(
            entry.get("plan_status"),
            default="frozen" if bool(entry.get("ready_for_execution")) else "missing",
        ),
        "ready_for_execution": bool(entry.get("ready_for_execution")),
        "workflow_evidence": _normalize_workflow_evidence(entry, index=index),
    }
    if normalized["plan_status"] == "frozen":
        normalized["ready_for_execution"] = True
    for key in (
        "workflow_token",
        "plan_dir",
        "blocked_reason",
        "blocked_at",
        "opened_at",
        "completed_at",
        "summary",
        "branch",
        "commit",
        "worktree",
    ):
        value = entry.get(key)
        if value is None:
            continue
        normalized[key] = value
    return normalized


def load_candidate_queue(run_dir: Path) -> dict[str, Any]:
    path = candidate_queue_path(run_dir)
    if not path.exists():
        return empty_candidate_queue()
    payload = json_load(path)
    if not isinstance(payload, dict):
        raise AfkModeError("candidates.json must contain a top-level object.")
    slices_raw = payload.get("slices")
    if slices_raw is None:
        slices_raw = []
    if not isinstance(slices_raw, list):
        raise AfkModeError("candidates.json slices must be a list.")
    slices = [normalize_candidate_entry(raw, index=index) for index, raw in enumerate(slices_raw)]
    seen: set[str] = set()
    for entry in slices:
        if entry["slice_id"] in seen:
            raise AfkModeError(f"Duplicate slice_id '{entry['slice_id']}' in candidates.json.")
        seen.add(entry["slice_id"])
    slices.sort(key=lambda item: (item["ordinal"], item["slice_id"]))
    return {
        "version": int(payload.get("version") or CANDIDATE_QUEUE_VERSION),
        "generated_at": str(payload.get("generated_at") or now_utc()),
        "slices": slices,
    }


def save_candidate_queue(run_dir: Path, payload: dict[str, Any]) -> None:
    normalized = load_candidate_queue_from_payload(payload)
    json_dump(candidate_queue_path(run_dir), normalized)


def load_candidate_queue_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AfkModeError("candidate queue payload must be a mapping.")
    slices_raw = payload.get("slices")
    if slices_raw is None:
        slices_raw = []
    if not isinstance(slices_raw, list):
        raise AfkModeError("candidate queue payload slices must be a list.")
    return {
        "version": int(payload.get("version") or CANDIDATE_QUEUE_VERSION),
        "generated_at": str(payload.get("generated_at") or now_utc()),
        "slices": [normalize_candidate_entry(raw, index=index) for index, raw in enumerate(slices_raw)],
    }


def _append_unique_slice(
    slices: list[dict[str, Any]],
    candidate: dict[str, Any],
    seen: set[str],
) -> None:
    slice_id = candidate["slice_id"]
    if slice_id in seen:
        return
    slices.append(candidate)
    seen.add(slice_id)


def _workflow_candidate(entry: dict[str, Any], ordinal: int) -> dict[str, Any]:
    task_id = str(entry.get("task_id") or f"workflow-{ordinal}").strip()
    title = str(entry.get("request_summary") or task_id).strip() or task_id
    return {
        "slice_id": task_id,
        "ordinal": ordinal,
        "title": title,
        "source": {
            "kind": "workflow_item",
            "task_id": task_id,
            "status_path": entry.get("status_path"),
            "workflow_path": entry.get("workflow_path"),
            "phase": entry.get("phase"),
        },
        "estimate": deepcopy(DEFAULT_CANDIDATE_ESTIMATE),
        "status": "queued",
        "dependencies": [],
        "requires_workflow_token": False,
        "plan_status": "missing",
        "ready_for_execution": False,
        "workflow_evidence": {
            "source": "workflow_state",
            "task_id": task_id,
            "status_path": entry.get("status_path"),
            "approved_at": entry.get("updated_at"),
            "validated": bool(entry.get("status_path")),
        },
    }


def _doc_candidate(
    *,
    relative_path: str,
    ordinal: int,
    requires_workflow_token: bool,
) -> dict[str, Any]:
    label = relative_path.strip() or f"truth-{ordinal}"
    return {
        "slice_id": f"truth-{slugify(label)}",
        "ordinal": ordinal,
        "title": f"Work from {label}",
        "source": {
            "kind": "repo_truth",
            "relative_path": label,
        },
        "estimate": deepcopy(DEFAULT_CANDIDATE_ESTIMATE),
        "status": "queued",
        "dependencies": [],
        "requires_workflow_token": requires_workflow_token,
        "plan_status": "missing",
        "ready_for_execution": False,
        "workflow_evidence": {
            "source": "repo_truth",
            "validated": False,
        }
        if requires_workflow_token
        else {},
    }


def build_initial_candidate_queue(discovery: dict[str, Any]) -> dict[str, Any]:
    workflow = discovery.get("workflow") or {}
    open_work = workflow.get("open_work") or []
    slices: list[dict[str, Any]] = []
    seen: set[str] = set()
    ordinal = 1

    for entry in open_work:
        if not isinstance(entry, dict):
            continue
        candidate = _workflow_candidate(entry, ordinal)
        _append_unique_slice(slices, candidate, seen)
        if candidate["slice_id"] in seen:
            ordinal += 1

    if not slices:
        truth_sources = [
            source
            for source in discovery.get("truth_sources") or []
            if isinstance(source, str) and source.strip() and source.strip() != "AGENTS.md"
        ]
        requires_workflow_token = bool(workflow.get("state_roots") or workflow.get("execution_entrypoints"))
        for relative_path in truth_sources[:3]:
            candidate = _doc_candidate(
                relative_path=relative_path,
                ordinal=ordinal,
                requires_workflow_token=requires_workflow_token,
            )
            _append_unique_slice(slices, candidate, seen)
            if candidate["slice_id"] in seen:
                ordinal += 1

    return {
        "version": CANDIDATE_QUEUE_VERSION,
        "generated_at": now_utc(),
        "slices": slices,
    }


def render_candidates_report(
    run_id: str,
    discovery: dict[str, Any],
    budget: str,
    queue: dict[str, Any],
) -> str:
    lines = [
        "# AFK Run Candidates",
        "",
        f"- Run ID: `{run_id}`",
        f"- Repo: `{discovery['repo_name']}`",
        f"- Budget: `{budget}`",
        f"- Trust Mode: `{discovery.get('trust_mode')}`",
        "",
        "## Trusted Repo Truth",
        "",
    ]
    truth_sources = discovery.get("truth_sources") or []
    if truth_sources:
        for truth_source in truth_sources:
            lines.append(f"- `{truth_source}`")
    else:
        lines.append("- None detected")

    lines.extend(
        [
            "",
            "## Candidate Queue",
            "",
        ]
    )
    slices = queue.get("slices") or []
    if not slices:
        lines.extend(
            [
                "No structured candidates were generated from repo-owned workflow items or canonical truth.",
                "",
                CANDIDATES_PLACEHOLDER,
                "",
            ]
        )
    else:
        for entry in slices:
            source = entry.get("source") or {}
            source_kind = str(source.get("kind") or "unknown")
            source_label = (
                source.get("task_id")
                or source.get("relative_path")
                or source.get("status_path")
                or source_kind
            )
            lines.extend(
                [
                    f"{entry['ordinal']}. `{entry['slice_id']}`",
                    f"   - Title: {entry.get('title')}",
                    f"   - Source: {source_kind} ({source_label})",
                    f"   - Status: {entry.get('status')}",
                    f"   - Plan: {entry.get('plan_status', 'missing')}",
                    f"   - Requires workflow token: {'yes' if entry.get('requires_workflow_token') else 'no'}",
                ]
            )
        lines.append("")

    lines.extend(
        [
            "Machine-readable estimate hints live in `candidates.meta.json`, and queue state lives in `candidates.json`.",
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


def write_candidates_artifacts(
    run_dir: Path,
    run_id: str,
    discovery: dict[str, Any],
    budget: str,
    queue: dict[str, Any],
) -> str:
    save_candidate_queue(run_dir, queue)
    content = render_candidates_report(run_id, discovery, budget, queue)
    write_text_atomic(run_dir / "candidates.md", content)
    meta_payload = {
        "slices": [
            {
                "slice_id": entry["slice_id"],
                **deepcopy(entry.get("estimate") or DEFAULT_CANDIDATE_ESTIMATE),
            }
            for entry in queue.get("slices") or []
        ]
    }
    json_dump(run_dir / CANDIDATES_META_FILENAME, meta_payload)
    return content


def find_candidate_entry(
    queue: dict[str, Any],
    slice_id: str,
) -> tuple[int, dict[str, Any]] | tuple[None, None]:
    for index, entry in enumerate(queue.get("slices") or []):
        if entry.get("slice_id") == slice_id:
            return index, entry
    return None, None


def upsert_candidate_entry(
    run_dir: Path,
    *,
    slice_id: str,
    ordinal: int,
    title: str | None = None,
    status: str | None = None,
    create_missing: bool = False,
    **updates: Any,
) -> dict[str, Any]:
    queue = load_candidate_queue(run_dir)
    index, entry = find_candidate_entry(queue, slice_id)
    if entry is None:
        if not create_missing:
            raise AfkModeError(f"Candidate '{slice_id}' was not found in candidates.json.")
        entry = {
            "slice_id": slice_id,
            "ordinal": ordinal,
            "title": title or slice_id,
            "source": {"kind": "manual_override"},
            "estimate": deepcopy(DEFAULT_CANDIDATE_ESTIMATE),
            "status": status or "queued",
            "dependencies": [],
            "requires_workflow_token": False,
        }
        queue["slices"].append(entry)
        index = len(queue["slices"]) - 1
    else:
        entry["ordinal"] = ordinal
        if title:
            entry["title"] = title
        if status:
            entry["status"] = status
    for key, value in updates.items():
        if value is None and key in {"blocked_reason", "blocked_at", "opened_at", "completed_at", "summary"}:
            entry.pop(key, None)
            continue
        entry[key] = value
    queue["slices"][index] = normalize_candidate_entry(entry, index=index)
    queue["slices"].sort(key=lambda item: (item["ordinal"], item["slice_id"]))
    save_candidate_queue(run_dir, queue)
    refreshed_index, refreshed_entry = find_candidate_entry(queue, slice_id)
    assert refreshed_index is not None and refreshed_entry is not None
    return refreshed_entry


def refresh_candidate_queue(run_dir: Path) -> dict[str, Any]:
    queue = load_candidate_queue(run_dir)
    changed = False
    refreshed_slices: list[dict[str, Any]] = []
    for index, entry in enumerate(queue.get("slices") or []):
        refreshed = dict(entry)
        plan_state = inspect_candidate_plan(
            run_dir,
            str(entry["slice_id"]),
            plan_dir=entry.get("plan_dir"),
        )
        refreshed["plan_dir"] = plan_state["plan_dir"]
        refreshed["plan_status"] = plan_state["plan_status"]
        refreshed["ready_for_execution"] = bool(plan_state["ready_for_execution"])
        normalized = normalize_candidate_entry(refreshed, index=index)
        refreshed_slices.append(normalized)
        if normalized != entry:
            changed = True
    if changed:
        queue = {
            **queue,
            "slices": refreshed_slices,
        }
        save_candidate_queue(run_dir, queue)
        return load_candidate_queue(run_dir)
    return queue
