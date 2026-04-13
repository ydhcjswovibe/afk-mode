from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import fcntl
import json
import shlex
from pathlib import Path
from typing import Any

from .common import (
    ACTIVE_RUNS_FILENAME,
    DEFAULT_RUN_ROOT,
    GUARDRAIL_ACTION_ASK_FIRST,
    GUARDRAIL_APPROVAL_SCOPE_EXACT_COMMAND_ONCE,
    GUARDRAIL_APPROVAL_SCOPE_RULE_FOR_RUN,
    GUARDRAIL_CATEGORY_GIT_PUSH,
    GUARDRAIL_CATEGORY_GLOBAL_GIT_CONFIG,
    GUARDRAIL_MATCH_CATEGORY,
    GUARDRAIL_MATCH_COMMAND_SUBSTRING,
    GUARDRAIL_MATCH_PATH,
    TRUST_MODE_TRUSTED,
    VERIFICATION_SOURCE_NONE,
    WRITE_MODE_NONE,
    AfkModeError,
    is_within,
    json_dump,
    json_load,
    now_utc,
    parse_timestamp,
    unique_strings,
)
from .estimation import active_slice_estimate, next_slice_estimate
from .hook_config import sync_afk_hooks
from .policy import normalize_guardrails


def active_runs_path(run_root: Path) -> Path:
    return run_root / ACTIVE_RUNS_FILENAME


def active_runs_lock_path(run_root: Path) -> Path:
    return run_root / f"{ACTIVE_RUNS_FILENAME}.lock"


@contextmanager
def active_runs_lock(run_root: Path):
    lock_path = active_runs_lock_path(run_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_active_runs(run_root: Path) -> dict[str, Any]:
    path = active_runs_path(run_root)
    if not path.exists():
        return {"updated_at": None, "runs": {}}
    try:
        payload = json_load(path)
    except json.JSONDecodeError as exc:
        raise AfkModeError(f"Active run registry is corrupted: {path}") from exc
    if not isinstance(payload, dict):
        raise AfkModeError(f"Active run registry must contain a top-level object: {path}")
    runs = payload.get("runs")
    if runs is None:
        payload["runs"] = {}
    elif not isinstance(runs, dict):
        raise AfkModeError(f"Active run registry field 'runs' must be an object: {path}")
    return payload


def save_active_runs(run_root: Path, payload: dict[str, Any]) -> None:
    payload["updated_at"] = now_utc()
    json_dump(active_runs_path(run_root), payload)


def load_run(run_dir: Path) -> dict[str, Any]:
    run_json = run_dir / "run.json"
    if not run_json.exists():
        raise AfkModeError(f"Run file not found: {run_json}")
    return json_load(run_json)


def save_run(run_dir: Path, payload: dict[str, Any]) -> None:
    payload["last_updated_at"] = now_utc()
    json_dump(run_dir / "run.json", payload)


def _running_entry_for_repo(payload: dict[str, Any], repo_root: Path) -> dict[str, Any] | None:
    entry = payload["runs"].get(str(repo_root))
    if entry is None:
        return None
    run_dir_value = entry.get("run_dir")
    if not run_dir_value:
        del payload["runs"][str(repo_root)]
        return None
    try:
        run_payload = load_run(Path(run_dir_value))
    except (AfkModeError, OSError, json.JSONDecodeError):
        del payload["runs"][str(repo_root)]
        return None
    if run_payload.get("status") != "running":
        del payload["runs"][str(repo_root)]
        return None
    return entry


def register_active_run(run_root: Path, repo_root: Path, run_dir: Path, run_id: str) -> None:
    with active_runs_lock(run_root):
        payload = load_active_runs(run_root)
        existing = _running_entry_for_repo(payload, repo_root)
        if existing and existing.get("run_id") != run_id:
            raise AfkModeError(
                "Repo already has an active afk run: "
                f"{existing['run_id']} at {existing['run_dir']}"
            )
        payload["runs"][str(repo_root)] = {
            "run_id": run_id,
            "run_dir": str(run_dir),
            "repo_root": str(repo_root),
        }
        save_active_runs(run_root, payload)
        sync_afk_hooks(run_root, enabled=True)


def clear_active_run(run_root: Path, repo_root: Path, run_id: str) -> None:
    with active_runs_lock(run_root):
        payload = load_active_runs(run_root)
        current = payload["runs"].get(str(repo_root))
        if current and current.get("run_id") == run_id:
            del payload["runs"][str(repo_root)]
            save_active_runs(run_root, payload)
        sync_afk_hooks(run_root, enabled=bool(payload.get("runs")))


def load_run_guardrails(run_payload: dict[str, Any]) -> dict[str, Any]:
    repo_root = Path(run_payload.get("repo_root") or ".")
    repo_context = run_payload.get("repo_context") or {}
    profile = repo_context.get("profile") or {}
    return normalize_guardrails(
        profile.get("guardrails"),
        repo_root,
        source_kind="runtime_payload",
    )


def load_run_verification_route(run_payload: dict[str, Any]) -> list[str]:
    route = run_payload.get("verification_route")
    if isinstance(route, list):
        return unique_strings(
            [value.strip() for value in route if isinstance(value, str) and value.strip()]
        )
    repo_context = run_payload.get("repo_context") or {}
    profile = repo_context.get("profile") or {}
    verify = profile.get("verify") or {}
    values = verify.get("commands") or []
    if not isinstance(values, list):
        return []
    return unique_strings(
        [value.strip() for value in values if isinstance(value, str) and value.strip()]
    )


def load_run_policy_source(run_payload: dict[str, Any]) -> str:
    policy_source = run_payload.get("policy_source")
    if isinstance(policy_source, str) and policy_source.strip():
        return policy_source.strip()
    repo_context = run_payload.get("repo_context") or {}
    repo_policy_source = repo_context.get("policy_source")
    return str(repo_policy_source or "unknown")


def load_run_write_mode(run_payload: dict[str, Any]) -> str:
    write_mode = run_payload.get("write_mode")
    if isinstance(write_mode, str) and write_mode.strip():
        return write_mode.strip()
    repo_context = run_payload.get("repo_context") or {}
    repo_write_mode = repo_context.get("write_mode")
    return str(repo_write_mode or WRITE_MODE_NONE)


def load_run_write_authorized(run_payload: dict[str, Any]) -> bool:
    if "write_authorized" in run_payload:
        return bool(run_payload.get("write_authorized"))
    repo_context = run_payload.get("repo_context") or {}
    return bool(repo_context.get("write_authorized"))


def load_run_verification_source(run_payload: dict[str, Any]) -> str:
    verification_source = run_payload.get("verification_source")
    if isinstance(verification_source, str) and verification_source.strip():
        return verification_source.strip()
    repo_context = run_payload.get("repo_context") or {}
    repo_verification_source = repo_context.get("verification_source")
    return str(repo_verification_source or VERIFICATION_SOURCE_NONE)


def _command_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _category_matches_command(command: str, category: str) -> bool:
    tokens = _command_tokens(command)
    if len(tokens) < 2:
        return False
    if category == GUARDRAIL_CATEGORY_GIT_PUSH:
        return tokens[0] == "git" and tokens[1] == "push"
    if category == GUARDRAIL_CATEGORY_GLOBAL_GIT_CONFIG:
        return tokens[0] == "git" and tokens[1] == "config" and "--global" in tokens[2:]
    return False


def _rule_matches_command_without_cwd(rule: dict[str, Any], command: str) -> bool:
    command_lower = command.lower()
    match_type = rule.get("match_type")
    if match_type == GUARDRAIL_MATCH_COMMAND_SUBSTRING:
        return str(rule.get("pattern") or "").lower() in command_lower
    if match_type == GUARDRAIL_MATCH_PATH:
        return str(rule.get("pattern") or "").replace("\\", "/").lower() in command_lower
    if match_type == GUARDRAIL_MATCH_CATEGORY:
        return _category_matches_command(command, str(rule.get("category") or ""))
    return False


def _guardrail_rules_by_id(run_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        rule["id"]: rule
        for rule in load_run_guardrails(run_payload).get("rules") or []
        if isinstance(rule, dict) and rule.get("id")
    }


def _normalize_approval_entry(
    value: dict[str, Any],
    rule_lookup: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    rule_id = value.get("rule_id")
    if isinstance(rule_id, str) and rule_id.strip():
        normalized_rule_id = rule_id.strip()
        rule = rule_lookup.get(normalized_rule_id, {})
        approval_scope = str(
            value.get("approval_scope")
            or rule.get("approval_scope")
            or ""
        ).strip()
        if approval_scope not in {
            GUARDRAIL_APPROVAL_SCOPE_EXACT_COMMAND_ONCE,
            GUARDRAIL_APPROVAL_SCOPE_RULE_FOR_RUN,
        }:
            return None
        normalized: dict[str, Any] = {
            "rule_id": normalized_rule_id,
            "title": str(value.get("title") or rule.get("title") or ""),
            "action": str(value.get("action") or rule.get("action") or GUARDRAIL_ACTION_ASK_FIRST),
            "match_type": str(value.get("match_type") or rule.get("match_type") or ""),
            "approval_scope": approval_scope,
            "reason": str(value.get("reason") or ""),
            "approved_at": str(value.get("approved_at") or ""),
        }
        if "pattern" in rule:
            normalized["pattern"] = rule["pattern"]
        if "category" in rule:
            normalized["category"] = rule["category"]
        approved_command = value.get("approved_command")
        if approval_scope == GUARDRAIL_APPROVAL_SCOPE_EXACT_COMMAND_ONCE:
            if not isinstance(approved_command, str) or not approved_command.strip():
                return None
            normalized["approved_command"] = approved_command.strip()
        return normalized

    command = value.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    return {
        "rule_id": "",
        "title": "Legacy exact command approval",
        "action": GUARDRAIL_ACTION_ASK_FIRST,
        "match_type": "legacy_command",
        "approval_scope": GUARDRAIL_APPROVAL_SCOPE_EXACT_COMMAND_ONCE,
        "reason": str(value.get("reason") or ""),
        "approved_at": str(value.get("approved_at") or ""),
        "approved_command": command.strip(),
        "legacy_command_alias": True,
    }


def load_guardrail_approvals(run_payload: dict[str, Any]) -> list[dict[str, Any]]:
    values = run_payload.get("guardrail_approvals") or []
    if not isinstance(values, list):
        return []
    rule_lookup = _guardrail_rules_by_id(run_payload)
    approvals: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        normalized = _normalize_approval_entry(value, rule_lookup)
        if normalized is not None:
            approvals.append(normalized)
    return approvals


def _resolve_guardrail_rule(
    run_payload: dict[str, Any],
    *,
    rule_id: str | None,
    approved_command: str | None,
) -> dict[str, Any]:
    rule_lookup = _guardrail_rules_by_id(run_payload)
    if rule_id is not None:
        normalized_rule_id = rule_id.strip()
        if not normalized_rule_id:
            raise AfkModeError("approve-guardrail requires a non-empty --rule-id.")
        rule = rule_lookup.get(normalized_rule_id)
        if rule is None:
            raise AfkModeError(f"Unknown guardrail rule id '{normalized_rule_id}'.")
        if rule.get("action") != GUARDRAIL_ACTION_ASK_FIRST:
            raise AfkModeError(f"Guardrail rule '{normalized_rule_id}' is not an ask_first rule.")
        return rule

    normalized_command = str(approved_command or "").strip()
    if not normalized_command:
        raise AfkModeError("approve-guardrail requires --rule-id or --approved-command.")
    matches = [
        rule
        for rule in rule_lookup.values()
        if rule.get("action") == GUARDRAIL_ACTION_ASK_FIRST
        and rule.get("approval_scope") == GUARDRAIL_APPROVAL_SCOPE_EXACT_COMMAND_ONCE
        and _rule_matches_command_without_cwd(rule, normalized_command)
    ]
    if not matches:
        raise AfkModeError(
            "No exact_command_once ask_first rule matched the approved command. "
            "Use --rule-id for scope-aware approval."
        )
    if len(matches) > 1:
        raise AfkModeError(
            "Approved command matched multiple exact_command_once guardrails. "
            "Use --rule-id to disambiguate."
        )
    return matches[0]


def approve_guardrail(
    run_dir: Path,
    approved_command: str | None = None,
    reason: str | None = None,
    *,
    rule_id: str | None = None,
) -> dict[str, Any]:
    payload = load_run(run_dir)
    if payload.get("status") != "running":
        raise AfkModeError(
            f"Cannot approve guardrails on a run with status '{payload.get('status')}'."
        )
    rule = _resolve_guardrail_rule(
        payload,
        rule_id=rule_id,
        approved_command=approved_command,
    )
    scope = rule["approval_scope"]
    normalized_command = str(approved_command or "").strip() or None
    if scope == GUARDRAIL_APPROVAL_SCOPE_EXACT_COMMAND_ONCE:
        if normalized_command is None:
            raise AfkModeError(
                f"Guardrail rule '{rule['id']}' requires --approved-command for exact_command_once approval."
            )
    elif normalized_command is not None and rule_id is not None:
        raise AfkModeError(
            f"Guardrail rule '{rule['id']}' uses rule_for_run approval and should not receive --approved-command."
        )

    approvals = load_guardrail_approvals(payload)
    retained: list[dict[str, Any]] = []
    for entry in approvals:
        if entry.get("legacy_command_alias"):
            if (
                scope == GUARDRAIL_APPROVAL_SCOPE_EXACT_COMMAND_ONCE
                and entry.get("approved_command") == normalized_command
            ):
                continue
            retained.append(entry)
            continue
        if entry.get("rule_id") != rule["id"]:
            retained.append(entry)
            continue
        if scope == GUARDRAIL_APPROVAL_SCOPE_RULE_FOR_RUN:
            continue
        if entry.get("approved_command") == normalized_command:
            continue
        retained.append(entry)

    approval_entry: dict[str, Any] = {
        "rule_id": rule["id"],
        "title": rule.get("title", ""),
        "action": rule.get("action", GUARDRAIL_ACTION_ASK_FIRST),
        "match_type": rule.get("match_type", ""),
        "approval_scope": scope,
        "reason": reason or "",
        "approved_at": now_utc(),
    }
    if scope == GUARDRAIL_APPROVAL_SCOPE_EXACT_COMMAND_ONCE:
        approval_entry["approved_command"] = normalized_command
    retained.append(approval_entry)
    payload["guardrail_approvals"] = retained
    save_run(run_dir, payload)
    return {
        "run_dir": str(run_dir),
        "approved": True,
        "rule_id": rule["id"],
        "title": rule.get("title", ""),
        "approval_scope": scope,
        "approved_command": normalized_command,
        "deprecated_command_alias_used": rule_id is None,
    }


def use_guardrail_approval(
    run_dir: Path,
    rule: dict[str, Any],
    command: str,
) -> dict[str, Any] | None:
    payload = load_run(run_dir)
    approvals = load_guardrail_approvals(payload)
    normalized_command = command.strip()
    for index, entry in enumerate(approvals):
        rule_matches = entry.get("rule_id") == rule["id"]
        legacy_matches = (
            entry.get("legacy_command_alias")
            and rule.get("approval_scope") == GUARDRAIL_APPROVAL_SCOPE_EXACT_COMMAND_ONCE
        )
        if not rule_matches and not legacy_matches:
            continue
        scope = entry.get("approval_scope")
        if scope == GUARDRAIL_APPROVAL_SCOPE_RULE_FOR_RUN and rule_matches:
            return entry
        if scope != GUARDRAIL_APPROVAL_SCOPE_EXACT_COMMAND_ONCE:
            continue
        if entry.get("approved_command") != normalized_command:
            continue
        approvals.pop(index)
        payload["guardrail_approvals"] = approvals
        save_run(run_dir, payload)
        return entry
    return None


def elapsed_and_remaining_seconds(run_payload: dict[str, Any]) -> tuple[int, int]:
    started_at = parse_timestamp(run_payload.get("started_at"))
    if started_at is None:
        raise AfkModeError("Run is missing a valid started_at timestamp.")
    elapsed = max(0, int((dt.datetime.now(dt.timezone.utc) - started_at).total_seconds()))
    remaining = max(0, int(run_payload["budget_seconds"]) - elapsed)
    return elapsed, remaining


def status(run_dir: Path) -> dict[str, Any]:
    payload = load_run(run_dir)
    elapsed, remaining = elapsed_and_remaining_seconds(payload)
    return {
        "run_id": payload["run_id"],
        "status": payload["status"],
        "repo_root": payload["repo_root"],
        "trust_mode": payload.get("trust_mode", TRUST_MODE_TRUSTED),
        "elapsed_seconds": elapsed,
        "remaining_seconds": remaining,
        "active_slice": payload.get("active_slice"),
        "completed_count": payload.get("completed_count", 0),
        "failed_count": payload.get("failed_count", 0),
        "git_baseline": payload["git_baseline"],
        "policy_source": load_run_policy_source(payload),
        "write_mode": load_run_write_mode(payload),
        "write_authorized": load_run_write_authorized(payload),
        "verification_source": load_run_verification_source(payload),
        "verification_route": load_run_verification_route(payload),
        "guardrails": load_run_guardrails(payload),
        "guardrail_approvals": load_guardrail_approvals(payload),
        "active_slice_estimate": active_slice_estimate(
            run_dir,
            payload,
            remaining_seconds=remaining,
        ),
        "next_slice_estimate": next_slice_estimate(
            run_dir,
            payload,
            remaining_seconds=remaining,
        ),
    }


def find_active_run_for_repo(repo_root: Path, run_root: Path = DEFAULT_RUN_ROOT) -> dict[str, Any] | None:
    target_root = repo_root.resolve()
    registry = load_active_runs(run_root)
    entry = _running_entry_for_repo(registry, target_root)
    if entry is None:
        return None
    run_dir = Path(entry["run_dir"])
    run_payload = load_run(run_dir)
    _, remaining = elapsed_and_remaining_seconds(run_payload)
    active = run_payload.get("active_slice") or {}
    return {
        "run_id": run_payload["run_id"],
        "run_dir": str(run_dir),
        "status": run_payload.get("status"),
        "remaining_seconds": remaining,
        "active_slice_id": active.get("slice_id"),
        "active_branch": active.get("branch"),
        "active_worktree": active.get("worktree"),
    }


def find_active_run_for_cwd(cwd: Path, run_root: Path = DEFAULT_RUN_ROOT) -> dict[str, Any] | None:
    registry = load_active_runs(run_root)
    cwd_resolved = cwd.resolve()
    matches: list[tuple[int, dict[str, Any]]] = []
    for entry in registry["runs"].values():
        run_dir = Path(entry["run_dir"])
        if not run_dir.exists():
            continue
        try:
            run_payload = load_run(run_dir)
        except AfkModeError:
            continue
        if run_payload.get("status") != "running":
            continue
        repo_root = Path(run_payload["repo_root"])
        if is_within(cwd_resolved, repo_root):
            matches.append((len(repo_root.parts), {"run_dir": str(run_dir), "run": run_payload}))
            continue
        active = run_payload.get("active_slice")
        if active:
            worktree = Path(active["worktree"])
            if is_within(cwd_resolved, worktree):
                matches.append((len(worktree.parts) + 1000, {"run_dir": str(run_dir), "run": run_payload}))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0], reverse=True)
    return matches[0][1]


def build_session_context(cwd: Path, run_root: Path = DEFAULT_RUN_ROOT) -> dict[str, Any] | None:
    match = find_active_run_for_cwd(cwd, run_root=run_root)
    if not match:
        return None
    run_payload = match["run"]
    elapsed, remaining = elapsed_and_remaining_seconds(run_payload)
    active = run_payload.get("active_slice")
    return {
        "run_id": run_payload["run_id"],
        "run_dir": match["run_dir"],
        "repo_root": run_payload["repo_root"],
        "repo_name": run_payload["repo_name"],
        "trust_mode": run_payload.get("trust_mode", TRUST_MODE_TRUSTED),
        "policy_source": load_run_policy_source(run_payload),
        "write_mode": load_run_write_mode(run_payload),
        "write_authorized": load_run_write_authorized(run_payload),
        "verification_source": load_run_verification_source(run_payload),
        "verification_route": load_run_verification_route(run_payload),
        "elapsed_seconds": elapsed,
        "remaining_seconds": remaining,
        "active_slice": active,
        "completed_count": run_payload.get("completed_count", 0),
        "failed_count": run_payload.get("failed_count", 0),
        "guardrails": load_run_guardrails(run_payload),
        "guardrail_approvals": load_guardrail_approvals(run_payload),
        "active_slice_estimate": active_slice_estimate(
            Path(match["run_dir"]),
            run_payload,
            remaining_seconds=remaining,
        ),
        "next_slice_estimate": next_slice_estimate(
            Path(match["run_dir"]),
            run_payload,
            remaining_seconds=remaining,
        ),
    }
