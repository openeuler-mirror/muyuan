"""Compact model-facing monitor view; dashboard inputs retain their full schema."""

from typing import Any

from goal_plus.projections.goal_tools import reconciliation_summary


def _fields(value: dict[str, Any] | None, names: tuple[str, ...]) -> dict[str, Any] | None:
    return {key: value[key] for key in names if key in value} if value is not None else None


def monitor_tool_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    if snapshot.get("ok") is False:
        return snapshot
    payload = {key: snapshot[key] for key in (
        "ok", "schema_version", "goal_plus_version", "snapshot_at", "selected_run_id", "execution_scope", "warnings",
    ) if key in snapshot}
    payload["goal_plus"] = _fields(snapshot.get("goal_plus"), (
        "goal_plus_id", "goal_revision", "status", "control", "phase", "next_action",
        "final_check_policy", "search_tasks_total",
    ))
    payload["run"] = _fields(snapshot.get("run"), (
        "run_id", "state", "frozen_spec_id", "candidates_total", "candidates_evaluated",
        "best_candidate_id", "best_score", "selected_candidate_id", "selected_score",
        "selected_iteration", "selected_git_head", "budget_used", "publication_outcome",
    ))
    if payload["run"] is not None:
        run = snapshot["run"]
        payload["run"]["host_cleanup"] = _fields(run.get("host_cleanup"), (
            "state", "error", "errors", "cleanup_errors", "detail",
        ))
        payload["run"]["scheduler"] = _fields(run.get("scheduler"), (
            "tasks", "attempts", "states", "pending", "consumed", "oldest_pending_age_seconds",
            "allocation_confirmations", "reschedule_flags",
        ))
        payload["run"]["allocation_decisions"] = _fields(run.get("allocation_decisions"), (
            "total", "pending", "applied", "reserved_replacements",
        ))
        payload["run"]["promotion"] = _fields(run.get("promotion"), (
            "state", "promotion_mode", "candidate_id", "selected_iteration",
            "selected_artifact_ref", "publication_artifact_ref", "artifact_path", "applied_at", "error",
        ))
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
    payload["strategy"] = _fields(snapshot.get("strategy"), (
        "name", "orchestration_mode", "native_host", "worker_host", "inner_agent",
        "max_parallel", "workspace_backend", "promotion_mode", "worker_budget", "worker_launch", "models",
    ))
    payload["reconciliation"] = (reconciliation_summary(snapshot["reconciliation"])
                                     if snapshot.get("reconciliation") else None)
    payload["candidates"] = {
        candidate_id: _fields(candidate, (
            "candidate_id", "status", "best_iteration_score", "best_iteration", "last_score",
            "last_disposition", "verifier_count", "touched_denied_files", "changed_outside_allowed",
        )) for candidate_id, candidate in snapshot.get("candidates", {}).items()
    }
    payload["subagents"] = [_fields(agent, (
        "agent_session_id", "candidate_id", "host", "verifier_count", "last_score",
        "last_verifier_at", "timed_out", "runner_failed", "failure_stage", "error_type", "error",
    )) for agent in snapshot.get("subagents", [])]
    orchestration = snapshot.get("feature_plugins", {}).get("orchestration")
    if orchestration:
        payload["feature_plugins"] = {"orchestration": {
            "summary": orchestration.get("summary"),
            "dispatches": [_fields(item, ("work_item_id", "goal_revision", "status",
                "worker", "attempt"))
                for item in orchestration.get("dispatches", [])
                if item.get("status") not in {"accepted", "cancelled", "superseded"}],
        }}
    return payload
