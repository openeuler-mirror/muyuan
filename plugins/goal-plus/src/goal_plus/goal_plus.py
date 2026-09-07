from __future__ import annotations
from goal_plus.domain.goal_commands import (
    DEFAULT_WORK_LAUNCH_TTL_SECONDS,
)

from pathlib import Path
from typing import Any

from goal_plus.domain.agents import AgentHostKind
from goal_plus.domain.goal import (
    GoalPlusActiveSession,
    GoalPlusFinalCheckerHost,
    GoalPlusGateEvent,
    GoalPlusGateResult,
    GoalPlusHostCommandInvocation,
    GoalPlusHostSessionDecision,
    GoalPlusHostSessionDelivery,
    GoalPlusHostSessionEvent,
    GoalPlusHostSessionInteraction,
    GoalPlusNextAction,
    GoalPlusRecord,
    GoalPlusSpecDraft,
    GoalPlusStatus,
    GoalPlusTriage,
    GoalPlusWorkEventKind,
)
from goal_plus.domain.goal_authorization import (
    is_terminal_goal,
)
from goal_plus.projections.usage import normalize_pi_usage
from goal_plus.paths import DEFAULT_RUNTIME_ROOT
from goal_plus.ports.goal_authorization import GoalToolTarget


class FileGoalPlusRuntime:
    """Small file-backed state machine for goal-plus orchestration."""

    def __init__(self, root_dir: Path | str = DEFAULT_RUNTIME_ROOT) -> None:
        from goal_plus.bootstrap import build_goal_authorization

        self.root_dir = Path(root_dir).resolve()
        self.goals_dir = self.root_dir / "goal-plus"
        self.authorization, self._queries = build_goal_authorization(self.root_dir)
        from goal_plus.bootstrap import build_goal_mutations

        self.state, self.goal_revisions, self.goal_control, self.goal_dispatch = (
            build_goal_mutations(self.root_dir, self.authorization)
        )
        from goal_plus.bootstrap import build_goal_closeout

        (
            self.goal_search,
            self.goal_completion,
            self.goal_gate,
            self.goal_reconciliation,
        ) = build_goal_closeout(self.root_dir, self.state, self.authorization)
        from goal_plus.bootstrap import build_goal_observation

        self.observations = build_goal_observation(self.state)

    def create_goal(
        self,
        raw_goal: str,
        source_path: str | None = None,
        policy: dict[str, Any] | None = None,
        *,
        host_invocation: GoalPlusHostCommandInvocation | None = None,
    ) -> GoalPlusRecord:
        return self.goal_revisions.create_goal(
            raw_goal, source_path, policy, host_invocation=host_invocation
        )

    def record_orchestrator_usage(
        self,
        goal_plus_id: str,
        *,
        host: str,
        usage: dict[str, Any],
        started_at: str | None = None,
    ) -> dict[str, Any]:
        return self.observations.record(goal_plus_id, host=host,
            usage=normalize_pi_usage(usage, scope="orchestrator_session"), started_at=started_at)

    def start_from_host_command(
        self,
        raw_goal: str,
        *,
        invocation: GoalPlusHostCommandInvocation | dict[str, Any],
        source_path: str | None = None,
        policy: dict[str, Any] | None = None,
    ) -> GoalPlusRecord:
        return self.goal_revisions.start_from_host_command(
            raw_goal, invocation=invocation, source_path=source_path, policy=policy
        )

    def status(self, goal_plus_id: str) -> GoalPlusRecord:
        return self.state.read_goal(goal_plus_id)

    def require_host_verified_active_goal(self) -> GoalPlusRecord:
        return self.authorization.require_unique_active()

    def require_active_tool_target(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> GoalPlusRecord:
        return self.authorization.require_active_target(
            GoalToolTarget.from_input(tool_name, tool_input)
        )

    @staticmethod
    def is_terminal(record: GoalPlusRecord) -> bool:
        return is_terminal_goal(record)

    def update_goal(
        self,
        goal_plus_id: str,
        raw_goal: str,
        expected_revision: int,
        reason: str | None = None,
        *,
        host_invocation: GoalPlusHostCommandInvocation | None = None,
    ) -> GoalPlusRecord:
        return self.goal_revisions.update_goal(
            goal_plus_id,
            raw_goal,
            expected_revision,
            reason,
            host_invocation=host_invocation,
        )

    def edit_from_host_command(
        self,
        goal_plus_id: str,
        raw_goal: str,
        *,
        expected_revision: int,
        invocation: GoalPlusHostCommandInvocation | dict[str, Any],
        reason: str | None = None,
    ) -> GoalPlusRecord:
        return self.goal_revisions.edit_from_host_command(
            goal_plus_id,
            raw_goal,
            expected_revision=expected_revision,
            invocation=invocation,
            reason=reason,
        )

    def control_from_host_command(
        self,
        goal_plus_id: str,
        *,
        invocation: GoalPlusHostCommandInvocation | dict[str, Any],
    ) -> GoalPlusRecord:
        return self.goal_control.control_from_host_command(
            goal_plus_id, invocation=invocation
        )

    def stop_for_host_retry(
        self, goal_plus_id: str, expectation: dict[str, Any]
    ) -> GoalPlusRecord:
        return self.goal_control.stop_for_host_retry(goal_plus_id, expectation)

    def handle_host_session_event(
        self,
        goal_plus_id: str,
        *,
        host: AgentHostKind,
        session_id: str,
        event: GoalPlusHostSessionEvent,
        delivery: GoalPlusHostSessionDelivery,
        interaction: GoalPlusHostSessionInteraction,
    ) -> GoalPlusHostSessionDecision:
        return self.goal_control.handle_host_session_event(
            goal_plus_id,
            host=host,
            session_id=session_id,
            event=event,
            delivery=delivery,
            interaction=interaction,
        )

    def activate_session(
        self,
        goal_plus_id: str,
        session: GoalPlusActiveSession | dict[str, Any],
    ) -> GoalPlusRecord:
        return self.goal_control.activate_session(goal_plus_id, session)

    def detach_session(
        self,
        goal_plus_id: str,
        *,
        session_id: str,
        reason: str,
    ) -> GoalPlusRecord:
        return self.goal_control.detach_session(
            goal_plus_id, session_id=session_id, reason=reason
        )

    def settle_nonactive_stop(
        self,
        goal_plus_id: str,
        *,
        session_id: str,
    ) -> GoalPlusRecord | None:
        return self.goal_control.settle_nonactive_stop(
            goal_plus_id, session_id=session_id
        )

    def record_session_gate_skipped(
        self,
        goal_plus_id: str,
        reason: str,
        current_session_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> GoalPlusRecord:
        return self.goal_control.record_session_gate_skipped(
            goal_plus_id, reason, current_session_id, context
        )

    def list_events(self, goal_plus_id: str) -> list[dict[str, Any]]:
        return self._queries.read_events(goal_plus_id)

    def record_triage(
        self,
        goal_plus_id: str,
        triage: GoalPlusTriage | dict[str, Any],
    ) -> GoalPlusRecord:
        return self.goal_search.record_triage(goal_plus_id, triage)

    def record_work_event(
        self,
        goal_plus_id: str,
        work_item_id: str,
        event: GoalPlusWorkEventKind,
        summary: str,
        *,
        host: str | None = None,
        task_name: str | None = None,
        agent_id: str | None = None,
        transcript_path: str | None = None,
        evidence: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        attempt_id: str | None = None,
        generation: int | None = None,
        launch_ttl_seconds: int = DEFAULT_WORK_LAUNCH_TTL_SECONDS,
    ) -> GoalPlusRecord:
        return self.goal_dispatch.record_work_event(
            goal_plus_id,
            work_item_id,
            event,
            summary,
            host=host,
            task_name=task_name,
            agent_id=agent_id,
            transcript_path=transcript_path,
            evidence=evidence,
            metadata=metadata,
            attempt_id=attempt_id,
            generation=generation,
            launch_ttl_seconds=launch_ttl_seconds,
        )

    def save_spec_draft(
        self,
        goal_plus_id: str,
        spec_draft: GoalPlusSpecDraft | dict[str, Any],
    ) -> GoalPlusRecord:
        return self.goal_search.save_spec_draft(goal_plus_id, spec_draft)

    def link_search_run(
        self,
        goal_plus_id: str,
        frozen_spec_id: str,
        run_id: str,
    ) -> GoalPlusRecord:
        return self.goal_search.link_search_run(goal_plus_id, frozen_spec_id, run_id)

    def record_search_result(
        self,
        goal_plus_id: str,
        run_id: str,
        selected_candidate_id: str | None = None,
        report_path: str | None = None,
        promotion_artifact_path: str | None = None,
        summary: str | None = None,
    ) -> GoalPlusRecord:
        return self.goal_search.record_search_result(
            goal_plus_id,
            run_id,
            selected_candidate_id,
            report_path,
            promotion_artifact_path,
            summary,
        )

    def prepare_final_check(
        self,
        goal_plus_id: str,
        checker_host: GoalPlusFinalCheckerHost,
    ) -> dict[str, Any]:
        return self.goal_completion.prepare_final_check(goal_plus_id, checker_host)

    def submit_final_check(
        self,
        goal_plus_id: str,
        check_id: str,
        goal_revision: int,
        verdict: str,
        summary: str,
        findings: list[dict[str, Any]] | None = None,
        evidence: list[dict[str, Any]] | None = None,
        checker_metadata: dict[str, Any] | None = None,
    ) -> GoalPlusRecord:
        return self.goal_completion.submit_final_check(
            goal_plus_id,
            check_id,
            goal_revision,
            verdict,
            summary,
            findings,
            evidence,
            checker_metadata,
        )

    def set_status(
        self,
        goal_plus_id: str,
        status: GoalPlusStatus,
        reason: str | None = None,
        evidence: list[dict[str, Any]] | None = None,
        next_action: GoalPlusNextAction | dict[str, Any] | None = None,
    ) -> GoalPlusRecord:
        return self.goal_completion.set_status(
            goal_plus_id, status, reason, evidence, next_action
        )

    def gate(
        self,
        goal_plus_id: str,
        event: GoalPlusGateEvent,
        context: dict[str, Any],
    ) -> GoalPlusGateResult:
        return self.goal_gate.gate(goal_plus_id, event, context)

    def reconciliation(self, goal_plus_id: str) -> dict[str, Any]:
        return self.goal_reconciliation.reconciliation(goal_plus_id)

    def require_search_execution(
        self, run_id: str, *, settlement: bool = False
    ) -> None:
        self.authorization.require_search_execution(run_id, settlement=settlement)

    def reconciliation_context(self, goal_plus_id: str) -> str:
        return self.goal_reconciliation.reconciliation_context(goal_plus_id)
