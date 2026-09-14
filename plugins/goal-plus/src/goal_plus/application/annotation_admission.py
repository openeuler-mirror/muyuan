"""Annotation admission from committed Evidence, independent of model execution."""

from __future__ import annotations

from typing import Literal

from goal_plus.application.annotation import (
    AnnotationWorkflow,
    EVIDENCE_ANNOTATION_RUN_STATES,
)
from goal_plus.application.search_context import SearchContext
from goal_plus.domain.annotation import (
    EvidenceAnnotationQueue,
    EvidenceAnnotationTask,
    EvidenceArtifactViewReference,
    EvidenceComparisonReference,
    ResolvedEvidenceAnnotatorProfile,
)
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.evidence import IterationRecord
from goal_plus.domain.evidence_policy import (
    artifact_iteration_eligible,
    iteration_artifact_ref,
    iteration_matches_evidence_reference,
)
from goal_plus.domain.search import FrozenSpec
from goal_plus.domain.serialization import canonical_json, sha256_text
from goal_plus.domain.time import outer_deadline_epoch
from goal_plus.ports.annotation import (
    AnnotationAdmissionSource,
    AnnotationCandidateEvidence,
    AnnotationAdmissionResult,
    AnnotationAdmissionError,
    AnnotationTaskKey,
    ThinkThreadAnnotationDispatch,
)

MAX_EVIDENCE_COMPARISON_PEERS = 8
OUTER_DEADLINE_ENV = "GOAL_PLUS_OUTER_DEADLINE_AT"
SUPPLEMENTAL_EVALUATION_REQUIRED_ENV = "GOAL_PLUS_SUPPLEMENTAL_EVALUATION_REQUIRED"
SUPPLEMENTAL_EVALUATION_ENABLED_ENV = "GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED"


class AnnotationAdmission:
    def __init__(
        self,
        *,
        workflow: AnnotationWorkflow,
        source: AnnotationAdmissionSource,
        context: SearchContext,
    ) -> None:
        self.workflow = workflow
        self.tasks = workflow.tasks
        self.evidence = workflow.evidence
        self.clock = workflow.clock
        self.source = source
        self.context = context

    def claim_next(
        self, run_id: str
    ) -> tuple[EvidenceAnnotationQueue, EvidenceAnnotationTask | None]:
        while True:
            queue = self.workflow.queue(run_id)
            if queue.state != "open":
                return queue, None
            eligible = self.eligible(run_id)
            if not eligible:
                return self.workflow.queue(run_id), None
            task = self.workflow.claim(run_id, *eligible[0])
            if task is not None:
                return self.workflow.queue(run_id), task

    def prepare_thinkthread_dispatch(self, run_id: str) -> ThinkThreadAnnotationDispatch:
        baseline = self.evidence.read_run(run_id).source_artifact_ref
        if (
            baseline is None
            or baseline.kind != "fs_snapshot"
            or baseline.provider != "thinkthread"
        ):
            raise RuntimeError("ThinkThread annotator requires the runtime-owned S0")
        eligible = self.eligible(run_id)
        if not eligible:
            raise RuntimeError("ThinkThread annotator spawn requires an eligible task")
        task = self.workflow.task(run_id, *eligible[0])
        if task is None or task.profile is None or task.profile.runtime_provider != "thinkthread":
            raise RuntimeError("ThinkThread annotation task has no ThinkThread profile")
        return ThinkThreadAnnotationDispatch(
            baseline_snapshot_id=baseline.id,
            queue=self.workflow.queue(run_id),
            task=task,
        )

    def create(
        self, run_id: str, candidate_id: str, iteration_number: int
    ) -> EvidenceAnnotationTask:
        """Admit committed Evidence; the caller holds the run lock."""
        with self.tasks.lock_tasks(run_id):
            run = self.evidence.read_run(run_id)
            frozen = self.source.read_frozen_spec(run.frozen_spec_id)
            iteration = next(
                (
                    item
                    for record in self.source.read_candidate_evidence(run_id)
                    if record.candidate_id == candidate_id
                    for item in record.iterations
                    if item.iteration == iteration_number
                ),
                None,
            )
            if iteration is None or (
                iteration.agent_session_id is None
                and frozen.spec.strategy.orchestration_mode != "adaptive_search"
            ):
                raise RuntimeError(
                    "annotation requires settled worker or Adaptive Evidence"
                )
            task = self._create_locked(run_id, frozen, candidate_id, iteration)
            self.repair_queue(self.workflow.queue(run_id))
            return task

    def ensure(self, run_id: str) -> None:
        """Repair missing tasks without affecting settled verifier Evidence."""
        with self.tasks.lock_run(run_id):
            if not self.workflow.is_active(run_id):
                return
            run = self.evidence.read_run(run_id)
            frozen = self.source.read_frozen_spec(run.frozen_spec_id)
            for record in self.source.read_candidate_evidence(run_id):
                for iteration in record.iterations:
                    if (
                        iteration.agent_session_id is None
                        and frozen.spec.strategy.orchestration_mode != "adaptive_search"
                    ):
                        continue
                    try:
                        if (
                            self.workflow.task(
                                run_id, record.candidate_id, iteration.iteration
                            )
                            is None
                        ):
                            self.create(
                                run_id, record.candidate_id, iteration.iteration
                            )
                    except Exception:
                        # A later settlement or Evidence read retries admission.
                        continue

    def repair_queue(
        self,
        queue: EvidenceAnnotationQueue,
    ) -> EvidenceAnnotationQueue:
        """Rebuild the open queue projection from durable task admissions."""

        if queue.state != "open":
            return queue
        tasks = [
            task
            for task in self.tasks.list_tasks(queue.run_id)
            if task.queue_generation == queue.generation
        ]
        revision = max(
            queue.revision,
            max((task.queue_revision for task in tasks), default=0),
        )
        accepted_tasks = len(tasks)
        if queue.revision == revision and queue.accepted_tasks == accepted_tasks:
            return queue
        repaired = queue.model_copy(
            update={
                "revision": revision,
                "accepted_tasks": accepted_tasks,
                "updated_at": self.clock.timestamp(),
            }
        )
        self.tasks.write_queue(repaired)
        return repaired

    def resolve_profile(
        self,
        frozen: FrozenSpec,
    ) -> tuple[ResolvedEvidenceAnnotatorProfile | None, str | None]:
        strategy = frozen.spec.strategy
        configured = strategy.evidence_annotator
        annotation_host = frozen.agent_harness
        worker_launch = strategy.worker_launch
        model = configured.model

        reasoning_effort = configured.reasoning_effort
        if (
            reasoning_effort is None
            and worker_launch is not None
        ):
            reasoning_effort = worker_launch.reasoning_effort
        if reasoning_effort is None:
            reasoning_effort = (
                self.context.source.read_host_settings().annotation_reasoning_effort
                or None
            )
        return (
            ResolvedEvidenceAnnotatorProfile(
                agent_harness=annotation_host,
                runtime_provider=frozen.runtime_provider,
                model=model,
                reasoning_effort=reasoning_effort,
                timeout_seconds=configured.timeout_seconds,
            ),
            None,
        )

    def artifact_view_refs(
        self,
        run_id: str,
        *,
        target_candidate_id: str,
        target_iteration: IterationRecord,
        comparison_basis: list[EvidenceComparisonReference],
    ) -> list[EvidenceArtifactViewReference]:
        records = {
            record.candidate_id: record
            for record in self.source.read_candidate_evidence(run_id)
        }

        def build(
            *,
            role: Literal["target", "peer"],
            record: AnnotationCandidateEvidence,
            iteration: IterationRecord,
        ) -> EvidenceArtifactViewReference:
            artifact_ref = iteration.artifact_ref
            if artifact_ref is None and iteration.git_head is not None:
                artifact_ref = ArtifactRef(
                    kind="git_commit",
                    id=iteration.git_head,
                    provider=record.workspace_provider,
                )
            if artifact_ref is None:
                raise RuntimeError(
                    "annotation Artifact View requires immutable Evidence"
                )
            identity = canonical_json(
                {
                    "run_id": run_id,
                    "candidate_id": record.candidate_id,
                    "iteration": iteration.iteration,
                    "artifact_ref": artifact_ref.model_dump(mode="json"),
                }
            )
            view_id = f"av_{sha256_text(identity)[:16]}"
            return EvidenceArtifactViewReference(
                view_id=view_id,
                role=role,
                candidate_id=record.candidate_id,
                iteration=iteration.iteration,
                artifact_ref=artifact_ref,
                workspace_provider=record.workspace_provider,
                display_name=(
                    f"{role}-{record.candidate_id}-iteration-{iteration.iteration:04d}"
                ),
            )

        target_record = records.get(target_candidate_id)
        if target_record is None:
            raise RuntimeError("annotation target candidate is unavailable")
        views = [
            build(
                role="target",
                record=target_record,
                iteration=target_iteration,
            )
        ]
        for reference in comparison_basis:
            peer_record = records.get(reference.candidate_id)
            if peer_record is None:
                raise RuntimeError("annotation comparison candidate is unavailable")
            peer_iteration = next(
                (
                    item
                    for item in peer_record.iterations
                    if iteration_matches_evidence_reference(
                        item, reference, peer_record.workspace_provider
                    )
                ),
                None,
            )
            if peer_iteration is None:
                raise RuntimeError("annotation comparison Evidence is unavailable")
            views.append(
                build(
                    role="peer",
                    record=peer_record,
                    iteration=peer_iteration,
                )
            )
        return views

    def _create_locked(
        self,
        run_id: str,
        frozen: FrozenSpec,
        candidate_id: str,
        iteration: IterationRecord,
    ) -> EvidenceAnnotationTask:
        if (
            (iteration.git_head is None and iteration.artifact_ref is None)
            or (
                iteration.attempt_base_git_head is None
                and iteration.attempt_base_artifact_ref is None
            )
            or iteration.settlement_id is None
        ):
            raise RuntimeError("worker Evidence requires exact immutable artifacts")
        existing = self.workflow.task(run_id, candidate_id, iteration.iteration)
        if existing is not None:
            if (
                existing.attempt_commit != iteration.git_head
                or existing.attempt_base_commit != iteration.attempt_base_git_head
                or existing.settlement_id != iteration.settlement_id
                or existing.attempt_artifact_ref != iteration.artifact_ref
                or existing.attempt_base_artifact_ref
                != iteration.attempt_base_artifact_ref
            ):
                raise RuntimeError("Evidence annotation task is immutable")
            return existing

        try:
            profile, error = self.resolve_profile(frozen)
        except Exception as exc:
            profile = None
            error = f"invalid annotator profile: {type(exc).__name__}: {exc}"
        outer_deadline = self.context.outer_deadline(run_id)
        if outer_deadline:
            deadline_epoch = outer_deadline_epoch(outer_deadline)
            if deadline_epoch is None:
                error = f"invalid {OUTER_DEADLINE_ENV} value"
                profile = None
            elif deadline_epoch <= self.clock.now():
                error = "annotation outer deadline already expired"
                profile = None
        now = self.clock.timestamp()
        supplemental_enabled = frozen.supplemental_evaluation_enabled
        if frozen.supplemental_evaluation_required and not supplemental_enabled:
            error = (
                f"{SUPPLEMENTAL_EVALUATION_REQUIRED_ENV}=1 requires "
                f"{SUPPLEMENTAL_EVALUATION_ENABLED_ENV}=1"
            )
            profile = None
        context = self.context.task_context(run_id, fallback=frozen.spec.objective)
        comparison_basis = (
            self.comparison_basis(run_id, target_candidate_id=candidate_id)
            if supplemental_enabled
            else []
        )
        artifact_views = self.artifact_view_refs(
            run_id,
            target_candidate_id=candidate_id,
            target_iteration=iteration,
            comparison_basis=comparison_basis,
        )
        queue = self.workflow.queue(run_id)
        if queue.state != "open" or not self.workflow.is_active(run_id):
            raise RuntimeError("Evidence annotation queue is closed")
        task_revision = max(
            (
                task.queue_revision
                for task in self.tasks.list_tasks(run_id)
                if task.queue_generation == queue.generation
            ),
            default=0,
        )
        queue_revision = max(queue.revision, task_revision) + 1
        task = EvidenceAnnotationTask(
            task_id=f"annotation_{iteration.settlement_id}",
            settlement_id=iteration.settlement_id,
            run_id=run_id,
            candidate_id=candidate_id,
            iteration=iteration.iteration,
            attempt_base_commit=iteration.attempt_base_git_head,
            attempt_commit=iteration.git_head,
            attempt_base_artifact_ref=iteration.attempt_base_artifact_ref,
            attempt_artifact_ref=iteration.artifact_ref,
            attempt_changed_files=list(iteration.attempt_changed_files),
            task_context_source=context.source,
            task_context_ref=context.reference,
            task_context_sha256=sha256_text(context.text),
            supplemental_evaluation_enabled=supplemental_enabled,
            comparison_basis=comparison_basis,
            artifact_views=artifact_views,
            profile=profile,
            outer_deadline_at=outer_deadline,
            queue_generation=queue.generation,
            queue_revision=queue_revision,
            state="terminal_error" if error else "pending",
            error_fingerprint=sha256_text(error) if error else None,
            last_error=error,
            created_at=now,
            updated_at=now,
        )
        # The task is the durable admission fact. Until queue.json catches up,
        # its future revision keeps the task ineligible for claiming.
        self.tasks.write_task(task)
        return task

    def reconcile(
        self,
        run_id: str,
    ) -> AnnotationAdmissionResult:
        """Backfill every settled worker iteration into the open task queue."""

        created: list[AnnotationTaskKey] = []
        errors: list[AnnotationAdmissionError] = []
        with self.tasks.lock_run(run_id):
            run = self.evidence.read_run(run_id)
            with self.tasks.lock_tasks(run_id):
                queue = self.workflow.queue(run_id)
                if (
                    queue.state != "open"
                    or run.invalidated_at is not None
                    or run.state not in EVIDENCE_ANNOTATION_RUN_STATES
                ):
                    return AnnotationAdmissionResult(
                        state="closed", created=created, errors=errors, queue=queue
                    )
                frozen = self.source.read_frozen_spec(run.frozen_spec_id)
                queue = self.repair_queue(queue)
                settled = [
                    (record.candidate_id, iteration)
                    for record in self.source.read_candidate_evidence(run_id)
                    for iteration in record.iterations
                    if iteration.agent_session_id is not None
                ]
                settled.sort(
                    key=lambda item: (
                        item[1].created_at,
                        item[0],
                        item[1].iteration,
                    )
                )
                for candidate_id, iteration in settled:
                    if (
                        self.workflow.task(
                            run_id,
                            candidate_id,
                            iteration.iteration,
                        )
                        is not None
                    ):
                        continue
                    try:
                        self._create_locked(
                            run_id,
                            frozen,
                            candidate_id,
                            iteration,
                        )
                    except (OSError, RuntimeError, ValueError) as exc:
                        errors.append(
                            AnnotationAdmissionError(
                                candidate_id=candidate_id,
                                iteration=iteration.iteration,
                                error=f"{type(exc).__name__}: {exc}"[:2000],
                            )
                        )
                        continue
                    created.append(
                        AnnotationTaskKey(
                            candidate_id=candidate_id, iteration=iteration.iteration
                        )
                    )
                queue = self.repair_queue(self.workflow.queue(run_id))
                return AnnotationAdmissionResult(
                    state="open", created=created, errors=errors, queue=queue
                )

    def comparison_basis(
        self,
        run_id: str,
        *,
        target_candidate_id: str,
    ) -> list[EvidenceComparisonReference]:
        run = self.evidence.read_run(run_id)
        frozen = self.source.read_frozen_spec(run.frozen_spec_id)
        maximize = frozen.spec.metric_direction == "maximize"
        settled = []
        for record in self.source.read_candidate_evidence(run_id):
            if record.candidate_id == target_candidate_id:
                continue
            eligible = [
                iteration
                for iteration in record.iterations
                if iteration.agent_session_id is not None
                and artifact_iteration_eligible(iteration)
                and iteration.disposition in {None, "keep", "retain"}
            ]
            if not eligible:
                continue
            best = max(
                eligible,
                key=lambda iteration: (
                    float(iteration.score) if maximize else -float(iteration.score),
                    iteration.created_at,
                    iteration.iteration,
                ),
            )
            settled.append((best.created_at, record, best))
        settled.sort(
            key=lambda item: (
                item[0],
                item[1].candidate_id,
                item[2].iteration,
            )
        )
        return [
            EvidenceComparisonReference(
                candidate_id=record.candidate_id,
                iteration=iteration.iteration,
                commit=iteration.git_head,
                artifact_ref=iteration_artifact_ref(
                    iteration, record.workspace_provider
                ),
            )
            for _, record, iteration in settled[-MAX_EVIDENCE_COMPARISON_PEERS:]
        ]

    def eligible(
        self,
        run_id: str,
        *,
        now_epoch: float | None = None,
    ) -> list[tuple[str, int]]:
        self.reconcile(run_id)
        if not self.workflow.is_active(run_id):
            return []
        queue = self.workflow.queue(run_id)
        now_epoch = self.clock.now() if now_epoch is None else now_epoch
        eligible = []
        for candidate_id, iteration in self.workflow.pending(run_id):
            task = self.workflow.task(run_id, candidate_id, iteration)
            if task is None or task.state not in {"pending", "retry_wait"}:
                continue
            if (
                task.queue_generation != queue.generation
                or task.queue_revision > queue.revision
            ):
                continue
            deadline = outer_deadline_epoch(task.outer_deadline_at)
            retry_at = outer_deadline_epoch(task.next_attempt_at)
            if deadline is not None and deadline <= now_epoch:
                continue
            if retry_at is not None and retry_at > now_epoch:
                continue
            eligible.append((candidate_id, iteration))
        return eligible
