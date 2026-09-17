"""Existing Goal authorization categories and pure session checks."""

import re
from goal_plus.domain.tool_identity import goal_plus_tool_name, native_tool_name
from goal_plus.domain.goal import GoalPlusCommandConfig
from goal_plus.domain.search import SearchSpec
from goal_plus.domain.worker_policy import parse_worker_model_counts

from goal_plus.domain.goal import (
    GoalPlusHostCommandInvocation,
    GoalPlusRecord,
    GoalPlusStatus,
)

GOAL_ID_RE = re.compile(r"gp_\d+")
TERMINAL_STATUSES: set[GoalPlusStatus] = {"complete", "abandoned"}
GOAL_PLUS_ENTRYPOINT_REQUIRED = (
    "GoalPlusEntrypointRequired: Goal Plus can only be started by Codex "
    "$goal-plus or Pi /goal-plus. Skill, MCP, CLI, and model-initiated creation "
    "are not permitted."
)
SEARCH_RECOVERY_TOOL_SUFFIXES = (
    "goal_plus_search_recovery_status",
    "goal_plus_search_prepare_recovery",
    "goal_plus_search_recover_candidate",
)
GOAL_SCOPED_TOOL_SUFFIXES = (
    "goal_plus_search_freeze_spec",
    "goal_plus_search_create",
    "goal_plus_record_triage",
    "goal_plus_record_work_event",
    "goal_plus_save_spec_draft",
    "goal_plus_link_search_run",
    "goal_plus_record_search_result",
    "goal_plus_prepare_final_check",
    "goal_plus_run_final_check",
    "goal_plus_run_work_item",
    "goal_plus_submit_final_check",
    "goal_plus_set_status",
    "goal_plus_gate",
)
LINKED_SEARCH_TOOL_SUFFIXES = (
    "goal_plus_session_run",
    "goal_plus_session_message",
    "goal_plus_search_invalidate_run",
    *SEARCH_RECOVERY_TOOL_SUFFIXES,
    "goal_plus_search_plan_next",
    "goal_plus_search_start_batch",
    "search_start_agent_session",
    "search_redispatch_candidate",
    "search_bind_agent_handle",
    "search_continue_agent_session",
    "goal_plus_search_get_agent_context",
    "goal_plus_search_get_global_evidence",
    "goal_plus_search_reference_open",
    "goal_plus_search_stage_shared_tool",
    "goal_plus_search_copy_shared_tool",
    "goal_plus_search_get_evidence_detail",
    "goal_plus_search_run_verifier",
    "goal_plus_search_list_iterations",
    "goal_plus_search_list_allocation_decisions",
    "goal_plus_search_apply_allocation_decision",
    "goal_plus_search_select",
    "goal_plus_search_promote",
    "goal_plus_search_apply_promotion",
)


def is_terminal_goal(record: GoalPlusRecord) -> bool:
    return record.status in TERMINAL_STATUSES or record.control.state == "closed"


def stopped_goal_message(record: GoalPlusRecord) -> str:
    """Explain why a stopped Goal rejects work, with the model's next step.

    Gate rejections must carry the authoritative persisted facts (control
    state/reason, status) plus an actionable instruction, so the model can
    relay the recovery step to the user without guessing internal logic.
    """
    if record.control.state == "paused":
        reason = record.control.reason or "explicit_pause"
        status_note = (
            f", status={record.status}" if record.status != "active" else ""
        )
        return (
            f"GoalPlusResumeRequired: Goal Plus {record.goal_plus_id} is paused "
            f"(control=paused, reason={reason}{status_note}). Only an explicit "
            "host resume command in the original native session can restart "
            "it. Tell the user to run the Goal Plus resume command, then retry."
        )
    if is_terminal_goal(record):
        return (
            f"GoalPlusTerminal: Goal Plus {record.goal_plus_id} is finished "
            f"(status={record.status}, control={record.control.state}). It "
            "cannot accept new work; read-only history tools remain available. "
            "Start a new task with the host start command if needed."
        )
    if record.status == "blocked":
        return (
            f"GoalPlusResumeRequired: Goal Plus {record.goal_plus_id} is "
            "stopped (status=blocked). An explicit host resume command in the "
            "original native session opens one retry with the recorded next "
            "action."
        )
    return (
        f"GoalPlusUserInputRequired: Goal Plus {record.goal_plus_id} is "
        f"waiting for user input (status={record.status}). Ask the user for "
        "the pending answer instead of using unrelated input."
    )


def has_host_invocation(record: GoalPlusRecord, invocation_id: str) -> bool:
    return any(
        invocation.invocation_id == invocation_id
        for invocation in record.host_command_invocations
    )


def tool_matches(tool_name: str, suffix: str) -> bool:
    # Bare actions originate inside the dedicated runtime/Pi dispatcher. Host
    # adapters must establish ownership before forwarding a bare action here.
    return tool_name == suffix or goal_plus_tool_name(tool_name) == suffix or (
        suffix in {"bash", "edit", "write", "shell", "exec_command", "apply_patch"}
        and native_tool_name(tool_name) == suffix
    )


def require_control_session(
    record: GoalPlusRecord, invocation: GoalPlusHostCommandInvocation
) -> None:
    active = record.active_session
    agent_harness = invocation.agent_harness
    historical_access = invocation.action == "summary" or (
        invocation.action == "edit" and record.status == "complete"
    )
    if (
        active is None
        or active.session_id != invocation.session_id
        or active.agent_harness != agent_harness
        or active.state == "stale"
        or (active.state == "detached" and not historical_access)
    ):
        raise RuntimeError(
            "Goal Plus control requires its attached original native session"
        )


def _model_reference_matches(requested: str, configured: str) -> bool:
    requested_parts = requested.strip().casefold().rsplit("/", 1)
    configured_parts = configured.strip().casefold().rsplit("/", 1)
    return (
        requested_parts == configured_parts
        or requested_parts[-1] == configured_parts[-1]
    )


def validate_search_command_config(
    config: GoalPlusCommandConfig,
    spec: SearchSpec,
) -> None:
    checks = {
        "max_parallel": (config.max_parallel, spec.budget.max_parallel),
        "workspace_provider": (config.workspace_provider, spec.workspace.provider),
        "promotion_mode": (config.promotion_mode, spec.workspace.promotion_mode),
        "strategy": (config.strategy, spec.strategy.name),
        "inner_agent": (config.inner_agent, spec.strategy.inner_agent),
    }
    for name, (requested, configured) in checks.items():
        if requested is not None and requested != configured:
            raise ValueError(
                f"explicit command config {name}={requested} requires SearchSpec "
                f"{name}={requested}; received {configured}"
            )

    if config.annotator is not None:
        configured_annotator = spec.strategy.evidence_annotator.model
        if configured_annotator is None or not _model_reference_matches(
            config.annotator,
            configured_annotator,
        ):
            raise ValueError(
                "explicit command config annotator= must match "
                "SearchSpec strategy.evidence_annotator.model"
            )

    if config.workers is not None:
        requested_models = parse_worker_model_counts(config.workers)
        configured_models = spec.strategy.models
        if len(requested_models) != len(configured_models):
            raise ValueError(
                "explicit command config workers= must match SearchSpec strategy.models"
            )
        for (requested_model, requested_count), configured_model in zip(
            requested_models,
            configured_models,
            strict=True,
        ):
            if not _model_reference_matches(requested_model, configured_model.model):
                raise ValueError(
                    "explicit command config workers= must match SearchSpec "
                    "strategy.models in order"
                )
            if requested_count is not None and requested_count != configured_model.count:
                raise ValueError(
                    f"explicit command config workers={requested_model}*{requested_count} "
                    f"requires strategy.models count={requested_count}; "
                    f"received {configured_model.count}"
                )
