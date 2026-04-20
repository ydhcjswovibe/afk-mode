from __future__ import annotations

import datetime as dt
import math
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from .common import (
    CANDIDATES_META_FILENAME,
    ESTIMATION_RISK_VALUES,
    ESTIMATION_SIZE_VALUES,
    ESTIMATION_VERIFY_COST_VALUES,
    WRITE_MODE_FALLBACK,
    AfkModeError,
    ensure_mapping,
    json_dump,
    json_load,
    parse_timestamp,
    repo_fingerprint,
)
from .candidate_queue import load_candidate_queue, refresh_candidate_queue


ESTIMATION_SOURCE_BASELINE = "baseline"
ESTIMATION_SOURCE_LOCAL_EXACT = "local_exact"
ESTIMATION_SOURCE_LOCAL_PARTIAL = "local_partial"
ESTIMATION_CONFIDENCE_LOW = "low"
ESTIMATION_CONFIDENCE_MEDIUM = "medium"
ESTIMATION_CONFIDENCE_HIGH = "high"
ESTIMATION_TERMINAL_STATUSES = {"success", "failed"}
RECENT_WINDOW_SAMPLES = 20
DEFAULT_ESTIMATION_CONFIG = {
    "size_minutes": {
        "small": 20.0,
        "medium": 45.0,
        "large": 90.0,
    },
    "verify_minutes": {
        "fast": 5.0,
        "medium": 15.0,
        "slow": 30.0,
    },
    "risk_multipliers": {
        "low": 1.0,
        "medium": 1.25,
        "high": 1.5,
    },
    "fallback_penalty_minutes": 10.0,
    "confidence_buffer_ratio": 0.35,
    "min_local_samples": 3,
}
DEFAULT_CANDIDATE_METADATA = {
    "size": "medium",
    "risk": "medium",
    "verify_cost": "medium",
}
RANKED_SLICE_RE = re.compile(r"^\s*(\d+)\.\s+`([^`]+)`\s*$")


def _normalized_positive_number(value: Any, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise AfkModeError(f"{label} must be a positive number.")
    normalized = float(value)
    if normalized <= 0:
        raise AfkModeError(f"{label} must be a positive number.")
    return normalized


def _normalize_non_negative_number(value: Any, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise AfkModeError(f"{label} must be a non-negative number.")
    normalized = float(value)
    if normalized < 0:
        raise AfkModeError(f"{label} must be a non-negative number.")
    return normalized


def _normalize_bucket_map(
    raw: Any,
    label: str,
    *,
    keys: set[str],
    defaults: dict[str, float],
    allow_zero: bool = False,
) -> dict[str, float]:
    mapping = ensure_mapping(raw, label)
    normalized = deepcopy(defaults)
    unknown = sorted(set(mapping) - keys)
    if unknown:
        raise AfkModeError(f"{label} contains unsupported keys: {', '.join(unknown)}.")
    for key, value in mapping.items():
        item_label = f"{label}.{key}"
        normalized[key] = (
            _normalize_non_negative_number(value, item_label)
            if allow_zero
            else _normalized_positive_number(value, item_label)
        )
    return normalized


def normalize_estimation_config(raw: Any) -> dict[str, Any]:
    config = ensure_mapping(raw, "profile plugin_overrides.afk_mode.estimation")
    normalized = {
        "size_minutes": _normalize_bucket_map(
            config.get("size_minutes"),
            "profile plugin_overrides.afk_mode.estimation.size_minutes",
            keys=ESTIMATION_SIZE_VALUES,
            defaults=DEFAULT_ESTIMATION_CONFIG["size_minutes"],
        ),
        "verify_minutes": _normalize_bucket_map(
            config.get("verify_minutes"),
            "profile plugin_overrides.afk_mode.estimation.verify_minutes",
            keys=ESTIMATION_VERIFY_COST_VALUES,
            defaults=DEFAULT_ESTIMATION_CONFIG["verify_minutes"],
        ),
        "risk_multipliers": _normalize_bucket_map(
            config.get("risk_multipliers"),
            "profile plugin_overrides.afk_mode.estimation.risk_multipliers",
            keys=ESTIMATION_RISK_VALUES,
            defaults=DEFAULT_ESTIMATION_CONFIG["risk_multipliers"],
        ),
        "fallback_penalty_minutes": _normalize_non_negative_number(
            config.get(
                "fallback_penalty_minutes",
                DEFAULT_ESTIMATION_CONFIG["fallback_penalty_minutes"],
            ),
            "profile plugin_overrides.afk_mode.estimation.fallback_penalty_minutes",
        ),
        "confidence_buffer_ratio": _normalize_non_negative_number(
            config.get(
                "confidence_buffer_ratio",
                DEFAULT_ESTIMATION_CONFIG["confidence_buffer_ratio"],
            ),
            "profile plugin_overrides.afk_mode.estimation.confidence_buffer_ratio",
        ),
    }
    min_local_samples = config.get(
        "min_local_samples",
        DEFAULT_ESTIMATION_CONFIG["min_local_samples"],
    )
    if not isinstance(min_local_samples, int) or isinstance(min_local_samples, bool) or min_local_samples < 1:
        raise AfkModeError(
            "profile plugin_overrides.afk_mode.estimation.min_local_samples must be a positive integer."
        )
    normalized["min_local_samples"] = min_local_samples
    return normalized


def load_profile_estimation_config(profile: dict[str, Any]) -> dict[str, Any]:
    plugin_overrides = ensure_mapping(profile.get("plugin_overrides"), "profile plugin_overrides")
    afk_overrides = ensure_mapping(
        plugin_overrides.get("afk_mode"),
        "profile plugin_overrides.afk_mode",
    )
    return normalize_estimation_config(afk_overrides.get("estimation"))


def candidates_meta_path(run_dir: Path) -> Path:
    return run_dir / CANDIDATES_META_FILENAME


def empty_candidates_metadata() -> dict[str, Any]:
    return {"slices": []}


def write_candidates_meta_stub(run_dir: Path) -> None:
    json_dump(candidates_meta_path(run_dir), empty_candidates_metadata())


def _load_run_payload(run_dir: Path) -> dict[str, Any]:
    run_json = run_dir / "run.json"
    if not run_json.exists():
        raise AfkModeError(f"Run file not found: {run_json}")
    return json_load(run_json)


def _remaining_seconds_from_payload(run_payload: dict[str, Any]) -> int:
    started_at = parse_timestamp(run_payload.get("started_at"))
    if started_at is None:
        raise AfkModeError("Run is missing a valid started_at timestamp.")
    elapsed = max(0, int((dt.datetime.now(dt.timezone.utc) - started_at).total_seconds()))
    return max(0, int(run_payload["budget_seconds"]) - elapsed)


def _metrics_root(run_dir: Path, run_payload: dict[str, Any]) -> Path:
    metrics_root = run_payload.get("metrics_root")
    if isinstance(metrics_root, str) and metrics_root.strip():
        return Path(metrics_root)
    return run_dir.parent.parent / "afk-metrics"


def _metrics_path(run_dir: Path, run_payload: dict[str, Any]) -> Path:
    repo_root = Path(run_payload["repo_root"])
    return _metrics_root(run_dir, run_payload) / f"{repo_fingerprint(repo_root)}.json"


def _load_metrics(run_dir: Path, run_payload: dict[str, Any]) -> dict[str, Any]:
    path = _metrics_path(run_dir, run_payload)
    if not path.exists():
        return {
            "repo_root": run_payload["repo_root"],
            "fingerprint": repo_fingerprint(Path(run_payload["repo_root"])),
            "samples": [],
        }
    payload = json_load(path)
    if not isinstance(payload, dict):
        raise AfkModeError(f"Metrics file must contain a mapping: {path}")
    if not isinstance(payload.get("samples"), list):
        raise AfkModeError(f"Metrics file is missing a samples list: {path}")
    return payload


def _save_metrics(run_dir: Path, run_payload: dict[str, Any], metrics: dict[str, Any]) -> None:
    path = _metrics_path(run_dir, run_payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    json_dump(path, metrics)


def append_terminal_sample(run_dir: Path, run_payload: dict[str, Any], entry: dict[str, Any]) -> None:
    status_name = str(entry.get("status") or "")
    if status_name not in ESTIMATION_TERMINAL_STATUSES:
        return
    wall_clock_seconds = _sample_seconds(entry.get("wall_clock_seconds"))
    verification_seconds = _sample_seconds(entry.get("verification_seconds"))
    sample = {
        "run_id": run_payload["run_id"],
        "slice_id": entry.get("slice_id"),
        "recorded_at": entry.get("recorded_at"),
        "status": status_name,
        "write_mode": run_payload.get("write_mode"),
        "size": entry.get("size", DEFAULT_CANDIDATE_METADATA["size"]),
        "risk": entry.get("risk", DEFAULT_CANDIDATE_METADATA["risk"]),
        "verify_cost": entry.get("verify_cost", DEFAULT_CANDIDATE_METADATA["verify_cost"]),
        "wall_clock_seconds": wall_clock_seconds,
        "verification_seconds": verification_seconds,
        "residual_seconds": _residual_seconds(wall_clock_seconds, verification_seconds),
        "estimate_point_minutes": float(entry.get("estimate_point_minutes") or 0.0),
        "estimate_upper_minutes": float(entry.get("estimate_upper_minutes") or 0.0),
    }
    metrics = _load_metrics(run_dir, run_payload)
    samples = []
    for existing in metrics.get("samples", []):
        if not isinstance(existing, dict):
            continue
        if existing.get("run_id") == sample["run_id"] and existing.get("slice_id") == sample["slice_id"]:
            continue
        samples.append(existing)
    samples.append(sample)
    metrics["repo_root"] = run_payload["repo_root"]
    metrics["fingerprint"] = repo_fingerprint(Path(run_payload["repo_root"]))
    metrics["updated_at"] = entry.get("recorded_at")
    metrics["samples"] = samples
    _save_metrics(run_dir, run_payload, metrics)


def parse_ranked_candidates(run_dir: Path) -> list[dict[str, Any]]:
    queue = load_candidate_queue(run_dir)
    if queue.get("slices"):
        return [
            {
                "ordinal": int(entry["ordinal"]),
                "slice_id": str(entry["slice_id"]),
                "status": str(entry.get("status") or "queued"),
            }
            for entry in queue["slices"]
        ]
    candidates_path = run_dir / "candidates.md"
    if not candidates_path.exists():
        raise AfkModeError("Run candidates.md is missing.")
    ranked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in candidates_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped == "Use one compact block per slice:":
            break
        match = RANKED_SLICE_RE.match(line)
        if not match:
            continue
        ordinal = int(match.group(1))
        slice_id = match.group(2).strip()
        if not slice_id or slice_id in seen:
            continue
        ranked.append(
            {
                "ordinal": ordinal,
                "slice_id": slice_id,
                "status": "queued",
            }
        )
        seen.add(slice_id)
    return ranked


def load_candidates_metadata(run_dir: Path) -> dict[str, dict[str, str]]:
    path = candidates_meta_path(run_dir)
    if not path.exists():
        return {}
    payload = json_load(path)
    if not isinstance(payload, dict):
        raise AfkModeError("candidates.meta.json must contain a top-level object.")
    slices = payload.get("slices")
    if slices is None:
        return {}
    if not isinstance(slices, list):
        raise AfkModeError("candidates.meta.json slices must be a list.")
    result: dict[str, dict[str, str]] = {}
    for index, raw in enumerate(slices):
        entry = ensure_mapping(raw, f"candidates.meta.json slices[{index}]")
        slice_id = str(entry.get("slice_id") or "").strip()
        if not slice_id:
            raise AfkModeError(f"candidates.meta.json slices[{index}].slice_id is required.")
        if slice_id in result:
            raise AfkModeError(f"Duplicate slice_id '{slice_id}' in candidates.meta.json.")
        size = str(entry.get("size") or "").strip()
        risk = str(entry.get("risk") or "").strip()
        verify_cost = str(entry.get("verify_cost") or "").strip()
        if size not in ESTIMATION_SIZE_VALUES:
            raise AfkModeError(
                f"candidates.meta.json slices[{index}].size must be one of: {', '.join(sorted(ESTIMATION_SIZE_VALUES))}."
            )
        if risk not in ESTIMATION_RISK_VALUES:
            raise AfkModeError(
                f"candidates.meta.json slices[{index}].risk must be one of: {', '.join(sorted(ESTIMATION_RISK_VALUES))}."
            )
        if verify_cost not in ESTIMATION_VERIFY_COST_VALUES:
            raise AfkModeError(
                "candidates.meta.json "
                f"slices[{index}].verify_cost must be one of: {', '.join(sorted(ESTIMATION_VERIFY_COST_VALUES))}."
            )
        result[slice_id] = {
            "size": size,
            "risk": risk,
            "verify_cost": verify_cost,
        }
    return result


def _candidate_metadata(
    metadata_lookup: dict[str, dict[str, str]],
    slice_id: str,
) -> tuple[dict[str, str], bool]:
    metadata = metadata_lookup.get(slice_id)
    if metadata is None:
        return deepcopy(DEFAULT_CANDIDATE_METADATA), True
    return deepcopy(metadata), False


def _sample_seconds(value: Any) -> float:
    try:
        normalized = float(value or 0.0)
    except (TypeError, ValueError):
        normalized = 0.0
    return max(0.0, normalized)


def _residual_seconds(wall_clock_seconds: float, verification_seconds: float) -> float:
    return round(max(0.0, wall_clock_seconds - verification_seconds), 3)


def _normalize_terminal_sample(raw: Any, *, sample_order: int) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    status_name = str(raw.get("status") or "")
    if status_name not in ESTIMATION_TERMINAL_STATUSES:
        return None
    try:
        wall_clock_seconds = float(raw.get("wall_clock_seconds") or 0.0)
    except (TypeError, ValueError):
        return None
    wall_clock_seconds = max(0.0, wall_clock_seconds)
    verification_seconds = _sample_seconds(raw.get("verification_seconds"))
    residual_seconds = (
        _sample_seconds(raw.get("residual_seconds"))
        if "residual_seconds" in raw
        else _residual_seconds(wall_clock_seconds, verification_seconds)
    )
    recorded_at = raw.get("recorded_at")
    recorded_timestamp = parse_timestamp(recorded_at) if isinstance(recorded_at, str) and recorded_at else None
    return {
        "status": status_name,
        "write_mode": str(raw.get("write_mode") or ""),
        "size": str(raw.get("size") or DEFAULT_CANDIDATE_METADATA["size"]),
        "risk": str(raw.get("risk") or DEFAULT_CANDIDATE_METADATA["risk"]),
        "verify_cost": str(raw.get("verify_cost") or DEFAULT_CANDIDATE_METADATA["verify_cost"]),
        "wall_clock_seconds": wall_clock_seconds,
        "verification_seconds": verification_seconds,
        "residual_seconds": residual_seconds,
        "recorded_at": recorded_at,
        "recorded_timestamp": recorded_timestamp.timestamp() if recorded_timestamp is not None else None,
        "sample_order": sample_order,
    }


def _terminal_samples(run_dir: Path, run_payload: dict[str, Any]) -> list[dict[str, Any]]:
    samples = _load_metrics(run_dir, run_payload).get("samples") or []
    normalized: list[dict[str, Any]] = []
    for index, sample in enumerate(samples):
        entry = _normalize_terminal_sample(sample, sample_order=index)
        if entry is None:
            continue
        normalized.append(entry)
    return normalized


def _recent_cohort_window(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        samples,
        key=lambda sample: (
            0 if sample.get("recorded_timestamp") is not None else 1,
            -float(sample.get("recorded_timestamp") or 0.0),
            -int(sample.get("sample_order") or 0),
        ),
    )
    return ordered[:RECENT_WINDOW_SAMPLES]


def _sample_minutes(values_seconds: list[float]) -> list[float]:
    return sorted(round(value / 60.0, 3) for value in values_seconds)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    count = len(ordered)
    midpoint = count // 2
    if count % 2 == 1:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2.0


def _p80(values: list[float]) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * 0.8) - 1)
    return ordered[index]


def _confidence_for(
    *,
    source: str,
    samples_used: int,
    metadata_defaulted: bool,
) -> str:
    if metadata_defaulted:
        return ESTIMATION_CONFIDENCE_LOW
    if source == ESTIMATION_SOURCE_BASELINE:
        return ESTIMATION_CONFIDENCE_LOW
    if source == ESTIMATION_SOURCE_LOCAL_EXACT and samples_used >= 5:
        return ESTIMATION_CONFIDENCE_HIGH
    if source == ESTIMATION_SOURCE_LOCAL_EXACT:
        return ESTIMATION_CONFIDENCE_MEDIUM
    if samples_used >= 5:
        return ESTIMATION_CONFIDENCE_MEDIUM
    return ESTIMATION_CONFIDENCE_LOW


def _fit_status(point_minutes: float, upper_minutes: float, remaining_seconds: int) -> str:
    remaining_minutes = remaining_seconds / 60.0
    if upper_minutes <= remaining_minutes:
        return "fit"
    if point_minutes <= remaining_minutes:
        return "tight"
    return "over"


def _reason(
    *,
    fit_status: str,
    source: str,
    samples_used: int,
    verify_cost: str,
    write_mode: str,
) -> str:
    if source == ESTIMATION_SOURCE_LOCAL_EXACT:
        return f"{fit_status} based on {samples_used} local exact samples"
    if source == ESTIMATION_SOURCE_LOCAL_PARTIAL:
        return f"{fit_status} based on {samples_used} local partial samples"
    if write_mode == WRITE_MODE_FALLBACK:
        return f"{fit_status} because fallback mode adds penalty and only baseline data is available"
    return f"{fit_status} because verify_cost={verify_cost} and only baseline data is available"


def _baseline_point_minutes(
    config: dict[str, Any],
    *,
    write_mode: str,
    size: str,
    risk: str,
    verify_cost: str,
) -> float:
    point = config["size_minutes"][size] + config["verify_minutes"][verify_cost]
    point *= config["risk_multipliers"][risk]
    if write_mode == WRITE_MODE_FALLBACK:
        point += config["fallback_penalty_minutes"]
    return round(point, 3)


def _select_local_bucket(
    samples: list[dict[str, Any]],
    *,
    write_mode: str,
    size: str,
    risk: str,
    verify_cost: str,
    min_local_samples: int,
) -> tuple[str, list[float]]:
    exact = [
        sample
        for sample in samples
        if sample["write_mode"] == write_mode
        and sample["size"] == size
        and sample["risk"] == risk
        and sample["verify_cost"] == verify_cost
    ]
    exact_window = [sample["wall_clock_seconds"] for sample in _recent_cohort_window(exact)]
    if len(exact_window) >= min_local_samples:
        return ESTIMATION_SOURCE_LOCAL_EXACT, exact_window

    partial = [
        sample
        for sample in samples
        if sample["write_mode"] == write_mode
        and sample["size"] == size
        and sample["verify_cost"] == verify_cost
    ]
    partial_window = [sample["wall_clock_seconds"] for sample in _recent_cohort_window(partial)]
    if len(partial_window) >= min_local_samples:
        return ESTIMATION_SOURCE_LOCAL_PARTIAL, partial_window

    broader = [
        sample
        for sample in samples
        if sample["size"] == size and sample["verify_cost"] == verify_cost
    ]
    broader_window = [sample["wall_clock_seconds"] for sample in _recent_cohort_window(broader)]
    if len(broader_window) >= min_local_samples:
        return ESTIMATION_SOURCE_LOCAL_PARTIAL, broader_window
    return ESTIMATION_SOURCE_BASELINE, []


def estimate_one_slice(
    run_dir: Path,
    run_payload: dict[str, Any],
    *,
    slice_id: str,
    ordinal: int,
    remaining_seconds: int | None = None,
    metadata_lookup: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    if remaining_seconds is None:
        remaining_seconds = _remaining_seconds_from_payload(run_payload)
    if metadata_lookup is None:
        metadata_lookup = load_candidates_metadata(run_dir)

    metadata, metadata_defaulted = _candidate_metadata(metadata_lookup, slice_id)
    size = metadata["size"]
    risk = metadata["risk"]
    verify_cost = metadata["verify_cost"]
    config = load_profile_estimation_config((run_payload.get("repo_context") or {}).get("profile") or {})
    point_minutes = _baseline_point_minutes(
        config,
        write_mode=str(run_payload.get("write_mode") or ""),
        size=size,
        risk=risk,
        verify_cost=verify_cost,
    )
    source = ESTIMATION_SOURCE_BASELINE
    samples_used = 0
    local_samples_source, local_seconds = _select_local_bucket(
        _terminal_samples(run_dir, run_payload),
        write_mode=str(run_payload.get("write_mode") or ""),
        size=size,
        risk=risk,
        verify_cost=verify_cost,
        min_local_samples=config["min_local_samples"],
    )
    if local_seconds:
        local_minutes = _sample_minutes(local_seconds)
        point_minutes = round(_median(local_minutes), 3)
        samples_used = len(local_minutes)
        source = local_samples_source
        if samples_used >= 5:
            upper_minutes = round(_p80(local_minutes), 3)
        else:
            upper_minutes = round(
                point_minutes * (1.0 + config["confidence_buffer_ratio"]),
                3,
            )
    else:
        upper_minutes = round(
            point_minutes * (1.0 + config["confidence_buffer_ratio"]),
            3,
        )

    fit_status = _fit_status(point_minutes, upper_minutes, remaining_seconds)
    estimation_confidence = _confidence_for(
        source=source,
        samples_used=samples_used,
        metadata_defaulted=metadata_defaulted,
    )
    reason = _reason(
        fit_status=fit_status,
        source=source,
        samples_used=samples_used,
        verify_cost=verify_cost,
        write_mode=str(run_payload.get("write_mode") or ""),
    )
    return {
        "slice_id": slice_id,
        "ordinal": ordinal,
        "size": size,
        "risk": risk,
        "verify_cost": verify_cost,
        "point_minutes": point_minutes,
        "upper_minutes": upper_minutes,
        "fit_status": fit_status,
        "estimation_confidence": estimation_confidence,
        "samples_used": samples_used,
        "source": source,
        "reason": reason,
        "metadata_defaulted": metadata_defaulted,
    }


def estimate_candidates(
    run_dir: Path,
    *,
    run_payload: dict[str, Any] | None = None,
    remaining_seconds: int | None = None,
) -> dict[str, Any]:
    payload = run_payload or _load_run_payload(run_dir)
    if remaining_seconds is None:
        remaining_seconds = _remaining_seconds_from_payload(payload)
    ranked = parse_ranked_candidates(run_dir)
    metadata_lookup = load_candidates_metadata(run_dir)
    return {
        "run_id": payload["run_id"],
        "remaining_seconds": remaining_seconds,
        "slices": [
            estimate_one_slice(
                run_dir,
                payload,
                slice_id=entry["slice_id"],
                ordinal=entry["ordinal"],
                remaining_seconds=remaining_seconds,
                metadata_lookup=metadata_lookup,
            )
            for entry in ranked
        ],
    }


def active_slice_estimate(
    run_dir: Path,
    run_payload: dict[str, Any],
    *,
    remaining_seconds: int | None = None,
) -> dict[str, Any] | None:
    active = run_payload.get("active_slice") or {}
    slice_id = active.get("slice_id")
    if not isinstance(slice_id, str) or not slice_id:
        return None
    if remaining_seconds is None:
        remaining_seconds = _remaining_seconds_from_payload(run_payload)
    if active.get("estimate_point_minutes") is not None:
        return {
            "slice_id": slice_id,
            "ordinal": active.get("ordinal"),
            "size": active.get("size", DEFAULT_CANDIDATE_METADATA["size"]),
            "risk": active.get("risk", DEFAULT_CANDIDATE_METADATA["risk"]),
            "verify_cost": active.get("verify_cost", DEFAULT_CANDIDATE_METADATA["verify_cost"]),
            "point_minutes": active.get("estimate_point_minutes"),
            "upper_minutes": active.get("estimate_upper_minutes"),
            "fit_status": active.get("estimate_fit_status"),
            "estimation_confidence": active.get("estimation_confidence"),
            "samples_used": active.get("estimate_samples_used", 0),
            "source": active.get("estimate_source", ESTIMATION_SOURCE_BASELINE),
            "reason": active.get("estimate_reason", ""),
            "warning": active.get("estimate_warning"),
            "opened_at": active.get("opened_at"),
            "remaining_seconds_at_open": active.get("remaining_seconds_at_open"),
        }
    return estimate_one_slice(
        run_dir,
        run_payload,
        slice_id=slice_id,
        ordinal=int(active.get("ordinal") or 0),
        remaining_seconds=remaining_seconds,
    )


def next_slice_estimate(
    run_dir: Path,
    run_payload: dict[str, Any],
    *,
    remaining_seconds: int | None = None,
) -> dict[str, Any] | None:
    if remaining_seconds is None:
        remaining_seconds = _remaining_seconds_from_payload(run_payload)
    queue = refresh_candidate_queue(run_dir)
    active_slice = str((run_payload.get("active_slice") or {}).get("slice_id") or "")
    metadata_lookup = load_candidates_metadata(run_dir)
    terminal = {
        str(entry.get("slice_id"))
        for entry in queue.get("slices") or []
        if str(entry.get("status") or "") in {"done", "failed", "blocked", "skipped"}
    }
    candidates: list[tuple[int, int, int, float, int, str, dict[str, Any]]] = []
    for entry in queue.get("slices") or []:
        slice_id = str(entry.get("slice_id") or "")
        queue_status = str(entry.get("status") or "queued")
        if slice_id == active_slice:
            continue
        if queue_status not in {"queued"}:
            continue
        workflow_evidence = entry.get("workflow_evidence") or {}
        if entry.get("requires_workflow_token") and not workflow_evidence.get("validated"):
            continue
        dependencies = [dependency for dependency in entry.get("dependencies") or [] if dependency]
        if any(dependency not in terminal for dependency in dependencies):
            continue
        estimate = estimate_one_slice(
            run_dir,
            run_payload,
            slice_id=slice_id,
            ordinal=int(entry["ordinal"]),
            remaining_seconds=remaining_seconds,
            metadata_lookup=metadata_lookup,
        )
        if estimate.get("fit_status") == "over":
            continue
        fit_rank = 0 if estimate.get("fit_status") == "fit" else 1
        source_kind = str((entry.get("source") or {}).get("kind") or "")
        workflow_rank = 0 if source_kind == "workflow_item" else 1
        ready_rank = 0 if entry.get("ready_for_execution") else 1
        candidates.append(
            (
                workflow_rank,
                ready_rank,
                fit_rank,
                float(estimate["upper_minutes"]),
                int(entry["ordinal"]),
                slice_id,
                estimate,
            )
        )
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[:6])
    return candidates[0][6]


def estimate_warning_for_slice(estimate: dict[str, Any] | None) -> str | None:
    if not estimate:
        return None
    fit_status = estimate.get("fit_status")
    if fit_status == "tight":
        return "Slice estimate is tight against the remaining budget."
    if fit_status == "over":
        return "Slice estimate likely exceeds the remaining budget."
    return None
