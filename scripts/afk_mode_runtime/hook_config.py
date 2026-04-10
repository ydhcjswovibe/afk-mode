from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from .common import json_dump, json_load


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
AFK_HOOK_SPECS = {
    "SessionStart": {
        "matcher": "startup|resume",
        "hooks": [
            {
                "type": "command",
                "command": f"python3 {PLUGIN_ROOT / 'hooks' / 'session_start.py'}",
                "statusMessage": "Loading AFK Mode run context",
            }
        ],
    },
    "PreToolUse": {
        "matcher": "Bash",
        "hooks": [
            {
                "type": "command",
                "command": f"python3 {PLUGIN_ROOT / 'hooks' / 'pre_tool_use.py'}",
                "statusMessage": "Checking AFK Mode Bash guardrails",
            }
        ],
    },
    "Stop": {
        "hooks": [
            {
                "type": "command",
                "command": f"python3 {PLUGIN_ROOT / 'hooks' / 'stop_guard.py'}",
                "statusMessage": "Checking AFK Mode active slice",
                "timeout": 30,
            }
        ],
    },
}
AFK_HOOK_COMMANDS = {
    hook["command"]
    for spec in AFK_HOOK_SPECS.values()
    for hook in spec["hooks"]
}


def hooks_config_path(run_root: Path) -> Path:
    return run_root.parent / "hooks.json"


def load_hooks_config(run_root: Path) -> dict[str, Any]:
    path = hooks_config_path(run_root)
    if not path.exists():
        return {"hooks": {}}
    payload = json_load(path)
    if not isinstance(payload, dict):
        return {"hooks": {}}
    hooks = payload.get("hooks")
    if not isinstance(hooks, dict):
        payload["hooks"] = {}
    return payload


def _strip_afk_hooks(event_entries: list[Any]) -> list[dict[str, Any]]:
    stripped: list[dict[str, Any]] = []
    for raw_entry in event_entries:
        if not isinstance(raw_entry, dict):
            continue
        entry = deepcopy(raw_entry)
        hooks = entry.get("hooks")
        if isinstance(hooks, list):
            filtered_hooks = []
            for raw_hook in hooks:
                if not isinstance(raw_hook, dict):
                    continue
                command = raw_hook.get("command")
                if isinstance(command, str) and command in AFK_HOOK_COMMANDS:
                    continue
                filtered_hooks.append(deepcopy(raw_hook))
            if filtered_hooks:
                entry["hooks"] = filtered_hooks
                stripped.append(entry)
            continue
        stripped.append(entry)
    return stripped


def sync_afk_hooks(run_root: Path, *, enabled: bool) -> dict[str, Any]:
    path = hooks_config_path(run_root)
    payload = load_hooks_config(run_root)
    hooks = payload.setdefault("hooks", {})
    result_hooks: dict[str, Any] = {}

    for event_name, raw_entries in hooks.items():
        entries = raw_entries if isinstance(raw_entries, list) else []
        stripped = _strip_afk_hooks(entries)
        if stripped:
            result_hooks[event_name] = stripped

    if enabled:
        for event_name, spec in AFK_HOOK_SPECS.items():
            entries = result_hooks.setdefault(event_name, [])
            entries.append(deepcopy(spec))

    payload["hooks"] = result_hooks
    path.parent.mkdir(parents=True, exist_ok=True)
    json_dump(path, payload)
    return payload
