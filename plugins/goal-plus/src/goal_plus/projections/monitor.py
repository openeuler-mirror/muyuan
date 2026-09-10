from __future__ import annotations


from datetime import datetime


from typing import Any


from goal_plus.domain.search import (
    AgentSessionRecord,
    FrozenSpec,
    SearchPlan,
)

from goal_plus.domain.artifacts import ArtifactRef

from goal_plus.ports.state import (
    CandidateRecord,
    ProviderMutationRecord,
    RunRecord,
)


from goal_plus.domain.goal import (
    GoalPlusLinkedSearch,
    GoalPlusRecord,
)

from goal_plus.domain.evidence import IterationRecord


from goal_plus.application.artifact_availability import artifact_availability


from goal_plus.projections.statistics import (
    aggregate_run_statistics,
    aggregate_usage,
)


def parse_utc_timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def numeric_value(value: Any) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def project_evidence_reads(
    sessions: list[AgentSessionRecord],
) -> dict[str, Any]:
    global_reads = [
        read for session in sessions for read in session.global_evidence_reads
    ]
    detail_reads = [
        read
        for session in sessions
        for read in getattr(session, "evidence_detail_reads", [])
    ]
    return {
        "global_total": len(global_reads),
        "global_explicit": sum(
            getattr(read, "source", "explicit") == "explicit" for read in global_reads
        ),
        "global_verifier_injected": sum(
            getattr(read, "source", "explicit") == "verifier_injected"
            for read in global_reads
        ),
        "detail_total": len(detail_reads),
        "sessions_with_global_reads": sum(
            bool(session.global_evidence_reads) for session in sessions
        ),
        "sessions_with_detail_reads": sum(
            bool(getattr(session, "evidence_detail_reads", [])) for session in sessions
        ),
        "latest_global_read_at": max(
            (read.read_at for read in global_reads), default=None
        ),
        "latest_detail_read_at": max(
            (read.read_at for read in detail_reads), default=None
        ),
    }


def metric_context(
    frozen: FrozenSpec,
    task: GoalPlusLinkedSearch | None,
    goal_metric_contexts: dict[int, dict[str, Any]],
) -> tuple[float | None, float | None]:
    draft = goal_metric_contexts.get(task.goal_revision) if task is not None else None
    baseline = None
    target = None
    if isinstance(draft, dict):
        baseline_payload = draft.get("baseline")
        metric_payload = draft.get("metric")
        correctness = draft.get("correctness_gate")
        if isinstance(baseline_payload, dict):
            baseline = numeric_value(baseline_payload.get(frozen.spec.metric_name))
        if isinstance(metric_payload, dict):
            target = numeric_value(metric_payload.get("target"))
        if target is None and isinstance(correctness, dict):
            target = numeric_value(correctness.get("score_threshold"))
    if target is None:
        target = numeric_value(frozen.spec.constraints.get("success_threshold"))
    return baseline, target


def project_strategy(
    frozen: FrozenSpec,
    latest_plan: SearchPlan | None,
) -> dict[str, Any]:
    strategy = frozen.spec.strategy
    latest_plan_payload: dict[str, Any] | None = None
    if latest_plan is not None:
        trace = latest_plan.strategy_trace
        latest_plan_payload = {
            "plan_id": latest_plan.plan_id,
            "status": latest_plan.status,
            "requested_k": latest_plan.requested_k,
            "planned_k": latest_plan.planned_k,
            "started_candidate_ids": latest_plan.started_candidate_ids,
            "selection_rule": trace.get("selection_rule"),
            "state": {key: trace[key] for key in _STRATEGY_STATE_KEYS if key in trace},
        }
    return {
        "name": strategy.name,
        "orchestration_mode": strategy.orchestration_mode,
        "native_host": frozen.native_host,
        "worker_host": frozen.worker_host,
        "max_parallel": frozen.spec.budget.max_parallel,
        "workspace_backend": frozen.spec.workspace.backend,
        "promotion_mode": frozen.spec.workspace.promotion_mode,
        "worker_budget": strategy.worker_budget.model_dump(mode="json") if strategy.worker_budget else None,
        "worker_launch": strategy.worker_launch.model_dump(mode="json", exclude_none=True)
        if strategy.worker_launch else None,
        "models": [model.model_dump(mode="json", exclude_none=True) for model in strategy.models],
        "inner_agent": strategy.inner_agent
        or ("common" if frozen.worker_host == "thinkthread" else "autoresearch"),
        "legacy_worker_agent_type": strategy.worker_agent_type,
        "latest_plan": latest_plan_payload,
    }


def best_candidate_iteration(
    candidate: CandidateRecord,
    metric_direction: str,
) -> IterationRecord | None:
    scored = [
        iteration
        for iteration in candidate.iterations
        if iteration.process_passed is True
        and iteration.score is not None
        and not iteration.touched_denied_files
        and not iteration.changed_outside_allowed
    ]
    if not scored:
        return None
    reverse = metric_direction == "maximize"
    return sorted(scored, key=lambda iteration: iteration.score, reverse=reverse)[0]


def project_goal(record: GoalPlusRecord | None) -> dict[str, Any] | None:
    if record is None:
        return None
    linked = (
        record.linked_search.model_dump(mode="json") if record.linked_search else None
    )
    next_action = (
        record.next_action.model_dump(mode="json") if record.next_action else None
    )
    latest_final_check = (
        record.final_checks[-1].model_dump(mode="json") if record.final_checks else None
    )
    return {
        "goal_plus_id": record.goal_plus_id,
        "status": record.status,
        "control": record.control.model_dump(mode="json"),
        "phase": record.phase,
        "raw_goal": record.raw_goal,
        "goal_revision": record.goal_revision,
        "goal_revisions_total": len(record.goal_revisions),
        "final_check_policy": record.policy.get("final_check", {"mode": "disabled"}),
        "latest_final_check": latest_final_check,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "search_tasks_total": len(record.search_tasks),
        "current_search_run_id": linked.get("run_id") if linked else None,
        "linked_search": linked,
        "next_action": next_action,
        "hook_counters": record.hook_counters,
    }


def observability_cost(observability: dict[str, Any]) -> float:
    usage = observability.get("usage")
    value = usage.get("cost_usd") if isinstance(usage, dict) else None
    return float(value) if isinstance(value, int | float) else 0.0


def project_context_usage(metrics: dict[str, Any]) -> dict[str, Any]:
    session_stats = metrics.get("session_stats")
    context = (
        session_stats.get("contextUsage") if isinstance(session_stats, dict) else None
    )
    if isinstance(context, dict):
        return {
            "tokens": context.get("tokens"),
            "context_window": context.get("contextWindow"),
            "percent": context.get("percent"),
            "source": "pi_session_stats",
        }
    tokens = session_stats.get("tokens") if isinstance(session_stats, dict) else None
    if isinstance(tokens, dict):
        return {
            "tokens": tokens.get("total"),
            "context_window": None,
            "percent": None,
            "source": "pi_session_tokens_total",
        }
    return {
        "tokens": None,
        "context_window": None,
        "percent": None,
        "source": "unknown",
    }


def session_liveness(
    candidate: CandidateRecord | None,
    *,
    latest_output_mtime: float | None,
    now: float,
    stale_after_seconds: int,
    timed_out: bool,
    runner_failed: bool,
    terminal_state: str = "unknown",
) -> str:
    if candidate and candidate.status == "failed":
        return "failed"
    if runner_failed:
        return "failed"
    if candidate and candidate.status == "evaluated":
        return "evaluated"
    if timed_out:
        return "timed_out"
    if terminal_state in {"completed", "interrupted"}:
        return terminal_state
    if (
        latest_output_mtime is not None
        and now - latest_output_mtime > stale_after_seconds
    ):
        return "stale"
    return "running_or_waiting"


def project_artifact_availability(run: RunRecord) -> list[dict[str, Any]]:
    payload = []
    for item in run.provider_cleanup:
        reference = ArtifactRef(
            kind=item.resource_kind,
            id=item.resource_id,
            provider=item.provider,
        )
        payload.append(
            {
                "artifact_ref": reference.model_dump(mode="json"),
                "artifact_availability": artifact_availability(
                    reference,
                    run.provider_cleanup,
                ),
            }
        )
    return payload


def state_counts(states: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for state in states:
        counts[state] = counts.get(state, 0) + 1
    return dict(sorted(counts.items()))


def snapshot_id_from_mutation(mutation: ProviderMutationRecord) -> str | None:
    if mutation.state not in {"succeeded", "closed"}:
        return None
    if mutation.operation == "baseline_snapshot":
        if mutation.result.get("ownerThinkthreadId") != mutation.identity.get(
            "root_thinkthread_id"
        ):
            return None
        value = mutation.result.get("snapshotId")
    elif mutation.operation == "branch_snapshot":
        if mutation.result.get("createdFromBranchId") != mutation.identity.get(
            "branch_id"
        ):
            return None
        value = mutation.result.get("snapshotId")
    elif mutation.operation == "publication_snapshot":
        snapshot = mutation.result.get("snapshot")
        if not isinstance(snapshot, dict) or snapshot.get(
            "ownerThinkthreadId"
        ) != mutation.identity.get("root_thinkthread_id"):
            return None
        value = snapshot.get("snapshotId")
    else:
        return None
    return value if isinstance(value, str) and value.startswith("fsnap-") else None


def aggregate_search_tasks(search_tasks: list[dict[str, Any]]) -> dict[str, Any]:
    read_payloads = [
        task["evidence_reads"]
        for task in search_tasks
        if isinstance(task.get("evidence_reads"), dict)
    ]
    annotation_payloads = [
        task["evidence_annotations"]
        for task in search_tasks
        if isinstance(task.get("evidence_annotations"), dict)
    ]
    annotation_usages = [
        item["usage"]
        for item in annotation_payloads
        if isinstance(item.get("usage"), dict)
        and int(item["usage"].get("sources_total") or 0) > 0
    ]
    return {
        "search_tasks_total": len(search_tasks),
        "planning_rounds_total": sum(
            task["planning_rounds_total"] for task in search_tasks
        ),
        "started_rounds_total": sum(
            task["started_rounds_total"] for task in search_tasks
        ),
        "candidates_total": sum(task["candidates_total"] for task in search_tasks),
        "candidates_evaluated": sum(
            task["candidates_evaluated"] for task in search_tasks
        ),
        "worker_sessions_total": sum(
            task["worker_sessions_total"] for task in search_tasks
        ),
        "verifier_runs_total": sum(
            task["verifier_runs_total"] for task in search_tasks
        ),
        "evidence_reads": {
            "global_total": sum(item["global_total"] for item in read_payloads),
            "global_explicit": sum(item["global_explicit"] for item in read_payloads),
            "global_verifier_injected": sum(
                item["global_verifier_injected"] for item in read_payloads
            ),
            "detail_total": sum(item["detail_total"] for item in read_payloads),
            "sessions_with_global_reads": sum(
                item["sessions_with_global_reads"] for item in read_payloads
            ),
            "sessions_with_detail_reads": sum(
                item["sessions_with_detail_reads"] for item in read_payloads
            ),
            "latest_global_read_at": max(
                (
                    item["latest_global_read_at"]
                    for item in read_payloads
                    if item["latest_global_read_at"] is not None
                ),
                default=None,
            ),
            "latest_detail_read_at": max(
                (
                    item["latest_detail_read_at"]
                    for item in read_payloads
                    if item["latest_detail_read_at"] is not None
                ),
                default=None,
            ),
        },
        "estimated_cost_total": sum(
            task["estimated_cost_total"] for task in search_tasks
        ),
        "annotator_usage": aggregate_usage(
            annotation_usages,
            scope="evidence_annotator",
        ),
        "statistics": aggregate_run_statistics(
            [task.get("statistics") for task in search_tasks]
        ),
    }


_STRATEGY_STATE_KEYS = (
    "generation",
    "generation_index",
    "population",
    "population_size",
    "tree_depth",
    "max_tree_depth",
    "frontier_node",
    "sampling_mode",
    "parent_candidate_id",
    "archive_candidate_ids",
    "inspiration_candidate_ids",
    "seed",
    "external_ref",
)
