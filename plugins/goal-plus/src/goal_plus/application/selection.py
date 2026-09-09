"""Selection state transitions, yielding required verification to the host."""

from typing import Any, Callable, Generator, Literal
from goal_plus.application.candidate_execution import (
    require_run_not_invalidated,
)
from goal_plus.domain.artifacts import (
    ArtifactRef,
)
from goal_plus.domain.evidence import (
    ScoreReport,
)
from goal_plus.domain.search import (
    RunState,
)
from goal_plus.domain.evidence_policy import (
    artifact_iteration_eligible,
    iteration_artifact_ref,
)
from goal_plus.ports.state import (
    CandidateRecord,
    RunRecord,
)
from goal_plus.application.candidate_recovery import CandidateRecoveryWorkflow
from goal_plus.application.candidate_artifacts import CandidateArtifacts
from goal_plus.application.candidate_ledger import CandidateLedger
from goal_plus.application.candidate_checkpoints import CandidateCheckpoints
from goal_plus.application.annotation import AnnotationWorkflow
from goal_plus.application.annotation_admission import AnnotationAdmission
from goal_plus.application.goal_authorization import GoalAuthorization
from goal_plus.ports.candidate_settlement import CandidateSettlementState
from goal_plus.ports.candidate_verification import CandidateVerificationRequest


class Selection:
    def __init__(
        self,
        *,
        state: CandidateSettlementState,
        recovery: CandidateRecoveryWorkflow,
        artifacts: CandidateArtifacts,
        candidate_ledger: CandidateLedger,
        checkpoints: CandidateCheckpoints,
        annotation: AnnotationWorkflow,
        annotation_admission: AnnotationAdmission,
        authorization: GoalAuthorization,
        persist_run: Callable[[RunRecord], None],
    ) -> None:
        self.state, self.recovery, self.artifacts, self.candidate_ledger = (
            state,
            recovery,
            artifacts,
            candidate_ledger,
        )
        self.checkpoints, self.annotation, self.annotation_admission = (
            checkpoints,
            annotation,
            annotation_admission,
        )
        self.authorization, self.persist_run = authorization, persist_run

    def select(
        self, run_id: str
    ) -> Generator[CandidateVerificationRequest, ScoreReport, dict[str, Any]]:
        self.authorization.require_search_execution(run_id)
        self._assert_required_evidence_annotations_complete(run_id)
        with self.state.lock_run(run_id):
            for candidate in self.state.read_candidates(run_id):
                self.recovery.require_closeout(run_id, candidate)
            run = self.state.read_run(run_id)
            require_run_not_invalidated(run, "select")
            if run.state not in {
                RunState.RUNNING,
                RunState.WAITING_FOR_WORKERS,
                RunState.SELECTING,
                RunState.SELECTION_BLOCKED,
                RunState.READY_TO_PROMOTE,
            }:
                raise RuntimeError(f"cannot select candidate from state {run.state}")
            run.state = RunState.SELECTING
            run.budget_used.pop("selection_blocked_reason", None)
            self.persist_run(run)
        frozen = self.state.read_frozen_spec(run.frozen_spec_id)
        records = self.state.read_candidates(run_id)
        options = self._selection_options(run, records, frozen.spec.metric_direction)
        if not options:
            self._mark_selection_blocked(
                run_id,
                "no verifier-backed candidate iteration is eligible for selection",
            )
            raise RuntimeError("no verified candidates available for selection")

        maximize = frozen.spec.metric_direction == "maximize"
        ranked = sorted(
            options,
            key=lambda item: (
                item[0] if maximize else -item[0],
                item[1].candidate_id == run.best_candidate_id,
            ),
            reverse=True,
        )
        selected_score: float | None = None
        selected_record: CandidateRecord | None = None
        selected_iteration: int | None = None
        selected_artifact_ref: ArtifactRef | None = None
        selected_git_head: str | None = None
        final_report: ScoreReport | None = None
        selection_evidence_source = "parent_verifier"
        for (
            option_score,
            option_record,
            option_iteration,
            option_artifact_ref,
        ) in ranked:
            option_record = self.state.read_candidate(
                run_id,
                option_record.candidate_id,
            )
            is_thinkthread = option_record.task.workspace_backend == "thinkthread"
            if option_artifact_ref and not is_thinkthread:
                self.artifacts.restore_candidate_artifact(
                    option_record,
                    frozen.spec.metric_name,
                    self.candidate_ledger.projection(
                        frozen,
                        option_record,
                    ),
                    option_artifact_ref,
                    (
                        f"goal-plus select {option_record.candidate_id} "
                        f"iteration {option_iteration}"
                    ),
                )
            worker_iteration = next(
                (
                    iteration
                    for iteration in option_record.iterations
                    if iteration.iteration == option_iteration
                    and (
                        iteration.artifact_ref == option_artifact_ref
                        or (
                            iteration.artifact_ref is None
                            and option_artifact_ref is not None
                            and iteration.git_head == option_artifact_ref.id
                        )
                    )
                    and iteration.agent_session_id is not None
                ),
                None,
            )
            current = (
                None
                if is_thinkthread
                else self.artifacts.candidate_artifact_state(run, frozen, option_record)
            )
            if (
                worker_iteration is not None
                and artifact_iteration_eligible(worker_iteration)
                and (
                    is_thinkthread
                    or (
                        current is not None
                        and worker_iteration.artifact_hash == current.artifact_hash
                        and current.artifact_clean
                    )
                )
            ):
                selected_score = option_score
                selected_record = option_record
                selected_iteration = option_iteration
                selected_artifact_ref = option_artifact_ref
                selected_git_head = (
                    option_artifact_ref.id
                    if option_artifact_ref is not None
                    and option_artifact_ref.kind == "git_commit"
                    else None
                )
                selection_evidence_source = "worker_evidence"
                break
            if is_thinkthread:
                # Provider-native selection never restores a Child branch or
                # reruns against a live tree. Only settled passing snapshot
                # Evidence can enter the options above.
                continue
            report = yield CandidateVerificationRequest(
                run_id, option_record.candidate_id
            )
            if report.process_passed and report.aggregate_score is not None:
                selected_score = report.aggregate_score
                selected_record = option_record
                selected_iteration = option_iteration
                selected_artifact_ref = option_artifact_ref
                selected_git_head = (
                    option_artifact_ref.id
                    if option_artifact_ref is not None
                    and option_artifact_ref.kind == "git_commit"
                    else None
                )
                final_report = report
                break

        if selected_record is None or selected_score is None:
            self._mark_selection_blocked(
                run_id,
                "all eligible candidate revisions failed verification",
            )
            raise RuntimeError("no selected candidate has passing verification")

        selected_best_evidence = next(
            (
                iteration
                for iteration in selected_record.iterations
                if iteration.iteration == selected_iteration
                and iteration_artifact_ref(
                    iteration, selected_record.task.workspace_backend
                )
                == selected_artifact_ref
                and artifact_iteration_eligible(iteration)
            ),
            None,
        )
        if selected_best_evidence is None:
            raise RuntimeError("selected candidate has no exact best iteration")
        if selected_record.task.workspace_backend == "thinkthread":
            selected_changed_files = list(selected_best_evidence.changed_files)
            selected_artifact_hash = selected_best_evidence.artifact_hash
            if not selected_artifact_hash:
                raise RuntimeError(
                    "selected ThinkThread Evidence has no artifact digest"
                )
        else:
            selected_changed_files = self.artifacts.workspace.candidate_changed_files(
                run, selected_record, selected_artifact_ref
            )
            selected_artifact_hash = self.artifacts.workspace.artifact_hash(
                selected_record.task.workspace,
                selected_changed_files,
            )
        with self.state.lock_run(run_id):
            run = self.state.read_run(run_id)
            require_run_not_invalidated(run, "record selection")
            self.annotation.close_queue(
                run_id,
                reason="search_selected",
            )
            run.state = RunState.READY_TO_PROMOTE
            run.selected_candidate_id = selected_record.candidate_id
            run.best_candidate_id = selected_record.candidate_id
            run.best_score = selected_score
            run.selected_score = selected_score
            run.selected_iteration = selected_iteration
            run.selected_git_head = selected_git_head
            run.selected_artifact_ref = selected_artifact_ref
            run.selected_artifact_hash = selected_artifact_hash
            run.budget_used.pop("selection_blocked_reason", None)
            selected_record = self.state.read_candidate(
                run_id, selected_record.candidate_id
            )
            selected_best = next(
                (
                    iteration
                    for iteration in selected_record.iterations
                    if iteration.iteration == selected_iteration
                    and iteration_artifact_ref(
                        iteration, selected_record.task.workspace_backend
                    )
                    == selected_artifact_ref
                    and artifact_iteration_eligible(iteration)
                ),
                None,
            )
            if selected_best is None:
                raise RuntimeError("selected candidate has no exact best iteration")
            self.checkpoints.write_best(
                run, frozen.spec, selected_record, selected_best
            )
            selected_record.promotion_report = None
            selected_record.promotion_evidence = None
            self.state.write_candidate(run_id, selected_record)
            self.persist_run(run)
        return {
            "selected_candidate_id": selected_record.candidate_id,
            "selected_score": selected_score,
            "selected_iteration": selected_iteration,
            "selected_git_head": selected_git_head,
            "selected_artifact_ref": (
                run.selected_artifact_ref.model_dump(mode="json")
                if run.selected_artifact_ref is not None
                else None
            ),
            "selected_artifact_hash": selected_artifact_hash,
            "selection_basis_score": (
                next(
                    (
                        score
                        for score, record, iteration, artifact_ref in ranked
                        if record.candidate_id == selected_record.candidate_id
                        and iteration == selected_iteration
                        and artifact_ref == selected_artifact_ref
                    ),
                    selected_score,
                )
            ),
            "final_verifier_score": (
                final_report.aggregate_score if final_report else selected_score
            ),
            "selection_evidence_source": selection_evidence_source,
            "best_candidate_id": run.best_candidate_id,
            "best_score": run.best_score,
        }

    def _mark_selection_blocked(self, run_id: str, reason: str) -> None:
        run = self.state.read_run(run_id)
        run.state = RunState.SELECTION_BLOCKED
        run.budget_used["selection_blocked_reason"] = reason
        self.persist_run(run)

    def _selection_options(
        self,
        run: RunRecord,
        records: list[CandidateRecord],
        metric_direction: Literal["maximize", "minimize"],
    ) -> list[tuple[float, CandidateRecord, int | None, ArtifactRef | None]]:
        options: list[
            tuple[float, CandidateRecord, int | None, ArtifactRef | None]
        ] = []
        for record in records:
            if record.task.workspace_backend == "thinkthread":
                for iteration in reversed(record.iterations):
                    artifact_ref = iteration_artifact_ref(
                        iteration, record.task.workspace_backend
                    )
                    if (
                        artifact_iteration_eligible(iteration)
                        and iteration.disposition not in {"discard", "failure"}
                        and artifact_ref is not None
                        and artifact_ref.kind == "fs_snapshot"
                        and artifact_ref.provider == "thinkthread"
                    ):
                        assert iteration.score is not None
                        options.append(
                            (
                                iteration.score,
                                record,
                                iteration.iteration,
                                artifact_ref,
                            )
                        )
                continue
            current_changed = self.artifacts.workspace.candidate_changed_files(
                run, record
            )
            current_artifact_hash = self.artifacts.workspace.artifact_hash(
                record.task.workspace, current_changed
            )
            report_is_represented = False
            for iteration in reversed(record.iterations):
                if (
                    iteration.process_passed is not True
                    or iteration.score is None
                    or iteration.touched_denied_files
                    or iteration.changed_outside_allowed
                    or iteration.disposition in {"discard", "failure"}
                ):
                    continue
                artifact_ref = iteration_artifact_ref(
                    iteration, record.task.workspace_backend
                )
                if artifact_ref and artifact_iteration_eligible(iteration):
                    options.append(
                        (
                            iteration.score,
                            record,
                            iteration.iteration,
                            artifact_ref,
                        )
                    )
                elif iteration.artifact_hash == current_artifact_hash:
                    options.append((iteration.score, record, iteration.iteration, None))
                if (
                    record.score_report
                    and iteration.artifact_hash == current_artifact_hash
                    and iteration.process_passed == record.score_report.process_passed
                    and iteration.score == record.score_report.aggregate_score
                ):
                    report_is_represented = True

            if (
                record.score_report
                and record.score_report.process_passed
                and record.score_report.aggregate_score is not None
                and not record.touched_denied_files
                and not record.changed_outside_allowed
                and not report_is_represented
            ):
                options.append(
                    (record.score_report.aggregate_score, record, None, None)
                )
        return options

    def _assert_required_evidence_annotations_complete(self, run_id: str) -> None:
        run = self.state.read_run(run_id)
        frozen = self.state.read_frozen_spec(run.frozen_spec_id)
        if not frozen.supplemental_evaluation_required:
            return
        self.annotation_admission.ensure(run_id)
        incomplete: list[str] = []
        for task in self.annotation.list_tasks(run_id):
            supplemental = (
                task.view.supplemental_evaluation if task.view is not None else None
            )
            if task.state != "completed" or supplemental is None:
                incomplete.append(
                    f"{task.candidate_id}/iteration-{task.iteration:04d}={task.state}"
                )
        if not incomplete:
            return
        try:
            self.annotation_admission.ensure(run_id)
        except Exception:
            pass
        reason = (
            "required supplemental Evidence annotations are incomplete: "
            + ", ".join(incomplete)
        )
        self._mark_selection_blocked(run_id, reason)
        raise RuntimeError(reason)
