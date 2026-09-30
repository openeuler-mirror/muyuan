"""Exact Child attempt admission, snapshot verification and shared settlement."""

from typing import Any
from goal_plus.domain.edit_surface import path_matches
from goal_plus.application.candidate_ranking import best_artifact_iteration_record
from goal_plus.ports.candidate_artifacts import CandidateArtifactState
from goal_plus.application.candidate_execution import (
    require_worker_iteration_state,
)
from goal_plus.domain.results_ledger import (
    valid_inner_git_head,
)
from goal_plus.domain.artifact_paths import (
    thinkthread_logical_changed_files,
)
from goal_plus.domain.agents import (
    WORKSPACE_LEDGER_PROJECTION_RESULTS_TSV,
)
from goal_plus.domain.artifacts import (
    ArtifactRef,
)
from goal_plus.domain.evidence import (
    CandidateAttemptAccepted,
    IterationDisposition,
    IterationRecord,
    ScoreReport,
    VerifierResult,
)
from goal_plus.domain.search import (
    AgentSessionRecord,
    VerifierRole,
)
from goal_plus.domain.shared_tools import (
    ToolizationDecision,
)
from goal_plus.ports.state import (
    CandidateRecord,
)
from goal_plus.application.settlement_policy import iteration_disposition
from goal_plus.application.candidate_settlement import CandidateSettlement
from goal_plus.application.candidate_sessions import CandidateSessions
from goal_plus.application.candidate_ledger import CandidateLedger
from goal_plus.application.shared_tools import SharedTools
from goal_plus.application.goal_authorization import GoalAuthorization
from goal_plus.ports.candidate_settlement import CandidateSettlementState, InnerGitSettlement
from goal_plus.ports.thinkthread_verification import DeferredCandidateAttempt, ThinkThreadAttemptIO
from goal_plus.ports.clock import Clock


class ThinkThreadVerification:
    def __init__(
        self,
        *,
        state: CandidateSettlementState,
        clock: Clock,
        authorization: GoalAuthorization,
        candidate_sessions: CandidateSessions,
        candidate_ledger: CandidateLedger,
        shared_tools: SharedTools,
        settlement: CandidateSettlement,
    ) -> None:
        self.state, self.clock, self.authorization = state, clock, authorization
        self.candidate_sessions, self.candidate_ledger = (
            candidate_sessions,
            candidate_ledger,
        )
        self.shared_tools, self.settlement = shared_tools, settlement
        self.trace = settlement.trace

    def _bound_candidate(
        self, run_id: str, candidate_id: str, agent_session_id: str,
        thinkthread_id: str, branch_id: str,
    ) -> CandidateRecord:
        session = self.candidate_sessions.session(agent_session_id, run_id=run_id)
        record = self.state.read_candidate(run_id, candidate_id)
        if record.task.workspace_provider != "thinkthread" or session.runtime_provider != "thinkthread":
            raise ValueError("candidate does not use the ThinkThread workspace_provider")
        binding = record.task.workspace_provider_handle
        if (
            session.candidate_id != candidate_id
            or session.session_handle.external_id != thinkthread_id
            or binding.get("thinkthread_id") != thinkthread_id
            or binding.get("branch_id") != branch_id
            or binding.get("agent_session_id") != agent_session_id
            or not thinkthread_id or not branch_id
        ):
            raise PermissionError("ThinkThread attempt identity does not match the bound Child")
        if record.status not in {"created", "evaluated"}:
            raise RuntimeError(f"cannot verify candidate in status {record.status}")
        return record

    def accept(
        self, submission: DeferredCandidateAttempt, *, run_id: str, candidate_id: str,
        agent_session_id: str | None, scope: str, hypothesis: str | None,
        toolization_decision: ToolizationDecision | dict[str, Any] | None,
    ) -> CandidateAttemptAccepted:
        """Admit a request for later quiescent verification; does not create Evidence."""
        if scope != "process":
            raise PermissionError("ThinkThread Child may request only process verification")
        if (run_id, candidate_id, agent_session_id) != (
            submission.run_id, submission.candidate_id, submission.agent_session_id,
        ):
            raise PermissionError("ThinkThread verifier request does not match the bound invocation")
        hypothesis = (hypothesis or "").strip()
        if not hypothesis:
            raise ValueError("worker process verifier requires a non-empty hypothesis")
        with self.state.lock_run(run_id):
            run = self.state.read_run(run_id)
            self.authorization.require_search_execution(run_id, settlement=True)
            require_worker_iteration_state(run, "accept ThinkThread attempt")
            record = self._bound_candidate(
                run_id, candidate_id, submission.agent_session_id,
                submission.thinkthread_id, submission.branch_id,
            )
            frozen = self.state.read_frozen_spec(run.frozen_spec_id)
            decision = self.shared_tools.normalize_toolization_decision(
                run_id=run_id, frozen=frozen, scope="process",
                agent_session_id=submission.agent_session_id,
                toolization_decision=toolization_decision,
            )
            prior = submission.prior
            if prior is not None:
                if prior.request_id != submission.request_id:
                    raise RuntimeError("only one candidate attempt may be accepted per Child turn")
                if (
                    (prior.run_id, prior.candidate_id, prior.agent_session_id, prior.attempt_id)
                    != (run_id, candidate_id, agent_session_id, submission.attempt_id)
                    or prior.hypothesis != hypothesis or prior.toolization_decision != decision
                ):
                    raise ValueError("verifier request id was reused with new content")
                self._record_submission_trace(
                    session=self.candidate_sessions.state.read_agent_session(
                        submission.agent_session_id,
                        run_id,
                    ),
                    accepted=prior,
                    iteration=len(record.iterations) + 1,
                )
                return prior
            if self.candidate_ledger.projection(frozen, record) == WORKSPACE_LEDGER_PROJECTION_RESULTS_TSV:
                self._validate_thinkthread_inner_git_attempt(record, submission.inner_git_attempt)
            elif submission.inner_git_attempt is not None:
                raise PermissionError("private inner Git metadata is not allowed for this inner-agent binding")
            accepted = CandidateAttemptAccepted(
                attempt_id=submission.attempt_id, request_id=submission.request_id,
                run_id=run_id, candidate_id=candidate_id, agent_session_id=submission.agent_session_id,
                hypothesis=hypothesis, toolization_decision=decision, accepted_at=self.clock.timestamp(),
                message="The attempt was accepted. Its exact snapshot verifier will run after "
                "this Child turn reaches a quiescent boundary. Finish the turn without "
                "further writes; the exact ScoreReport will be injected into the next wake.",
            )
            self._record_submission_trace(
                session=self.candidate_sessions.state.read_agent_session(
                    submission.agent_session_id,
                    run_id,
                ),
                accepted=accepted,
                iteration=len(record.iterations) + 1,
            )
            return accepted

    def result(
        self, submission: DeferredCandidateAttempt, run_id: str, candidate_id: str,
        agent_session_id: str | None, attempt_id: str,
    ) -> dict[str, Any]:
        if (run_id, candidate_id, agent_session_id) != (
            submission.run_id, submission.candidate_id, submission.agent_session_id,
        ):
            raise PermissionError("ThinkThread verifier query does not match the bound invocation")
        record = self._bound_candidate(
            run_id, candidate_id, submission.agent_session_id,
            submission.thinkthread_id, submission.branch_id,
        )
        if record.score_report is not None and record.score_report.attempt_id == attempt_id:
            result = record.score_report.model_dump(mode="json")
            status = "ready"
        elif submission.prior is not None and submission.prior.attempt_id == attempt_id:
            result = submission.prior.model_dump(mode="json")
            status = "accepted"
        else:
            raise ValueError("VerifierAttemptUnavailable: unknown verification attempt")
        session = self.candidate_sessions.state.read_agent_session(
            submission.agent_session_id,
            run_id,
        )
        self._record_observation_trace(session, attempt_id, status)
        return result

    def _record_submission_trace(
        self,
        *,
        session: AgentSessionRecord,
        accepted: CandidateAttemptAccepted,
        iteration: int,
    ) -> None:
        try:
            self.trace.record(
                session,
                kind="verifier.submitted",
                operation_id=accepted.attempt_id,
                occurred_at=accepted.accepted_at,
                deduplicate=True,
                payload={
                    "attempt_id": accepted.attempt_id,
                    "iteration": iteration,
                    "request_id": accepted.request_id,
                },
            )
        except Exception as exc:
            self.trace.failed(
                session,
                operation="verifier_submission",
                operation_id=accepted.attempt_id,
                error=exc,
                verified_target={"attempt_id": accepted.attempt_id},
            )

    def _record_observation_trace(
        self,
        session: AgentSessionRecord,
        attempt_id: str,
        status: str,
    ) -> None:
        try:
            self.trace.record(
                session,
                kind="verifier.observed",
                operation_id=attempt_id,
                payload={"attempt_id": attempt_id, "status": status},
            )
        except Exception as exc:
            self.trace.failed(
                session,
                operation="verifier_observation",
                operation_id=attempt_id,
                error=exc,
                verified_target={"attempt_id": attempt_id},
            )

    def verify(
        self,
        *,
        run_id: str,
        candidate_id: str,
        agent_session_id: str,
        thinkthread_id: str,
        branch_id: str,
        hypothesis: str,
        attempt_id: str,
        io: ThinkThreadAttemptIO,
        inner_git_attempt: dict[str, Any] | None = None,
        toolization_decision: ToolizationDecision | dict[str, Any] | None = None,
        recovery_deadline: float | None = None,
    ) -> ScoreReport:
        """Capture and verify one quiesced Child attempt on its exact snapshot."""

        with self.state.lock_run(run_id):
            run = self.state.read_run(run_id)
            self.authorization.require_search_execution(
                run.run_id, settlement=True
            )
            require_worker_iteration_state(run, "verify ThinkThread attempt")
            frozen = self.state.read_frozen_spec(run.frozen_spec_id)
            normalized_toolization_decision = (
                self.shared_tools.normalize_toolization_decision(
                    run_id=run_id,
                    frozen=frozen,
                    scope="process",
                    agent_session_id=agent_session_id,
                    toolization_decision=toolization_decision,
                )
            )
            record = self._bound_candidate(
                run_id, candidate_id, agent_session_id, thinkthread_id, branch_id,
            )
            for iteration in record.iterations:
                provider = iteration.metrics.get("_candidate_attempt")
                legacy = iteration.metrics.get("_thinkthread_settlement")
                recorded_id = (
                    provider.get("id")
                    if isinstance(provider, dict)
                    else legacy.get("key")
                    if isinstance(legacy, dict)
                    else None
                )
                if recorded_id == attempt_id:
                    if record.score_report is None:
                        raise RuntimeError(
                            "verified ThinkThread attempt is missing its score report"
                        )
                    self.settlement.reconcile(run, frozen, record)
                    return record.score_report.model_copy(
                        update={
                            "attempt_id": attempt_id,
                            "attempt_iteration": iteration.iteration,
                            "attempt_artifact_ref": iteration.artifact_ref,
                        }
                    )
            iteration_number = len(record.iterations) + 1
            baseline = run.source_artifact_ref
            attempt_base = (
                record.results_ledger_artifact_ref or record.task.base_artifact_ref
            )
        if baseline is None or attempt_base is None:
            raise RuntimeError("ThinkThread candidate is missing its snapshot anchors")
        child = io.read_child(thinkthread_id)
        if (
            child.thinkthread_id != thinkthread_id
            or child.branch_id != branch_id
            or child.parent_thinkthread_id
            != run.source_provider_handle.get("root_thinkthread_id")
        ):
            raise PermissionError("ThinkThread attempt Child ownership mismatch")
        if not child.ready or child.pending_wake:
            raise RuntimeError(
                "ThinkThread candidate settlement requires a ready Agent with no "
                "pending wake"
            )
        snapshot = io.capture(
            run_id=run_id,
            candidate_id=candidate_id,
            branch_id=branch_id,
            iteration=iteration_number,
            attempt_id=attempt_id,
            recovery_deadline=recovery_deadline,
        )
        if (
            snapshot.branch_id != branch_id
            or snapshot.artifact_ref.kind != "fs_snapshot"
            or snapshot.artifact_ref.provider != "thinkthread"
            or not snapshot.request_id
        ):
            raise RuntimeError("ThinkThread attempt snapshot identity mismatch")
        attempt_ref = snapshot.artifact_ref
        reader = io
        source_relative_path = str(
            run.source_provider_handle.get("source_relative_path") or "."
        )
        attempt_changed_files = thinkthread_logical_changed_files(
            source_relative_path,
            reader.changed_files(attempt_base, attempt_ref),
        )
        cumulative_changed_files = thinkthread_logical_changed_files(
            source_relative_path,
            reader.changed_files(baseline, attempt_ref),
        )
        touched_denied = any(
            path_matches(path, frozen.spec.edit_surface.deny)
            for path in cumulative_changed_files
        )
        changed_outside_allowed = any(
            not path_matches(path, frozen.spec.edit_surface.allow)
            for path in cumulative_changed_files
        )
        max_changes = frozen.spec.edit_surface.max_file_changes
        if max_changes is not None and len(cumulative_changed_files) > max_changes:
            changed_outside_allowed = True
        artifact_hash = reader.canonical_digest(baseline, attempt_ref)
        attempt = CandidateArtifactState(
            changed_files=cumulative_changed_files,
            touched_denied_files=touched_denied,
            changed_outside_allowed=changed_outside_allowed,
            artifact_hash=artifact_hash,
            artifact_ref=attempt_ref,
            artifact_status=[],
            artifact_clean=True,
            git_head=None,
            git_status=[],
            git_artifact_clean=False,
        )
        if touched_denied or changed_outside_allowed:
            report = ScoreReport(
                run_id=run_id,
                candidate_id=candidate_id,
                validity_passed=False,
                process_passed=False,
                aggregate_score=0.0,
                verifier_results=[
                    VerifierResult(
                        name="edit_surface_check",
                        role=VerifierRole.ANTI_CHEAT_GATE,
                        passed=False,
                        score=0.0,
                        metrics={
                            "detected_changed_files": cumulative_changed_files,
                            "touched_denied_files": touched_denied,
                            "changed_outside_allowed": changed_outside_allowed,
                        },
                        failure_class="EditSurfaceViolation",
                    )
                ],
                touched_denied_files=touched_denied,
                changed_outside_allowed=changed_outside_allowed,
                hardcoding_suspected=True,
            )
        else:
            report = io.run_commands(
                run=run,
                frozen=frozen,
                candidate_id=candidate_id,
                artifact_ref=attempt_ref,
                commands=frozen.spec.process_verifiers,
                phase="candidate",
                iteration=iteration_number,
                recovery_deadline=recovery_deadline,
            )
        provider_request_ids = [snapshot.request_id]
        provider_request_ids.extend(
            str(result.metrics["request_id"])
            for result in report.verifier_results
            if isinstance(result.metrics.get("request_id"), str)
        )
        session = self.candidate_sessions.session(agent_session_id, run_id=run_id)
        workspace_ledger_projection = self.candidate_ledger.projection(
            frozen,
            record,
        )
        inner_git_settlement: InnerGitSettlement | None = None
        expected_disposition: IterationDisposition | None = None
        settled_artifact_ref: ArtifactRef | None = None
        if workspace_ledger_projection == WORKSPACE_LEDGER_PROJECTION_RESULTS_TSV:
            private_attempt = self._validate_thinkthread_inner_git_attempt(
                record,
                inner_git_attempt,
            )
            prior_best = best_artifact_iteration_record(
                record,
                frozen.spec.metric_direction,
            )
            provisional = IterationRecord(
                iteration=iteration_number,
                score=report.aggregate_score,
                process_passed=report.process_passed,
                artifact_ref=attempt_ref,
                artifact_clean=True,
                touched_denied_files=touched_denied,
                changed_outside_allowed=changed_outside_allowed,
                created_at=self.clock.timestamp(),
            )
            expected_disposition = iteration_disposition(
                provisional,
                prior_best.score if prior_best is not None else None,
                frozen.spec.metric_direction,
            )
            restore_git_head: str | None = None
            if expected_disposition in {"discard", "failure"}:
                if prior_best is None:
                    restore_git_head = str(private_attempt["baseline_git_head"])
                else:
                    restore_git_head = self._thinkthread_inner_git_attempt_head(
                        record,
                        prior_best.iteration,
                    )
            inner_result = io.settle_inner_git(
                run_id=run_id,
                candidate_id=candidate_id,
                attempt_id=attempt_id,
                attempt_artifact_ref=attempt_ref,
                source_relative_path=source_relative_path,
                iteration=iteration_number,
                metric_name=frozen.spec.metric_name,
                score=report.aggregate_score,
                status="pass" if report.process_passed else "fail",
                hypothesis=hypothesis,
                disposition=expected_disposition,
                attempt_git_head=str(private_attempt["attempt_git_head"]),
                result_count_before=int(private_attempt["result_count_before"]),
                restore_git_head=restore_git_head,
                recovery_deadline=recovery_deadline,
            )
            inner_git_settlement = inner_result.settlement
            if (
                inner_result.artifact_ref.kind != "fs_snapshot"
                or inner_result.artifact_ref.provider != "thinkthread"
                or not inner_result.request_id
                or inner_git_settlement.attempt_git_head
                != private_attempt["attempt_git_head"]
                or inner_git_settlement.baseline_git_head
                != private_attempt["baseline_git_head"]
                or inner_git_settlement.result_count != len(record.results_ledger) + 1
                or inner_git_settlement.restored_to_git_head != restore_git_head
            ):
                raise RuntimeError("ThinkThread inner Git settlement identity mismatch")
            settled_artifact_ref = inner_result.artifact_ref
            provider_request_ids.append(inner_result.request_id)
        elif inner_git_attempt is not None:
            raise PermissionError(
                "private inner Git metadata is not allowed for this inner-agent binding"
            )
        return self.settlement.record(
            run_id=run_id,
            candidate_id=candidate_id,
            frozen=frozen,
            report=report,
            attempt=attempt,
            pre_attempt_settled_head=None,
            pre_attempt_settled_artifact_ref=attempt_base,
            attempt_changed_files=attempt_changed_files,
            hypothesis=hypothesis,
            agent_session_id=agent_session_id,
            session=session,
            toolization_decision=normalized_toolization_decision,
            provider_request_ids=provider_request_ids,
            attempt_id=attempt_id,
            expected_disposition=expected_disposition,
            settled_artifact_ref=settled_artifact_ref,
            inner_git_settlement=inner_git_settlement,
        )

    def _validate_thinkthread_inner_git_attempt(
        self,
        record: CandidateRecord,
        value: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise RuntimeError(
                "ThinkThread AutoResearch-Git attempt is missing private Git metadata"
            )
        persisted_baseline = record.inner_agent_state.get("baseline_git_head")
        if (
            value.get("protocol") != "goal-plus.inner-git.v1"
            or value.get("adapter") != "autoresearch-git"
            or not valid_inner_git_head(value.get("attempt_git_head"))
            or not valid_inner_git_head(value.get("baseline_git_head"))
            or not isinstance(value.get("result_count_before"), int)
            or isinstance(value.get("result_count_before"), bool)
            or value["result_count_before"] != len(record.results_ledger)
            or (
                persisted_baseline is not None
                and value.get("baseline_git_head") != persisted_baseline
            )
        ):
            raise RuntimeError(
                "ThinkThread AutoResearch-Git metadata does not match the canonical ledger"
            )
        return dict(value)

    @staticmethod
    def _thinkthread_inner_git_attempt_head(
        record: CandidateRecord,
        iteration: int,
    ) -> str:
        state = record.inner_agent_state
        attempts = state.get("attempt_git_heads")
        head = attempts.get(str(iteration)) if isinstance(attempts, dict) else None
        if not valid_inner_git_head(head):
            raise RuntimeError(
                "ThinkThread AutoResearch-Git restore target is missing from private state"
            )
        return str(head)
