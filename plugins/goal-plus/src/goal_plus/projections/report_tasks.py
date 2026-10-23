from __future__ import annotations


from typing import Any

from goal_plus.application.candidate_ranking import best_iteration_record
from goal_plus.ports.report import ReportTaskInputs
from goal_plus.projections.fork import dispatch_group_summary
from goal_plus.projections.publication import publication_summary
from goal_plus.projections.report_format import parse_epoch
from goal_plus.projections.report_statistics import pi_dispatch_usage


def _contract_changes(before: Any, after: Any, field: str) -> list[dict[str, Any]]:
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        return [
            change
            for key in sorted(before.keys() | after.keys())
            for change in _contract_changes(before.get(key), after.get(key), f"{field}.{key}")
        ]
    return [{"field": field, "before": before, "after": after}]


def _evaluation_contracts(inputs: ReportTaskInputs) -> list[dict[str, Any]]:
    contracts = []
    for index, frozen in enumerate(
        sorted(inputs.frozen_specs.values(), key=lambda item: (item.created_at, item.frozen_spec_id)),
        start=1,
    ):
        parent = inputs.frozen_specs.get(frozen.parent_frozen_spec_id)
        changes = []
        if parent is not None:
            changes = _contract_changes(
                parent.spec.model_dump(mode="json"), frozen.spec.model_dump(mode="json"), "spec"
            ) + _contract_changes(parent.verifier_hashes, frozen.verifier_hashes, "verifier_hashes")
        contracts.append({
            **frozen.model_dump(mode="json"),
            "alias": f"V{index}",
            "changes": changes,
            "usages": {
                "initial": frozen.frozen_spec_id == inputs.run.frozen_spec_id,
                "selected": frozen.frozen_spec_id == inputs.run.selected_frozen_spec_id,
                "active_candidate_ids": [c.candidate_id for c in inputs.candidates if
                    (c.active_frozen_spec_id or inputs.run.frozen_spec_id) == frozen.frozen_spec_id],
                "revision_candidate_ids": [c.candidate_id for c in inputs.candidates if
                    any(r.frozen_spec_id == frozen.frozen_spec_id for r in c.spec_revisions)],
                "iterations": [{"candidate_id": c.candidate_id, "iteration": i.iteration}
                    for c in inputs.candidates for i in c.iterations if
                    (i.frozen_spec_id or inputs.run.frozen_spec_id) == frozen.frozen_spec_id],
                "child_frozen_spec_ids": [item.frozen_spec_id for item in inputs.frozen_specs.values()
                    if item.parent_frozen_spec_id == frozen.frozen_spec_id],
            },
        })
    return contracts


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
    verifier_enabled = frozen.spec.strategy.requires_llm_verifier
    contracts = _evaluation_contracts(inputs)
    session_ids_by_candidate: dict[str, list[str]] = {}
    for session in sessions:
        session_ids_by_candidate.setdefault(session.candidate_id, []).append(
            session.agent_session_id
        )

    verifier_views = inputs.verifier_views
    records_by_candidate: dict[str, list[Any]] = {}
    if verifier_views is not None:
        for record in verifier_views.verifier_records:
            records_by_candidate.setdefault(record.subject.candidate_id, []).append(record)

    candidate_payloads: list[dict[str, Any]] = []
    for candidate in candidates:
        active = inputs.candidate_specs[candidate.candidate_id]
        best = best_iteration_record(candidate, active.spec.metric_direction)
        score_report = candidate.score_report
        if score_report is not None and (score_report.frozen_spec_id or run.frozen_spec_id) != active.frozen_spec_id:
            score_report = None
        candidate_verifier_records = records_by_candidate.get(candidate.candidate_id, [])
        latest_verifier_record = (
            candidate_verifier_records[-1] if candidate_verifier_records else None
        )
        candidate_payload = {
            "candidate_id": candidate.candidate_id,
            "frozen_spec_id": active.frozen_spec_id,
            "spec_hash": active.spec_hash,
            "active_frozen_spec_id": active.frozen_spec_id,
            "active_spec_hash": active.spec_hash,
            "metric_name": active.spec.metric_name,
            "metric_direction": active.spec.metric_direction,
            "latest_frozen_spec_id": (
                candidate.score_report.frozen_spec_id or run.frozen_spec_id
                if candidate.score_report is not None else None
            ),
            "latest_spec_hash": candidate.score_report.spec_hash if candidate.score_report else None,
            "spec_revisions": [item.model_dump(mode="json") for item in candidate.spec_revisions],
            "candidate_origin": candidate.task.candidate_origin.model_dump(mode="json") if candidate.task.candidate_origin else None,
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
                    score_report.aggregate_score
                    if score_report is not None
                    and score_report.process_passed
                    else None
                )
            ),
            "process_passed": (
                True
                if best is not None
                else (
                    score_report.process_passed
                    if score_report is not None
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
        if verifier_enabled:
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
                    "verifier_record_count": len(candidate_verifier_records),
                    "latest_verifier_record": (
                        latest_verifier_record.model_dump(mode="json")
                        if latest_verifier_record is not None
                        else None
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
            "agent_harness": session.agent_harness,
            "runtime_provider": session.runtime_provider,
            "candidate_origin": session.candidate_origin.model_dump(mode="json") if session.candidate_origin else None,
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
            "model_failure": session.session_handle.metadata.get("model_failure"),
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
        raw_dispatches = session.session_handle.metadata.get("dispatches")
        dispatches = raw_dispatches if isinstance(raw_dispatches, list) else []
        if not dispatches:
            session_payloads.append(
                {
                    **base_payload,
                    "timeline_session_id": session.agent_session_id,
                    "dispatch_index": 1,
                    "dispatch_count": 1,
                    "process_pid": session.session_handle.metadata.get("process_pid"),
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
                iteration
                for iteration in attributed_iterations
                if iteration.process_passed is True
                and iteration.score is not None
                and not iteration.touched_denied_files
                and not iteration.changed_outside_allowed
            ]
            dispatch_score = None
            score_spec_ids = {item.frozen_spec_id or run.frozen_spec_id for item in scored}
            if len(score_spec_ids) == 1:
                direction = inputs.iterations[(session.candidate_id, scored[0].iteration)]["metric_direction"]
                dispatch_score = (
                    min(item.score for item in scored)
                    if direction == "minimize"
                    else max(item.score for item in scored)
                )
            processed_tokens, cost_usd = pi_dispatch_usage(dispatch.get("usage"))
            timed_out = bool(dispatch.get("timed_out"))
            runner_failed = bool(dispatch.get("runner_failed"))
            terminal_state = dispatch.get("state") or (
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
                    "native_session_id": dispatch.get("native_session_id"),
                    "invocation_id": dispatch.get("invocation_id"),
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
                    "frozen_spec_id": next(iter(score_spec_ids)) if len(score_spec_ids) == 1 else None,
                    "score_comparable": len(score_spec_ids) <= 1,
                }
            )

    plan_payloads = [
        {
            "plan_id": plan.plan_id,
            "status": plan.status,
            "created_at": plan.created_at,
            "strategy": plan.strategy.name,
            "initial_context": plan.strategy.initial_context,
            "adaptive_search": plan.strategy.adaptive_search_enabled,
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
        "dispatch_group": dispatch_group_summary(run.dispatch_group),
        "evaluation_contracts": contracts,
        "selected_contract": next((item for item in contracts if item["usages"]["selected"]), None),
        "frozen_spec": {
            "frozen_spec_id": frozen.frozen_spec_id,
            "spec_hash": frozen.spec_hash,
            "parent_frozen_spec_id": frozen.parent_frozen_spec_id,
            "revision_reason": frozen.revision_reason,
            "dispatch_group_id": frozen.dispatch_group_id,
            "dispatch_target_index": frozen.dispatch_target_index,
            "objective": frozen.spec.objective,
            "metric_name": frozen.spec.metric_name,
            "metric_direction": frozen.spec.metric_direction,
            "workspace_provider": frozen.spec.workspace.provider,
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
        "shared_knowledge_view": (
            inputs.shared_knowledge_view.model_dump(mode="json")
            if inputs.shared_knowledge_view is not None else None
        ),
        "knowledge_outcomes": inputs.knowledge_outcomes,
        "reference_impacts": [
            {
                **impact.__dict__,
                "source_artifact_ref": impact.source_artifact_ref.model_dump(
                    mode="json"
                ),
                "subsequent_artifact_ref": (
                    impact.subsequent_artifact_ref.model_dump(mode="json")
                    if impact.subsequent_artifact_ref is not None
                    else None
                ),
                "compared_files": list(impact.compared_files),
            }
            for impact in inputs.reference_impacts
        ],
    }
    if verifier_views is not None:
        task_payload.update(
            {
                "allocation_decisions": [
                    item.model_dump(mode="json")
                    for item in verifier_views.allocation_decisions
                ],
                "verifier_tasks": [
                    task.model_dump(mode="json")
                    for task in verifier_views.verifier_tasks
                ],
                "verifier_records": [
                    item.model_dump(mode="json")
                    for item in verifier_views.verifier_records
                ],
            }
        )
    return task_payload
