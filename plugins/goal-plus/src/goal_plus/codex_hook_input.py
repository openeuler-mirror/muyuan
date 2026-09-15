"""Native Codex hook input decoding and exact turn-context observation."""

import json
from pathlib import Path
from typing import Any


CODEX_CANDIDATE_SESSION_ENV = "GOAL_PLUS_CODEX_CANDIDATE_SESSION_ID"
CODEX_ORDINARY_ROLE_ENV = "GOAL_PLUS_CODEX_ORDINARY_ROLE"


def codex_turn_event(hook_input: dict[str, Any]) -> dict[str, Any] | None:
    """Read the exact Codex turn that emitted this hook."""
    transcript = transcript_path(hook_input)
    requested_turn_id = turn_id(hook_input)
    if transcript is None or requested_turn_id is None:
        return None
    path = Path(transcript).expanduser()
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict) or event.get("type") != "turn_context":
                    continue
                payload = event.get("payload")
                if (
                    isinstance(payload, dict)
                    and payload.get("turn_id") == requested_turn_id
                ):
                    return event
    except OSError:
        return None
    return None


def transcript_path(hook_input: dict[str, Any]) -> str | None:
    for key in (
        "transcript_path",
        "transcriptPath",
        "agent_transcript_path",
        "agentTranscriptPath",
    ):
        value = hook_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def turn_id(hook_input: dict[str, Any]) -> str | None:
    for key in ("turn_id", "turnId"):
        value = hook_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def tool_input(hook_input: dict[str, Any]) -> dict[str, Any]:
    value = raw_tool_input(hook_input)
    return value if isinstance(value, dict) else {}


def raw_tool_input(hook_input: dict[str, Any]) -> Any:
    for key in ("tool_input", "toolInput", "input"):
        value = hook_input.get(key)
        if isinstance(value, (dict, list, str)):
            return value
    return {}
