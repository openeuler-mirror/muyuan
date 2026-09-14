"""Native structured-output schema and JSONL event decoding."""

import json
import os
from pathlib import Path
import stat
from typing import Any


# Structured responses use a bounded 2 MiB payload budget;
# event transcripts have a separate, larger limit in process_output.
INFERENCE_RESULT_LIMIT_BYTES = 2 * 1024 * 1024


def read_inference_output(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("InferenceOutputInvalidFile: expected a regular result file")
        if info.st_size > INFERENCE_RESULT_LIMIT_BYTES:
            raise ValueError(f"InferenceOutputLimitExceeded: result exceeds {INFERENCE_RESULT_LIMIT_BYTES} bytes")
        content = stream.read(INFERENCE_RESULT_LIMIT_BYTES + 1)
        if len(content) > INFERENCE_RESULT_LIMIT_BYTES:
            raise ValueError(f"InferenceOutputLimitExceeded: result exceeds {INFERENCE_RESULT_LIMIT_BYTES} bytes")
    return content.decode("utf-8")


def strict_output_schema(schema: dict[str, Any]) -> dict[str, Any]:
    normalized = json.loads(json.dumps(schema))

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            value.pop("default", None)
            properties = value.get("properties")
            if isinstance(properties, dict):
                value["required"] = list(properties)
                value["additionalProperties"] = False
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(normalized)
    return normalized


def pi_assistant_message(stdout: str) -> dict[str, Any] | None:
    selected: dict[str, Any] | None = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "message_end":
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                selected = message
        elif event.get("type") == "agent_end":
            messages = event.get("messages")
            if isinstance(messages, list):
                selected = next(
                    (item for item in reversed(messages)
                     if isinstance(item, dict) and item.get("role") == "assistant"),
                    selected,
                )
    return selected
