"""Local attempt admission and promotion Evidence, separate from hard settlement."""

from typing import Any, Callable, Literal
from goal_plus.domain.edit_surface import path_matches
from goal_plus.application.candidate_execution import (
    require_run_not_invalidated,
    require_worker_iteration_state,
)
from goal_plus.domain.results_ledger import (
    normalize_result_text,
)
from goal_plus.domain.artifacts import (
    ArtifactRef,
)
from goal_plus.domain.evidence import (
    ScoreReport,
    VerifierResult,
)
from goal_plus.domain.promotion import (
    PromotionEvidence,
)
from goal_plus.domain.search import (
    AgentSessionRecord,
    FrozenSpec,
    RunState,
    VerifierRole,
)
from goal_plus.domain.time import outer_deadline_epoch
from goal_plus.domain.shared_tools import (
    ToolizationDecision,
)
from goal_plus.ports.state import (
    CandidateRecord,
    RunRecord,
)
from goal_plus.application.candidate_settlement import CandidateSettlement
from goal_plus.application.candidate_sessions import CandidateSessions
from goal_plus.application.candidate_artifacts import CandidateArtifacts
from goal_plus.application.candidate_ledger import CandidateLedger
from goal_plus.application.shared_tools import SharedTools
from goal_plus.application.goal_authorization import GoalAuthorization
from goal_plus.application.search_context import SearchContext
from goal_plus.ports.candidate_settlement import CandidateSettlementState
from goal_plus.ports.candidate_verification import CandidateVerifiers
from goal_plus.ports.clock import Clock


class CandidateVerification:
    def __init__(
        self,
        *,
        state: CandidateSettlementState,
        clock: Clock,
        authorization: GoalAuthorization,
        context: SearchContext,
        candidate_sessions: CandidateSessions,
        artifacts: CandidateArtifacts,
        candidate_ledger: CandidateLedger,
        shared_tools: SharedTools,
        settlement: CandidateSettlement,
        verifiers: CandidateVerifiers,
        persist_run: Callable[[RunRecord], None],
    ) -> None:
        self.state, self.clock, self.authorization, self.context = (
            state,
            clock,
            authorization,
            context,
        )
        self.candidate_sessions, self.artifacts, self.candidate_ledger = (
            candidate_sessions,
            artifacts,
            candidate_ledger,
        )
        self.shared_tools, self.settlement = (
            shared_tools,
            settlement,
        )
        self.verifiers, self.persist_run = verifiers, persist_run

    def verify(
        self,
        run_id: str,
        candidate_id: str,
        scope: Literal["process", "promotion"] = "process",
        agent_session_id: str | None = None,
        hypothesis: str | None = None,
        toolization_decision: ToolizationDecision | dict[str, Any] | None = None,
    ) -> ScoreReport:
        """Subagent self-score with ``agent_session_id``; main final verify
        without it. Process calls record ranking iterations; promotion calls
        retain separate acceptance evidence.
        """
        with self.state.lock_run(run_id):
            run = self.state.read_run(run_id)
            if scope == "process":
                self.authorization.require_search_execution(run.run_id)
                require_worker_iteration_state(run, "run verifier")
            else:
                require_run_not_invalidated(run, "run verifier")
        frozen = self.state.read_frozen_spec(run.frozen_spec_id)
        normalized_toolization_decision = (
            self.shared_tools.normalize_toolization_decision(
                run_id=run_id,
                frozen=frozen,
                scope=scope,
                agent_session_id=agent_session_id,
                toolization_decision=toolization_decision,
            )
        )
        record = self.state.read_candidate(run_id, candidate_id)
        if record.status not in {"created", "evaluated"}:
            raise RuntimeError(f"cannot verify candidate in status {record.status}")
        if scope == "process" and (
            frozen.spec.strategy.orchestration_mode == "adaptive_search"
        ):
            self.candidate_sessions.admission.reconcile_candidate_execution_allowed(
                run_id,
                candidate_id,
                "run_verifier",
            )
        if scope == "promotion":
            if agent_session_id is not None:
                raise PermissionError(
                    "promotion verification is parent-owned and cannot be "
                    "called from a candidate agent session"
                )
            if (
                run.state != RunState.READY_TO_PROMOTE
                or run.selected_candidate_id != candidate_id
                or not (run.selected_artifact_ref or run.selected_git_head)
            ):
                raise RuntimeError(
                    "promotion verification requires the candidate and immutable "
                    "artifact selected by search_select"
                )

        session: AgentSessionRecord | None = None
        if agent_session_id:
            session = self.candidate_sessions.session(agent_session_id, run_id=run_id)
            if session.candidate_id != candidate_id:
                raise ValueError("agent_session_id does not belong to this candidate")
            if scope == "process":
                hypothesis = normalize_result_text(hypothesis or "")
                if not hypothesis:
                    raise ValueError(
                        "worker process verifier requires a non-empty hypothesis"
                    )

        if scope == "process":
            outer_deadline = outer_deadline_epoch(self.context.outer_deadline(run_id))
            if outer_deadline is not None:
                verifier_seconds = sum(
                    float(command.timeout_seconds)
                    + 2 * self.verifiers.termination_grace_seconds
                    for command in frozen.spec.process_verifiers
                )
                closeout_seconds = max(
                    0.0,
                    float(
                        frozen.spec.strategy.config.get("reserve_closeout_seconds", 0)
                        or 0
                    ),
                )
                remaining_seconds = outer_deadline - self.clock.now()
                required_seconds = verifier_seconds + closeout_seconds
                if remaining_seconds < required_seconds:
                    raise RuntimeError(
                        "VerifierDeadlineInsufficient: process verifier not started; "
                        f"{remaining_seconds:.1f}s remain, but the verifier suite may "
                        f"use {verifier_seconds:.1f}s and closeout reserves "
                        f"{closeout_seconds:.1f}s"
                    )

        results_were_initialized = bool(
            record.results_ledger_artifact_ref or record.results_ledger_git_head
        )
        self.candidate_ledger.ensure(frozen, record)
        if not results_were_initialized:
            self.state.write_candidate(run_id, record)
        pre_attempt_settled_head = record.results_ledger_git_head
        pre_attempt_artifact_ref = record.results_ledger_artifact_ref
        if pre_attempt_artifact_ref is None and pre_attempt_settled_head is not None:
            pre_attempt_artifact_ref = ArtifactRef(
                kind="git_commit",
                id=pre_attempt_settled_head,
                provider=record.task.workspace_backend,
            )
        if (
            pre_attempt_settled_head is None
            and pre_attempt_artifact_ref is not None
            and pre_attempt_artifact_ref.kind == "git_commit"
        ):
            pre_attempt_settled_head = pre_attempt_artifact_ref.id

        state = self.artifacts.candidate_artifact_state(run, frozen, record)
        self.artifacts.apply_candidate_artifact_state(record, state)
        if scope == "process":
            attempt_artifact_ref = (
                self.artifacts.workspace.commit_workspace_iteration(
                    record,
                    (
                        f"search verifier iteration "
                        f"{candidate_id}:{len(record.iterations) + 1}"
                    ),
                )
            )
            if attempt_artifact_ref is None or pre_attempt_artifact_ref is None:
                raise RuntimeError(
                    "candidate verifier requires immutable attempt and settled artifacts"
                )
            if not self.artifacts.workspace.is_descendant(
                record.task,
                pre_attempt_artifact_ref,
                attempt_artifact_ref,
            ):
                raise RuntimeError(
                    "candidate artifact history diverged from the settled results ledger"
                )
            attempt_changed_files = self.artifacts.workspace.changed_between(
                record.task,
                pre_attempt_artifact_ref,
                attempt_artifact_ref,
            )
            state = self.artifacts.candidate_artifact_state(run, frozen, record)
            self.artifacts.apply_candidate_artifact_state(record, state)
            if (
                state.artifact_ref != attempt_artifact_ref
                or not state.artifact_clean
            ):
                raise RuntimeError(
                    "candidate attempt is not fully captured by its artifact"
                )

        precheck = self._precheck_candidate(frozen, record)
        if precheck is not None:
            report = precheck
        else:
            commands = (
                frozen.spec.process_verifiers
                if scope == "process"
                else frozen.spec.promotion_verifiers
            )
            if not commands:
                commands = frozen.spec.process_verifiers
            report = self.verifiers.run_commands(
                run, frozen, record, commands, scope
            )

        if scope == "process":
            return self.settlement.record(
                run_id=run_id,
                candidate_id=candidate_id,
                frozen=frozen,
                report=report,
                attempt=state,
                pre_attempt_settled_head=pre_attempt_settled_head,
                pre_attempt_settled_artifact_ref=pre_attempt_artifact_ref,
                attempt_changed_files=attempt_changed_files,
                hypothesis=hypothesis,
                agent_session_id=agent_session_id,
                session=session,
                toolization_decision=normalized_toolization_decision,
            )

        if report.promotion_passed is None:
            report = report.model_copy(
                update={"promotion_passed": report.process_passed}
            )
        state = self.artifacts.candidate_artifact_state(run, frozen, record)

        with self.state.lock_run(run_id):
            run = self.state.read_run(run_id)
            require_run_not_invalidated(run, "record verifier result")
            record = self.state.read_candidate(run_id, candidate_id)
            self.artifacts.apply_candidate_artifact_state(record, state)
            record.promotion_report = report
            record.promotion_evidence = PromotionEvidence(
                candidate_id=candidate_id,
                selected_git_head=run.selected_git_head,
                git_head=run.selected_git_head,
                selected_artifact_ref=run.selected_artifact_ref,
                artifact_ref=state.artifact_ref,
                artifact_hash=state.artifact_hash,
                passed=bool(report.promotion_passed),
                created_at=self.clock.timestamp(),
            )
            self.state.write_candidate(run_id, record)
            self.persist_run(run)

        return report

    def _precheck_candidate(
        self,
        frozen: FrozenSpec,
        record: CandidateRecord,
    ) -> ScoreReport | None:
        results: list[VerifierResult] = []

        if record.touched_denied_files or record.changed_outside_allowed:
            outside_allowed = [
                path
                for path in record.detected_changed_files
                if not path_matches(path, frozen.spec.edit_surface.allow)
            ]
            frozen_paths = set(frozen.verifier_hashes)
            verifier_workspace_side_effects = bool(outside_allowed) and all(
                path.startswith(".goal-plus-verifiers/") and path not in frozen_paths
                for path in outside_allowed
            )
            failure_class = (
                "VerifierWorkspaceSideEffect"
                if verifier_workspace_side_effects
                else "EditSurfaceViolation"
            )
            results.append(
                VerifierResult(
                    name="edit_surface_check",
                    role=VerifierRole.ANTI_CHEAT_GATE,
                    passed=False,
                    score=0.0,
                    metrics={
                        "detected_changed_files": record.detected_changed_files,
                        "touched_denied_files": record.touched_denied_files,
                        "changed_outside_allowed": record.changed_outside_allowed,
                        "infrastructure_failure": verifier_workspace_side_effects,
                        "candidate_action": (
                            "stop_and_report"
                            if verifier_workspace_side_effects
                            else "repair_candidate_edit_surface"
                        ),
                    },
                    failure_class=failure_class,
                )
            )

        hash_failures = self.verifiers.frozen_hash_failures(
            frozen, record.task.workspace
        )
        if hash_failures:
            results.append(
                VerifierResult(
                    name="frozen_hash_check",
                    role=VerifierRole.ANTI_CHEAT_GATE,
                    passed=False,
                    score=0.0,
                    metrics={"hash_failures": hash_failures},
                    failure_class="FrozenVerifierModified",
                )
            )

        if not results:
            return None

        return ScoreReport(
            run_id=record.task.run_id,
            candidate_id=record.candidate_id,
            parent_id=record.task.parent_id,
            validity_passed=False,
            process_passed=False,
            promotion_passed=None,
            aggregate_score=0.0,
            verifier_results=results,
            touched_denied_files=record.touched_denied_files,
            changed_outside_allowed=record.changed_outside_allowed,
            hardcoding_suspected=any(
                result.failure_class
                in {"EditSurfaceViolation", "FrozenVerifierModified"}
                for result in results
            ),
        )
