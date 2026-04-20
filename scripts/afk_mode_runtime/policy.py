from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from .common import (
    CHECKED_IN_PROFILE_RELATIVE_PATHS,
    DEFAULT_PROFILE_ROOT,
    GUARDRAIL_ACTIONS,
    GUARDRAIL_ACTION_ASK_FIRST,
    GUARDRAIL_APPROVAL_SCOPES,
    GUARDRAIL_APPROVAL_SCOPE_EXACT_COMMAND_ONCE,
    GUARDRAIL_CATEGORIES,
    GUARDRAIL_MATCH_CATEGORY,
    GUARDRAIL_MATCH_COMMAND_SUBSTRING,
    GUARDRAIL_MATCH_PATH,
    SUPPORTED_CAPABILITIES,
    TRUST_MODE_ASSISTIVE,
    TRUST_MODE_OBSERVE_ONLY,
    TRUST_MODE_TRUSTED,
    VERIFICATION_SOURCE_FALLBACK,
    VERIFICATION_SOURCE_NONE,
    VERIFICATION_SOURCE_REPO_OWNED,
    WRITE_MODE_FALLBACK,
    WRITE_MODE_NONE,
    WRITE_MODE_REPO_OWNED,
    AfkModeError,
    checked_in_profile_candidates,
    default_profile_path,
    ensure_mapping,
    normalize_str_list,
    sha256_text,
    unique_strings,
    yaml_load,
)
from .estimation import normalize_estimation_config


LEGACY_GUARDRAIL_KEYS = (
    "ask_first",
    "blocked_commands",
    "blocked_paths",
)
LEGACY_COMPAT_SOURCES = {
    "user_local_overlay",
    "session_override",
    "runtime_payload",
}


def _normalize_guardrail_rule(raw: Any, label: str) -> dict[str, Any]:
    mapping = ensure_mapping(raw, label)
    rule_id = str(mapping.get("id") or "").strip()
    if not rule_id:
        raise AfkModeError(f"{label}.id is required.")
    action = str(mapping.get("action") or "").strip()
    if action not in GUARDRAIL_ACTIONS:
        raise AfkModeError(
            f"{label}.action must be one of: {', '.join(sorted(GUARDRAIL_ACTIONS))}."
        )
    match_type = str(mapping.get("match_type") or "").strip()
    if match_type not in {
        GUARDRAIL_MATCH_COMMAND_SUBSTRING,
        GUARDRAIL_MATCH_PATH,
        GUARDRAIL_MATCH_CATEGORY,
    }:
        raise AfkModeError(
            f"{label}.match_type must be one of: "
            f"{GUARDRAIL_MATCH_COMMAND_SUBSTRING}, {GUARDRAIL_MATCH_PATH}, {GUARDRAIL_MATCH_CATEGORY}."
        )

    title_value = mapping.get("title")
    title = None
    if title_value is not None:
        if not isinstance(title_value, str) or not title_value.strip():
            raise AfkModeError(f"{label}.title must be a non-empty string when set.")
        title = title_value.strip()

    normalized: dict[str, Any] = {
        "id": rule_id,
        "action": action,
        "match_type": match_type,
    }
    if title is not None:
        normalized["title"] = title

    if match_type in {GUARDRAIL_MATCH_COMMAND_SUBSTRING, GUARDRAIL_MATCH_PATH}:
        pattern = mapping.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            raise AfkModeError(f"{label}.pattern is required for match_type={match_type}.")
        normalized["pattern"] = pattern.strip()
    else:
        category = str(mapping.get("category") or "").strip()
        if category not in GUARDRAIL_CATEGORIES:
            raise AfkModeError(
                f"{label}.category must be one of: {', '.join(sorted(GUARDRAIL_CATEGORIES))}."
            )
        normalized["category"] = category

    approval_scope = mapping.get("approval_scope")
    if action == GUARDRAIL_ACTION_ASK_FIRST:
        if not isinstance(approval_scope, str) or approval_scope not in GUARDRAIL_APPROVAL_SCOPES:
            raise AfkModeError(
                f"{label}.approval_scope must be one of: "
                f"{', '.join(sorted(GUARDRAIL_APPROVAL_SCOPES))}."
            )
        normalized["approval_scope"] = approval_scope
    elif approval_scope not in (None, ""):
        raise AfkModeError(f"{label}.approval_scope is only valid for ask_first rules.")

    return normalized


def _unique_guardrail_rules(rules: list[dict[str, Any]], label: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, rule in enumerate(rules):
        normalized = _normalize_guardrail_rule(rule, f"{label}[{index}]")
        rule_id = normalized["id"]
        if rule_id in seen:
            raise AfkModeError(f"Duplicate guardrail rule id '{rule_id}' in {label}.")
        seen.add(rule_id)
        result.append(normalized)
    return result


def _legacy_rule_id(action: str, match_type: str, target: str) -> str:
    return f"legacy-{action}-{match_type}-{sha256_text(target)[:10]}"


def _legacy_ask_first_match_type(pattern: str, repo_root: Path) -> str:
    if Path(pattern).is_absolute():
        return GUARDRAIL_MATCH_PATH
    if "/" in pattern or "\\" in pattern:
        return GUARDRAIL_MATCH_PATH
    if (repo_root / pattern).exists():
        return GUARDRAIL_MATCH_PATH
    return GUARDRAIL_MATCH_COMMAND_SUBSTRING


def _convert_legacy_guardrails(guardrails: dict[str, Any], repo_root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    converted: list[dict[str, Any]] = []
    converted_ids: list[str] = []

    for pattern in normalize_str_list(guardrails.get("blocked_commands"), "guardrails.blocked_commands"):
        rule_id = _legacy_rule_id("deny", GUARDRAIL_MATCH_COMMAND_SUBSTRING, pattern)
        converted.append(
            {
                "id": rule_id,
                "title": f"Legacy blocked command: {pattern}",
                "action": "deny",
                "match_type": GUARDRAIL_MATCH_COMMAND_SUBSTRING,
                "pattern": pattern,
            }
        )
        converted_ids.append(rule_id)

    for path_spec in normalize_str_list(guardrails.get("blocked_paths"), "guardrails.blocked_paths"):
        rule_id = _legacy_rule_id("deny", GUARDRAIL_MATCH_PATH, path_spec)
        converted.append(
            {
                "id": rule_id,
                "title": f"Legacy blocked path: {path_spec}",
                "action": "deny",
                "match_type": GUARDRAIL_MATCH_PATH,
                "pattern": path_spec,
            }
        )
        converted_ids.append(rule_id)

    for pattern in normalize_str_list(guardrails.get("ask_first"), "guardrails.ask_first"):
        match_type = _legacy_ask_first_match_type(pattern, repo_root)
        rule_id = _legacy_rule_id("ask-first", match_type, pattern)
        converted.append(
            {
                "id": rule_id,
                "title": f"Legacy ask-first: {pattern}",
                "action": GUARDRAIL_ACTION_ASK_FIRST,
                "match_type": match_type,
                "pattern": pattern,
                "approval_scope": GUARDRAIL_APPROVAL_SCOPE_EXACT_COMMAND_ONCE,
            }
        )
        converted_ids.append(rule_id)

    return _unique_guardrail_rules(converted, "legacy guardrails.rules"), converted_ids


def normalize_guardrails(
    raw: Any,
    repo_root: Path,
    *,
    source_kind: str,
) -> dict[str, Any]:
    guardrails = ensure_mapping(raw, "profile guardrails")
    has_legacy = any(key in guardrails for key in LEGACY_GUARDRAIL_KEYS)
    if has_legacy and source_kind not in LEGACY_COMPAT_SOURCES:
        raise AfkModeError(
            "Checked-in profiles must use guardrails.rules; "
            "legacy ask_first/blocked_commands/blocked_paths are no longer allowed."
        )

    rules_raw = guardrails.get("rules")
    if rules_raw is None:
        rules_raw = []
    if not isinstance(rules_raw, list):
        raise AfkModeError("profile guardrails.rules must be a list.")
    rules = _unique_guardrail_rules(rules_raw, "profile guardrails.rules")
    converted_rule_ids: list[str] = []
    if has_legacy:
        legacy_rules, converted_rule_ids = _convert_legacy_guardrails(guardrails, repo_root)
        seen = {rule["id"] for rule in rules}
        for rule in legacy_rules:
            if rule["id"] in seen:
                raise AfkModeError(
                    f"Duplicate guardrail rule id '{rule['id']}' after converting legacy guardrails."
                )
            rules.append(rule)
            seen.add(rule["id"])

    return {
        "rules": rules,
        "legacy_guardrails_converted": bool(converted_rule_ids),
        "converted_rule_ids": converted_rule_ids,
    }


def merge_guardrail_policies(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    base_guardrails = ensure_mapping(base, "guardrails")
    overlay_guardrails = ensure_mapping(overlay, "guardrails")
    base_rules = _unique_guardrail_rules(base_guardrails.get("rules") or [], "guardrails.rules")
    overlay_rules = _unique_guardrail_rules(
        overlay_guardrails.get("rules") or [],
        "guardrails.rules",
    )
    merged_rules = [deepcopy(rule) for rule in base_rules]
    seen = {rule["id"] for rule in merged_rules}
    for rule in overlay_rules:
        if rule["id"] in seen:
            raise AfkModeError(f"Duplicate guardrail rule id '{rule['id']}' across profile layers.")
        merged_rules.append(deepcopy(rule))
        seen.add(rule["id"])
    converted_rule_ids = unique_strings(
        normalize_str_list(
            base_guardrails.get("converted_rule_ids"),
            "guardrails.converted_rule_ids",
        )
        + normalize_str_list(
            overlay_guardrails.get("converted_rule_ids"),
            "guardrails.converted_rule_ids",
        )
    )
    return {
        "rules": merged_rules,
        "legacy_guardrails_converted": bool(
            base_guardrails.get("legacy_guardrails_converted")
            or overlay_guardrails.get("legacy_guardrails_converted")
        ),
        "converted_rule_ids": converted_rule_ids,
    }


def merge_profile_dicts(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in overlay.items():
        if key == "plugin_overrides":
            existing = ensure_mapping(merged.get(key), "plugin_overrides")
            for plugin_name, plugin_value in ensure_mapping(value, "plugin_overrides").items():
                existing_value = existing.get(plugin_name)
                if isinstance(existing_value, dict) and isinstance(plugin_value, dict):
                    combined = deepcopy(existing_value)
                    combined.update(deepcopy(plugin_value))
                    existing[plugin_name] = combined
                else:
                    existing[plugin_name] = deepcopy(plugin_value)
            merged[key] = existing
            continue
        if key == "guardrails":
            merged[key] = merge_guardrail_policies(
                ensure_mapping(merged.get(key), "guardrails"),
                ensure_mapping(value, "guardrails"),
            )
            continue
        merged[key] = deepcopy(value)
    return merged


def normalize_profile(raw: dict[str, Any], repo_root: Path, *, source_kind: str) -> dict[str, Any]:
    truth = ensure_mapping(raw.get("truth"), "profile truth")
    verify = ensure_mapping(raw.get("verify"), "profile verify")
    workflow = ensure_mapping(raw.get("workflow"), "profile workflow")
    skills = ensure_mapping(raw.get("skills"), "profile skills")
    plugin_overrides = deepcopy(ensure_mapping(raw.get("plugin_overrides"), "profile plugin_overrides"))
    if "afk_mode" in plugin_overrides:
        afk_overrides = ensure_mapping(
            plugin_overrides.get("afk_mode"),
            "profile plugin_overrides.afk_mode",
        )
        normalized_afk_overrides = deepcopy(afk_overrides)
        normalized_afk_overrides["estimation"] = normalize_estimation_config(
            afk_overrides.get("estimation")
        )
        plugin_overrides["afk_mode"] = normalized_afk_overrides
    capabilities = normalize_str_list(raw.get("capabilities"), "profile capabilities")
    unknown_capabilities = sorted(set(capabilities) - SUPPORTED_CAPABILITIES)
    if unknown_capabilities:
        raise AfkModeError(
            "Unsupported profile capabilities: " + ", ".join(unknown_capabilities)
        )
    version = raw.get("version", 1)
    if not isinstance(version, int):
        raise AfkModeError("profile version must be an integer.")
    load_command = skills.get("load_command")
    if load_command is not None and not isinstance(load_command, str):
        raise AfkModeError("profile skills.load_command must be a string when set.")
    return {
        "version": version,
        "repo_id": str(raw.get("repo_id") or repo_root.name),
        "display_name": str(raw.get("display_name") or repo_root.name),
        "truth": {
            "order": normalize_str_list(truth.get("order"), "profile truth.order"),
        },
        "verify": {
            "commands": normalize_str_list(verify.get("commands"), "profile verify.commands"),
        },
        "workflow": {
            "status_roots": normalize_str_list(
                workflow.get("status_roots"), "profile workflow.status_roots"
            ),
            "execution_entrypoints": normalize_str_list(
                workflow.get("execution_entrypoints"),
                "profile workflow.execution_entrypoints",
            ),
        },
        "guardrails": normalize_guardrails(
            raw.get("guardrails"),
            repo_root,
            source_kind=source_kind,
        ),
        "skills": {
            "paths": normalize_str_list(skills.get("paths"), "profile skills.paths"),
            "load_command": load_command,
        },
        "capabilities": capabilities,
        "plugin_overrides": plugin_overrides,
    }


def load_profile_file(path: Path, repo_root: Path, *, source_kind: str) -> dict[str, Any]:
    return normalize_profile(yaml_load(path), repo_root, source_kind=source_kind)


def _checked_in_verify_commands(checked_in_profile: dict[str, Any] | None) -> list[str]:
    if checked_in_profile is None:
        return []
    return normalize_str_list(
        checked_in_profile.get("verify", {}).get("commands"),
        "profile verify.commands",
    )


def _generic_verification_route(signals: dict[str, Any]) -> list[str]:
    verification = ensure_mapping(signals.get("verification"), "signals verification")
    return unique_strings(
        normalize_str_list(
            verification.get("commands"),
            "signals verification.commands",
        )
    )


def _generic_file_checks(signals: dict[str, Any]) -> list[str]:
    verification = ensure_mapping(signals.get("verification"), "signals verification")
    return unique_strings(
        normalize_str_list(
            verification.get("generic_file_checks"),
            "signals verification.generic_file_checks",
        )
    )


def resolve_verification_source(
    checked_in_profile: dict[str, Any] | None,
    signals: dict[str, Any],
) -> str:
    if _checked_in_verify_commands(checked_in_profile):
        return VERIFICATION_SOURCE_REPO_OWNED
    if _generic_verification_route(signals) or _generic_file_checks(signals):
        return VERIFICATION_SOURCE_FALLBACK
    return VERIFICATION_SOURCE_NONE


def resolve_verification_route(
    checked_in_profile: dict[str, Any] | None,
) -> list[str]:
    if checked_in_profile is None:
        return []
    return _checked_in_verify_commands(checked_in_profile)


def resolve_fallback_verification_route(signals: dict[str, Any]) -> list[str]:
    return _generic_verification_route(signals)


def _fallback_truth_ready(signals: dict[str, Any]) -> bool:
    truth_order = normalize_str_list(
        signals.get("truth_order"),
        "signals truth_order",
    )
    return any(source != "AGENTS.md" for source in truth_order)


def resolve_write_mode(
    checked_in_profile: dict[str, Any] | None,
    merged_profile: dict[str, Any] | None,
    signals: dict[str, Any],
    *,
    verification_source: str,
) -> tuple[str, bool]:
    declared_capabilities = set((merged_profile or {}).get("capabilities") or [])
    repo_owned_allowed = (
        checked_in_profile is not None
        and "isolated_write" in declared_capabilities
        and bool(_checked_in_verify_commands(checked_in_profile))
    )
    if repo_owned_allowed:
        return WRITE_MODE_REPO_OWNED, True
    fallback_available = (
        checked_in_profile is None
        and _fallback_truth_ready(signals)
        and verification_source == VERIFICATION_SOURCE_FALLBACK
    )
    if fallback_available:
        return WRITE_MODE_FALLBACK, False
    return WRITE_MODE_NONE, False


def resolve_trust_mode(
    checked_in_profile: dict[str, Any] | None,
    merged_profile: dict[str, Any] | None,
    signals: dict[str, Any],
    *,
    verification_source: str,
) -> str:
    if checked_in_profile is not None:
        truth_ready = bool(checked_in_profile["truth"]["order"]) or bool(signals["truth_order"])
        verify_ready = bool(_checked_in_verify_commands(checked_in_profile))
        if truth_ready and verify_ready:
            return TRUST_MODE_TRUSTED
        return TRUST_MODE_ASSISTIVE
    if merged_profile is not None and signals["truth_order"] and verification_source != VERIFICATION_SOURCE_NONE:
        return TRUST_MODE_ASSISTIVE
    if signals["truth_order"] and verification_source != VERIFICATION_SOURCE_NONE:
        return TRUST_MODE_ASSISTIVE
    return TRUST_MODE_OBSERVE_ONLY


def build_profile_enablement_hint(repo_root: Path, profile_root: Path = DEFAULT_PROFILE_ROOT) -> str:
    return (
        "Add a checked-in repo profile at "
        f"{repo_root / CHECKED_IN_PROFILE_RELATIVE_PATHS[0]} "
        f"or {repo_root / CHECKED_IN_PROFILE_RELATIVE_PATHS[1]} "
        ". Local overlays at "
        f"{default_profile_path(repo_root, profile_root)} only narrow policy and cannot grant repo-owned write access."
    )


def build_repo_context(
    repo_root: Path,
    signals: dict[str, Any],
    *,
    explicit_profile: Path | None = None,
    profile_root: Path = DEFAULT_PROFILE_ROOT,
) -> dict[str, Any]:
    checked_in_profile: dict[str, Any] | None = None
    overlay_profile: dict[str, Any] | None = None
    sources: list[dict[str, str]] = []
    checked_in_capabilities: set[str] | None = None
    checked_in_paths = checked_in_profile_candidates(repo_root)
    overlay_path = default_profile_path(repo_root, profile_root)
    profile_paths: list[tuple[str, Path, bool]] = []
    for checked_in_path in checked_in_paths:
        profile_paths.append(("checked_in", checked_in_path, False))
    profile_paths.append(("user_local_overlay", overlay_path, False))
    if explicit_profile is not None:
        profile_paths.append(("session_override", explicit_profile, True))

    seen_profile_paths: set[Path] = set()
    for kind, path, required in profile_paths:
        try:
            resolved_path = path.resolve()
        except OSError:
            resolved_path = path
        if resolved_path in seen_profile_paths:
            continue
        seen_profile_paths.add(resolved_path)
        if not path.exists():
            if required:
                raise AfkModeError(f"Requested profile was not found: {path}")
            continue
        normalized = load_profile_file(path, repo_root, source_kind=kind)
        if kind == "checked_in":
            checked_in_profile = (
                normalized
                if checked_in_profile is None
                else merge_profile_dicts(checked_in_profile, normalized)
            )
            current_capabilities = set(normalized["capabilities"])
            checked_in_capabilities = (
                current_capabilities
                if checked_in_capabilities is None
                else checked_in_capabilities & current_capabilities
            )
        else:
            overlay_profile = (
                normalized
                if overlay_profile is None
                else merge_profile_dicts(overlay_profile, normalized)
            )
        sources.append({"kind": kind, "path": str(path)})

    merged_profile: dict[str, Any] | None = None
    if checked_in_profile is not None and overlay_profile is not None:
        merged_profile = merge_profile_dicts(checked_in_profile, overlay_profile)
    elif checked_in_profile is not None:
        merged_profile = deepcopy(checked_in_profile)
    elif overlay_profile is not None:
        merged_profile = deepcopy(overlay_profile)

    if merged_profile is not None and checked_in_capabilities is not None:
        merged_profile["capabilities"] = [
            capability
            for capability in merged_profile["capabilities"]
            if capability in checked_in_capabilities
        ]
    if merged_profile is not None and checked_in_profile is not None:
        merged_profile["verify"] = deepcopy(checked_in_profile["verify"])
        merged_profile["workflow"] = deepcopy(checked_in_profile["workflow"])
        merged_profile["guardrails"] = merge_guardrail_policies(
            checked_in_profile.get("guardrails") or {},
            overlay_profile.get("guardrails") or {} if overlay_profile is not None else {},
        )

    verification_source = resolve_verification_source(checked_in_profile, signals)
    verification_route = resolve_verification_route(checked_in_profile)
    fallback_verification_route = resolve_fallback_verification_route(signals)
    write_mode, write_authorized = resolve_write_mode(
        checked_in_profile,
        merged_profile,
        signals,
        verification_source=verification_source,
    )
    trust_mode = resolve_trust_mode(
        checked_in_profile,
        merged_profile,
        signals,
        verification_source=verification_source,
    )
    checked_in_source = next((source["path"] for source in sources if source["kind"] == "checked_in"), None)
    policy_source = "repo_owned"
    if checked_in_profile is None:
        if merged_profile is not None:
            policy_source = "local_overlay"
        else:
            policy_source = "heuristic"
    return {
        "fingerprint": signals["fingerprint"],
        "checked_in_profile_path": checked_in_source or str(checked_in_paths[0]),
        "checked_in_profile_candidates": [str(path) for path in checked_in_paths],
        "user_local_profile_path": str(overlay_path),
        "suggested_profile_path": str(overlay_path),
        "sources": sources,
        "profile": merged_profile,
        "checked_in_profile_present": checked_in_profile is not None,
        "policy_source": policy_source,
        "trust_mode": trust_mode,
        "verification_source": verification_source,
        "verification_route": verification_route,
        "fallback_verification_route": fallback_verification_route,
        "write_mode": write_mode,
        "write_authorized": write_authorized,
    }


def request_capability(repo_context: dict[str, Any], capability: str) -> dict[str, Any]:
    if capability not in SUPPORTED_CAPABILITIES:
        raise AfkModeError(f"Unsupported capability request: {capability}")
    profile = repo_context.get("profile") or {}
    signals = repo_context["signals"]
    trust_mode = repo_context["trust_mode"]
    declared_capabilities = set(profile.get("capabilities") or [])
    verify_commands = (
        repo_context.get("verification_route")
        or repo_context.get("fallback_verification_route")
        or _generic_verification_route(signals)
    )
    workflow_roots = profile.get("workflow", {}).get("status_roots") or [
        root["relative_path"] for root in signals["workflow_state"]["roots"]
    ]
    skill_paths = profile.get("skills", {}).get("paths") or signals["skills"]["paths"]

    if capability == "read_discovery":
        return {"capability": capability, "allowed": True, "reason": "Discovery is always allowed."}
    if capability == "verification_routing":
        allowed = (
            trust_mode in {TRUST_MODE_TRUSTED, TRUST_MODE_ASSISTIVE}
            and (
                bool(verify_commands)
                or bool(_generic_file_checks(signals))
            )
        )
        reason = (
            "Verification routes are available."
            if allowed
            else "No reliable verification route is available yet."
        )
        return {"capability": capability, "allowed": allowed, "reason": reason}
    if capability == "repo_workflow_read":
        allowed = trust_mode in {TRUST_MODE_TRUSTED, TRUST_MODE_ASSISTIVE} and bool(workflow_roots)
        reason = (
            "Repo workflow state can be read."
            if allowed
            else "No authoritative repo workflow state path was detected."
        )
        return {"capability": capability, "allowed": allowed, "reason": reason}
    if capability == "skill_guidance":
        allowed = bool(skill_paths)
        reason = (
            "Repo-local skill guidance is available."
            if allowed
            else "No repo-local skill index was detected."
        )
        return {"capability": capability, "allowed": allowed, "reason": reason}
    if capability == "session_guardrails":
        allowed = repo_context.get("write_mode") in {WRITE_MODE_REPO_OWNED, WRITE_MODE_FALLBACK}
        reason = (
            "AFK-mode session guardrails are available for runnable repos."
            if allowed
            else "Session guardrails only activate once repo-owned or fallback write admission is possible."
        )
        return {"capability": capability, "allowed": allowed, "reason": reason}
    allowed = capability == "isolated_write" and repo_context.get("write_mode") == WRITE_MODE_REPO_OWNED
    allowed = allowed and bool(repo_context.get("write_authorized")) and capability in declared_capabilities
    reason = (
        "Isolated writes are allowed by the trusted repo profile."
        if allowed
        else "Repo-owned isolated writes require a checked-in repo policy with an explicit safe verification path. "
        + build_profile_enablement_hint(repo_context["repo_root_path"], repo_context["profile_root"])
    )
    return {"capability": capability, "allowed": allowed, "reason": reason}


def build_bootstrap_profile(
    repo_root: Path,
    signals: dict[str, Any],
    *,
    allow_isolated_write: bool,
) -> dict[str, Any]:
    capabilities = [
        "read_discovery",
        "verification_routing",
        "session_guardrails",
    ]
    if signals["workflow_state"]["roots"]:
        capabilities.append("repo_workflow_read")
    return {
        "version": 1,
        "repo_id": repo_root.name,
        "display_name": repo_root.name,
        "truth": {
            "order": signals["truth_order"],
        },
        "verify": {
            "commands": [],
        },
        "workflow": {
            "status_roots": [root["relative_path"] for root in signals["workflow_state"]["roots"]],
            "execution_entrypoints": [],
        },
        "guardrails": {
            "rules": [],
        },
        "skills": signals["skills"],
        "capabilities": unique_strings(capabilities),
        "plugin_overrides": {
            "afk_mode": {
                "candidate_docs": signals["truth_order"],
                "discovery_hints": {
                    "verification_commands": signals["verification"]["commands"],
                    "generic_file_checks": signals["verification"]["generic_file_checks"],
                    "execution_entrypoints": signals["workflow_state"]["execution_entrypoints"],
                },
            }
        },
    }
