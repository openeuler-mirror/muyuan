"""Host-authorized control and session transitions."""

from __future__ import annotations
from goal_plus.domain.agents import AgentHarnessKind


from goal_plus.domain.goal import (
    GoalPlusActiveSession,
    GoalPlusControl,
    GoalPlusHostCommandInvocation,
    GoalPlusHostSessionDecision,
    GoalPlusHostSessionDelivery,
    GoalPlusHostSessionEvent,
    GoalPlusHostSessionInteraction,
    GoalPlusRecord,
)


from goal_plus.domain.goal_authorization import (
    GOAL_PLUS_ENTRYPOINT_REQUIRED as GOAL_PLUS_ENTRYPOINT_REQUIRED,
    has_host_invocation,
    is_terminal_goal,
    require_control_session,
)


from typing import Any
from goal_plus.domain.goal_policy import with_host_invocation
from goal_plus.application.goal_authorization import GoalAuthorization
from goal_plus.ports.goal_state import GoalState
from goal_plus.ports.clock import Clock


class GoalControl:
    def __init__(
        self, state: GoalState, clock: Clock, authorization: GoalAuthorization
    ) -> None:
        self.state, self.clock, self.authorization = state, clock, authorization

    def control_from_host_command(
        self,
        goal_plus_id: str,
        *,
        invocation: GoalPlusHostCommandInvocation | dict[str, Any],
    ) -> GoalPlusRecord:
        with self.state.lock_goal(goal_plus_id):
            parsed = GoalPlusHostCommandInvocation.model_validate(invocation)
            if parsed.action not in {"summary", "pause", "resume", "clear"}:
                raise ValueError("unsupported Goal Plus control action")
            record = self.state.read_goal(goal_plus_id)
            require_control_session(record, parsed)
            if parsed.action == "summary":
                return record
            # A replay acknowledges the original request; it never re-applies it to
            # a later pause/edit/clear state.
            if has_host_invocation(record, parsed.invocation_id):
                return record
            if is_terminal_goal(record) and parsed.action != "clear":
                raise RuntimeError(
                    "A terminal or cleared Goal Plus task cannot resume or pause"
                )
            expectation = parsed.resume_expectation
            if expectation is not None:
                if (
                    expectation.goal_plus_id != record.goal_plus_id
                    or expectation.goal_revision != record.goal_revision
                    or expectation.session_id != parsed.session_id
                    or expectation.control_version != record.control.version
                    or record.status != "active"
                    or record.control.state != "paused"
                    or record.control.reason
                    not in {"execution_lost", "harness_interruption"}
                ):
                    raise RuntimeError(
                        "AutomaticResumeRejected: task identity, stop version or recovery authority changed"
                    )
            state, reason = {
                "pause": ("paused", "user_pause"),
                "resume": ("active", None),
                "clear": ("closed", "user_clear"),
            }[parsed.action]
            changed = (state, reason) != (record.control.state, record.control.reason)
            control = GoalPlusControl(
                state=state,
                reason=reason,
                version=record.control.version + int(changed),
            )
            status = record.status
            if parsed.action == "clear":
                status = "abandoned"
            elif parsed.action == "resume" and status == "blocked":
                status = "active"
            updated = with_host_invocation(
                record, parsed, self.clock.timestamp()
            ).model_copy(
                update={
                    "status": status,
                    "control": control,
                    "next_action": None
                    if parsed.action == "clear"
                    else record.next_action,
                }
            )
            self.state.write_goal(updated)
            self.state.append_event(
                goal_plus_id,
                "control_changed",
                {
                    "action": parsed.action,
                    "control": control.model_dump(mode="json"),
                    "invocation_id": parsed.invocation_id,
                    "automatic": expectation is not None,
                },
            )
            return updated

    def stop_for_host_retry(
        self, goal_plus_id: str, expectation: dict[str, Any]
    ) -> GoalPlusRecord:
        with self.state.lock_goal(goal_plus_id):
            """Host-only CAS before an owned timeout, never a model tool.

            Recording this BEFORE the host interrupts distinguishes its timeout
            from a user abort. A later pause/clear invalidates the admission version.
            """
            record = self.state.read_goal(goal_plus_id)
            actual = {
                "goal_plus_id": record.goal_plus_id,
                "goal_revision": record.goal_revision,
                "control_version": record.control.version,
                "session_id": record.active_session.session_id
                if record.active_session
                else None,
            }
            if (
                actual != expectation
                or record.status != "active"
                or record.control.state != "active"
                or record.active_session is None
                or record.active_session.state != "attached"
            ):
                raise RuntimeError(
                    "HostInterruptionRejected: task changed or is already stopped"
                )
            updated = record.model_copy(
                update={
                    "control": GoalPlusControl(
                        state="paused",
                        reason="harness_interruption",
                        version=record.control.version + 1,
                    ),
                    "updated_at": self.clock.timestamp(),
                }
            )
            self.state.write_goal(updated)
            self.state.append_event(
                goal_plus_id,
                "host_interruption",
                {
                    "expected": expectation,
                    "control": updated.control.model_dump(mode="json"),
                },
            )
            return updated

    def handle_host_session_event(
        self,
        goal_plus_id: str,
        *,
        agent_harness: AgentHarnessKind,
        session_id: str,
        event: GoalPlusHostSessionEvent,
        delivery: GoalPlusHostSessionDelivery,
        interaction: GoalPlusHostSessionInteraction,
    ) -> GoalPlusHostSessionDecision:
        with self.state.lock_goal(goal_plus_id):
            """Apply the host-neutral native-session continuation state machine."""

            record = self.state.read_goal(goal_plus_id)
            active = record.active_session
            decision = "stop"
            reason = "session_unavailable"
            session_state: str = active.state if active is not None else "unavailable"

            if is_terminal_goal(record):
                session_state = "terminal"
                reason = "goal_terminal"
            elif (
                active is None or active.agent_harness != agent_harness or active.session_id != session_id
            ):
                reason = "session_mismatch"
            elif active.state in {"stale", "detached"}:
                reason = f"session_{active.state}"
            elif event in {"interrupt", "execution_lost"}:
                now = self.clock.timestamp()
                stop_reason = (
                    "user_interrupt" if event == "interrupt" else "execution_lost"
                )
                if record.control.state == "active":
                    record = record.model_copy(
                        update={
                            "control": GoalPlusControl(
                                state="paused",
                                reason=stop_reason,
                                version=record.control.version + 1,
                            ),
                            "updated_at": now,
                        }
                    )
                    self.state.write_goal(record)
                    self.state.append_event(
                        goal_plus_id,
                        "session_paused",
                        {
                            "agent_harness": agent_harness,
                            "session_id": session_id,
                            "reason": stop_reason,
                        },
                    )
                reason = record.control.reason or stop_reason
            elif record.control.state != "active" or record.status == "blocked":
                decision = "wait_for_user"
                reason = record.control.reason or "explicit_resume_required"
            elif event == "user_turn":
                decision = "continue_after_user"
                reason = "user_turn"
            elif event == "continue_confirmed":
                # Compatibility of the event vocabulary is not permission to resume.
                # Only a host-command invocation can change task control state.
                decision = "wait_for_user"
                reason = "explicit_resume_required"
            elif event == "session_start":
                decision = "wait_for_user"
                reason = "host_requires_user_turn"

            result = GoalPlusHostSessionDecision(
                goal_plus_id=goal_plus_id,
                session_id=session_id,
                record_status=record.status,
                session_state=session_state,
                decision=decision,
                reason=reason,
                control=record.control,
            )
            self.state.append_event(
                goal_plus_id,
                "session_continuation_decided",
                {
                    "agent_harness": agent_harness,
                    "event": event,
                    "delivery": delivery,
                    "interaction": interaction,
                    **result.model_dump(mode="json"),
                },
            )
            return result

    def activate_session(
        self,
        goal_plus_id: str,
        session: GoalPlusActiveSession | dict[str, Any],
    ) -> GoalPlusRecord:
        with self.state.lock_goal(goal_plus_id):
            record = self.state.read_goal(goal_plus_id)
            now = self.clock.timestamp()
            if isinstance(session, GoalPlusActiveSession):
                attached_at = (
                    record.active_session.attached_at
                    if record.active_session is not None
                    and record.active_session.session_id == session.session_id
                    else session.attached_at
                )
                parsed = session.model_copy(
                    update={
                        "state": "attached",
                        "attached_at": attached_at,
                        "last_seen_at": now,
                    }
                )
            else:
                data = dict(session)
                existing = record.active_session
                if (
                    existing is not None
                    and existing.session_id == data.get("session_id")
                    and existing.agent_harness == data.get("agent_harness")
                ):
                    data.setdefault("attached_at", existing.attached_at)
                else:
                    data.setdefault("attached_at", now)
                data.setdefault("last_seen_at", now)
                data.setdefault("state", "attached")
                parsed = GoalPlusActiveSession.model_validate(data)

            updated = record.model_copy(
                update={
                    "active_session": parsed,
                    "updated_at": now,
                }
            )
            self.state.write_goal(updated)
            self.state.append_event(
                goal_plus_id,
                "session_activated",
                parsed.model_dump(mode="json"),
            )
            return updated

    def detach_session(
        self,
        goal_plus_id: str,
        *,
        session_id: str,
        reason: str,
    ) -> GoalPlusRecord:
        with self.state.lock_goal(goal_plus_id):
            record = self.state.read_goal(goal_plus_id)
            active = record.active_session
            if active is None or active.session_id != session_id:
                return record
            now = self.clock.timestamp()
            detached = active.model_copy(
                update={
                    "state": "detached",
                    "last_seen_at": now,
                    "metadata": {**active.metadata, "detach_reason": reason},
                }
            )
            updated = record.model_copy(
                update={"active_session": detached, "updated_at": now}
            )
            self.state.write_goal(updated)
            self.state.append_event(
                goal_plus_id,
                "session_detached",
                {
                    "session_id": session_id,
                    "reason": reason,
                },
            )
            return updated

    def settle_nonactive_stop(
        self,
        goal_plus_id: str,
        *,
        session_id: str,
    ) -> GoalPlusRecord | None:
        with self.state.lock_goal(goal_plus_id):
            """Consume one non-active Stop summary and detach its host session."""
            record = self.state.read_goal(goal_plus_id)
            if not is_terminal_goal(record):
                return None
            if record.status == "active":
                raise RuntimeError(
                    f"Goal Plus {goal_plus_id} is active; use gate() for its Stop event"
                )
            active = record.active_session
            if (
                active is None
                or active.session_id != session_id
                or active.state != "attached"
            ):
                return None

            now = self.clock.timestamp()
            counters = dict(record.hook_counters)
            counters["stop"] = counters.get("stop", 0) + 1
            detach_reason = "nonactive_stop_summary_emitted"
            detached = active.model_copy(
                update={
                    "state": "detached",
                    "last_seen_at": now,
                    "metadata": {
                        **active.metadata,
                        "detach_reason": detach_reason,
                        "stop_summary_emitted_at": now,
                    },
                }
            )
            updated = record.model_copy(
                update={
                    "active_session": detached,
                    "hook_counters": counters,
                    "updated_at": now,
                }
            )
            self.state.write_goal(updated)
            self.state.append_event(
                goal_plus_id,
                "gate_allowed",
                {"event": "stop", "reason": None},
            )
            self.state.append_event(
                goal_plus_id,
                "session_detached",
                {
                    "session_id": session_id,
                    "reason": detach_reason,
                },
            )
            return updated

    def record_session_gate_skipped(
        self,
        goal_plus_id: str,
        reason: str,
        current_session_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> GoalPlusRecord:
        with self.state.lock_goal(goal_plus_id):
            record = self.state.read_goal(goal_plus_id)
            self.state.append_event(
                goal_plus_id,
                "session_gate_skipped",
                {
                    "reason": reason,
                    "current_session_id": current_session_id,
                    "active_session": (
                        record.active_session.model_dump(mode="json")
                        if record.active_session is not None
                        else None
                    ),
                    "context": context or {},
                },
            )
            return record
