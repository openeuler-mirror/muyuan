"""Read-only Goal admission and durable tool-target authorization.

Native caller identity remains a host fact. A target is only a reference, not
proof that its caller owns the original native session.
"""

from goal_plus.domain.goal import GoalPlusHostCommandInvocation, GoalPlusRecord
from goal_plus.domain.agents import AgentHarnessKind
from goal_plus.domain.goal_authorization import (
    GOAL_PLUS_ENTRYPOINT_REQUIRED,
    GOAL_SCOPED_TOOL_SUFFIXES,
    LINKED_SEARCH_TOOL_SUFFIXES,
    SEARCH_RECOVERY_TOOL_SUFFIXES,
    has_host_invocation,
    is_terminal_goal,
    stopped_goal_message,
    tool_matches,
)
from goal_plus.ports.goal_authorization import GoalAuthorizationSource, GoalToolTarget
from goal_plus.ports.scope import ScopeAttachment


class GoalAuthorization:
    def __init__(self, source: GoalAuthorizationSource) -> None:
        self._source = source

    def search_agent_harness(self, goal_plus_id: str) -> AgentHarnessKind:
        record = self.require_active_target(
            GoalToolTarget("goal_plus_search_freeze_spec", goal_plus_id=goal_plus_id)
        )
        session = record.active_session
        if session is None or session.state != "attached":
            raise ValueError("Search requires an attached Main native host session")
        if session.agent_harness == "codex":
            agent_harness = "codex"
        elif session.agent_harness == "pi":
            agent_harness = "pi"
        else:
            raise ValueError("Search Main native host must be codex or pi")
        if not any(
            item.agent_harness == agent_harness and item.session_id == session.session_id
            for item in record.host_command_invocations
        ):
            raise ValueError("Search native host must match its verified Main invocation")
        return agent_harness

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
                item.agent_harness == invocation.agent_harness
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
        if len(nonterminal) == 1 and (
            nonterminal[0].entrypoint_status == "host_verified"
            and nonterminal[0].host_command_invocations
            and (
                nonterminal[0].status != "active"
                or nonterminal[0].control.state != "active"
            )
        ):
            raise RuntimeError(stopped_goal_message(nonterminal[0]))
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

    def require_active_target(
        self, target: GoalToolTarget, *, settlement: bool = False
    ) -> GoalPlusRecord:
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
        allowed_control = {"active", "paused"} if settlement else {"active"}
        if (
            record.control.state not in allowed_control
            or is_terminal_goal(record)
            or (not settlement and record.status != "active")
            or record.entrypoint_status != "host_verified"
            or not record.host_command_invocations
        ):
            if (
                record.entrypoint_status == "host_verified"
                and record.host_command_invocations
            ):
                # A host-verified record that stopped carries its own
                # authoritative reason; do not misreport it as an
                # entrypoint violation.
                raise RuntimeError(stopped_goal_message(record))
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

    def require_main_search_session(
        self, run_id: str, *, agent_harness: AgentHarnessKind, session_id: str,
        execution: bool,
    ) -> GoalPlusRecord:
        """Check a host-supplied caller; read/cleanup never grants execution."""
        owner = self.run_owner(run_id)
        if owner is None:
            raise PermissionError("GoalPlusOwnershipMismatch: session control requires a linked Search run")
        record = self.require_main_goal_session(owner, agent_harness=agent_harness, session_id=session_id, execution=execution)
        if execution:
            self.require_active_target(GoalToolTarget("search_start_agent_session", run_id=run_id))
            self.require_search_execution(run_id)
        return record

    def require_main_goal_session(
        self, goal_plus_id: str, *, agent_harness: AgentHarnessKind, session_id: str, execution: bool,
    ) -> GoalPlusRecord:
        record = self._source.read_goal(goal_plus_id)
        binding = record.active_session
        if (
            binding is None
            or binding.agent_harness != agent_harness
            or binding.session_id != session_id
            or not any(
                item.agent_harness == agent_harness and item.session_id == session_id
                for item in record.host_command_invocations
            )
        ):
            raise PermissionError("MainSessionMismatch: session control requires the owning native Main")
        if execution:
            self.require_active_target(GoalToolTarget("goal_plus_prepare_final_check", goal_plus_id=goal_plus_id))
            if binding.state != "attached":
                raise PermissionError("MainSessionMismatch: execution requires an attached native Main")
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
            tool_matches(target.tool_name, "goal_plus_search_create")
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
                if any(item.run_id == run_id for item in record.search_create_requests):
                    raise RuntimeError("SearchLinkRequired: Search creation has not been linked to its Goal")
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
                if not any(
                    task.goal_revision == record.goal_revision for task in linked
                ):
                    raise RuntimeError(
                        "GoalPlusResumeRequired: linked Search run belongs to "
                        f"an old Goal revision (run_id={run_id}, current "
                        f"revision={record.goal_revision}). Do not reuse "
                        "old-revision runs; create a new Search for the "
                        "current revision."
                    )
                raise RuntimeError(
                    stopped_goal_message(record)
                    + f" Run {run_id} remains linked; its settled Evidence "
                    "is preserved."
                )
