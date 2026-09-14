from goal_plus.application.recovery_queries import CandidateRecoveryQueries
from goal_plus.application.recovery_policy import (
    validate_recovery_ids,
    candidate_recovery_point,
)

"""Generation fencing and exact committed-checkpoint recovery."""

from typing import Callable
from goal_plus.application.candidate_ranking import best_artifact_iteration_record
from goal_plus.application.verifier import verifier_subject
from goal_plus.application.candidate_execution import (
    require_candidate_not_recovering,
    require_worker_iteration_state,
)
from goal_plus.domain.results_ledger import (
    RESULTS_TSV_RELATIVE_PATH,
    render_results_tsv,
)
from goal_plus.domain.search import (
    CandidateRecovery,
    FrozenSpec,
)
from goal_plus.domain.time import outer_deadline_epoch
from goal_plus.ports.state import (
    CandidateRecord,
    RunRecord,
)
from goal_plus.application.settlement_policy import update_best_seen
from goal_plus.application.candidate_sessions import CandidateSessions
from goal_plus.application.candidate_ledger import CandidateLedger
from goal_plus.application.candidate_checkpoints import CandidateCheckpoints
from goal_plus.application.candidate_settlement import CandidateSettlement
from goal_plus.application.shared_tools import SharedTools
from goal_plus.application.annotation_admission import AnnotationAdmission
from goal_plus.application.goal_authorization import GoalAuthorization
from goal_plus.application.search_context import SearchContext
from goal_plus.ports.clock import Clock
from goal_plus.ports.candidate_settlement import CandidateSettlementState
from goal_plus.ports.candidate_recovery import CandidateRecoveryWorkspace


class CandidateRecoveryWorkflow:
    def __init__(
        self,
        *,
        state: CandidateSettlementState,
        clock: Clock,
        workspace: CandidateRecoveryWorkspace,
        sessions: CandidateSessions,
        authorization: GoalAuthorization,
        context: SearchContext,
        candidate_ledger: CandidateLedger,
        shared_tools: SharedTools,
        checkpoints: CandidateCheckpoints,
        settlement: CandidateSettlement,
        annotation_admission: AnnotationAdmission,
        persist_run: Callable[[RunRecord], None],
    ) -> None:
        self.queries = CandidateRecoveryQueries(state, workspace, sessions.state)
        self.state, self.clock, self.workspace = state, clock, workspace
        self.sessions, self.authorization, self.context = (
            sessions,
            authorization,
            context,
        )
        self.candidate_ledger, self.shared_tools, self.checkpoints = (
            candidate_ledger,
            shared_tools,
            checkpoints,
        )
        self.settlement, self.annotation_admission, self.persist_run = (
            settlement,
            annotation_admission,
            persist_run,
        )

    def require_closeout(self, run_id: str, record: CandidateRecord) -> None:
        require_candidate_not_recovering(record)
        if record.recovery is None or self.queries.has_fresh_evidence(run_id, record):
            return
        deadline = outer_deadline_epoch(self.context.outer_deadline(run_id))
        if deadline is not None and deadline <= self.clock.now():
            # Finalization may select already settled evidence, but cannot
            # restart optimization merely to satisfy a recovery launch step.
            return
        raise RuntimeError(
            "SearchRecoveryIncomplete: obtain fresh bound-worker Evidence before selection or promotion"
        )

    def prepare_candidate_recovery(
        self, run_id: str, candidate_id: str
    ) -> CandidateRecovery:
        """Fence old execution before a host attempts to stop/reconcile workers.

        No process control or workspace mutation occurs here. A repeat before a
        fresh generation is dispatched returns the same recovery identity.
        """
        self.authorization.require_search_execution(run_id)
        validate_recovery_ids(run_id, candidate_id)
        run = self.state.read_run(run_id)
        frozen = self.state.read_frozen_spec(run.frozen_spec_id)
        if frozen.spec.strategy.orchestration_mode == "adaptive_search":
            self.sessions.admission.assert_candidate_execution_allowed(
                self.state.read_candidate(run_id, candidate_id),
                "recover",
            )
        with self.state.lock_run(run_id):
            run = self.state.read_run(run_id)
            self.authorization.require_search_execution(run.run_id)
            require_worker_iteration_state(run, "prepare recovery")
            record = self.state.read_candidate(run_id, candidate_id)
            frozen = self.state.read_frozen_spec(run.frozen_spec_id)
            if record.status not in {"created", "evaluated"}:
                raise RuntimeError("RecoveryCandidateUnavailable")
            if frozen.spec.strategy.orchestration_mode == "adaptive_search":
                self.sessions.admission.assert_candidate_execution_allowed(
                    record, "recover"
                )
            sessions = [
                s
                for s in self.sessions.state.read_agent_sessions(run_id)
                if s.candidate_id == candidate_id
            ]
            current = [
                s
                for s in sessions
                if s.execution_generation == record.execution_generation
            ]
            if record.recovery is not None and (
                record.recovery.state != "restored"
                or (
                    not current
                    and candidate_recovery_point(record) == record.recovery.point
                )
            ):
                return record.recovery
            self.workspace.require_recoverable(record)
            point = candidate_recovery_point(record)
            record.execution_generation += 1
            record.recovery = CandidateRecovery(
                recovery_id=self.workspace.new_recovery_id(),
                generation=record.execution_generation,
                point=point,
                fenced_session_ids=[s.agent_session_id for s in sessions],
                created_at=self.clock.timestamp(),
            )
            self.state.write_candidate(run_id, record)
            return record.recovery

    def request(
        self, run_id: str, candidate_id: str, recovery_id: str
    ) -> CandidateRecovery:
        validate_recovery_ids(run_id, candidate_id)
        recovery = self.state.read_candidate(run_id, candidate_id).recovery
        if recovery is None or recovery.recovery_id != recovery_id:
            raise RuntimeError("RecoveryIdentityMismatch")
        return recovery

    def recover_candidate(
        self, run_id: str, candidate_id: str, recovery_id: str
    ) -> CandidateRecovery:
        """Restore the prepared provider-native checkpoint after host quiescence.

        The host adapter supplies the durable approval; the model cannot assert
        quiescence with a boolean. Lock ordering matches verifier settlement.
        """
        self.authorization.require_search_execution(run_id)
        validate_recovery_ids(run_id, candidate_id)
        with self.state.lock_verifier(run_id, candidate_id):
            with self.state.lock_run(run_id):
                run = self.state.read_run(run_id)
                self.authorization.require_search_execution(run.run_id)
                require_worker_iteration_state(run, "recover candidate")
                record = self.state.read_candidate(run_id, candidate_id)
                recovery = record.recovery
                if recovery is None or recovery.recovery_id != recovery_id:
                    raise RuntimeError("RecoveryIdentityMismatch")
                if recovery.state == "restored":
                    return recovery
                if candidate_recovery_point(record) != recovery.point:
                    raise RuntimeError("RecoveryCheckpointChanged")
                if recovery.fenced_session_ids:
                    proof = self.workspace.read_approval(recovery_id)
                    if proof.get("recovery") != recovery.model_dump(
                        mode="json", exclude={"state"}
                    ):
                        raise RuntimeError("RecoveryHostQuiescenceRequired")
                self.workspace.require_recoverable(record)
                frozen = self.state.read_frozen_spec(run.frozen_spec_id)
                recovery.state = "restoring"
                self.state.write_candidate(run_id, record)
                restored = self.workspace.restore(
                    record,
                    recovery,
                    {
                        RESULTS_TSV_RELATIVE_PATH: render_results_tsv(
                            record.results_ledger, frozen.spec.metric_name
                        )
                    },
                )
                if restored.provider != recovery.point.artifact_ref.provider:
                    raise RuntimeError("RecoveryArtifactProviderMismatch")
                if not self.workspace.matches_checkpoint(
                    record, recovery.point.artifact_ref, restored
                ):
                    raise RuntimeError(
                        "RecoveryArtifactMismatch: restored tree is not the checkpoint"
                    )
                self.candidate_ledger.workspace.check(record, frozen.spec.metric_name)
                # Tool-copy staging belonged to the interrupted, uncommitted attempt.
                record.pending_tool_copies = []
                self._reconcile_recovered_settlements(run, frozen, record)
                recovery.state = "restored"
                recovery.restored_artifact_ref = restored
                recovery.restored_at = self.clock.timestamp()
                self.state.write_candidate(run_id, record)
                return recovery

    def _reconcile_recovered_settlements(
        self, run: RunRecord, frozen: FrozenSpec, record: CandidateRecord
    ) -> None:
        """Repair derived state from committed Evidence, never rerun a verifier."""
        if frozen.spec.shared_dir.enabled:
            assert record.recovery is not None
            self.shared_tools.publication.recover_candidate_publications(
                run.run_id,
                record.candidate_id,
                {
                    tool.tool_id
                    for iteration in record.iterations
                    for tool in iteration.shared_tools
                },
                record.recovery.recovery_id,
            )
        records = self.state.read_candidates(run.run_id)
        for candidate in records:
            best = best_artifact_iteration_record(
                candidate, frozen.spec.metric_direction
            )
            if best is not None and candidate.score_report is not None:
                report = candidate.score_report.model_copy(
                    update={
                        "aggregate_score": best.score,
                        "disposition": best.disposition,
                    }
                )
                if update_best_seen(run, frozen.spec, report):
                    self.checkpoints.write_best(run, frozen.spec, candidate, best)
        run.candidates_evaluated = sum(c.status == "evaluated" for c in records)
        self.persist_run(run)
        for session in self.sessions.state.read_agent_sessions(run.run_id):
            if session.candidate_id != record.candidate_id:
                continue
            count = sum(
                i.agent_session_id == session.agent_session_id
                for i in record.iterations
            )
            session.counters["verifier_runs"] = count
            self.sessions.state.write_agent_session(session)
        for iteration in record.iterations:
            if iteration.agent_session_id is not None or (
                frozen.spec.strategy.orchestration_mode == "adaptive_search"
            ):
                self.annotation_admission.create(
                    run.run_id, record.candidate_id, iteration.iteration
                )
            if (
                frozen.spec.strategy.requires_llm_verifier
                and iteration.settlement_id is not None
                and iteration.artifact_hash is not None
            ):
                try:
                    allocation = frozen.spec.strategy.allocation
                    rules = allocation.verifier_rules() if allocation is not None else None
                    self.settlement.verifier.ensure_assessment(
                        verifier_subject(frozen, record, iteration),
                        rules.policy_id if rules is not None else None,
                        rules.score_names if rules is not None else [],
                    )
                except Exception:
                    # Selection or candidate recovery can replay this enqueue.
                    pass
        self.state.write_candidate(run.run_id, record)
