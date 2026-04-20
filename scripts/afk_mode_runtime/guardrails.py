from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from .common import (
    DEFAULT_RUN_ROOT,
    DESTRUCTIVE_COMMAND_RE,
    GUARDRAIL_ACTION_ASK_FIRST,
    GUARDRAIL_ACTION_DENY,
    GUARDRAIL_APPROVAL_SCOPE_EXACT_COMMAND_ONCE,
    GUARDRAIL_APPROVAL_SCOPE_RULE_FOR_RUN,
    GUARDRAIL_CATEGORY_GIT_PUSH,
    GUARDRAIL_CATEGORY_GLOBAL_GIT_CONFIG,
    GUARDRAIL_MATCH_CATEGORY,
    GUARDRAIL_MATCH_COMMAND_SUBSTRING,
    GUARDRAIL_MATCH_PATH,
    WRITE_MODE_FALLBACK,
    is_within,
)
from .run_state import build_session_context, use_guardrail_approval


def _guardrail_roots(context: dict[str, Any]) -> list[Path]:
    roots = [Path(context["repo_root"])]
    active = context.get("active_slice")
    if active and active.get("worktree"):
        roots.append(Path(active["worktree"]))
    return roots


def _resolve_guardrail_paths(path_spec: str, context: dict[str, Any]) -> list[Path]:
    path = Path(path_spec)
    if path.is_absolute():
        return [path]
    return [root / path for root in _guardrail_roots(context)]


def _path_guardrail_hit(
    cwd: Path,
    command: str,
    path_spec: str,
    context: dict[str, Any],
) -> str | None:
    cwd_resolved = cwd.resolve()
    command_lower = command.lower()
    normalized_spec = path_spec.replace("\\", "/").lower()
    if normalized_spec and normalized_spec in command_lower:
        return path_spec
    for candidate in _resolve_guardrail_paths(path_spec, context):
        candidate_str = str(candidate).replace("\\", "/").lower()
        if is_within(cwd_resolved, candidate):
            return str(candidate)
        if candidate_str in command_lower:
            return str(candidate)
    return None


def _command_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _category_guardrail_hit(command: str, category: str) -> str | None:
    tokens = _command_tokens(command)
    if len(tokens) < 2:
        return None
    if category == GUARDRAIL_CATEGORY_GIT_PUSH and tokens[0] == "git" and tokens[1] == "push":
        return "git push"
    if (
        category == GUARDRAIL_CATEGORY_GLOBAL_GIT_CONFIG
        and tokens[0] == "git"
        and tokens[1] == "config"
        and "--global" in tokens[2:]
    ):
        return "git config --global"
    return None


def _match_guardrail(
    rule: dict[str, Any],
    cwd: Path,
    command: str,
    context: dict[str, Any],
) -> str | None:
    match_type = rule.get("match_type")
    if match_type == GUARDRAIL_MATCH_COMMAND_SUBSTRING:
        pattern = str(rule.get("pattern") or "")
        if pattern and pattern.lower() in command.lower():
            return pattern
        return None
    if match_type == GUARDRAIL_MATCH_PATH:
        return _path_guardrail_hit(cwd, command, str(rule.get("pattern") or ""), context)
    if match_type == GUARDRAIL_MATCH_CATEGORY:
        return _category_guardrail_hit(command, str(rule.get("category") or ""))
    return None


def _deny_pretool(reason: str, system_message: str, *, guardrail: dict[str, Any] | None = None) -> dict[str, Any]:
    output = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }
    if guardrail is not None:
        output["guardrail"] = guardrail
    return {
        "hookSpecificOutput": output,
        "systemMessage": system_message,
    }


def _guardrail_label(rule: dict[str, Any]) -> str:
    return str(rule.get("title") or rule.get("id") or "guardrail")


def _approval_command(run_dir: str, rule: dict[str, Any], command: str) -> str:
    wrapper_path = Path(__file__).resolve().parents[2] / "afk-mode"
    if wrapper_path.exists():
        base = (
            f"{shlex.quote(str(wrapper_path))} approve-guardrail "
            f"--run-dir {shlex.quote(run_dir)} "
            f"--rule-id {shlex.quote(str(rule['id']))} "
        )
    else:
        helper_path = Path(__file__).resolve().parents[1] / "afk_mode.py"
        base = (
            f"python3 {shlex.quote(str(helper_path))} approve-guardrail "
            f"--run-dir {shlex.quote(run_dir)} "
            f"--rule-id {shlex.quote(str(rule['id']))} "
        )
    if rule.get("approval_scope") == GUARDRAIL_APPROVAL_SCOPE_EXACT_COMMAND_ONCE:
        base += f"--approved-command {shlex.quote(command)} "
    base += '--reason "user approved ask-first guardrail"'
    return base


def _fallback_builtin_guardrail(command: str) -> dict[str, str] | None:
    tokens = _command_tokens(command)
    lowered = command.lower()
    if len(tokens) >= 2 and tokens[0] == "git":
        blocked_git_ops = {
            "branch",
            "checkout",
            "cherry-pick",
            "config",
            "merge",
            "push",
            "rebase",
            "switch",
            "tag",
        }
        if tokens[1] in blocked_git_ops:
            if tokens[1] == "config" and "--global" not in tokens[2:]:
                return None
            return {
                "rule_id": f"fallback-deny-git-{tokens[1]}",
                "title": "Fallback write mode blocks structural git commands",
                "action": GUARDRAIL_ACTION_DENY,
                "match_type": "builtin",
                "matched_by": f"git {tokens[1]}",
            }
    protected_targets = (
        ".codex/",
        ".github/",
        ".gitlab/",
        "agents.md",
        "package.json",
        "pyproject.toml",
        "cargo.toml",
        "requirements.txt",
        "dockerfile",
        "docker-compose",
        ".env",
    )
    for target in protected_targets:
        if target in lowered:
            return {
                "rule_id": "fallback-deny-protected-path-target",
                "title": "Fallback write mode blocks structural flow paths",
                "action": GUARDRAIL_ACTION_DENY,
                "match_type": "builtin",
                "matched_by": target,
            }
    return None


def pretool_decision(cwd: Path, command: str, run_root: Path = DEFAULT_RUN_ROOT) -> dict[str, Any] | None:
    context = build_session_context(cwd, run_root=run_root)
    if context is None:
        return None
    if DESTRUCTIVE_COMMAND_RE.search(command):
        active = context.get("active_slice")
        target = active["slice_id"] if active else context["run_id"]
        return _deny_pretool(
            f"Destructive Bash command blocked during active afk run {target}.",
            "AFK Mode blocked a destructive Bash command during an active run.",
        )
    if context.get("write_mode") == WRITE_MODE_FALLBACK:
        fallback_guardrail = _fallback_builtin_guardrail(command)
        if fallback_guardrail is not None:
            active = context.get("active_slice")
            target = active["slice_id"] if active else context["run_id"]
            return _deny_pretool(
                f"Fallback write guardrail blocked work during active afk run {target}.",
                "AFK Mode blocked a command because fallback write mode only allows "
                "source-and-test changes inside the run-owned branch/worktree.",
                guardrail=fallback_guardrail,
            )

    guardrails = context.get("guardrails") or {}
    rules = guardrails.get("rules") or []
    matched_denies: list[tuple[dict[str, Any], str]] = []
    matched_ask_first: list[tuple[dict[str, Any], str]] = []
    for rule in rules:
        matched_by = _match_guardrail(rule, cwd, command, context)
        if matched_by is None:
            continue
        if rule.get("action") == GUARDRAIL_ACTION_DENY:
            matched_denies.append((rule, matched_by))
            continue
        if rule.get("action") == GUARDRAIL_ACTION_ASK_FIRST:
            matched_ask_first.append((rule, matched_by))

    if matched_denies:
        rule, matched_by = matched_denies[0]
        active = context.get("active_slice")
        target = active["slice_id"] if active else context["run_id"]
        label = _guardrail_label(rule)
        return _deny_pretool(
            f"Repo profile guardrail '{rule['id']}' blocked work during active afk run {target}.",
            f"AFK Mode blocked work due to guardrail '{label}' ({rule['id']}); matched by '{matched_by}'.",
            guardrail={
                "rule_id": rule["id"],
                "title": rule.get("title", ""),
                "action": rule["action"],
                "match_type": rule["match_type"],
                "matched_by": matched_by,
            },
        )

    for rule, matched_by in matched_ask_first:
        approval = use_guardrail_approval(Path(context["run_dir"]), rule, command)
        if approval is not None:
            continue
        active = context.get("active_slice")
        target = active["slice_id"] if active else context["run_id"]
        label = _guardrail_label(rule)
        scope = rule.get("approval_scope", GUARDRAIL_APPROVAL_SCOPE_RULE_FOR_RUN)
        approval_cmd = _approval_command(context["run_dir"], rule, command)
        scope_text = (
            "this exact command once"
            if scope == GUARDRAIL_APPROVAL_SCOPE_EXACT_COMMAND_ONCE
            else "this guardrail for the rest of the run"
        )
        return _deny_pretool(
            (
                f"Repo profile requires explicit approval for guardrail '{rule['id']}' "
                f"during active afk run {target}."
            ),
            "AFK Mode blocked an ask-first guardrail. "
            f"Guardrail '{label}' ({rule['id']}) matched by '{matched_by}' and needs approval_scope="
            f"{scope}. If the user explicitly approves, allow {scope_text} with: {approval_cmd}",
            guardrail={
                "rule_id": rule["id"],
                "title": rule.get("title", ""),
                "action": rule["action"],
                "match_type": rule["match_type"],
                "matched_by": matched_by,
                "approval_scope": scope,
            },
        )

    active = context.get("active_slice")
    if active:
        worktree = Path(active["worktree"])
        if not is_within(cwd, worktree) and str(worktree) not in command:
            return {
                "systemMessage": (
                    f"Active afk slice {active['slice_id']} is open at {worktree}. "
                    "Prefer running Bash inside that worktree until the slice is recorded."
                )
            }
    return None
