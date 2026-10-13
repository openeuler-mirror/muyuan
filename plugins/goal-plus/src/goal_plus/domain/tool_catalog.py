"""Model-visible tool names, roles and frozen capability conditions."""

from typing import Literal

CONTROLLER_ONLY_CLOSEOUT_ENV = "GOAL_PLUS_CONTROLLER_ONLY_CLOSEOUT"
CONTROLLER_ONLY_CLOSEOUT_TOOL_NAMES = frozenset(
    {
        "goal_plus_search_select",
        "goal_plus_search_promote",
        "goal_plus_search_apply_promotion",
        "goal_plus_record_search_result",
    }
)

SEARCH_TOOL_NAMES = {
    "goal_plus_get_tool_schema",
    "goal_plus_list_models",
    "goal_plus_search_freeze_spec",
    "goal_plus_search_create",
    "goal_plus_search_invalidate_run",
    "goal_plus_search_status",
    "goal_plus_search_recovery_status",
    "goal_plus_search_prepare_recovery",
    "goal_plus_search_recover_candidate",
    "goal_plus_search_list_history",
    "goal_plus_search_plan_next",
    "goal_plus_search_start_batch",
    "goal_plus_search_get_agent_context",
    "goal_plus_search_get_global_evidence",
    "goal_plus_search_reference_open",
    "goal_plus_search_stage_shared_tool",
    "goal_plus_search_copy_shared_tool",
    "goal_plus_search_get_evidence_detail",
    "goal_plus_search_get_agent_observability",
    "goal_plus_search_read_shared_cache",
    "goal_plus_search_append_shared_cache",
    "goal_plus_search_run_verifier",
    "goal_plus_search_list_iterations",
    "goal_plus_search_list_allocation_decisions",
    "goal_plus_search_apply_allocation_decision",
    "goal_plus_search_select",
    "goal_plus_search_report",
    "goal_plus_search_promote",
    "goal_plus_search_apply_promotion",
}

GOAL_PLUS_TOOL_NAMES = {
    "goal_plus_status",
    "goal_plus_history",
    "goal_plus_record_triage",
    "goal_plus_record_work_event",
    "goal_plus_record_search_result",
    "goal_plus_run_final_check",
    "goal_plus_run_work_item",
    "goal_plus_submit_final_check",
    "goal_plus_set_status",
}

ToolCallerRole = Literal["main", "candidate", "final_checker", "ordinary"]

CANDIDATE_TOOL_NAMES = frozenset(
    {
        "goal_plus_search_get_agent_context",
        "goal_plus_search_get_global_evidence",
        "goal_plus_search_reference_open",
        "goal_plus_search_stage_shared_tool",
        "goal_plus_search_copy_shared_tool",
        "goal_plus_search_get_evidence_detail",
        "goal_plus_search_read_shared_cache",
        "goal_plus_search_append_shared_cache",
        "goal_plus_search_run_verifier",
        "goal_plus_search_list_iterations",
    }
)
FINAL_CHECKER_TOOL_NAMES = frozenset(
    {"goal_plus_status", "goal_plus_submit_final_check"}
)


def require_tool_role(
    role: ToolCallerRole,
    tool_name: str,
    *,
    caller_agent_session_id: str | None = None,
    requested_agent_session_ids: list[str] | None = None,
) -> None:
    """Enforce model-facing role boundaries identified by a trusted host."""

    if role == "main":
        allowed = tool_name not in {"goal_plus_submit_final_check", *(CANDIDATE_TOOL_NAMES - {"goal_plus_search_run_verifier"})}
    elif role == "candidate":
        allowed = tool_name in CANDIDATE_TOOL_NAMES
    elif role == "final_checker":
        allowed = tool_name in FINAL_CHECKER_TOOL_NAMES
    else:
        allowed = False
    if not allowed:
        raise PermissionError(f"GoalPlusRoleDenied: {role} cannot call {tool_name}")
    if role == "candidate" and (
        caller_agent_session_id is None
        or set(requested_agent_session_ids or []) != {caller_agent_session_id}
    ):
        raise PermissionError(
            "GoalPlusSessionMismatch: candidate tools require the caller's own "
            "agent_session_id"
        )



SESSION_TOOL_NAMES = {"goal_plus_session_" + action for action in ("run", "wait_any", "message", "interrupt", "stop", "close")}
PUBLIC_TOOL_NAMES = SEARCH_TOOL_NAMES | GOAL_PLUS_TOOL_NAMES | SESSION_TOOL_NAMES | {"goal_plus_monitor_snapshot"}

CANDIDATE_CAPABILITIES = {
    "goal_plus_search_get_evidence_detail": "supplemental",
    "goal_plus_search_stage_shared_tool": "shared_tools",
    "goal_plus_search_copy_shared_tool": "shared_tools",
    "goal_plus_search_read_shared_cache": "shared_cache",
    "goal_plus_search_append_shared_cache": "shared_cache",
}
