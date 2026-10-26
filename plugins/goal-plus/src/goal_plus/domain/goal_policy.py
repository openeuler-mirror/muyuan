"""Pure Goal policy, revision deadlines and invocation provenance."""

from typing import Any
from datetime import datetime, timedelta, timezone
from goal_plus.domain.goal import (
    GoalPlusRecord,
    GoalPlusHostCommandInvocation,
    GoalPlusExecutionPolicy,
)
from goal_plus.domain.goal_commands import DEFAULT_EXECUTION_POLICY
from goal_plus.domain.goal_authorization import has_host_invocation


def normalize_policy(policy: dict[str, Any] | None) -> dict[str, Any]:
    normalized = dict(policy or {})
    execution = normalized.get("execution", {})
    if not isinstance(execution, dict):
        raise ValueError("policy.execution must be an object")
    normalized["execution"] = GoalPlusExecutionPolicy.model_validate(
        {**DEFAULT_EXECUTION_POLICY, **execution}
    ).model_dump(mode="json", exclude_none=True)
    final_check = normalized.get("final_check")
    if final_check is None:
        return normalized
    if isinstance(final_check, str):
        mode = final_check
        final_check = {"mode": mode}
    if not isinstance(final_check, dict):
        raise ValueError("policy.final_check must be an object or mode string")
    mode = final_check.get("mode", "disabled")
    if mode not in {"disabled", "required"}:
        raise ValueError("policy.final_check.mode must be 'disabled' or 'required'")
    normalized["final_check"] = {**final_check, "mode": mode}
    return normalized


def deadline_from_creation(
    created_at: str,
    max_duration_seconds: int | None,
) -> str | None:
    if max_duration_seconds is None:
        return None
    created = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    return (created + timedelta(seconds=max_duration_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def with_host_invocation(
    record: GoalPlusRecord,
    invocation: GoalPlusHostCommandInvocation,
    now: str,
) -> GoalPlusRecord:
    if has_host_invocation(record, invocation.invocation_id):
        return record
    return record.model_copy(
        update={
            "entrypoint_status": "host_verified",
            "host_command_invocations": [
                *record.host_command_invocations,
                invocation.model_copy(deep=True),
            ],
            "updated_at": now,
        }
    )
