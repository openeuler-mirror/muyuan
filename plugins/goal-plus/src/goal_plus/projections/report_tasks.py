from __future__ import annotations


from typing import Any

from goal_plus.ports.report import ReportTaskInputs
from goal_plus.projections.publication import publication_summary
from goal_plus.projections.report_format import parse_epoch
from goal_plus.projections.report_statistics import pi_dispatch_usage


def task_details(
    inputs: ReportTaskInputs, task_summary: dict[str, Any], report_run_id: str
) -> dict[str, Any]:
    run = inputs.run
    run_id = run.run_id
    frozen = inputs.frozen
    plans = inputs.plans
    candidates = inputs.candidates
    sessions = inputs.sessions
    observations = inputs.observations
    search_scheduler_enabled = frozen.spec.strategy.search_scheduler is not None
    session_ids_by_candidate: dict[str, list[str]] = {}
    for session in sessions:
        session_ids_by_candidate.setdefault(session.candidate_id, []).append(
            session.agent_session_id
        )

    scheduler_views = inputs.scheduler_views
    rewards_by_candidate: dict[str, list[Any]] = {}
    if scheduler_views is not None:
        for reward in scheduler_views.reward_records:
            rewards_by_candidate.setdefault(reward.candidate_id, []).append(reward)

    candidate_payloads: list[dict[str, Any]] = []
    for candidate in candidates:
        scored = [
            iteration
            for iteration in candidate.iterations
            if iteration.process_passed is True
            and iteration.score is not None
            and not iteration.touched_denied_files
            and not iteration.changed_outside_allowed
        ]
        best = None
        if scored:
            reverse = frozen.spec.metric_direction == "maximize"
            best = sorted(scored, key=lambda item: item.score, reverse=reverse)[0]
        candidate_rewards = rewards_by_candidate.get(candidate.candidate_id, [])
        latest_reward = candidate_rewards[-1] if candidate_rewards else None
        candidate_payload = {
            "candidate_id": candidate.candidate_id,
            "status": candidate.status,
            "plan_id": candidate.task.plan_id,
            "parent_id": candidate.task.parent_id,
            "parent_candidate_ids": candidate.task.parent_candidate_ids,
            "base_candidate_id": candidate.task.base_candidate_id,
            "hypothesis": candidate.task.hypothesis,
            "selected_model": (
                candidate.task.selected_model.model
                if candidate.task.selected_model
                else None
            ),
            "model_provenance": candidate.task.model_provenance,
            "selected": candidate.candidate_id == run.selected_candidate_id,
            "score": (
                best.score
                if best is not None
                else (
                    candidate.score_report.aggregate_score
                    if candidate.score_report is not None
                    and candidate.score_report.process_passed
                    else None
                )
            ),
            "process_passed": (
                True
                if best is not None
                else (
                    candidate.score_report.process_passed
                    if candidate.score_report is not None
                    else None
                )
            ),
            "best_iteration": best.iteration if best is not None else None,
            "best_score": best.score if best is not None else None,
            "iterations_total": len(candidate.iterations),
            "session_ids": session_ids_by_candidate.get(candidate.candidate_id, []),
            "changed_files": candidate.detected_changed_files,
            "promotion_passed": (
                candidate.promotion_report.promotion_passed
                if candidate.promotion_report is not None
                else None
            ),
            "promotion_evidence_at": (
                candidate.promotion_evidence.created_at
                if candidate.promotion_evidence is not None
                else None
            ),
            "iterations": [
                inputs.iterations[(candidate.candidate_id, iteration.iteration)]
                for iteration in candidate.iterations
            ],
        }
        if search_scheduler_enabled:
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
                    "scheduler_reward_count": len(candidate_rewards),
                    "latest_scheduler_reward": (
                        latest_reward.reward.model_dump(mode="json")
                        if latest_reward is not None
                        else None
                    ),
                    "schedule_settlement_watermark": (
                        candidate.schedule_settlement_watermark
                    ),
                }
            )
        candidate_payloads.append(candidate_payload)

    candidate_iterations_by_id = {
        candidate.candidate_id: list(candidate.iterations) for candidate in candidates
    }
    session_payloads: list[dict[str, Any]] = []
    for session in sessions:
        observation = observations[session.agent_session_id]
        execution = observation.get("execution")
        execution = execution if isinstance(execution, dict) else {}
        usage = observation.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        context = observation.get("context")
        context = context if isinstance(context, dict) else {}
        activity = observation.get("activity")
        activity = activity if isinstance(activity, dict) else None
        session_iterations = [
            iteration
            for candidate in candidates
            for iteration in candidate.iterations
            if iteration.agent_session_id == session.agent_session_id
        ]
        global_evidence_reads = [
            read.model_dump(mode="json") for read in session.global_evidence_reads
        ]
        evidence_detail_reads = [
            read.model_dump(mode="json")
            for read in getattr(session, "evidence_detail_reads", [])
        ]
        base_payload = {
            "agent_session_id": session.agent_session_id,
            "candidate_id": session.candidate_id,
            "selected_model": (
                session.selected_model.model if session.selected_model else None
            ),
            "model_provenance": session.model_provenance,
            "host": session.host,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "provider": execution.get("provider"),
            "model": execution.get("model"),
            "terminal_state": execution.get("terminal_state"),
            "started_at": execution.get("started_at") or session.created_at,
            "ended_at": execution.get("ended_at"),
            "duration_seconds": execution.get("duration_seconds"),
            "timed_out": bool(execution.get("timed_out")),
            "runner_failed": bool(execution.get("runner_failed")),
            "processed_tokens": usage.get("processed_tokens"),
            "cost_usd": usage.get("cost_usd"),
            "tool_calls": usage.get("tool_calls"),
            "context_tokens": context.get("tokens"),
            "context_percent": context.get("percent"),
            "verifier_runs": len(session_iterations),
            "global_evidence_reads": global_evidence_reads,
            "global_evidence_reads_total": len(global_evidence_reads),
            "global_evidence_reads_explicit": sum(
                read.get("source", "explicit") == "explicit"
                for read in global_evidence_reads
            ),
            "global_evidence_reads_verifier_injected": sum(
                read.get("source") == "verifier_injected"
                for read in global_evidence_reads
            ),
            "latest_global_evidence_read_at": (
                max(read["read_at"] for read in global_evidence_reads)
                if global_evidence_reads
                else None
            ),
            "evidence_detail_reads": evidence_detail_reads,
            "evidence_detail_reads_total": len(evidence_detail_reads),
            "latest_evidence_detail_read_at": (
                max(read["read_at"] for read in evidence_detail_reads)
                if evidence_detail_reads
                else None
            ),
            "observability_source": observation.get("source"),
            "activity": activity,
            "errors": observation.get("errors") or [],
        }
        raw_dispatches = session.host_handle.metadata.get("dispatches")
        dispatches = raw_dispatches if isinstance(raw_dispatches, list) else []
        if not dispatches:
            session_payloads.append(
                {
                    **base_payload,
                    "timeline_session_id": session.agent_session_id,
                    "dispatch_index": 1,
                    "dispatch_count": 1,
                    "process_pid": session.host_handle.metadata.get("process_pid"),
                }
            )
            continue

        candidate_iterations = candidate_iterations_by_id.get(session.candidate_id, [])
        for index, raw_dispatch in enumerate(dispatches, start=1):
            dispatch = raw_dispatch if isinstance(raw_dispatch, dict) else {}
            start_at = dispatch.get("started_at")
            end_at = dispatch.get("ended_at")
            start_epoch = parse_epoch(start_at)
            end_epoch = parse_epoch(end_at)
            next_start_epoch = (
                parse_epoch(dispatches[index].get("started_at"))
                if index < len(dispatches) and isinstance(dispatches[index], dict)
                else None
            )
            score_end_epoch = next_start_epoch
            if score_end_epoch is None and end_epoch is not None:
                score_end_epoch = end_epoch + 5.0
            attributed_iterations = []
            for iteration in candidate_iterations:
                if iteration.agent_session_id not in {
                    None,
                    session.agent_session_id,
                }:
                    continue
                iteration_epoch = parse_epoch(iteration.created_at)
                if iteration_epoch is None:
                    continue
                if start_epoch is not None and iteration_epoch < start_epoch - 1.0:
                    continue
                if score_end_epoch is not None and iteration_epoch > score_end_epoch:
                    continue
                attributed_iterations.append(iteration)
            scored = [
                iteration.score
                for iteration in attributed_iterations
                if iteration.process_passed is True
                and iteration.score is not None
                and not iteration.touched_denied_files
                and not iteration.changed_outside_allowed
            ]
            dispatch_score = None
            if scored:
                dispatch_score = (
                    min(scored)
                    if frozen.spec.metric_direction == "minimize"
                    else max(scored)
                )
            processed_tokens, cost_usd = pi_dispatch_usage(dispatch.get("usage"))
            timed_out = bool(dispatch.get("timed_out"))
            runner_failed = bool(dispatch.get("runner_failed"))
            terminal_state = (
                "failed" if runner_failed else "timed_out" if timed_out else "completed"
            )
            session_payloads.append(
                {
                    **base_payload,
                    "timeline_session_id": (
                        f"{session.agent_session_id} / dispatch {index}"
                    ),
                    "dispatch_index": index,
                    "dispatch_count": len(dispatches),
                    "process_pid": dispatch.get("process_pid"),
                    "terminal_state": terminal_state,
                    "started_at": start_at or base_payload["started_at"],
                    "ended_at": end_at or base_payload["ended_at"],
                    "duration_seconds": dispatch.get("duration_seconds"),
                    "timed_out": timed_out,
                    "runner_failed": runner_failed,
                    "processed_tokens": processed_tokens,
                    "cost_usd": cost_usd,
                    "tool_calls": None,
                    "verifier_runs": sum(
                        iteration.agent_session_id == session.agent_session_id
                        for iteration in attributed_iterations
                    ),
                    "score": dispatch_score,
                }
            )

    plan_payloads = [
        {
            "plan_id": plan.plan_id,
            "status": plan.status,
            "created_at": plan.created_at,
            "strategy": plan.strategy.name,
            "orchestration_mode": plan.strategy.orchestration_mode,
            "requested_k": plan.requested_k,
            "planned_k": plan.planned_k,
            "remaining_budget": plan.remaining_budget,
            "started_candidate_ids": plan.started_candidate_ids,
            "work_orders_total": len(plan.work_orders),
            "trace": (
                plan.strategy_trace.get("reason")
                or plan.strategy_trace.get("selection_rule")
                or plan.strategy_trace
            ),
        }
        for plan in plans
    ]
    task_payload = {
        **task_summary,
        "is_report_run": run_id == report_run_id,
        "run": {**run.model_dump(mode="json"), **publication_summary(run)},
        "strategy": frozen.spec.strategy.model_dump(mode="json", exclude_none=True),
        "frozen_spec": {
            "frozen_spec_id": frozen.frozen_spec_id,
            "spec_hash": frozen.spec_hash,
            "objective": frozen.spec.objective,
            "metric_name": frozen.spec.metric_name,
            "metric_direction": frozen.spec.metric_direction,
            "workspace_backend": frozen.spec.workspace.backend,
            "source_artifact_ref": (
                frozen.source_artifact_ref.model_dump(mode="json")
                if frozen.source_artifact_ref is not None
                else None
            ),
            "origin_verification": (
                frozen.origin_verification.model_dump(mode="json")
                if frozen.origin_verification is not None
                else None
            ),
            "strategy": frozen.spec.strategy.model_dump(mode="json", exclude_none=True),
            "budget": frozen.spec.budget.model_dump(mode="json", exclude_none=True),
        },
        "plans": plan_payloads,
        "candidates": candidate_payloads,
        "sessions": session_payloads,
    }
    if scheduler_views is not None:
        task_payload.update(
            {
                "allocation_decisions": [
                    item.model_dump(mode="json")
                    for item in scheduler_views.allocation_decisions
                ],
                "schedule_tasks": [
                    task.model_dump(mode="json")
                    for task in scheduler_views.schedule_tasks
                ],
                "scheduler_rewards": [
                    item.model_dump(mode="json")
                    for item in scheduler_views.reward_records
                ],
            }
        )
    return task_payload
