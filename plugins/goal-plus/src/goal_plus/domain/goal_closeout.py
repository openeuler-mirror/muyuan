"""Pure Goal phase, final-check and tool admission policy."""

from typing import Any
from goal_plus.domain.goal_commands import (
    MUTATING_TOOL_SUFFIXES,
)


from goal_plus.domain.goal import (
    GoalPlusFinalCheck,
    GoalPlusNextAction,
    GoalPlusRecord,
    GoalPlusSpecDraft,
    GoalPlusTriage,
)

from goal_plus.domain.goal_authorization import (
    tool_matches,
)


def triage_phase_and_action(
    triage: GoalPlusTriage,
) -> tuple[str, GoalPlusNextAction]:
    if not triage.is_optimization or triage.recommended_phase == "goal":
        return (
            "goal",
            GoalPlusNextAction(
                kind="execute_goal",
                description=(
                    "执行原始目标；主 Agent 按需委派 subagent，并在适合可度量并行探索时进入 Search。"
                ),
                required=True,
            ),
        )
    missing = ", ".join(triage.missing) if triage.missing else "spec 细节"
    return (
        "spec_discovery",
        GoalPlusNextAction(
            kind=(
                "draft_initial_search_spec"
                if triage.recommended_phase == "search"
                else "discover_spec"
            ),
            description=f"Search 前完成 spec discovery。缺少：{missing}。",
            required=True,
            metadata={"missing": triage.missing},
        ),
    )


def require_current_search_result(
    record: GoalPlusRecord,
    action: str,
) -> None:
    current_tasks = [
        task
        for task in record.search_tasks
        if task.goal_revision == record.goal_revision
    ]
    search_required = (
        (record.triage is not None and record.triage.is_optimization)
        or record.phase in {"spec_discovery", "search", "final_audit"}
        or bool(current_tasks)
    )
    if not search_required:
        return
    linked = record.linked_search
    if (
        linked is None
        or linked.goal_revision != record.goal_revision
        or linked.result_recorded_at is None
    ):
        raise RuntimeError(
            f"cannot {action}; Search completion requires "
            "goal_plus_record_search_result for the current goal revision "
            "after promotion and any required apply"
        )


SEARCH_EVIDENCE_IDENTITY_KEYS = ("candidate_id", "selected_candidate_id")


def require_honest_search_evidence(
    record: GoalPlusRecord,
    evidence: list[dict[str, Any]] | None,
    action: str,
) -> None:
    """Refuse terminal evidence that names Search facts no settled run produced.

    Candidate identities only exist inside Search runs. A Goal without a
    recorded current-revision search result must not present candidate or
    selection claims as terminal evidence: native collab agents and manual
    edits are not a Search run and never populate search_tasks.
    """
    linked = record.linked_search
    if (
        linked is not None
        and linked.goal_revision == record.goal_revision
        and linked.result_recorded_at is not None
    ):
        return
    for entry in evidence or []:
        if not isinstance(entry, dict):
            continue
        for key in SEARCH_EVIDENCE_IDENTITY_KEYS:
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                raise RuntimeError(
                    f"cannot {action}; evidence claims {key}={value} but this "
                    "Goal has no recorded search result for the current goal "
                    "revision. Run the Search tool chain (freeze spec, create "
                    "run, candidates, selection, promotion, then "
                    "goal_plus_record_search_result) or remove claims that no "
                    "Search run produced; native collab agents cannot "
                    "substitute a Search run"
                )


def spec_draft_next_action(spec_draft: GoalPlusSpecDraft) -> GoalPlusNextAction:
    if spec_draft.confidence != "high" or spec_draft.open_questions:
        return GoalPlusNextAction(
            kind="resolve_spec_questions",
            description="冻结 SearchSpec 前解决 open question。",
            required=True,
            metadata={"open_questions": spec_draft.open_questions},
        )
    return GoalPlusNextAction(
        kind="freeze_search_spec",
        description="自主冻结高置信度 SearchSpec 和 verifier 产物，然后创建 Search run。",
        required=True,
    )


def tool_name(context: dict[str, Any]) -> str:
    value = context.get("tool_name") or context.get("toolName") or ""
    return str(value)


def tool_input(context: dict[str, Any]) -> dict[str, Any]:
    for key in ("tool_input", "toolInput", "input"):
        value = context.get(key)
        if isinstance(value, dict):
            return value
    return {}


def is_mutating_tool(tool_name: str) -> bool:
    return any(tool_matches(tool_name, suffix) for suffix in MUTATING_TOOL_SUFFIXES)


def should_block_mutation(record: GoalPlusRecord) -> bool:
    if record.next_action is None or not record.next_action.required:
        return False
    if record.next_action.kind == "address_final_check_findings":
        return False
    return record.phase in {"intake", "final_audit", "final_check"}


def mutation_block_reason(record: GoalPlusRecord) -> str:
    action = record.next_action
    if action is None:
        return "Goal Plus 状态尚未准备好使用变更类工具。"
    return f"使用变更类工具前先完成当前 Goal Plus next action：{action.description}"


def is_final_checker_context(context: dict[str, Any]) -> bool:
    return context.get("goal_plus_subagent_role") == "final_checker"


def final_check_mode(record: GoalPlusRecord) -> str:
    final_check = record.policy.get("final_check")
    if isinstance(final_check, str):
        return final_check
    if isinstance(final_check, dict):
        mode = final_check.get("mode")
        if isinstance(mode, str):
            return mode
    return "disabled"


def latest_final_check(record: GoalPlusRecord) -> GoalPlusFinalCheck | None:
    return record.final_checks[-1] if record.final_checks else None


def continuation_prompt(record: GoalPlusRecord) -> str:
    action = record.next_action
    action_text = action.description if action else "继续 active 的 goal-plus 任务。"
    check_id = action.metadata.get("check_id") if action is not None else None
    check_text = f" (check_id={check_id})" if isinstance(check_id, str) else ""
    return (
        f"Goal Plus 在 phase {record.phase} 中仍处于 active。\n"
        "暂时不要停止。下一项必需 action 是：\n"
        f"  {action_text}{check_text}\n"
        "完成该 action 后，在停止前更新 goal-plus 状态。"
    )
