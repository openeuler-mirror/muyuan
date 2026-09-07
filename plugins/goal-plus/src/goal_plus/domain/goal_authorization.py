"""Existing Goal authorization categories and pure session checks."""

import re
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
    "search_recovery_status",
    "search_prepare_recovery",
    "search_recover_candidate",
)
GOAL_SCOPED_TOOL_SUFFIXES = (
    "search_freeze_spec",
    "search_create",
    "goal_plus_record_triage",
    "goal_plus_record_work_event",
    "goal_plus_save_spec_draft",
    "goal_plus_link_search_run",
    "goal_plus_record_search_result",
    "goal_plus_prepare_final_check",
    "goal_plus_submit_final_check",
    "goal_plus_set_status",
    "goal_plus_gate",
)
LINKED_SEARCH_TOOL_SUFFIXES = (
    "search_invalidate_run",
    *SEARCH_RECOVERY_TOOL_SUFFIXES,
    "search_plan_next",
    "search_start_batch",
    "search_start_agent_session",
    "search_redispatch_candidate",
    "search_bind_agent_handle",
    "search_continue_agent_session",
    "search_get_agent_context",
    "search_get_global_evidence",
    "search_reference_open",
    "search_stage_shared_tool",
    "search_copy_shared_tool",
    "search_get_evidence_detail",
    "search_run_verifier",
    "search_list_iterations",
    "search_list_allocation_decisions",
    "search_apply_allocation_decision",
    "search_select",
    "search_promote",
    "search_apply_promotion",
    "pi_search_pool_open",
    "pi_search_pool_wait_any",
    "pi_search_pool_snapshot",
    "pi_search_pool_continue",
    "pi_search_pool_close",
)


def is_terminal_goal(record: GoalPlusRecord) -> bool:
    return record.status in TERMINAL_STATUSES or record.control.state == "closed"


def has_host_invocation(record: GoalPlusRecord, invocation_id: str) -> bool:
    return any(
        invocation.invocation_id == invocation_id
        for invocation in record.host_command_invocations
    )


def tool_matches(tool_name: str, suffix: str) -> bool:
    return (
        tool_name == suffix
        or tool_name.endswith(f"__{suffix}")
        or tool_name.endswith(f".{suffix}")
    )


def require_control_session(
    record: GoalPlusRecord, invocation: GoalPlusHostCommandInvocation
) -> None:
    active = record.active_session
    host = "pi-rpc" if invocation.host == "pi" else invocation.host
    historical_access = invocation.action == "summary" or (
        invocation.action == "edit" and record.status == "complete"
    )
    if (
        active is None
        or active.session_id != invocation.session_id
        or active.host != host
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
        "workspace_backend": (config.workspace_backend, spec.workspace.backend),
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
            if requested_count != configured_model.count:
                raise ValueError(
                    "explicit command config workers= counts must match SearchSpec "
                    "strategy.models counts"
                )
