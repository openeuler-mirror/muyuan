"""Goal creation and optimistic revision changes."""

from __future__ import annotations


from goal_plus.domain.goal import (
    GoalPlusControl,
    GoalPlusGoalRevision,
    GoalPlusHostCommandInvocation,
    GoalPlusNextAction,
    GoalPlusRecord,
)


from goal_plus.domain.goal_authorization import (
    GOAL_PLUS_ENTRYPOINT_REQUIRED as GOAL_PLUS_ENTRYPOINT_REQUIRED,
    has_host_invocation,
    require_control_session,
)


from typing import Any
from goal_plus.domain.goal_commands import (
    normalize_goal_plus_raw_goal,
    parse_goal_plus_command_config,
    exploration_mode_from_raw_goal,
)
from goal_plus.domain.goal_policy import (
    normalize_policy,
    deadline_from_creation,
    with_host_invocation,
)
from goal_plus.application.goal_authorization import GoalAuthorization
from goal_plus.ports.goal_state import GoalState
from goal_plus.ports.clock import Clock


class GoalRevisions:
    def __init__(
        self, state: GoalState, clock: Clock, authorization: GoalAuthorization
    ) -> None:
        self.state, self.clock, self.authorization = state, clock, authorization

    def create_goal(
        self,
        raw_goal: str,
        source_path: str | None = None,
        policy: dict[str, Any] | None = None,
        *,
        host_invocation: GoalPlusHostCommandInvocation | None = None,
    ) -> GoalPlusRecord:
        goal_plus_id = self.state.next_goal_id()
        now = self.clock.timestamp()
        normalized_policy = normalize_policy(policy)
        _objective, command_config = parse_goal_plus_command_config(raw_goal)
        normalized_raw_goal = normalize_goal_plus_raw_goal(raw_goal)
        record = GoalPlusRecord(
            goal_plus_id=goal_plus_id,
            raw_goal=normalized_raw_goal,
            command_config=command_config,
            source_path=source_path,
            outer_deadline_at=deadline_from_creation(
                now, command_config.max_duration_seconds
            ),
            status="active",
            phase="intake",
            policy=normalized_policy,
            goal_revision=1,
            goal_revisions=[
                GoalPlusGoalRevision(
                    revision=1,
                    raw_goal=normalized_raw_goal,
                    command_config=command_config,
                    reason="目标已创建",
                    created_at=now,
                )
            ],
            next_action=GoalPlusNextAction(
                kind="record_triage",
                description="判断原始目标应按 /goal 运行，还是升级到 Search Mode。",
                required=True,
            ),
            entrypoint_status=(
                "host_verified" if host_invocation is not None else "legacy_unverified"
            ),
            host_command_invocations=(
                [host_invocation.model_copy(deep=True)]
                if host_invocation is not None
                else []
            ),
            created_at=now,
            updated_at=now,
        )
        self.state.write_goal(record)
        self.state.append_event(
            goal_plus_id,
            "created",
            {
                "raw_goal": record.raw_goal,
                "command_config": command_config.model_dump(
                    mode="json", exclude_none=True
                ),
                "goal_revision": record.goal_revision,
                "policy": record.policy,
                "host_invocation": (
                    host_invocation.model_dump(mode="json")
                    if host_invocation is not None
                    else None
                ),
            },
        )
        return record

    def start_from_host_command(
        self,
        raw_goal: str,
        *,
        invocation: GoalPlusHostCommandInvocation | dict[str, Any],
        source_path: str | None = None,
        policy: dict[str, Any] | None = None,
    ) -> GoalPlusRecord:
        parsed = (
            invocation
            if isinstance(invocation, GoalPlusHostCommandInvocation)
            else GoalPlusHostCommandInvocation.model_validate(invocation)
        )
        if parsed.action != "start":
            raise ValueError(
                "Goal Plus start requires a host invocation with action=start"
            )
        with self.state.lock_entrypoint():
            existing = self.authorization.require_start(parsed)
            if existing is not None:
                return existing
            return self.create_goal(
                raw_goal,
                source_path=source_path,
                policy=policy,
                host_invocation=parsed,
            )

    def update_goal(
        self,
        goal_plus_id: str,
        raw_goal: str,
        expected_revision: int,
        reason: str | None = None,
        *,
        host_invocation: GoalPlusHostCommandInvocation | None = None,
    ) -> GoalPlusRecord:
        with self.state.lock_goal(goal_plus_id):
            record = self.state.read_goal(goal_plus_id)
            if host_invocation is not None:
                if host_invocation.action != "edit":
                    raise ValueError(
                        "Goal Plus edit requires a host invocation with action=edit"
                    )
                if has_host_invocation(record, host_invocation.invocation_id):
                    return record
                require_control_session(record, host_invocation)
            if (
                record.status == "abandoned"
                or record.control.state == "closed"
                and record.status != "complete"
            ):
                raise RuntimeError(
                    "A cleared or historical terminal Goal cannot be edited"
                )
            if record.goal_revision != expected_revision:
                raise RuntimeError(
                    f"Goal Plus revision conflict: expected {expected_revision}, "
                    f"current revision is {record.goal_revision}."
                )
            _objective, updated_command_config = parse_goal_plus_command_config(
                raw_goal
            )
            if (
                updated_command_config.mode is None
                and record.command_config.mode is not None
            ):
                updated_command_config = updated_command_config.model_copy(
                    update={"mode": record.command_config.mode}
                )
            if (
                updated_command_config.max_duration_seconds is None
                and record.command_config.max_duration_seconds is not None
            ):
                updated_command_config = updated_command_config.model_copy(
                    update={
                        "max_duration_seconds": (
                            record.command_config.max_duration_seconds
                        )
                    }
                )
            updated_raw_goal = normalize_goal_plus_raw_goal(
                raw_goal,
                inherited_mode=exploration_mode_from_raw_goal(record.raw_goal),
            )
            if (
                updated_raw_goal == record.raw_goal
                and updated_command_config == record.command_config
            ):
                if host_invocation is None:
                    return record
                updated = with_host_invocation(
                    record, host_invocation, self.clock.timestamp()
                )
                self.state.write_goal(updated)
                self.state.append_event(
                    goal_plus_id,
                    "host_command_authorized",
                    host_invocation.model_dump(mode="json"),
                )
                return updated

            now = self.clock.timestamp()
            next_revision = record.goal_revision + 1
            checks = [
                check.model_copy(update={"status": "superseded", "completed_at": now})
                if check.status == "pending"
                else check.model_copy(deep=True)
                for check in record.final_checks
            ]
            work_items = [
                item.model_copy(update={"status": "superseded", "updated_at": now})
                if item.goal_revision == record.goal_revision
                and item.status not in {"accepted", "cancelled", "superseded"}
                else item.model_copy(deep=True)
                for item in record.work_items
            ]
            revision = GoalPlusGoalRevision(
                revision=next_revision,
                raw_goal=updated_raw_goal,
                command_config=updated_command_config,
                reason=reason or "用户编辑了 Goal Plus 目标",
                created_at=now,
            )
            updated = record.model_copy(
                update={
                    "raw_goal": updated_raw_goal,
                    "command_config": updated_command_config,
                    "outer_deadline_at": deadline_from_creation(
                        record.created_at,
                        updated_command_config.max_duration_seconds,
                    ),
                    "goal_revision": next_revision,
                    "goal_revisions": [*record.goal_revisions, revision],
                    "final_checks": checks,
                    "work_items": work_items,
                    "status": record.status
                    if record.status in {"blocked", "needs_user"}
                    else "active",
                    "control": GoalPlusControl(
                        state="active"
                        if record.status == "complete"
                        else record.control.state,
                        reason=None
                        if record.status == "complete"
                        else record.control.reason,
                        version=record.control.version + 1,
                    ),
                    "phase": "intake",
                    "triage": None,
                    "spec_draft": None,
                    "linked_search": None,
                    "next_action": GoalPlusNextAction(
                        kind="record_triage",
                        description=(
                            "在继续工作或进入 Search Mode 前，重新分类修订后的原始目标。"
                        ),
                        required=True,
                        metadata={"goal_revision": next_revision},
                    ),
                    "entrypoint_status": (
                        "host_verified"
                        if host_invocation is not None
                        else record.entrypoint_status
                    ),
                    "host_command_invocations": (
                        [
                            *record.host_command_invocations,
                            host_invocation.model_copy(deep=True),
                        ]
                        if host_invocation is not None
                        else record.host_command_invocations
                    ),
                    "updated_at": now,
                }
            )
            self.state.write_goal(updated)
            self.state.append_event(
                goal_plus_id,
                "goal_updated",
                {
                    "previous_revision": record.goal_revision,
                    "goal_revision": next_revision,
                    "previous_raw_goal": record.raw_goal,
                    "raw_goal": updated_raw_goal,
                    "previous_command_config": record.command_config.model_dump(
                        mode="json", exclude_none=True
                    ),
                    "command_config": updated_command_config.model_dump(
                        mode="json", exclude_none=True
                    ),
                    "reason": revision.reason,
                    "host_invocation": (
                        host_invocation.model_dump(mode="json")
                        if host_invocation is not None
                        else None
                    ),
                },
            )
            return updated

    def edit_from_host_command(
        self,
        goal_plus_id: str,
        raw_goal: str,
        *,
        expected_revision: int,
        invocation: GoalPlusHostCommandInvocation | dict[str, Any],
        reason: str | None = None,
    ) -> GoalPlusRecord:
        parsed = (
            invocation
            if isinstance(invocation, GoalPlusHostCommandInvocation)
            else GoalPlusHostCommandInvocation.model_validate(invocation)
        )
        return self.update_goal(
            goal_plus_id,
            raw_goal,
            expected_revision,
            reason,
            host_invocation=parsed,
        )
