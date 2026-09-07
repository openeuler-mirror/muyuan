"""Deterministic parsing of the existing outer-deadline formats."""

import math
from datetime import datetime, timezone


def outer_deadline_epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        numeric = float(value)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    if not math.isfinite(numeric):
        return None
    if numeric > 10_000_000_000:
        numeric /= 1000
    return numeric
