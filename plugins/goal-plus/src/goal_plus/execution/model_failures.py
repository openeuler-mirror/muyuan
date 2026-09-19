"""Normalize native provider failures without retaining credential values."""

from __future__ import annotations

import re
from typing import Any

from goal_plus.ports.session import ModelExecutionFailure


_STATUS = re.compile(r"\b([1-5][0-9]{2})\b")
_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)(api[-_ ]?key\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
)


def _redact_message(message: str) -> str:
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            message = pattern.sub(lambda match: f"{match.group(1)}<redacted>", message)
        else:
            message = pattern.sub("<redacted>", message)
    return message


def redact_model_error(error: object) -> str:
    """Bound and redact provider text before it enters durable receipts."""
    return _redact_message(str(error).strip() or "model request failed")[-2000:]


def normalize_model_failure(
    error: object,
    *,
    phase: str = "request",
    model: str | None = None,
    provider: str | None = None,
    endpoint: str | None = None,
    credential: str | None = None,
    source: str | None = None,
) -> ModelExecutionFailure | None:
    if error is None:
        return None
    message = redact_model_error(error)
    lowered = message.casefold()
    status_match = _STATUS.search(message)
    status = int(status_match.group(1)) if status_match else None
    if "credential" in lowered or "api key" in lowered or status in {401, 403}:
        code = "credential_failure" if status in {401, 403} or "credential" in lowered or "api key" in lowered else "provider_failure"
        retryable = False
    elif status == 429 or "rate limit" in lowered or "quota" in lowered:
        code, retryable = "rate_limited", True
    elif status == 404 or "model not found" in lowered or "unknown model" in lowered:
        code, retryable = "model_not_found", False
    elif "timeout" in lowered or "timed out" in lowered:
        code, retryable = "timeout", True
    elif "connect" in lowered or "endpoint" in lowered or "network" in lowered:
        code, retryable = "endpoint_unreachable", True
    elif phase == "spawn":
        code, retryable = "child_spawn_failed", False
    elif phase == "transport":
        code, retryable = "transport_failed", True
    else:
        code, retryable = "provider_rejected", False
    return ModelExecutionFailure(
        phase=phase if phase in {"admission", "spawn", "request", "response", "transport", "unknown"} else "unknown",
        code=code,
        message=message[-2000:],
        provider=provider,
        model=model,
        endpoint=endpoint,
        credential=credential,
        upstream_status=status,
        retryable=retryable,
        source=source,
    )


def model_identity(model: str | None) -> tuple[str | None, str | None]:
    if not isinstance(model, str) or not model.strip():
        return None, None
    value = model.strip()
    if "/" not in value:
        return None, value
    provider, model_id = value.split("/", 1)
    return provider or None, model_id or None
