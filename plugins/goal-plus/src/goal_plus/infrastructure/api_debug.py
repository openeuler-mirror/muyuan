"""Opt-in response size diagnostics, separate from protocol output."""

import json
import os
import sys
from typing import Literal


def api_debug_enabled() -> bool:
    return os.environ.get("GOAL_PLUS_DEBUG") == "1"


def log_api_response_size(
    tool: str,
    payload: str,
    *,
    transport: str,
    format: Literal["json", "text"],
) -> None:
    if not api_debug_enabled():
        return
    try:
        print(
            json.dumps({
                "event": "goal_plus_api_response_size",
                "transport": transport,
                "tool": tool,
                "format": format,
                "bytes": len(payload.encode("utf-8")),
            }),
            file=sys.stderr,
            flush=True,
        )
    except (OSError, ValueError, UnicodeError):
        # Diagnostics must not turn a completed mutation into a failed tool call.
        pass
