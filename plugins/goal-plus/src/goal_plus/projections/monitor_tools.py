"""Compact model-facing monitor view; dashboard inputs retain their full schema."""

from typing import Any

from goal_plus.projections.goal_tools import reconciliation_summary


def _fields(value: dict[str, Any] | None, names: tuple[str, ...]) -> dict[str, Any] | None:
    return {key: value[key] for key in names if value.get(key) is not None} if value is not None else None


def monitor_tool_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    if snapshot.get("ok") is False:
        return snapshot
    payload = {key: snapshot[key] for key in (
        "ok", "schema_version", "snapshot_at", "warnings",
    ) if key in snapshot}
    payload["goal_plus"] = _fields(snapshot.get("goal_plus"), (
        "goal_plus_id", "goal_revision", "status", "control", "phase", "next_action",
    ))
    payload["run"] = _fields(snapshot.get("run"), (
        "run_id", "state", "candidates_total", "candidates_evaluated",
        "best_candidate_id", "best_score", "selected_candidate_id", "selected_score",
        "selected_iteration",
    ))
    if payload["run"] is not None:
        run = snapshot["run"]
        payload["run"]["host_cleanup"] = _fields(run.get("host_cleanup"), (
            "state", "error", "errors", "cleanup_errors", "detail",
        ))
        payload["run"]["llm_verifier"] = _fields(run.get("llm_verifier"), (
            "tasks_total", "task_states", "records_total",
        ))
        payload["run"]["allocation_decisions"] = _fields(run.get("allocation_decisions"), (
            "total", "pending", "interrupted", "applied", "superseded",
            "terminal_error", "expansions",
        ))
        payload["run"]["promotion"] = _fields(run.get("promotion"), (
            "state", "promotion_mode", "error",
        ))
        outcome = run.get("publication_outcome")
        if outcome and outcome.get("error"):
            payload["run"]["publication_error"] = outcome["error"]
        annotations = run.get("evidence_annotations")
        payload["run"]["evidence_annotations"] = _fields(annotations, (
            "tasks", "attempts", "views_published", "states",
        ))
        if annotations:
            payload["run"]["evidence_annotations"]["active_attempts"] = [
                _fields(attempt, ("candidate_id", "iteration", "attempt", "state",
                                  "elapsed_seconds", "timeout_seconds", "detail"))
                for attempt in annotations.get("active_attempts", [])
            ]
        payload["run"] = {key: value for key, value in payload["run"].items() if value is not None}
    scope = _fields(snapshot.get("execution_scope"), ("state", "active_count", "cleanup_errors"))
    if scope is not None:
        # Unknown resource counts must not be presented as zero.
        scope["active_count"] = snapshot["execution_scope"].get("active_count")
        payload["execution_scope"] = scope
    payload["reconciliation"] = (reconciliation_summary(snapshot["reconciliation"])
                                     if snapshot.get("reconciliation") else None)
    payload["candidates"] = {
        candidate_id: _fields(candidate, (
            "status", "touched_denied_files", "changed_outside_allowed",
        )) for candidate_id, candidate in snapshot.get("candidates", {}).items()
        if candidate.get("touched_denied_files") or candidate.get("changed_outside_allowed")
    }
    payload["subagents"] = [_fields(agent, (
        "agent_session_id", "candidate_id", "timed_out", "runner_failed", "failure_stage", "error_type", "error", "model_failure",
    )) for agent in snapshot.get("subagents", [])
        if agent.get("timed_out") or agent.get("runner_failed") or agent.get("error")]
    orchestration = snapshot.get("feature_plugins", {}).get("orchestration")
    if orchestration:
        dispatches = [_fields(item, ("work_item_id", "goal_revision", "status",
                "worker", "attempt"))
                for item in orchestration.get("dispatches", [])
                if item.get("status") not in {"accepted", "cancelled", "superseded"}]
        if dispatches:
            payload["feature_plugins"] = {"orchestration": {"dispatches": dispatches}}
    payload = {key: value for key, value in payload.items() if value not in (None, [], {})}
    payload["details"] = {"tool": "goal_plus_monitor_snapshot", "detail": True}
    return payload
