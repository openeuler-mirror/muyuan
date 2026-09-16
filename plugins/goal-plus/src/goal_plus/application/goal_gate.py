"""Host event admission and durable continuation decisions."""

from __future__ import annotations
from typing import Any
from goal_plus.domain.goal_commands import (
    DEFAULT_EXECUTION_POLICY,
)


from goal_plus.domain.goal import (
    GoalPlusGateEvent,
    GoalPlusGateResult,
    GoalPlusRecord,
)

from goal_plus.domain.goal_authorization import (
    GOAL_SCOPED_TOOL_SUFFIXES,
    LINKED_SEARCH_TOOL_SUFFIXES,
    SEARCH_RECOVERY_TOOL_SUFFIXES,
    stopped_goal_message,
    tool_matches,
)
from goal_plus.domain.tool_identity import goal_plus_tool_name


from goal_plus.ports.goal_state import GoalState
from goal_plus.ports.clock import Clock
from goal_plus.ports.goal_authorization import GoalToolTarget
from goal_plus.application.goal_authorization import GoalAuthorization
from goal_plus.domain.goal_closeout import (
    tool_name,
    tool_input,
    is_search_tool,
    is_mutating_tool,
    should_block_mutation,
    mutation_block_reason,
    is_final_checker_context,
    final_check_mode,
    latest_final_check,
    continuation_prompt,
)
from goal_plus.application.goal_reconciliation import GoalReconciliation
from goal_plus.domain.goal_time import elapsed_seconds, format_elapsed


class GoalGate:
    def __init__(
        self,
        state: GoalState,
        clock: Clock,
        authorization: GoalAuthorization,
        reconciliation: GoalReconciliation,
    ) -> None:
        self.state, self.clock = state, clock
        self.authorization = authorization
        self.reconciliation = reconciliation

    def gate(
        self,
        goal_plus_id: str,
        event: GoalPlusGateEvent,
        context: dict[str, Any],
    ) -> GoalPlusGateResult:
        with self.state.lock_goal(goal_plus_id):
            record = self.state.read_goal(goal_plus_id)
            target = GoalToolTarget.from_input(tool_name(context), tool_input(context))
            if event == "stop" and (
                context.get("mode") == "plan"
                or context.get("has_pending_user_input")
                or context.get("main_busy")
            ):
                return self._record_gate(
                    record, event, "allow", reason="host_not_ready_for_automatic_turn"
                )
            if record.control.state != "active":
                owned_tool = goal_plus_tool_name(tool_name(context))
                tool = owned_tool or tool_name(context)
                if (
                    tool == "goal_plus_gate"
                    and tool_input(context).get("event") == "stop"
                ):
                    error = self.authorization.ownership_error(record, target)
                    return self._record_gate(
                        record, event, "block" if error else "allow", reason=error
                    )
                settlement_or_read = {
                    "goal_plus_status",
                    "goal_plus_history",
                    "goal_plus_monitor_snapshot",
                    "goal_plus_list_models",
                    "goal_plus_record_work_event",
                    "goal_plus_submit_final_check",
                    "goal_plus_search_status",
                    "goal_plus_search_recovery_status",
                    "goal_plus_search_list_history",
                    "goal_plus_search_list_iterations",
                    "goal_plus_search_get_agent_observability",
                    "goal_plus_search_report",
                    "goal_plus_search_run_verifier",
                }
                is_domain = owned_tool is not None or tool in {
                    *GOAL_SCOPED_TOOL_SUFFIXES, *LINKED_SEARCH_TOOL_SUFFIXES,
                }
                if (
                    event == "pre_tool_use"
                    and is_domain
                    and tool not in settlement_or_read
                ):
                    return self._record_gate(
                        record,
                        event,
                        "block",
                        reason=stopped_goal_message(record),
                    )
                return self._record_gate(record, event, "allow")
            if event == "pre_tool_use" and any(
                tool_matches(tool_name(context), name)
                for name in SEARCH_RECOVERY_TOOL_SUFFIXES
            ):
                if record.status != "active" or self.authorization.ownership_error(
                    record, target
                ):
                    return self._record_gate(
                        record,
                        event,
                        "block",
                        reason="SearchRecoveryOwnershipRequired: recovery requires an active Goal and its current-revision linked run.",
                    )
            if record.status != "active":
                return self._record_gate(record, event, "allow")

            if is_final_checker_context(context):
                return self._record_gate(record, event, "allow")

            if event == "subagent_stop":
                final_check = latest_final_check(record)
                if (
                    final_check is not None
                    and final_check.goal_revision == record.goal_revision
                    and final_check.status == "failed"
                ):
                    return self._record_gate(record, event, "allow")

                subagent_role = context.get("goal_plus_subagent_role")
                if subagent_role in {"search_candidate", "ordinary"}:
                    # A native return is not Search Evidence or Goal completion.
                    return self._record_gate(record, event, "allow")

            if event == "stop":
                reason = "停止前审计完整的原始目标。"
                if record.next_action is not None and record.next_action.required:
                    reason = record.next_action.description
                elif final_check_mode(record) == "required":
                    final_check = latest_final_check(record)
                    if (
                        final_check is None
                        or final_check.goal_revision != record.goal_revision
                        or final_check.status != "passed"
                    ):
                        reason = "停止前，为当前目标修订版运行必需的独立最终检查。"
                return self._record_gate(
                    record,
                    event,
                    "block",
                    reason=reason,
                    continuation_prompt=self._stop_audit_prompt(record),
                )

            if event == "subagent_stop" and record.next_action is not None:
                if record.next_action.required:
                    return self._record_gate(
                        record,
                        event,
                        "block",
                        reason=record.next_action.description,
                        continuation_prompt=continuation_prompt(record),
                    )
                if final_check_mode(record) != "required":
                    return self._record_gate(record, event, "allow")

            if event == "subagent_stop" and final_check_mode(record) == "required":
                final_check = latest_final_check(record)
                if (
                    final_check is None
                    or final_check.goal_revision != record.goal_revision
                    or final_check.status != "passed"
                ):
                    reason = "停止前，为当前目标修订版运行必需的独立最终检查。"
                    return self._record_gate(
                        record,
                        event,
                        "block",
                        reason=reason,
                        continuation_prompt=(
                            f"Goal Plus {record.goal_plus_id} 修订版 {record.goal_revision} 需要独立"
                            "最终检查。调用 goal_plus_run_final_check 并审查返回的结论。"
                        ),
                    )

            if event == "pre_tool_use":
                current_tool = tool_name(context)
                ownership_error = self.authorization.ownership_error(record, target)
                if ownership_error is not None:
                    return self._record_gate(
                        record,
                        event,
                        "block",
                        reason=ownership_error,
                    )
                if is_mutating_tool(current_tool) and should_block_mutation(record):
                    return self._record_gate(
                        record,
                        event,
                        "block",
                        reason=mutation_block_reason(record),
                    )
            return self._record_gate(record, event, "allow")

    def _record_gate(
        self,
        record: GoalPlusRecord,
        event: GoalPlusGateEvent,
        decision: str,
        reason: str | None = None,
        continuation_prompt: str | None = None,
    ) -> GoalPlusGateResult:
        counters = dict(record.hook_counters)
        counters[event] = counters.get(event, 0) + 1
        updated = record.model_copy(
            update={"hook_counters": counters, "updated_at": self.clock.timestamp()}
        )
        self.state.write_goal(updated)
        self.state.append_event(
            record.goal_plus_id,
            f"gate_{decision}ed",
            {"event": event, "reason": reason},
        )
        return GoalPlusGateResult(
            decision=decision,  # type: ignore[arg-type]
            phase=record.phase,
            status=record.status,
            reason=reason,
            continuation_prompt=continuation_prompt,
            goal_plus_id=record.goal_plus_id,
            goal_revision=record.goal_revision,
            control_version=record.control.version,
            session_id=record.active_session.session_id
            if record.active_session
            else None,
        )

    def _stop_audit_prompt(self, record: GoalPlusRecord) -> str:
        checked_at = self.clock.timestamp()
        elapsed = format_elapsed(elapsed_seconds(record.created_at, self.clock.now()))
        action = record.next_action
        action_text = action.description if action else "没有记录下一步 action。"
        final_check_text = (
            "该目标修订版必须通过独立最终检查；调用 goal_plus_run_final_check 并审查结论。"
            if final_check_mode(record) == "required"
            else "policy 不要求独立最终检查。"
        )
        active_items = [
            item
            for item in record.work_items
            if item.goal_revision == record.goal_revision
            and item.status in {"launching", "active"}
        ]
        execution_text = (
            ", ".join(f"{item.work_item_id}={item.status}" for item in active_items)
            if active_items
            else "无活跃 subagent 派发。"
        )
        return (
            "Goal Plus 仍处于 active。顶层 agent 只有在记录真实终态后才能停止。\n\n"
            "该修订版的完整原始目标：\n"
            "---\n"
            f"{record.raw_goal}\n"
            "---\n\n"
            "时间上下文：\n"
            f"- created_at_utc: {record.created_at}\n"
            f"- checked_at_utc: {checked_at}\n"
            f"- elapsed: {elapsed}\n\n"
            f"当前 phase：{record.phase}\n"
            f"当前 next action：{action_text}\n"
            f"编排 policy：{record.policy.get('execution', DEFAULT_EXECUTION_POLICY)}\n"
            f"活跃 subagent 派发：{execution_text}\n"
            f"最终检查 policy：{final_check_text}\n\n"
            "对照持久证据审计完整原始目标中的每项要求。如果原始目标包含时间限制，使用上述"
            "时间戳进行判断：只要仍有可用时间且目标未完成，就继续工作；达到或超过时间条件时，"
            "停止新增工作、保留最佳持久结果并完成必需审计。时间条件不覆盖已经完成的结果终态："
            "若必需工作、验证、提升和审计已经完成，即使实际收尾晚于预设也使用 complete，并在"
            "evidence 中分别记录预设时间和实际完成收尾时间；不要输出 late，也不要仅因超出预设"
            "而使用 blocked。blocked 只表示仍缺少外部输入、权限、资源或状态变化，导致任务确实"
            "无法继续。如果没有时间限制，继续到目标满足，或出现这种真实阻塞。单次 worker 或"
            "probe 返回不代表 Goal Plus 任务完成。停止前，使用 complete、blocked 或 abandoned"
            "调用 goal_plus_set_status，并提供真实原因和证据。\n\n"
            + self.reconciliation.reconciliation_context(record.goal_plus_id)
        )
