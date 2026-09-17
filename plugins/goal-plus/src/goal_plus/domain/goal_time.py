"""Goal elapsed-time display from explicit clock input."""

from datetime import datetime, timezone


def elapsed_seconds(created_at: str, now: float) -> int | None:
    try:
        created = (
            datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except (TypeError, ValueError):
        return None
    return max(0, int(now - created))
