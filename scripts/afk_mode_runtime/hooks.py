from __future__ import annotations

from pathlib import Path

from .guardrails import pretool_decision
from .run_state import build_session_context


def session_start_payload(cwd: Path) -> dict | None:
    context = build_session_context(cwd)
    if context is None:
        return None
    active = context.get("active_slice")
    active_text = "No active slice."
    if active:
        active_text = (
            f"Active slice {active['slice_id']} on branch {active['branch']} "
            f"at {active['worktree']}."
        )
    guardrails = context.get("guardrails") or {}
    rules = guardrails.get("rules") or []
    ask_first_count = sum(1 for rule in rules if rule.get("action") == "ask_first")
    deny_count = sum(1 for rule in rules if rule.get("action") == "deny")
    approvals = context.get("guardrail_approvals") or []
    run_scope_approvals = sum(
        1 for approval in approvals if approval.get("approval_scope") == "rule_for_run"
    )
    exact_scope_approvals = sum(
        1 for approval in approvals if approval.get("approval_scope") == "exact_command_once"
    )
    guardrail_text = ""
    if rules:
        guardrail_text = (
            f" Guardrails active: ask-first {ask_first_count}, deny {deny_count}."
        )
    if approvals:
        guardrail_text += (
            f" Approvals active: run-wide {run_scope_approvals}, exact-command pending {exact_scope_approvals}."
        )
    if guardrails.get("legacy_guardrails_converted"):
        converted = len(guardrails.get("converted_rule_ids") or [])
        guardrail_text += f" Legacy guardrails converted: {converted}."
    message = (
        f"AFK Mode run {context['run_id']} is active for repo {context['repo_name']}. "
        f"Write mode: {context.get('write_mode', 'unknown')}. "
        f"{active_text} Remaining budget: {context['remaining_seconds']}s. "
        f"Completed: {context['completed_count']}, failed: {context['failed_count']}. "
        f"Use the plugin runtime helper to record outcomes before stopping."
        f"{guardrail_text}"
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": message,
        }
    }


def pre_tool_payload(cwd: Path, command: str) -> dict | None:
    return pretool_decision(cwd, command)


def stop_guard_payload(cwd: Path) -> dict | None:
    context = build_session_context(cwd)
    if context is None or context.get("active_slice") is None:
        return None
    active = context["active_slice"]
    return {
        "decision": "block",
        "reason": (
            f"Active afk slice {active['slice_id']} is still open at "
            f"{active['worktree']}. Record success or failure, archive a patch "
            "if needed, then clean the run-owned worktree before stopping."
        ),
    }
