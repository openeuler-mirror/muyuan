"""Exact selected artifact admission and local publication preparation."""

from pathlib import Path
from typing import Callable
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.evidence_policy import artifact_iteration_eligible
from goal_plus.domain.promotion import PromotionRecord
from goal_plus.domain.search import RunState
from goal_plus.ports.state import RunRecord
from goal_plus.ports.clock import Clock
from goal_plus.ports.publication import PromotionState, LocalPublicationPreparation
from goal_plus.application.goal_authorization import GoalAuthorization
from goal_plus.application.candidate_execution import require_run_not_invalidated
from goal_plus.application.candidate_spec import iteration_spec_id
from collections.abc import Generator
from goal_plus.domain.evidence import ScoreReport
from goal_plus.ports.candidate_verification import CandidateVerificationRequest
from goal_plus.application.candidate_artifacts import CandidateArtifacts
from goal_plus.application.candidate_ledger import CandidateLedger
from goal_plus.application.candidate_recovery import CandidateRecoveryWorkflow
from goal_plus.application.thinkthread_publication import ThinkThreadPublication


class Promotion:
    def __init__(
        self,
        *,
        state: PromotionState,
        io: LocalPublicationPreparation,
        clock: Clock,
        persist_run: Callable[[RunRecord], None],
        authorization: GoalAuthorization,
        artifacts: CandidateArtifacts,
        candidate_ledger: CandidateLedger,
        recovery: CandidateRecoveryWorkflow,
        thinkthread: ThinkThreadPublication,
    ) -> None:
        self.state, self.io, self.clock, self.persist_run = (
            state,
            io,
            clock,
            persist_run,
        )
        self.authorization, self.artifacts = authorization, artifacts
        self.candidate_ledger, self.recovery, self.thinkthread = (
            candidate_ledger,
            recovery,
            thinkthread,
        )

    def promote(
        self, run_id: str, candidate_id: str
    ) -> Generator[CandidateVerificationRequest, ScoreReport, Path]:
        self.authorization.require_search_execution(run_id)
        run = self.state.read_run(run_id)
        require_run_not_invalidated(run, "promote")
        if run.selected_candidate_id != candidate_id:
            raise RuntimeError(
                "cannot promote candidate before goal_plus_search_select selects it"
            )
        frozen = self.state.read_frozen_spec(run.selected_frozen_spec_id or run.frozen_spec_id)

        def reject_promotion(message: str) -> None:
            latest_run = self.state.read_run(run_id)
            latest_run.state = RunState.READY_TO_PROMOTE
            self.persist_run(latest_run)
            raise RuntimeError(message)

        record = self.state.read_candidate(run_id, candidate_id)
        selected_artifact_ref = run.selected_artifact_ref
        self.recovery.require_closeout(run_id, record)
        if selected_artifact_ref is None and run.selected_git_head is not None:
            selected_artifact_ref = ArtifactRef(
                kind="git_commit",
                id=run.selected_git_head,
                provider=record.task.workspace_provider,
            )
        if selected_artifact_ref is None:
            reject_promotion(
                "cannot promote candidate without an immutable selected artifact"
            )
        if record.task.workspace_provider == "thinkthread":
            return self.thinkthread.prepare(
                run,
                frozen,
                record,
                selected_artifact_ref,
            )
        current = self.artifacts.candidate_artifact_state(run, frozen, record)
        if run.selected_artifact_hash is not None and (
            current.artifact_hash != run.selected_artifact_hash
            or not current.artifact_clean
        ):
            reject_promotion(
                "cannot promote candidate because the selected artifact changed"
            )
        self.artifacts.restore_candidate_artifact(
            record,
            frozen.spec.metric_name,
            self.candidate_ledger.projection(frozen, record),
            selected_artifact_ref,
            f"goal-plus promote {candidate_id} selected revision",
        )
        selected = self.artifacts.candidate_artifact_state(run, frozen, record)
        self.artifacts.apply_candidate_artifact_state(record, selected)
        detected_changed = selected.changed_files
        artifact_hash = selected.artifact_hash
        self.state.write_candidate(run_id, record)
        if run.selected_artifact_hash is None:
            run.selected_artifact_hash = artifact_hash
            self.persist_run(run)
        elif artifact_hash != run.selected_artifact_hash:
            reject_promotion(
                "cannot promote candidate because the selected artifact changed"
            )
        selected_iteration_evidence = next(
            (
                iteration
                for iteration in record.iterations
                if iteration.iteration == run.selected_iteration
                and iteration_spec_id(run, iteration) == frozen.frozen_spec_id
                and (
                    iteration.artifact_ref == selected_artifact_ref
                    or (
                        iteration.artifact_ref is None
                        and iteration.git_head == run.selected_git_head
                    )
                )
                and artifact_iteration_eligible(iteration)
                and iteration.artifact_hash == selected.artifact_hash
            ),
            None,
        )
        if selected_iteration_evidence is None and (
            not record.score_report or not record.score_report.process_passed
        ):
            reject_promotion("cannot promote candidate without a passing score report")
        if record.touched_denied_files or record.changed_outside_allowed:
            reject_promotion(
                "cannot promote candidate that changed denied/out-of-surface files"
            )

        if frozen.spec.promotion_verifiers:
            promotion_report = yield CandidateVerificationRequest(
                run_id, candidate_id, scope="promotion"
            )
            run = self.state.read_run(run_id)
            record = self.state.read_candidate(run_id, candidate_id)
            detected_changed = self.artifacts.workspace.candidate_changed_files(
                run, record, selected_artifact_ref
            )
            artifact_hash = self.artifacts.workspace.artifact_hash(
                record.task.workspace, detected_changed
            )
            evidence = record.promotion_evidence
            evidence_is_current = bool(
                evidence
                and (evidence.frozen_spec_id or run.frozen_spec_id) == frozen.frozen_spec_id
                and (evidence.spec_hash is None or evidence.spec_hash == frozen.spec_hash)
                and evidence.candidate_id == candidate_id
                and evidence.selected_artifact_ref == selected_artifact_ref
                and evidence.artifact_ref == selected.artifact_ref
                and evidence.artifact_hash == artifact_hash
                and evidence.artifact_hash == run.selected_artifact_hash
                and evidence.passed
            )
            if not promotion_report.promotion_passed or not evidence_is_current:
                reject_promotion(
                    "cannot promote candidate without fresh passing promotion evidence"
                )

        with self.state.lock_run(run_id):
            run = self.state.read_run(run_id)
            require_run_not_invalidated(run, "record promotion")
            patch_path = self.io.patch_path(run_id, candidate_id)
            if self.io.native_promotion(run, record):
                prepared = self.io.prepare_local(
                    run, record, selected_artifact_ref, detected_changed
                )
                if prepared.artifact_path is None:
                    raise RuntimeError(
                        "provider-native promotion did not produce an artifact"
                    )
                patch_path = prepared.artifact_path
                run.promotion = PromotionRecord(
                    frozen_spec_id=frozen.frozen_spec_id, spec_hash=frozen.spec_hash,
                    promotion_id=f"promotion_{candidate_id}",
                    run_id=run_id,
                    candidate_id=candidate_id,
                    provider=prepared.provider,
                    strategy=prepared.strategy,
                    promotion_mode=frozen.spec.workspace.promotion_mode,
                    base_artifact_ref=prepared.base_artifact_ref,
                    selected_artifact_ref=prepared.selected_artifact_ref,
                    publication_artifact_ref=(prepared.publication_artifact_ref),
                    artifact_path=prepared.artifact_path,
                    source_repository=prepared.source_repository,
                    source_subdir=prepared.source_subdir,
                    changed_files=list(prepared.changed_files),
                    provider_handle=dict(prepared.provider_handle),
                    apply_request_id=prepared.provider_handle.get("apply_request_id"),
                    created_at=self.clock.timestamp(),
                )
            else:
                if selected_artifact_ref.kind != "git_commit":
                    raise RuntimeError(
                        "promotion publication currently requires a supported "
                        "provider artifact"
                    )
                self.io.write_patch(
                    Path(run.source_path),
                    record.task.workspace,
                    selected_artifact_ref.id,
                    detected_changed,
                    patch_path,
                )
                base_ref = record.task.base_artifact_ref
                if base_ref is None:
                    base_ref = ArtifactRef(
                        kind="git_commit",
                        id=record.task.workspace_base_revision
                        or selected_artifact_ref.id,
                        provider=record.task.workspace_provider,
                    )
                run.promotion = PromotionRecord(
                    frozen_spec_id=frozen.frozen_spec_id, spec_hash=frozen.spec_hash,
                    promotion_id=f"promotion_{candidate_id}",
                    run_id=run_id,
                    candidate_id=candidate_id,
                    provider=record.task.workspace_provider,
                    strategy="legacy_patch",
                    promotion_mode=frozen.spec.workspace.promotion_mode,
                    base_artifact_ref=base_ref,
                    selected_artifact_ref=selected_artifact_ref,
                    artifact_path=patch_path,
                    source_subdir=record.task.workspace_source_subdir,
                    changed_files=detected_changed,
                    created_at=self.clock.timestamp(),
                )
            run.state = RunState.PROMOTED
            run.selected_candidate_id = candidate_id
            self.persist_run(run)
        return patch_path
