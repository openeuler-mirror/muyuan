from datetime import datetime
from typing import Any
from goal_plus.ports.monitor import MonitorCandidateInputs, MonitorSessionInputs
from goal_plus.projections.monitor import (
    best_candidate_iteration,
    project_context_usage,
    session_liveness,
    observability_cost,
    project_evidence_reads,
)


def _avg_round_seconds(iterations: list[Any]) -> float | None:
    """Mean interval between consecutive settled verifier iterations."""
    timestamps: list[float] = []
    for item in iterations:
        try:
            parsed = datetime.fromisoformat(item.created_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            return None
        timestamps.append(parsed.timestamp())
    intervals = [
        later - earlier for earlier, later in zip(timestamps, timestamps[1:])
        if later >= earlier
    ]
    if not intervals:
        return None
    return sum(intervals) / len(intervals)


def project_candidate(inputs: MonitorCandidateInputs) -> dict[str, Any]:
    candidate, candidate_sessions = inputs.candidate, inputs.sessions
    last_iteration = candidate.iterations[-1] if candidate.iterations else None
    best_iteration = best_candidate_iteration(candidate, inputs.metric_direction)
    workspace_ledger_projection, results_tsv = (
        inputs.ledger_projection,
        inputs.results_tsv,
    )
    verifier_enabled = inputs.verifier_enabled
    shared_status_counts: dict[str, int] = {}
    toolization_outcome_counts: dict[str, int] = {}
    toolization_signal_counts: dict[str, int] = {}
    toolization_exclusion_counts: dict[str, int] = {}
    toolization_advisory_counts: dict[str, int] = {}
    verifier_records = inputs.verifier_records
    latest_verifier_record = verifier_records[-1] if verifier_records else None
    for item in candidate.iterations:
        status = item.shared_tool_publish_status
        shared_status_counts[status] = shared_status_counts.get(status, 0) + 1
        decision = item.toolization_decision
        if decision is not None:
            toolization_outcome_counts[decision.outcome] = (
                toolization_outcome_counts.get(decision.outcome, 0) + 1
            )
            for signal in decision.signals:
                toolization_signal_counts[signal] = (
                    toolization_signal_counts.get(signal, 0) + 1
                )
            if decision.exclusion is not None:
                toolization_exclusion_counts[decision.exclusion] = (
                    toolization_exclusion_counts.get(decision.exclusion, 0) + 1
                )
        for advisory in item.toolization_advisories:
            toolization_advisory_counts[advisory] = (
                toolization_advisory_counts.get(advisory, 0) + 1
            )
    candidate_payload = {
        "candidate_id": candidate.candidate_id,
        "status": candidate.status,
        "plan_id": candidate.task.plan_id,
        "parent_id": candidate.task.parent_id,
        "parent_candidate_ids": candidate.task.parent_candidate_ids,
        "base_candidate_id": candidate.task.base_candidate_id,
        "agent_session_count": len(candidate_sessions),
        "process_dispatch_count": sum(
            int(session.session_handle.metadata.get("dispatch_count") or 1)
            for session in candidate_sessions
        ),
        "verifier_count": len(candidate.iterations),
        "last_score": last_iteration.score if last_iteration else None,
        "last_disposition": (last_iteration.disposition if last_iteration else None),
        "last_verifier_at": last_iteration.created_at if last_iteration else None,
        "last_git_head": last_iteration.git_head if last_iteration else None,
        "avg_round_seconds": _avg_round_seconds(candidate.iterations),
        "best_iteration": best_iteration.iteration if best_iteration else None,
        "best_iteration_score": best_iteration.score if best_iteration else None,
        "best_iteration_at": best_iteration.created_at if best_iteration else None,
        "best_iteration_git_head": best_iteration.git_head if best_iteration else None,
        "workspace_git_head_after_settlement": (
            last_iteration.workspace_git_head_after_settlement
            if last_iteration
            else None
        ),
        "changed_files": candidate.detected_changed_files,
        "touched_denied_files": candidate.touched_denied_files,
        "changed_outside_allowed": candidate.changed_outside_allowed,
        "shared_tools_published_total": sum(
            len(item.shared_tools) for item in candidate.iterations
        ),
        "shared_tool_staged_entries_last": (
            list(last_iteration.shared_tool_staged_entries) if last_iteration else []
        ),
        "shared_tool_staged_file_count_last": (
            last_iteration.shared_tool_staged_file_count if last_iteration else 0
        ),
        "shared_tool_publish_status_last": (
            last_iteration.shared_tool_publish_status if last_iteration else None
        ),
        "shared_tool_publish_status_counts": shared_status_counts,
        "toolization_decision_last": (
            last_iteration.toolization_decision.model_dump(mode="json")
            if last_iteration and last_iteration.toolization_decision
            else None
        ),
        "toolization_advisories_last": (
            list(last_iteration.toolization_advisories) if last_iteration else []
        ),
        "toolization_outcome_counts": toolization_outcome_counts,
        "toolization_signal_counts": toolization_signal_counts,
        "toolization_exclusion_counts": toolization_exclusion_counts,
        "toolization_advisory_counts": toolization_advisory_counts,
        "result_ledger_source": "gp",
        "result_count": len(candidate.results_ledger),
        "workspace_ledger_projection": workspace_ledger_projection,
        "results_tsv": results_tsv,
    }
    if verifier_enabled:
        candidate_verifier_tasks = inputs.verifier_tasks
        last_verifier_task = (
            candidate_verifier_tasks[-1] if candidate_verifier_tasks else None
        )
        candidate_payload.update(
            {
                "allocation_depth": candidate.task.allocation_depth,
                "allocation_eligibility": candidate.allocation_eligibility,
                "retired_by_decision_id": candidate.retired_by_decision_id,
                "expansion_source": (
                    candidate.task.expansion_source.model_dump(mode="json")
                    if candidate.task.expansion_source is not None
                    else None
                ),
                "latest_verifier_record": (
                    latest_verifier_record.model_dump(mode="json")
                    if latest_verifier_record is not None
                    else None
                ),
                "verifier_record_count": len(verifier_records),
                "verifier_tasks_total": len(candidate_verifier_tasks),
                "verifier_tasks_pending": sum(
                    item.state in {"pending", "retry_wait"}
                    for item in candidate_verifier_tasks
                ),
                "last_verifier_task": (
                    last_verifier_task.model_dump(mode="json")
                    if last_verifier_task is not None
                    else None
                ),
            }
        )
    return candidate_payload


def project_session(
    inputs: MonitorSessionInputs,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    session = inputs.session
    now, stale_after_seconds = inputs.now, inputs.stale_after_seconds
    main_agent = {
        "estimated_cost_total": 0.0,
        "context_tokens_max": None,
        "context_percent_max": None,
    }
    warnings, subagents = [], []
    candidate = inputs.candidate
    metadata = session.session_handle.metadata
    time_advisory = inputs.time_advisory
    observability = inputs.observation
    execution = (
        observability.get("execution")
        if isinstance(observability.get("execution"), dict)
        else {}
    )
    metrics = (
        metadata.get("pi_metrics")
        if isinstance(metadata.get("pi_metrics"), dict)
        else {}
    )
    usage_delta = metrics.get("usage_delta") if isinstance(metrics, dict) else None
    usage_total = metrics.get("usage_total") if isinstance(metrics, dict) else None
    context = (
        observability.get("context")
        if isinstance(observability.get("context"), dict)
        else project_context_usage(metrics if isinstance(metrics, dict) else {})
    )
    latest_output_mtime = inputs.latest_output_mtime
    timed_out = bool(execution.get("timed_out"))
    runner_failed = bool(execution.get("runner_failed"))
    terminal_state = str(execution.get("terminal_state") or "unknown")
    session_iterations = [
        iteration
        for iteration in (candidate.iterations if candidate else [])
        if iteration.agent_session_id == session.agent_session_id
    ]
    raw_dispatches = metadata.get("dispatches")
    dispatches = raw_dispatches if isinstance(raw_dispatches, list) else []
    dispatch_count = int(metadata.get("dispatch_count") or len(dispatches) or 1)
    liveness = session_liveness(
        candidate,
        latest_output_mtime=latest_output_mtime,
        now=now,
        stale_after_seconds=stale_after_seconds,
        timed_out=timed_out,
        runner_failed=runner_failed,
        terminal_state=terminal_state,
    )
    main_agent["estimated_cost_total"] = float(
        main_agent["estimated_cost_total"]
    ) + observability_cost(observability)
    if isinstance(context.get("tokens"), int | float):
        current = main_agent["context_tokens_max"]
        main_agent["context_tokens_max"] = max(current or 0, context["tokens"])  # type: ignore[arg-type]
    if isinstance(context.get("percent"), int | float):
        current_percent = main_agent["context_percent_max"]
        main_agent["context_percent_max"] = max(
            current_percent or 0.0, context["percent"]
        )  # type: ignore[arg-type]
    if timed_out:
        warnings.append(
            {
                "kind": "subagent_timed_out",
                "agent_session_id": session.agent_session_id,
                "candidate_id": session.candidate_id,
            }
        )
    if runner_failed:
        warnings.append(
            {
                "kind": "subagent_runner_failed",
                "agent_session_id": session.agent_session_id,
                "candidate_id": session.candidate_id,
                "failure_stage": metadata.get("failure_stage"),
                "error_type": metadata.get("error_type"),
                "error": metadata.get("error"),
            }
        )
    if liveness == "stale":
        warnings.append(
            {
                "kind": "subagent_stale",
                "agent_session_id": session.agent_session_id,
                "candidate_id": session.candidate_id,
            }
        )
    subagents.append(
        {
            "agent_session_id": session.agent_session_id,
            "candidate_id": session.candidate_id,
            "agent_harness": session.agent_harness,
            "runtime_provider": session.runtime_provider,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "attempt_count": dispatch_count,
            "dispatch_count": dispatch_count,
            "dispatches": dispatches,
            "verifier_count": len(session_iterations),
            "session_verifier_count": len(session_iterations),
            "candidate_verifier_count": len(candidate.iterations) if candidate else 0,
            "last_score": candidate.iterations[-1].score
            if candidate and candidate.iterations
            else None,
            "last_verifier_at": (
                candidate.iterations[-1].created_at
                if candidate and candidate.iterations
                else None
            ),
            "duration_seconds": execution.get("duration_seconds"),
            "usage_delta": usage_delta if isinstance(usage_delta, dict) else None,
            "usage_total": usage_total if isinstance(usage_total, dict) else None,
            "context": context,
            "event_log": inputs.logs["event_log"],
            "text_log": inputs.logs["text_log"],
            "session_file": inputs.logs["session_file"],
            "timed_out": timed_out,
            "runner_failed": runner_failed,
            "progress_handoff": (
                metadata.get("progress_handoff")
                if isinstance(metadata.get("progress_handoff"), dict)
                else None
            ),
            "failure_stage": metadata.get("failure_stage"),
            "error_type": metadata.get("error_type"),
            "error": metadata.get("error"),
            "time_advisory_sent": time_advisory is not None,
            "time_advisory": time_advisory,
            "raw_logging": bool(metadata.get("raw_logging")),
            "evidence_reads": project_evidence_reads([session]),
            "liveness": liveness,
            "observability": observability,
        }
    )
    return subagents[0], main_agent, warnings
