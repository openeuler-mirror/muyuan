"""Deterministic multi-score selection with exact verifier evidence binding."""

from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import Any, Literal

from goal_plus.application.annotation import AnnotationWorkflow
from goal_plus.application.annotation_admission import AnnotationAdmission
from goal_plus.application.candidate_artifacts import CandidateArtifacts
from goal_plus.application.candidate_checkpoints import CandidateCheckpoints
from goal_plus.application.candidate_execution import require_run_not_invalidated, require_fork_ready
from goal_plus.application.candidate_ledger import CandidateLedger
from goal_plus.application.candidate_spec import iteration_spec_id
from goal_plus.application.candidate_recovery import CandidateRecoveryWorkflow
from goal_plus.application.goal_authorization import GoalAuthorization
from goal_plus.application.settlement_verifier import build_dispatch_audit_context, verifier_subject
from goal_plus.application.verifier import (
    VerifierWorkflow,
)
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.evidence import IterationRecord, ScoreReport
from goal_plus.domain.evidence_policy import (
    artifact_iteration_eligible,
    iteration_artifact_ref,
)
from goal_plus.domain.search import FrozenSpec, RunState
from goal_plus.domain.selection import (
    ControllerSelectionDirective,
    ScoreResolution,
    SelectionOption,
    SelectionSubjectKey,
    resolve_candidate_pool,
    select_winner,
)
from goal_plus.domain.verifier import (
    VerifierRecord,
    VerifierTask,
    verifier_subject_identity,
)
from goal_plus.ports.candidate_settlement import CandidateSettlementState
from goal_plus.ports.candidate_verification import CandidateVerificationRequest
from goal_plus.ports.clock import Clock
from goal_plus.ports.state import CandidateRecord, RunRecord


@dataclass(frozen=True)
class _BaseOption:
    score: float
    record: CandidateRecord
    iteration: IterationRecord
    artifact_ref: ArtifactRef | None
    verifier_subject_hash: str

    @property
    def subject(self) -> SelectionSubjectKey:
        return SelectionSubjectKey(
            candidate_id=self.record.candidate_id,
            iteration=self.iteration.iteration,
            settlement_id=self.iteration.settlement_id,
            artifact_hash=self.iteration.artifact_hash,
            verifier_subject_hash=self.verifier_subject_hash,
        )


def _ordered_subjects(
    subjects: set[SelectionSubjectKey],
) -> list[dict[str, Any]]:
    return [
        item.__dict__
        for item in sorted(
            subjects,
            key=lambda item: (
                item.candidate_id,
                item.iteration if item.iteration is not None else -1,
                item.settlement_id or "",
                item.artifact_hash or "",
                item.verifier_subject_hash,
            ),
        )
    ]


class _HardScoreSource:
    key = "hard_score"

    def __init__(self, direction: Literal["maximize", "minimize"]) -> None:
        self.direction = direction

    def resolve(self, option: SelectionOption) -> ScoreResolution:
        return ScoreResolution.resolved(option.hard_score)


class _QualityScoreSource:
    key = "quality_score"
    direction = "maximize"

    def __init__(
        self,
        *,
        tasks: list[VerifierTask],
        records: list[VerifierRecord],
    ) -> None:
        self.tasks = tasks
        self.records = records

    def resolve(self, option: SelectionOption) -> ScoreResolution:
        subject = option.subject
        if (
            subject.iteration is None
            or subject.settlement_id is None
            or subject.artifact_hash is None
            or not subject.verifier_subject_hash
        ):
            return ScoreResolution.unavailable("selection subject is not exact")
        matching_tasks = [
            task
            for task in self.tasks
            if task.subject.candidate_id == subject.candidate_id
            and task.subject.iteration == subject.iteration
            and task.subject.settlement_id == subject.settlement_id
            and task.subject.settlement.attempt_artifact_hash
            == subject.artifact_hash
            and verifier_subject_identity(task.subject)
            == subject.verifier_subject_hash
        ]
        if not matching_tasks and not any(
            task.subject.settlement_id == subject.settlement_id for task in self.tasks
        ):
            return ScoreResolution.pending("verifier submission awaits admission")
        if len(matching_tasks) != 1:
            return ScoreResolution.unavailable("exact verifier task is missing or inconsistent")
        task = matching_tasks[0]
        if task.state in {"pending", "running", "retry_wait"}:
            return ScoreResolution.pending(f"verifier task is {task.state}")
        if task.state == "abandoned":
            return ScoreResolution.unavailable("verifier task failed terminally")
        if task.state != "completed" or task.output_record_id is None:
            return ScoreResolution.unavailable("verifier task is not completed")
        output_records = [
            item
            for item in self.records
            if item.record_id == task.output_record_id
        ]
        if len(output_records) != 1:
            return ScoreResolution.unavailable(
                "authoritative VerifierRecord is missing or ambiguous"
            )
        record = output_records[0]
        if (
            record.task_id != task.task_id
            or record.subject != task.subject
            or record.score_contract_hash != task.score_contract.contract_hash
        ):
            return ScoreResolution.unavailable(
                "authoritative VerifierRecord identity does not match its task"
            )
        return ScoreResolution.resolved(record.quality_score)


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
        verifier: VerifierWorkflow,
        clock: Clock,
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
        self.authorization = authorization
        self.verifier = verifier
        self.clock = clock
        self.persist_run = persist_run

    def select(
        self,
        run_id: str,
        *,
        frozen_spec_id: str | None = None,
        controller_directive: ControllerSelectionDirective | None = None,
    ) -> Generator[CandidateVerificationRequest, ScoreReport, dict[str, Any]]:
        self.authorization.require_search_execution(run_id)
        run = self.state.read_run(run_id)
        selected_spec_id = frozen_spec_id or run.frozen_spec_id
        allowed_specs = {run.frozen_spec_id} | {
            revision.frozen_spec_id for candidate in self.state.read_candidates(run_id)
            for revision in candidate.spec_revisions
        }
        if selected_spec_id not in allowed_specs:
            raise ValueError("SelectionSpecMismatch: version does not belong to this run")
        frozen = self.state.read_frozen_spec(selected_spec_id)
        self._assert_required_fork_audits_complete(run_id)
        self._assert_required_evidence_annotations_complete(run_id)
        replay = None
        with self.state.lock_run(run_id):
            for candidate in self.state.read_candidates(run_id):
                self.recovery.require_closeout(run_id, candidate)
            run = self.state.read_run(run_id)
            require_run_not_invalidated(run, "select")
            if run.selected_candidate_id is not None and (
                controller_directive is not None
                or run.controller_selection is not None
            ):
                if (
                    controller_directive is None
                    or run.controller_selection != controller_directive
                    or (run.selected_frozen_spec_id or run.frozen_spec_id) != selected_spec_id
                ):
                    raise RuntimeError(
                        "controller selection conflicts with the persisted selection"
                    )
                target = controller_directive.target
                if not (
                    run.selected_candidate_id == target.candidate_id
                    and run.selected_iteration == target.iteration
                    and run.selected_artifact_ref == target.artifact_ref
                    and run.selected_artifact_hash == target.artifact_hash
                ):
                    raise RuntimeError(
                        "persisted controller selection has inconsistent artifact identity"
                    )
                replay = run
            else:
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

        if replay is not None:
            selection_spec = frozen.spec.strategy.selection
            return {
                "selected_candidate_id": replay.selected_candidate_id,
                "selected_frozen_spec_id": selected_spec_id,
                "selected_score": replay.selected_score,
                "selected_hard_score": replay.selected_hard_score,
                "selected_quality_score": replay.selected_quality_score,
                "selected_iteration": replay.selected_iteration,
                "selected_git_head": replay.selected_git_head,
                "selected_artifact_ref": (
                    replay.selected_artifact_ref.model_dump(mode="json")
                    if replay.selected_artifact_ref is not None
                    else None
                ),
                "selected_artifact_hash": replay.selected_artifact_hash,
                "ranking_keys": selection_spec.ranking_keys,
                "ranking_tolerance": selection_spec.ranking_tolerance,
                "selection_trace": replay.selection_trace,
                "selection_exclusions": replay.selection_exclusions,
                "selection_basis_hard_score": replay.selection_basis_hard_score,
                "final_verifier_score": replay.final_verifier_score,
                "selection_evidence_source": replay.selection_evidence_source,
                "controller_selection": controller_directive.model_dump(mode="json"),
                "best_candidate_id": replay.best_candidate_id,
                "best_score": replay.best_score,
            }
        records = self.state.read_candidates(run_id)
        base_options = self._selection_options(run, records, frozen)
        if not base_options:
            self._mark_selection_blocked(
                run_id,
                "no hard-settled candidate iteration is eligible for selection",
            )
            raise RuntimeError("no verified candidates available for selection")

        controller_option = None
        if controller_directive is not None:
            target = controller_directive.target
            matches = [
                item
                for item in base_options
                if item.record.candidate_id == target.candidate_id
                and item.iteration.iteration == target.iteration
                and item.iteration.settlement_id == target.settlement_id
                and item.artifact_ref == target.artifact_ref
                and item.iteration.artifact_hash == target.artifact_hash
            ]
            if len(matches) != 1:
                self._mark_selection_blocked(
                    run_id,
                    "controller-selected subject is not one exact hard-settled option",
                )
                raise RuntimeError(
                    "controller-selected subject is not an eligible exact selection option"
                )
            controller_option = matches[0]

        selection_spec = frozen.spec.strategy.selection
        domain_options = [
            SelectionOption(
                subject=item.subject,
                hard_score=item.score,
                original_index=index,
                preferred=(
                    item.subject == controller_option.subject
                    if controller_option is not None
                    else item.record.candidate_id == run.best_candidate_id
                ),
            )
            for index, item in enumerate(base_options)
        ]
        score_sources = {
            "hard_score": _HardScoreSource(frozen.spec.metric_direction),
        }
        if "quality_score" in selection_spec.ranking_keys:
            score_sources["quality_score"] = _QualityScoreSource(
                tasks=self.verifier.list_tasks(run_id),
                records=self.verifier.list_records(run_id),
            )
        pool = resolve_candidate_pool(
            domain_options, selection_spec.ranking_keys, score_sources
        )
        exclusions = [dict(item) for item in pool.exclusions]
        if pool.state == "wait":
            run = self.state.read_run(run_id)
            run.selection_exclusions = exclusions
            run.budget_used["selection_wait_reason"] = (
                "required verifier score sources are pending"
            )
            self.persist_run(run)
            raise RuntimeError("selection is waiting for required verifier scores")
        if pool.state == "blocked":
            run = self.state.read_run(run_id)
            run.selection_exclusions = exclusions
            self.persist_run(run)
            self._mark_selection_blocked(
                run_id, "all candidates lack a required finite ranking score"
            )
            raise RuntimeError("no candidate satisfies the frozen ranking sources")

        by_subject = {item.subject: item for item in base_options}
        rejected: set[SelectionSubjectKey] = set()
        selection_trace: list[dict[str, Any]] = []
        selected: _BaseOption | None = None
        selected_values: dict[str, float] = {}
        final_report: ScoreReport | None = None
        evidence_source = "worker_bound"
        while len(rejected) < len(pool.pool):
            result = select_winner(
                pool.pool,
                selection_spec.ranking_keys,
                selection_spec.ranking_tolerance,
                score_sources,
                rejected,
            )
            option = by_subject[result.winner.option.subject]
            selection_trace.append(
                {
                    "winner_subject": option.subject.__dict__,
                    "stages": list(result.trace),
                    "rejected_before_attempt": _ordered_subjects(rejected),
                    **(
                        {
                            "controller_directive": controller_directive.model_dump(
                                mode="json"
                            )
                        }
                        if controller_directive is not None
                        else {}
                    ),
                }
            )
            if (
                controller_option is not None
                and option.subject != controller_option.subject
            ):
                run = self.state.read_run(run_id)
                run.selection_trace = selection_trace
                run.selection_exclusions = exclusions
                self.persist_run(run)
                self._mark_selection_blocked(
                    run_id,
                    "controller-selected subject is outside the frozen ranking survivors",
                )
                raise RuntimeError(
                    "controller-selected subject is outside the frozen selection gate"
                )
            option_record = self.state.read_candidate(
                run_id, option.record.candidate_id
            )
            is_thinkthread = option_record.task.workspace_provider == "thinkthread"
            if option.artifact_ref is not None and not is_thinkthread:
                self.artifacts.restore_candidate_artifact(
                    option_record,
                    frozen.spec.metric_name,
                    self.candidate_ledger.projection(frozen, option_record),
                    option.artifact_ref,
                    (
                        f"goal-plus select {option_record.candidate_id} "
                        f"iteration {option.iteration.iteration}"
                    ),
                )
            current = (
                None
                if is_thinkthread
                else self.artifacts.candidate_artifact_state(
                    run, frozen, option_record
                )
            )
            bound = artifact_iteration_eligible(option.iteration) and (
                is_thinkthread
                or (
                    current is not None
                    and option.iteration.artifact_hash == current.artifact_hash
                    and current.artifact_clean
                )
            )
            if bound:
                selected = option
                selected_values = result.winner.values
                evidence_source = "worker_bound"
                break
            if not is_thinkthread:
                report = yield CandidateVerificationRequest(
                    run_id, option_record.candidate_id,
                    frozen_spec_id=selected_spec_id,
                    iteration=option.iteration.iteration,
                    artifact_ref=option.artifact_ref,
                    artifact_hash=option.iteration.artifact_hash,
                )
                if report.process_passed and report.aggregate_score is not None:
                    selected = option
                    selected_values = result.winner.values
                    final_report = report
                    evidence_source = "parent_reverify"
                    break
            rejected.add(option.subject)
            if controller_option is not None:
                run = self.state.read_run(run_id)
                run.rejected_selection_subjects = _ordered_subjects(rejected)
                run.selection_trace = selection_trace
                run.selection_exclusions = exclusions
                self.persist_run(run)
                self._mark_selection_blocked(
                    run_id,
                    "controller-selected subject failed hard artifact revalidation",
                )
                raise RuntimeError(
                    "controller-selected subject failed hard artifact revalidation"
                )

        if selected is None:
            run = self.state.read_run(run_id)
            run.rejected_selection_subjects = _ordered_subjects(rejected)
            run.selection_trace = selection_trace
            self.persist_run(run)
            self._mark_selection_blocked(
                run_id, "all selected artifact subjects failed hard revalidation"
            )
            raise RuntimeError("no selected candidate has passing verification")

        selected_record = self.state.read_candidate(
            run_id, selected.record.candidate_id
        )
        selected_artifact_ref = selected.artifact_ref
        selected_git_head = (
            selected_artifact_ref.id
            if selected_artifact_ref is not None
            and selected_artifact_ref.kind == "git_commit"
            else None
        )
        if selected_record.task.workspace_provider == "thinkthread":
            selected_artifact_hash = selected.iteration.artifact_hash
            if not selected_artifact_hash:
                raise RuntimeError("selected ThinkThread Evidence has no artifact digest")
        else:
            selected_changed_files = self.artifacts.workspace.candidate_changed_files(
                run, selected_record, selected_artifact_ref
            )
            selected_artifact_hash = self.artifacts.workspace.artifact_hash(
                selected_record.task.workspace, selected_changed_files
            )
        final_score = (
            final_report.aggregate_score if final_report is not None else selected.score
        )
        with self.state.lock_run(run_id):
            run = self.state.read_run(run_id)
            require_run_not_invalidated(run, "record selection")
            self.annotation.close_queue(run_id, reason="search_selected")
            run.state = RunState.READY_TO_PROMOTE
            run.selected_candidate_id = selected_record.candidate_id
            run.selected_frozen_spec_id = selected_spec_id
            run.selected_score = selected.score
            run.selected_hard_score = selected.score
            run.selected_quality_score = selected_values.get("quality_score")
            run.selection_basis_hard_score = selected.score
            run.final_verifier_score = final_score
            run.selection_evidence_source = evidence_source
            run.controller_selection = controller_directive
            run.selection_trace = selection_trace
            run.selection_exclusions = exclusions
            run.rejected_selection_subjects = _ordered_subjects(rejected)
            run.selected_iteration = selected.iteration.iteration
            run.selected_git_head = selected_git_head
            run.selected_artifact_ref = selected_artifact_ref
            run.selected_artifact_hash = selected_artifact_hash
            run.budget_used.pop("selection_blocked_reason", None)
            run.budget_used.pop("selection_wait_reason", None)
            selected_record = self.state.read_candidate(
                run_id, selected_record.candidate_id
            )
            selected_iteration = next(
                (
                    item
                    for item in selected_record.iterations
                    if item.iteration == selected.iteration.iteration
                    and item.settlement_id == selected.iteration.settlement_id
                    and item.artifact_hash == selected.iteration.artifact_hash
                ),
                None,
            )
            if selected_iteration is None:
                raise RuntimeError("selected candidate has no exact Evidence subject")
            self.checkpoints.write_best(
                run, frozen.spec, selected_record, selected_iteration
            )
            selected_record.promotion_report = None
            selected_record.promotion_evidence = None
            self.state.write_candidate(run_id, selected_record)
            self.persist_run(run)
        return {
            "selected_candidate_id": selected_record.candidate_id,
            "selected_frozen_spec_id": selected_spec_id,
            "selected_score": selected.score,
            "selected_hard_score": selected.score,
            "selected_quality_score": selected_values.get("quality_score"),
            "selected_iteration": selected.iteration.iteration,
            "selected_git_head": selected_git_head,
            "selected_artifact_ref": (
                selected_artifact_ref.model_dump(mode="json")
                if selected_artifact_ref is not None
                else None
            ),
            "selected_artifact_hash": selected_artifact_hash,
            "ranking_keys": selection_spec.ranking_keys,
            "ranking_tolerance": selection_spec.ranking_tolerance,
            "selection_trace": selection_trace,
            "selection_exclusions": exclusions,
            "selection_basis_hard_score": selected.score,
            "final_verifier_score": final_score,
            "selection_evidence_source": evidence_source,
            "controller_selection": (
                controller_directive.model_dump(mode="json")
                if controller_directive is not None
                else None
            ),
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
        frozen: FrozenSpec,
    ) -> list[_BaseOption]:
        options: list[_BaseOption] = []
        for record in records:
            current_artifact_hash = None
            if record.task.workspace_provider != "thinkthread":
                current_changed = self.artifacts.workspace.candidate_changed_files(
                    run, record
                )
                current_artifact_hash = self.artifacts.workspace.artifact_hash(
                    record.task.workspace, current_changed
                )
            for iteration in reversed(record.iterations):
                if (
                    iteration_spec_id(run, iteration) != frozen.frozen_spec_id
                    or (iteration.spec_hash is not None and iteration.spec_hash != frozen.spec_hash)
                    or not artifact_iteration_eligible(iteration)
                    or iteration.disposition in {"discard", "failure"}
                    or iteration.score is None
                    or iteration.settlement_id is None
                    or iteration.artifact_hash is None
                ):
                    continue
                artifact_ref = iteration_artifact_ref(
                    iteration, record.task.workspace_provider
                )
                if record.task.workspace_provider == "thinkthread":
                    if (
                        artifact_ref is None
                        or artifact_ref.kind != "fs_snapshot"
                        or artifact_ref.provider != "thinkthread"
                    ):
                        continue
                elif artifact_ref is None and iteration.artifact_hash != current_artifact_hash:
                    continue
                options.append(
                    _BaseOption(
                        iteration.score,
                        record,
                        iteration,
                        artifact_ref,
                        verifier_subject_identity(
                            verifier_subject(frozen, record, iteration)
                        ),
                    )
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
        reason = (
            "required supplemental Evidence annotations are incomplete: "
            + ", ".join(incomplete)
        )
        self._mark_selection_blocked(run_id, reason)
        raise RuntimeError(reason)

    def _assert_required_fork_audits_complete(self, run_id: str) -> None:
        """Validate committed audit subjects before admitting any fork result."""
        run = self.state.read_run(run_id)
        frozen = self.state.read_frozen_spec(run.frozen_spec_id)
        if not frozen.spec.strategy.fork_enabled:
            return
        candidates = self.state.read_candidates(run_id)
        require_fork_ready(run, frozen, candidates)
        if frozen.spec.strategy.llm_validation == "off":
            return
        tasks = self.verifier.list_tasks(run_id)
        records = {item.record_id: item for item in self.verifier.list_records(run_id)}
        pending, failed, branches = [], [], []
        for branch in run.dispatch_group.targets:
            candidate = next(c for c in candidates if c.candidate_id == branch.candidate_id)
            states, record_ids = [], []
            for iteration in candidate.iterations:
                execution_spec = self.state.read_frozen_spec(iteration_spec_id(run, iteration))
                submission = iteration.verifier_submission
                expected = verifier_subject(execution_spec, candidate, iteration)
                matching = [
                    task for task in tasks
                    if submission is not None and submission.subject == expected
                    and task.subject == expected and task.input == submission.input
                    and task.score_contract == submission.score_contract
                    and task.input.dispatch_audit_context is not None
                ]
                status, output_id = "failed", None
                if len(matching) == 1:
                    task = matching[0]
                    audit = task.input.dispatch_audit_context
                    expected_context = build_dispatch_audit_context(
                        execution_spec, branch.binding, expected.settlement,
                    )
                    # Close facts belong to the committed submission's historical
                    # snapshot. Task/input exact matching above protects them;
                    # later host closure must not rebuild that historical input.
                    exact_context = (
                        audit.model_copy(update={"close_receipt": {}}) == expected_context
                    )
                    if exact_context and task.state in {"pending", "running", "retry_wait"}:
                        status = "pending"
                    output = records.get(task.output_record_id)
                    if (exact_context and task.state == "completed" and output is not None
                            and output.outcome == "completed" and output.task_id == task.task_id
                            and output.subject == expected and output.score_contract_hash == task.score_contract.contract_hash):
                        status, output_id = "completed", output.record_id
                states.append(status)
                if output_id:
                    record_ids.append(output_id)
                detail = f"{candidate.candidate_id}/iteration-{iteration.iteration:04d}={status}"
                if status == "pending":
                    pending.append(detail)
                elif status == "failed":
                    failed.append(detail)
            audit_state = "failed" if "failed" in states else "pending" if not states or "pending" in states else "completed"
            branches.append(branch.model_copy(update={"audit_state": audit_state,
                                                       "audit_record_id": record_ids[-1] if record_ids else None}))
        if branches != run.dispatch_group.targets:
            with self.state.lock_run(run_id):
                current = self.state.read_run(run_id)
                if current.dispatch_group == run.dispatch_group:
                    current.dispatch_group = run.dispatch_group.model_copy(update={"targets": branches, "updated_at": self.clock.timestamp()})
                    self.persist_run(current)
        if frozen.spec.strategy.llm_validation != "required" or not (pending or failed):
            return
        reason = ("required fork LLM audit failed or is missing: " if failed else
                  "required fork LLM audit is pending: ") + ", ".join(failed + pending)
        self._mark_selection_blocked(run_id, reason)
        raise RuntimeError(reason)
