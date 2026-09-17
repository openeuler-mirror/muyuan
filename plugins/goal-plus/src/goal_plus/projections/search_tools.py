"""Model-facing Search summaries; persisted research and report inputs stay complete."""

from typing import Any

from goal_plus.domain.search import CandidateTask, FrozenSpec, SearchPlan
from goal_plus.domain.promotion import PromotionRecord


def frozen_spec_receipt(frozen: FrozenSpec) -> dict[str, Any]:
    payload: dict[str, Any] = {"frozen_spec_id": frozen.frozen_spec_id, "state": "frozen"}
    baseline = frozen.origin_verification
    payload.update(
        metric_name=frozen.spec.metric_name,
        baseline={"evaluation_completed": baseline.evaluation_completed,
                  "passed": baseline.passed, "score": baseline.aggregate_score} if baseline else None,
        promotion_verifiers=[command.name for command in frozen.spec.promotion_verifiers],
    )
    capacity = frozen.workspace_capacity_estimate
    if capacity is not None and capacity.fits_available_space is False:
        payload["warnings"] = [{
            "code": "WorkspaceCapacityExceeded",
            "required_bytes": capacity.conservative_upper_bound_bytes,
            "available_bytes": capacity.available_bytes,
        }]
    return payload


def search_plan_receipt(plan: SearchPlan) -> dict[str, Any]:
    payload = plan.model_dump(mode="json", include={
        "plan_id", "status", "planned_k", "requires_agent_proposals",
    })
    if plan.work_orders:
        payload["work_orders"] = [order.model_dump(mode="json", include={
            "slot", "intent", "instructions",
        }) for order in plan.work_orders]
    if plan.selected_models:
        payload["selected_models"] = [model.model_dump(mode="json", include={
            "slot", "model", "provider", "reasoning_effort", "service_tier",
        }, exclude_none=True) for model in plan.selected_models]
    if plan.started_candidate_ids:
        payload["started_candidate_ids"] = plan.started_candidate_ids
    return payload


def candidate_task_receipt(task: CandidateTask) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "candidate_id": task.candidate_id, "workspace_provider": task.workspace_provider,
    }
    if task.selected_model is not None:
        payload["selected_model"] = task.selected_model.model_dump(mode="json", include={
            "model", "provider", "reasoning_effort", "service_tier",
        }, exclude_none=True)
    return payload


def promotion_apply_receipt(promotion: PromotionRecord) -> dict[str, Any]:
    payload = promotion.model_dump(mode="json", include={
        "promotion_id", "state", "changed_files", "applied_source_revision", "error",
    }, exclude_none=True)
    if promotion.source_repository is not None:
        payload["source_path"] = str(promotion.source_repository / promotion.source_subdir)
    return payload


def history_page(items: list[dict[str, Any]], offset: int, limit: int) -> dict[str, Any]:
    from goal_plus.projections.goal_tools import GOAL_HISTORY_MAX_LIMIT

    if offset < 0 or not 1 <= limit <= GOAL_HISTORY_MAX_LIMIT:
        raise ValueError("history offset must be nonnegative and limit must be between 1 and 100")
    page = items[offset:offset + limit]
    return {"items": page, "total": len(items),
            "next_offset": offset + len(page) if offset + len(page) < len(items) else None}


def search_history_summary(history: dict[str, Any]) -> dict[str, Any]:
    payload = {key: history[key] for key in (
        "run_id", "state", "metric_name", "metric_direction", "best_candidate_id",
        "best_score", "total_candidates", "returned_candidates", "top_n", "sort_by",
    ) if key in history}
    for key in (
        "source_run_id", "invalidated_at", "invalidation_reason",
        "invalidation_summary", "replacement_run_id",
    ):
        if history.get(key):
            payload[key] = history[key]
    if history.get("inherited_research"):
        payload["inherited_research_available"] = True

    candidates = []
    for candidate in history["candidates"]:
        row = {key: candidate[key] for key in (
            "candidate_id", "status", "intent", "score", "process_passed",
            "evidence_source", "best_iteration", "changed_files",
        ) if key in candidate}
        for key in (
            "best_git_head", "summary", "next_ideas", "blockers", "failure_classes",
            "touched_denied_files", "changed_outside_allowed",
        ):
            if candidate.get(key):
                row[key] = candidate[key]

        # A settled best can survive a failed or rolled-back latest attempt.
        if (
            candidate.get("latest_score") != candidate.get("score")
            or candidate.get("latest_process_passed") != candidate.get("process_passed")
            or candidate.get("latest_disposition") not in (None, "keep")
            or candidate.get("latest_failure_classes")
        ):
            row["latest"] = {key.removeprefix("latest_"): candidate[key] for key in (
                "latest_score", "latest_process_passed", "latest_disposition",
                "latest_failure_classes",
            ) if key in candidate}
        candidates.append(row)
    payload["candidates"] = candidates

    research = {key: history[key] for key in (
        "feature_ledger", "pitfalls", "verifier_assessments",
    ) if history.get(key)}
    if research:
        payload["research_rollup"] = research
    payload["details"] = {"tool": "goal_plus_search_list_history", "detail": True}
    return payload
