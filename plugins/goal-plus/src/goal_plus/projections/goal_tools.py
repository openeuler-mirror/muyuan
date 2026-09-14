"""Model-facing Goal replies, independent of durable record serialization."""

from typing import Any

from goal_plus.domain.goal import GoalPlusLinkedSearch, GoalPlusRecord, GoalPlusWorkItem
from goal_plus.domain.goal_closeout import final_check_mode

# Interactive history reads have a small default and an explicit per-call item ceiling.
GOAL_HISTORY_DEFAULT_LIMIT = 20
GOAL_HISTORY_MAX_LIMIT = 100


def goal_receipt(record: GoalPlusRecord) -> dict[str, Any]:
    return record.model_dump(mode="json", include={
        "goal_plus_id", "goal_revision", "status", "control", "phase", "next_action",
    })


def work_item_summary(item: GoalPlusWorkItem) -> dict[str, Any]:
    return item.model_dump(mode="json", include={
        "work_item_id", "goal_revision", "status", "attempt_id", "generation",
        "launch_deadline", "host", "task_name", "agent_id", "result_summary",
        "result_digest",
    }, exclude_none=True)


def reconciliation_summary(value: dict[str, Any]) -> dict[str, Any]:
    runs = []
    for run in value["runs"]:
        item = {key: run[key] for key in ("run_id", "state", "next_step", "error") if key in run}
        if "candidates" in run:
            item["candidates"] = [
                {
                    **{key: candidate[key] for key in (
                        "candidate_id", "generation", "next_step", "checkpoint_error",
                        "workspace_inspection_error",
                    ) if candidate.get(key) is not None},
                    **({"recovery_id": candidate["recovery"]["recovery_id"]}
                       if candidate.get("recovery") else {}),
                }
                for candidate in run["candidates"]
            ]
        runs.append(item)
    return {"resume_eligible": value.get("resume_eligible"), "runs": runs}


def search_task_receipt(task: GoalPlusLinkedSearch) -> dict[str, Any]:
    return task.model_dump(mode="json", include={
        "run_id", "selected_candidate_id", "result_recorded_at",
    }, exclude_none=True)


def goal_status(record: GoalPlusRecord, *, detail: bool = False) -> dict[str, Any]:
    current_search = record.linked_search
    if current_search is not None and current_search.goal_revision != record.goal_revision:
        current_search = None
    checks = [check for check in record.final_checks if check.goal_revision == record.goal_revision]
    latest_check = checks[-1] if checks else None
    payload = {
        **goal_receipt(record),
        "outer_deadline_at": record.outer_deadline_at,
        "current_search_run_id": current_search.run_id if current_search else None,
        "work_items": [work_item_summary(item) for item in record.work_items
                       if item.goal_revision == record.goal_revision
                       and item.status not in {"accepted", "cancelled", "superseded"}],
        "final_check_policy": {"mode": final_check_mode(record)},
    }
    if latest_check:
        payload["latest_final_check"] = latest_check.model_dump(mode="json", include={
            "check_id", "goal_revision", "status",
        }, exclude_none=True)
    pending_creates = [item for item in record.search_create_requests if item.state != "linked"]
    if pending_creates:
        payload["search_create_requests"] = [item.model_dump(mode="json", include={
            "request_id", "run_id", "state", "error", "goal_revision",
        }, exclude_none=True) for item in pending_creates]
    if detail:
        payload.update(record.model_dump(mode="json", include={
            "raw_goal", "policy", "triage", "spec_draft", "source_path",
            "command_config", "active_session",
        }))
        payload.update({
            "current_search": current_search.model_dump(mode="json", exclude_none=True)
                              if current_search else None,
            "search_tasks_total": len(record.search_tasks),
            "work_items_total": len(record.work_items),
            "latest_final_check": latest_check.model_dump(mode="json") if latest_check else None,
        })
    else:
        payload["details"] = {"tool": "goal_plus_status", "detail": True}
    return payload
