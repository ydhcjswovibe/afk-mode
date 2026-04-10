#!/usr/bin/env python3
"""Guard risky Bash commands during active afk-mode runs."""

from __future__ import annotations

import json
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from afk_mode_runtime.hooks import pre_tool_payload  # noqa: E402


def main() -> int:
    payload = json.load(sys.stdin)
    command = payload.get("tool_input", {}).get("command", "")
    decision = pre_tool_payload(Path(payload["cwd"]), command)
    if decision is None:
        return 0
    print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
