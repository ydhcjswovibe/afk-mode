#!/usr/bin/env python3
"""Emit resume context for active afk-mode runs."""

from __future__ import annotations

import json
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from afk_mode_runtime.hooks import session_start_payload  # noqa: E402


def main() -> int:
    payload = json.load(sys.stdin)
    response = session_start_payload(Path(payload["cwd"]))
    if response is None:
        return 0
    print(json.dumps(response))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
