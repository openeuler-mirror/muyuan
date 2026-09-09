from __future__ import annotations


from datetime import datetime, timezone


from html import escape

from math import isclose, isfinite


from typing import Any

PLATFORM_STOP_CLOSEOUT_MARKER = (
    "fenced by user platform-stop closeout after search promotion"
)


def format_text(value: Any) -> str:
    if value is None or value == "":
        return "Not observed"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def escape_html(value: Any) -> str:
    return escape(format_text(value), quote=True)


def parse_epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def format_timestamp(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def format_duration(value: Any) -> str:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return "Not observed"
    seconds = float(value)
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remainder = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes}m {remainder}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def format_milliseconds(value: Any) -> str:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return "Not observed"
    formatted = f"{float(value):,.3f}".rstrip("0").rstrip(".")
    return f"{formatted} ms"


def format_bytes(value: Any) -> str:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return "Not observed"
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(amount) < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TiB"


def numeric_value(value: Any, *, digits: int = 2) -> str:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return "Not observed"
    if isinstance(value, int) or float(value).is_integer():
        return f"{int(value):,}"
    return f"{float(value):,.{digits}f}".rstrip("0").rstrip(".")


def format_percent(value: Any) -> str:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return "Not observed"
    return f"{float(value) * 100:.1f}%"


def format_cost(value: Any) -> str:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return "Not observed"
    return f"${float(value):,.6f}".rstrip("0").rstrip(".")


def status_class(value: Any) -> str:
    normalized = str(value or "").lower()
    if normalized in {
        "allow",
        "complete",
        "completed",
        "promoted",
        "passed",
        "valid",
        "keep",
        "evaluated",
        "productive",
        "quiet",
        "success",
        "budget_reached",
        "completed_in_finalization_grace",
        "closeout_exempt",
        "terminal-allow",
    }:
        return "success"
    if normalized in {
        "block",
        "error",
        "failed",
        "failure",
        "aborted",
        "blocked",
        "timed_out",
        "timeout",
        "polling-only",
        "answer-only",
        "empty-output",
        "cannot-continue",
        "terminal_error",
        "invalid",
    }:
        return "failure"
    if normalized in {
        "active",
        "running",
        "waiting_for_workers",
        "ready_to_promote",
        "planned",
        "started",
        "pending",
        "retry_wait",
        "discard",
        "unavailable",
        "unverified-edit",
        "unverified-output",
        "unverified-tail",
        "unverified-tooling",
        "verified-no-revision",
    }:
        return "warning"
    if normalized == "official":
        return "official"
    return ""


def render_status(value: Any) -> str:
    return f'<span class="status {status_class(value)}">{escape_html(value)}</span>'


def reported_view_state(item: dict[str, Any]) -> str:
    state = str(item.get("view_state") or "not_requested")
    error = str(item.get("view_error") or "").casefold()
    if state == "terminal_error" and PLATFORM_STOP_CLOSEOUT_MARKER in error:
        return "closeout_exempt"
    return state


def finite_float(value: Any) -> float | None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    number = float(value)
    return number if isfinite(number) else None


def per_minute(value: Any, duration_seconds: Any) -> float | None:
    amount = finite_float(value)
    duration = finite_float(duration_seconds)
    if amount is None or duration is None or duration <= 0:
        return None
    return amount / duration * 60.0


def is_better_score(value: float, current: float, direction: str) -> bool:
    if isclose(value, current, rel_tol=1e-9, abs_tol=1e-12):
        return False
    return value < current if direction == "minimize" else value > current


def collect_session_scores(task: dict[str, Any], direction: str) -> dict[str, float]:
    scores: dict[str, float] = {}
    for candidate in task.get("candidates", []):
        for iteration in candidate.get("iterations", []):
            if iteration.get("process_passed") is not True:
                continue
            session_id = iteration.get("agent_session_id")
            score = finite_float(iteration.get("score"))
            if not isinstance(session_id, str) or score is None:
                continue
            current = scores.get(session_id)
            if current is None or is_better_score(score, current, direction):
                scores[session_id] = score
    return scores


def timeline_score_baseline(
    task: dict[str, Any],
) -> tuple[float | None, str | None]:
    scores = (task.get("statistics") or {}).get("scores") or {}
    configured = finite_float(scores.get("baseline"))
    if configured is not None:
        return configured, "configured"
    return None, None
