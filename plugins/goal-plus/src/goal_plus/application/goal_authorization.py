"""Read-only Goal admission and durable tool-target authorization.

Native caller identity remains a host fact. A target is only a reference, not
proof that its caller owns the original native session.
"""

from goal_plus.domain.goal import GoalPlusHostCommandInvocation, GoalPlusRecord
from goal_plus.domain.goal_authorization import (
    GOAL_PLUS_ENTRYPOINT_REQUIRED,
    GOAL_SCOPED_TOOL_SUFFIXES,
    LINKED_SEARCH_TOOL_SUFFIXES,
    SEARCH_RECOVERY_TOOL_SUFFIXES,
    has_host_invocation,
    is_terminal_goal,
    tool_matches,
)
from goal_plus.ports.goal_authorization import GoalAuthorizationSource, GoalToolTarget
from goal_plus.ports.scope import ScopeAttachment


class GoalAuthorization:
    def __init__(self, source: GoalAuthorizationSource) -> None:
        self._source = source

    def nonterminal_records(self) -> list[GoalPlusRecord]:
        records = [
            self._source.read_goal(goal_id)
            for goal_id in sorted(self._source.goal_ids())
        ]
        return [record for record in records if not is_terminal_goal(record)]

    def record_for_invocation(self, invocation_id: str) -> GoalPlusRecord | None:
        matches = []
        for goal_id in sorted(self._source.goal_ids(), reverse=True):
            record = self._source.read_goal(goal_id)
            if has_host_invocation(record, invocation_id):
                matches.append(record)
        return next(
            (record for record in matches if not is_terminal_goal(record)),
            matches[0] if matches else None,
        )

    def require_start(
        self, invocation: GoalPlusHostCommandInvocation
    ) -> GoalPlusRecord | None:
        """Return a replay or permit creation; caller holds the entrypoint lock."""
        if invocation.action != "start":
            raise ValueError(
                "Goal Plus start requires a host invocation with action=start"
            )
        existing = self.record_for_invocation(invocation.invocation_id)
        if existing is not None and not is_terminal_goal(existing):
            return existing
        session_records = [
            record
            for record in self.nonterminal_records()
            if any(
                item.host == invocation.host
                and item.session_id == invocation.session_id
                for item in record.host_command_invocations
            )
        ]
        if session_records:
            active_ids = ", ".join(record.goal_plus_id for record in session_records)
            raise RuntimeError(
                "This native host session already owns a nonterminal Goal Plus "
                f"record: {active_ids}. Use the exact host edit or resume command "
                "for that session instead."
            )
        return None

    def require_unique_active(self) -> GoalPlusRecord:
        """Preserve the explicit legacy query; never use it to infer a target."""
        nonterminal = self.nonterminal_records()
        if (
            len(nonterminal) != 1
            or nonterminal[0].status != "active"
            or nonterminal[0].control.state != "active"
            or nonterminal[0].entrypoint_status != "host_verified"
            or not nonterminal[0].host_command_invocations
        ):
            detail = "none" if not nonterminal else "ambiguous or unverified"
            raise RuntimeError(
                f"{GOAL_PLUS_ENTRYPOINT_REQUIRED} Active host-verified records: {detail}."
            )
        return nonterminal[0]

    def require_active_target(self, target: GoalToolTarget) -> GoalPlusRecord:
        goal_plus_id = target.goal_plus_id
        if goal_plus_id is None:
            run_id = self.target_run_id(target)
            goal_plus_id = self.run_owner(run_id) if run_id else None
        if goal_plus_id is None:
            raise RuntimeError(
                "GoalPlusOwnershipMismatch: tool input does not identify a "
                "host-authorized Goal Plus record"
            )
        try:
            record = self._source.read_goal(goal_plus_id)
        except FileNotFoundError as exc:
            if not self.nonterminal_records():
                raise RuntimeError(
                    f"{GOAL_PLUS_ENTRYPOINT_REQUIRED} Active host-verified records: none."
                ) from exc
            raise RuntimeError(
                "GoalPlusOwnershipMismatch: tool goal_plus_id does not identify "
                "a Goal Plus record"
            ) from exc
        if (
            record.status != "active"
            or record.control.state != "active"
            or record.entrypoint_status != "host_verified"
            or not record.host_command_invocations
        ):
            raise RuntimeError(
                f"{GOAL_PLUS_ENTRYPOINT_REQUIRED} Active host-verified record: "
                f"{record.goal_plus_id}."
            )
        error = self.ownership_error(record, target)
        if error is not None:
            raise RuntimeError(error)
        return record

    def require_scope_attachment(self, goal_plus_id: str, attachment: ScopeAttachment | None) -> GoalPlusRecord:
        record = self.require_active_target(GoalToolTarget("host_execution_scope_open", goal_plus_id))
        if attachment is None or not (
            attachment.kind == "goal" and attachment.identity == goal_plus_id
            or attachment.kind == "search_run" and any(task.run_id == attachment.identity for task in record.search_tasks)
        ):
            raise RuntimeError("GoalPlusOwnershipMismatch: execution scope run is not linked to Goal")
        return record

    def ownership_error(
        self, record: GoalPlusRecord, target: GoalToolTarget
    ) -> str | None:
        if any(
            tool_matches(target.tool_name, suffix)
            for suffix in GOAL_SCOPED_TOOL_SUFFIXES
        ):
            if target.goal_plus_id != record.goal_plus_id:
                return (
                    "GoalPlusOwnershipMismatch: tool goal_plus_id does not match "
                    "the host-authorized Goal Plus record"
                )
        requires_linked_run = any(
            tool_matches(target.tool_name, suffix)
            for suffix in LINKED_SEARCH_TOOL_SUFFIXES
        )
        is_successor_create = (
            tool_matches(target.tool_name, "search_create")
            and (target.source_run_id is not None or target.source_run_requested)
        )
        if not requires_linked_run and not is_successor_create:
            return None
        run_id = (
            target.source_run_id
            if is_successor_create and target.source_run_id
            else self.target_run_id(target)
        )
        current_revision_only = any(
            tool_matches(target.tool_name, suffix)
            for suffix in SEARCH_RECOVERY_TOOL_SUFFIXES
        )
        if run_id is None or not any(
            task.run_id == run_id
            and (
                not current_revision_only or task.goal_revision == record.goal_revision
            )
            for task in record.search_tasks
        ):
            return (
                "GoalPlusOwnershipMismatch: Search operation requires a run linked "
                "to the host-authorized Goal Plus record"
            )
        return None

    def target_run_id(self, target: GoalToolTarget) -> str | None:
        if target.run_id is not None:
            return target.run_id
        if target.agent_session_id is not None:
            matches = self._source.session_run_ids(target.agent_session_id)
            if len(matches) == 1:
                return matches[0]
        if target.pool_id is not None:
            return self._source.pool_run_id(target.pool_id)
        return None

    def run_owner(self, run_id: str) -> str | None:
        for goal_id in sorted(self._source.goal_ids()):
            record = self._source.read_goal(goal_id)
            if any(task.run_id == run_id for task in record.search_tasks):
                return record.goal_plus_id
        return None

    def require_search_execution(
        self, run_id: str, *, settlement: bool = False
    ) -> None:
        """Historical links retain authority; truly standalone Search is allowed."""
        for goal_id in self._source.goal_ids():
            record = self._source.read_goal(goal_id)
            linked = [task for task in record.search_tasks if task.run_id == run_id]
            if not linked:
                continue
            allowed_control = {"active", "paused"} if settlement else {"active"}
            if (
                record.control.state not in allowed_control
                or is_terminal_goal(record)
                or (not settlement and record.status != "active")
                or not any(
                    task.goal_revision == record.goal_revision for task in linked
                )
            ):
                raise RuntimeError(
                    "GoalPlusResumeRequired: linked Search is stopped or belongs to an old revision"
                )
