"""Publication snapshot intent, independent verification and durable preparation."""

from pathlib import Path
from typing import Callable
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.evidence_policy import artifact_iteration_eligible
from goal_plus.domain.promotion import (
    PromotionRecord,
    PromotionEvidence,
    PublicationOutcome,
)
from goal_plus.domain.search import FrozenSpec, RunState
from goal_plus.ports.state import RunRecord, CandidateRecord
from goal_plus.ports.clock import Clock
from goal_plus.ports.publication import (
    ThinkThreadPublicationState, ThinkThreadPublicationArtifacts,
    ThinkThreadPublicationApply, PublicationVerification, PublicationSnapshotIntent,
)
from goal_plus.application.candidate_execution import require_run_not_invalidated
from goal_plus.application.candidate_spec import iteration_spec_id
from goal_plus.application.publication_contracts import publication_apply_intent


class ThinkThreadPublication:
    def __init__(
        self,
        state: ThinkThreadPublicationState,
        io: ThinkThreadPublicationArtifacts,
        apply: ThinkThreadPublicationApply,
        verification: PublicationVerification,
        clock: Clock,
        persist_run: Callable[[RunRecord], None],
    ) -> None:
        self.state, self.io, self.clock, self.persist_run = (
            state,
            io,
            clock,
            persist_run,
        )
        self.apply, self.verification = apply, verification

    def prepare(
        self,
        run: RunRecord,
        frozen: FrozenSpec,
        record: CandidateRecord,
        selected_artifact_ref: ArtifactRef,
    ) -> Path:
        candidate_id = record.candidate_id
        if run.state == RunState.PROMOTED and run.promotion is not None:
            if run.promotion.candidate_id != candidate_id:
                raise RuntimeError("existing promotion belongs to another candidate")
            if run.promotion.artifact_path is None:
                raise RuntimeError(
                    "existing ThinkThread promotion omitted its manifest"
                )
            return run.promotion.artifact_path
        if (
            selected_artifact_ref.kind != "fs_snapshot"
            or selected_artifact_ref.provider != "thinkthread"
        ):
            raise RuntimeError(
                "ThinkThread promotion requires an exact selected snapshot"
            )
        iteration = next(
            (
                item
                for item in record.iterations
                if item.iteration == run.selected_iteration
                and iteration_spec_id(run, item) == frozen.frozen_spec_id
                and (item.spec_hash is None or item.spec_hash == frozen.spec_hash)
                and item.artifact_ref == selected_artifact_ref
                and artifact_iteration_eligible(item)
                and item.artifact_hash == run.selected_artifact_hash
            ),
            None,
        )
        if iteration is None:
            raise RuntimeError(
                "cannot promote ThinkThread candidate without exact passing Evidence"
            )
        if record.touched_denied_files or record.changed_outside_allowed:
            raise RuntimeError(
                "cannot promote candidate that changed denied/out-of-surface files"
            )
        baseline = run.source_artifact_ref
        if (
            baseline is None
            or baseline.kind != "fs_snapshot"
            or baseline.provider != "thinkthread"
        ):
            raise RuntimeError("ThinkThread promotion requires the runtime-owned S0")

        source_subdir = record.task.workspace_source_subdir
        dependencies = [
            item.model_dump(mode="json") for item in frozen.spec.workspace.dependencies
        ]
        plan = self.io.plan_thinkthread(
            base_artifact_ref=baseline,
            selected_artifact_ref=selected_artifact_ref,
            source_subdir=source_subdir,
            changed_files=list(iteration.changed_files),
            dependencies=dependencies,
        )
        manifest_path = self.state.publication_path(run.run_id, candidate_id)
        publication_request_id: str | None = None
        if plan.changes:
            root_id = run.source_provider_handle.get("root_thinkthread_id")
            if not isinstance(root_id, str) or not root_id:
                raise RuntimeError("ThinkThread publication has no root owner")
            snapshot_intent = PublicationSnapshotIntent(
                run_id=run.run_id,
                candidate_id=candidate_id,
                root_thinkthread_id=root_id,
                plan=plan,
            )
            patch_mutation = self.io.prepare_snapshot(snapshot_intent)
            publication_request_id = patch_mutation.request_id
            self.io.write_manifest(
                manifest_path,
                run_id=run.run_id,
                candidate_id=candidate_id,
                plan=plan,
                publication_request_id=publication_request_id,
                state="publication_snapshot_applying",
            )
            patch_result = self.io.materialize_snapshot(snapshot_intent)
            if patch_result.request_id != publication_request_id:
                raise RuntimeError(
                    "publication snapshot RequestId changed during execution"
                )
            if patch_result.owner_thinkthread_id != root_id:
                raise RuntimeError(
                    "publication snapshot returned the wrong snapshot owner"
                )
            if (
                patch_result.artifact_ref.provider != "thinkthread"
                or patch_result.artifact_ref.kind != "fs_snapshot"
            ):
                raise RuntimeError("publication returned a non-ThinkThread snapshot")
            publication_snapshot_id = patch_result.artifact_ref.id
        else:
            publication_snapshot_id = baseline.id
        publication_ref = ArtifactRef(
            kind="fs_snapshot",
            id=publication_snapshot_id,
            provider="thinkthread",
        )

        promotion_report = self.verification.verify_publication(
            self.state.read_run(run.run_id),
            frozen,
            candidate_id,
            publication_ref,
            iteration.iteration,
        )
        promotion_report = promotion_report.model_copy(update={
            "frozen_spec_id": frozen.frozen_spec_id, "spec_hash": frozen.spec_hash,
        })
        if not promotion_report.promotion_passed:
            raise RuntimeError(
                "cannot promote candidate without passing exact publication verification"
            )
        publication_hash = self.io.canonical_digest(
            baseline,
            publication_ref,
        )
        provisional = PromotionRecord(
            frozen_spec_id=frozen.frozen_spec_id, spec_hash=frozen.spec_hash,
            promotion_id=f"promotion_{candidate_id}",
            run_id=run.run_id,
            candidate_id=candidate_id,
            provider="thinkthread",
            strategy="fs_apply",
            promotion_mode=frozen.spec.workspace.promotion_mode,
            base_artifact_ref=baseline,
            selected_artifact_ref=selected_artifact_ref,
            publication_artifact_ref=publication_ref,
            artifact_path=manifest_path,
            source_subdir=source_subdir,
            changed_files=list(plan.changed_files),
            provider_handle={
                "dependencies": list(plan.dependencies),
                **(
                    {"publication_request_id": publication_request_id}
                    if publication_request_id is not None
                    else {}
                ),
            },
            created_at=self.clock.timestamp(),
        )
        publication_revision = (
            run.publication_outcome.revision + 1
            if run.publication_outcome is not None
            else 1
        )
        apply_intent = publication_apply_intent(
            provisional,
            publication_revision,
        )
        apply_mutation = self.apply.prepare_apply(apply_intent)
        provisional.apply_request_id = apply_mutation.request_id
        provisional.provider_handle["apply_request_id"] = apply_mutation.request_id
        self.io.write_manifest(
            manifest_path,
            run_id=run.run_id,
            candidate_id=candidate_id,
            plan=plan,
            publication_request_id=publication_request_id,
            publication_snapshot_id=publication_snapshot_id,
            apply_request_id=apply_mutation.request_id,
            state="prepared",
        )
        with self.state.lock_run(run.run_id):
            latest = self.state.read_run(run.run_id)
            require_run_not_invalidated(latest, "record promotion")
            latest.state = RunState.PROMOTED
            latest.search_result = "passed"
            latest.promotion = provisional
            latest.publication_outcome = PublicationOutcome(
                run_id=run.run_id,
                revision=publication_revision,
                state="prepared",
                base_artifact_ref=baseline,
                selected_artifact_ref=selected_artifact_ref,
                publication_artifact_ref=publication_ref,
                apply_request_id=apply_mutation.request_id,
                changed_paths=list(plan.changed_files),
                dependency_summary={
                    "dependencies": list(plan.dependencies),
                    "policy_id": "safe_content_v1",
                    "publication_request_id": publication_request_id,
                },
                updated_at=self.clock.timestamp(),
            )
            record = self.state.read_candidate(run.run_id, candidate_id)
            record.promotion_report = promotion_report
            record.promotion_evidence = PromotionEvidence(
                frozen_spec_id=frozen.frozen_spec_id, spec_hash=frozen.spec_hash,
                candidate_id=candidate_id,
                selected_artifact_ref=selected_artifact_ref,
                artifact_ref=publication_ref,
                artifact_hash=publication_hash,
                passed=True,
                created_at=self.clock.timestamp(),
            )
            self.state.write_candidate(run.run_id, record)
            self.persist_run(latest)
        return manifest_path
