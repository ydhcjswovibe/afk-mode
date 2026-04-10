#!/usr/bin/env python3
"""Prevent stopping with an unresolved active afk-mode slice."""

from __future__ import annotations

import json
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from afk_mode_runtime.hooks import stop_guard_payload  # noqa: E402


def main() -> int:
    payload = json.load(sys.stdin)
    if payload.get("stop_hook_active"):
        print(json.dumps({"continue": True}))
        return 0
    response = stop_guard_payload(Path(payload["cwd"]))
    if response is None:
        return 0
    print(json.dumps(response))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
