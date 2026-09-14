"""Independent final-check and terminal Goal transitions."""

from __future__ import annotations
from typing import Any

from pydantic import TypeAdapter

from goal_plus.domain.goal import (
    GoalPlusControl,
    GoalPlusFinalCheck,
    GoalPlusFinalCheckerIdentity,
    GoalPlusFinalCheckerHost,
    GoalPlusNextAction,
    GoalPlusRecord,
    GoalPlusStatus,
)

from goal_plus.domain.goal_authorization import (
    TERMINAL_STATUSES,
    is_terminal_goal,
)


from goal_plus.ports.goal_state import GoalState
from goal_plus.ports.clock import Clock
from goal_plus.ports.goal_search import GoalSearchArtifacts
from goal_plus.application.goal_final_check_launch import final_check_launch
from goal_plus.domain.goal_closeout import (
    require_current_search_result,
    final_check_mode,
    latest_final_check,
)
from goal_plus.application.goal_reconciliation import GoalReconciliation


class GoalCompletion:
    def __init__(
        self,
        state: GoalState,
        clock: Clock,
        artifacts: GoalSearchArtifacts,
        reconciliation: GoalReconciliation,
    ) -> None:
        self.state, self.clock = state, clock
        self.artifacts = artifacts
        self.reconciliation = reconciliation

    def require_native_checker(
        self, identity: GoalPlusFinalCheckerIdentity, native_session_id: str,
    ) -> GoalPlusRecord:
        record = self.state.read_goal(identity.goal_plus_id)
        check = next((item for item in record.final_checks if item.check_id == identity.check_id), None)
        if (
            check is None or check.goal_revision != identity.goal_revision
            or record.goal_revision != identity.goal_revision
            or not native_session_id
            or check.checker_metadata.get("native_session_id") != native_session_id
        ):
            raise PermissionError("FinalCheckerIdentityMismatch: native checker does not own this check")
        return record

    def bind_native_checker(
        self, identity: GoalPlusFinalCheckerIdentity, *, agent_harness: GoalPlusFinalCheckerHost,
        native_session_id: str, scope_id: str, control_version: int,
    ) -> None:
        with self.state.lock_goal(identity.goal_plus_id):
            record = self.state.read_goal(identity.goal_plus_id)
            check = next((item for item in record.final_checks if item.check_id == identity.check_id), None)
            if (
                record.status != "active" or record.control.state != "active"
                or record.control.version != control_version
                or record.goal_revision != identity.goal_revision
                or check is None or check.goal_revision != identity.goal_revision
                or check.checker_harness != agent_harness or check.status != "pending"
                or not native_session_id
            ):
                raise PermissionError("FinalCheckerAdmissionDenied: check is not the current pending request")
            binding = {"native_session_id": native_session_id, "scope_id": scope_id, "control_version": control_version}
            if check.checker_metadata.get("native_session_id"):
                if all(check.checker_metadata.get(key) == value for key, value in binding.items()):
                    return
                raise PermissionError("FinalCheckerAlreadyBound: check already has a native session")
            check.checker_metadata.update(binding)
            self.state.write_goal(record)

    def prepare_final_check(
        self,
        goal_plus_id: str,
        checker_harness: GoalPlusFinalCheckerHost,
        *,
        expected_revision: int | None = None,
        expected_control_version: int | None = None,
    ) -> dict[str, Any]:
        with self.state.lock_goal(goal_plus_id):
            record = self.state.read_goal(goal_plus_id)
            if ((expected_revision is not None and record.goal_revision != expected_revision)
                    or (expected_control_version is not None and record.control.version != expected_control_version)):
                raise PermissionError("FinalCheckerAdmissionDenied: Goal changed before check preparation")
            if record.status != "active" or record.control.state != "active":
                raise RuntimeError(
                    "Final check can only start for an active Goal Plus record."
                )
            if final_check_mode(record) != "required":
                raise RuntimeError(
                    "This Goal Plus record does not require a final check."
                )
            if record.phase not in {"goal", "final_audit", "final_check"}:
                raise RuntimeError(
                    "Finish intake, spec discovery, and Search Mode before starting final check."
                )
            self.reconciliation.require_idle_execution(record, "start final check")
            require_current_search_result(record, "start final check")

            current = latest_final_check(record)
            if (
                current is not None and current.status == "pending"
                and current.checker_metadata.get("native_session_id")
                and current.checker_metadata.get("control_version") != record.control.version
            ):
                current.status = "superseded"
                current.completed_at = self.clock.timestamp()
            if (
                current is not None
                and current.goal_revision == record.goal_revision
                and current.status == "pending"
            ):
                check = current
            else:
                now = self.clock.timestamp()
                check = GoalPlusFinalCheck(
                    check_id=(
                        f"fc_{goal_plus_id.removeprefix('gp_')}_r{record.goal_revision}_"
                        f"{len(record.final_checks) + 1:03d}"
                    ),
                    goal_revision=record.goal_revision,
                    checker_harness=checker_harness,
                    requested_phase=(
                        current.requested_phase
                        if record.phase == "final_check" and current is not None
                        else record.phase
                    ),
                    requested_at=now,
                )
                updated = record.model_copy(
                    update={
                        "phase": "final_check",
                        "final_checks": [*record.final_checks, check],
                        "next_action": GoalPlusNextAction(
                            kind="run_final_check",
                            description=(
                                "为当前目标修订版启动独立的最终检查审查员，并记录其结构化结论。"
                            ),
                            required=True,
                            metadata={
                                "check_id": check.check_id,
                                "goal_revision": record.goal_revision,
                                "checker_harness": checker_harness,
                            },
                        ),
                        "updated_at": now,
                    }
                )
                self.state.write_goal(updated)
                self.state.append_event(
                    goal_plus_id,
                    "final_check_requested",
                    check.model_dump(mode="json"),
                )
                record = updated

            return {
                "goal_plus_id": goal_plus_id,
                "goal_revision": record.goal_revision,
                "check": check.model_dump(mode="json"),
                "launch": final_check_launch(record, check),
            }

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
        *, native_session_id: str | None = None,
    ) -> GoalPlusRecord:
        with self.state.lock_goal(goal_plus_id):
            record = self.state.read_goal(goal_plus_id)
            if is_terminal_goal(record):
                raise RuntimeError(
                    "Final check cannot change a terminal or cleared Goal Plus task"
                )
            if goal_revision != record.goal_revision:
                raise RuntimeError(
                    f"Final check is stale for goal revision {goal_revision}; "
                    f"current revision is {record.goal_revision}."
                )
            check_index = next(
                (
                    index
                    for index, check in enumerate(record.final_checks)
                    if check.check_id == check_id
                ),
                None,
            )
            if check_index is None:
                raise RuntimeError(f"Unknown final check id: {check_id}")
            check = record.final_checks[check_index]
            if check.checker_metadata.get("native_session_id") and (
                native_session_id != check.checker_metadata["native_session_id"]
                or check.checker_metadata.get("control_version") != record.control.version
                or record.status != "active" or record.control.state != "active"
            ):
                raise PermissionError("FinalCheckerIdentityMismatch: checker activation is no longer authorized")
            if check.goal_revision != goal_revision:
                raise RuntimeError(
                    "Final check goal revision does not match the submission."
                )
            if check.status != "pending":
                raise RuntimeError(f"Final check {check_id} is already {check.status}.")
            if verdict not in {"pass", "fail", "interrupted"}:
                raise ValueError(
                    "final check verdict must be 'pass', 'fail', or 'interrupted'"
                )
            normalized_summary = summary.strip()
            if not normalized_summary:
                raise ValueError("final check summary must be non-empty")
            normalized_evidence = evidence or []
            if verdict == "pass" and not normalized_evidence:
                raise ValueError("a passing final check requires concrete evidence")
            if verdict == "pass":
                self.reconciliation.require_idle_execution(record, "pass final check")
                require_current_search_result(record, "pass final check")

            now = self.clock.timestamp()
            completed = check.model_copy(
                update={
                    "status": (
                        "passed"
                        if verdict == "pass"
                        else "interrupted"
                        if verdict == "interrupted"
                        else "failed"
                    ),
                    "completed_at": now,
                    "summary": normalized_summary,
                    "findings": findings or [],
                    "evidence": normalized_evidence,
                    "checker_metadata": {**(checker_metadata or {}), **check.checker_metadata},
                }
            )
            checks = list(record.final_checks)
            checks[check_index] = completed
            if verdict == "pass":
                update: dict[str, Any] = {
                    "status": "complete",
                    "control": GoalPlusControl(
                        state="closed",
                        reason="complete",
                        version=record.control.version + 1,
                    ),
                    "phase": "final_check",
                    "next_action": None,
                    "final_checks": checks,
                    "updated_at": now,
                }
            elif verdict == "fail":
                update = {
                    "status": "active",
                    "phase": check.requested_phase,
                    "next_action": GoalPlusNextAction(
                        kind="address_final_check_findings",
                        description=(
                            "处理独立最终检查发现的所有问题，然后申请新的检查。"
                        ),
                        required=True,
                        metadata={
                            "check_id": check_id,
                            "goal_revision": goal_revision,
                            "findings": findings or [],
                        },
                    ),
                    "final_checks": checks,
                    "updated_at": now,
                }
            else:
                update = {
                    "status": "active",
                    "phase": check.requested_phase,
                    "next_action": GoalPlusNextAction(
                        kind="retry_final_check",
                        description=(
                            "独立最终检查员在给出结论前被中断。申请并运行新的最终检查。"
                        ),
                        required=True,
                        metadata={
                            "check_id": check_id,
                            "goal_revision": goal_revision,
                        },
                    ),
                    "final_checks": checks,
                    "updated_at": now,
                }
            updated = record.model_copy(update=update)
            self.state.write_goal(updated)
            self.state.append_event(
                goal_plus_id,
                "final_check_submitted",
                completed.model_dump(mode="json"),
            )
            if verdict == "pass":
                self.state.append_event(
                    goal_plus_id,
                    "status_changed",
                    {
                        "status": "complete",
                        "reason": "required independent final check passed",
                        "evidence": normalized_evidence,
                    },
                )
            return updated

    def set_status(
        self,
        goal_plus_id: str,
        status: GoalPlusStatus,
        reason: str | None = None,
        evidence: list[dict[str, Any]] | None = None,
        next_action: GoalPlusNextAction | dict[str, Any] | None = None,
    ) -> GoalPlusRecord:
        status = TypeAdapter(GoalPlusStatus).validate_python(status)
        with self.state.lock_goal(goal_plus_id):
            record = self.state.read_goal(goal_plus_id)
            if is_terminal_goal(record) and status != record.status:
                raise RuntimeError(
                    "A terminal Goal requires an explicit host edit or a new task"
                )
            if status == "active" and record.status == "blocked":
                raise RuntimeError("A blocked Goal requires an explicit host resume")
            if status == "complete":
                self.reconciliation.require_idle_execution(record, "complete Goal Plus")
                require_current_search_result(record, "complete Goal Plus")
            if status == "complete" and final_check_mode(record) == "required":
                final_check = latest_final_check(record)
                if (
                    final_check is None
                    or final_check.goal_revision != record.goal_revision
                    or final_check.status != "passed"
                ):
                    raise RuntimeError(
                        "Goal Plus completion requires a passing final check for the current goal revision."
                    )
            parsed_next_action = (
                GoalPlusNextAction.model_validate(next_action)
                if isinstance(next_action, dict)
                else next_action
            )
            updated = record.model_copy(
                update={
                    "status": status,
                    "next_action": None
                    if status in TERMINAL_STATUSES
                    else parsed_next_action or record.next_action,
                    "control": (
                        GoalPlusControl(
                            state="closed",
                            reason=status,
                            version=record.control.version + 1,
                        )
                        if status in TERMINAL_STATUSES
                        else GoalPlusControl(
                            state="paused",
                            reason="blocked",
                            version=record.control.version + 1,
                        )
                        if status == "blocked"
                        else record.control
                    ),
                    "updated_at": self.clock.timestamp(),
                }
            )
            self.state.write_goal(updated)
            self.state.append_event(
                goal_plus_id,
                "status_changed",
                {"status": status, "reason": reason, "evidence": evidence or []},
            )
            return updated
