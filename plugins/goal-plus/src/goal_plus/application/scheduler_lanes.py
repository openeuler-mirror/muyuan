"""Scheduler admission, lane projections and confirmation barriers without hosts."""

from goal_plus.application.scheduler import SchedulerWorkflow
from goal_plus.application.search_context import SearchContext
from goal_plus.domain.adaptive.models import (
    ResolvedSchedulerProfile,
    SchedulerRewardRecord,
    ScheduleTask,
    SearchSchedulerSpec,
    requests_allocation_confirmation,
    scheduler_reward_records,
)
from goal_plus.domain.adaptive.scheduler import confirmation_execution_barrier, scheduler_node_id
from goal_plus.domain.evidence import IterationRecord
from goal_plus.domain.search import FrozenSpec
from goal_plus.domain.serialization import sha256_text
from goal_plus.domain.time import outer_deadline_epoch
from goal_plus.ports.annotation import AnnotationTaskQuery
from goal_plus.ports.scheduler_lanes import (
    SchedulerCandidateProjection,
    SchedulerIterationProjection,
    SchedulerLaneState,
    SchedulerReportProjection,
)
from goal_plus.ports.state import CandidateRecord, RunRecord

OUTER_DEADLINE_ENV = "GOAL_PLUS_OUTER_DEADLINE_AT"


class SchedulerLanes:
    def __init__(
        self,
        *,
        workflow: SchedulerWorkflow,
        source: SchedulerLaneState,
        annotation: AnnotationTaskQuery,
        context: SearchContext,
    ) -> None:
        self.workflow = workflow
        self.tasks = workflow.tasks
        self.clock = workflow.clock
        self.source = source
        self.annotation = annotation
        self.context = context

    @staticmethod
    def _projection(record: CandidateRecord) -> SchedulerCandidateProjection:
        return SchedulerCandidateProjection(
            candidate_id=record.candidate_id,
            iterations=tuple(
                SchedulerIterationProjection(
                    iteration=item.iteration,
                    schedule_task_id=item.schedule_task_id,
                    scheduler_status=item.scheduler_status,
                    scheduler_error=item.scheduler_error,
                )
                for item in record.iterations
            ),
            settlement_watermark=record.schedule_settlement_watermark,
            report=(
                SchedulerReportProjection(
                    schedule_task_id=record.score_report.schedule_task_id,
                    scheduler_status=record.score_report.scheduler_status,
                )
                if record.score_report is not None
                else None
            ),
        )

    def bind_decision(
        self,
        task: ScheduleTask,
        decision_id: str,
        used_candidate_ids: list[str],
    ) -> None:
        with self.tasks.lock_run(task.run_id):
            current = self.tasks.load_schedule_task(
                task.run_id,
                task.candidate_id,
                task.iteration,
            )
            if current is None:
                raise RuntimeError("cannot bind Decision to a missing ScheduleTask")
            if current.decision_id not in {None, decision_id}:
                raise RuntimeError("ScheduleTask is already bound to another Decision")
            self.tasks.write_schedule_task(
                current.model_copy(
                    update={
                        "decision_id": decision_id,
                        "reserved_candidate_ids": used_candidate_ids,
                    }
                )
            )

    def list_rewards(
        self,
        run_id: str,
        candidate_id: str | None = None,
    ) -> list[SchedulerRewardRecord]:
        return scheduler_reward_records(
            self.tasks.load_schedule_tasks(run_id),
            candidate_id=candidate_id,
        )

    def pending_decision_tasks(self, run_id: str) -> list[ScheduleTask]:
        """Return completed confirmation tasks not yet bound to a Decision."""

        return sorted(
            (
                task
                for task in self.tasks.load_schedule_tasks(run_id)
                if task.state == "completed"
                and task.consumed_at is not None
                and task.output is not None
                and task.output.allocation_flag
                and task.output.decision is not None
                and task.reserved_candidate_ids
                and task.decision_id is None
            ),
            key=lambda task: (task.updated_at, task.candidate_id, task.iteration),
        )

    def run_active(self, run_id: str) -> bool:
        return self.workflow.run_active(run_id)

    def _resolve_profile(
        self,
        configured: SearchSchedulerSpec,
    ) -> ResolvedSchedulerProfile:
        return ResolvedSchedulerProfile(
            host=configured.host,
            model=configured.model,
            reasoning_effort=configured.reasoning_effort,
            timeout_seconds=configured.timeout_seconds,
        )

    def reserve_candidate_ids(
        self,
        run: RunRecord,
        frozen: FrozenSpec,
    ) -> list[str]:
        max_candidates = frozen.spec.budget.max_candidates
        records = self.source.read_candidates(run.run_id)
        materialized = {item.candidate_id for item in records}
        schedule_tasks = self.tasks.load_schedule_tasks(run.run_id)
        allocated_ids = materialized | {
            candidate_id
            for task in schedule_tasks
            for candidate_id in task.reserved_candidate_ids
        }
        reserved = {
            candidate_id
            for task in schedule_tasks
            for candidate_id in task.reserved_candidate_ids
            if candidate_id not in materialized
            and (
                task.state not in {"completed", "terminal_error"}
                or task.decision_id is not None
            )
        }
        eligible = sum(item.allocation_eligibility == "eligible" for item in records)
        available = max(
            0,
            min(
                eligible,
                frozen.spec.budget.max_parallel - len(reserved),
            ),
        )
        if max_candidates is not None:
            available = min(
                available,
                max(0, max_candidates - len(materialized) - len(reserved)),
            )
        result: list[str] = []
        for _ in range(available):
            self._advance_candidate_index(run, allocated_ids)
            result.append(f"c{run.next_candidate_index:03d}")
            run.next_candidate_index += 1
        self._advance_candidate_index(run, allocated_ids)
        return result

    @staticmethod
    def _advance_candidate_index(run: RunRecord, allocated_ids: set[str]) -> None:
        # Task-first admission can outlive the run counter write. Terminal tasks
        # release capacity, but their allocated identities must not be reused.
        while f"c{run.next_candidate_index:03d}" in allocated_ids:
            run.next_candidate_index += 1

    def create_task_locked(
        self,
        run: RunRecord,
        frozen: FrozenSpec,
        record: CandidateRecord,
        iteration: IterationRecord,
    ) -> ScheduleTask:
        """Use the settlement owner's snapshots while its run lock is held."""
        configured = frozen.spec.strategy.search_scheduler
        if configured is None:
            raise RuntimeError("ScheduleTask requires search_scheduler configuration")
        policies = frozen.search_scheduler_policies
        if policies is None:
            raise RuntimeError("ScheduleTask requires frozen scheduler policies")
        if (
            iteration.git_head is None
            or iteration.attempt_base_git_head is None
            or iteration.settlement_id is None
        ):
            raise RuntimeError("ScheduleTask requires exact attempt commits")
        annotation = self.annotation.task(
            run.run_id,
            record.candidate_id,
            iteration.iteration,
        )
        if annotation is None or annotation.state not in {
            "completed",
            "terminal_error",
        }:
            raise RuntimeError("ScheduleTask requires a terminal AnnotationTask")
        if (
            annotation.settlement_id != iteration.settlement_id
            or annotation.attempt_commit != iteration.git_head
            or annotation.attempt_base_commit != iteration.attempt_base_git_head
        ):
            raise RuntimeError("AnnotationTask does not match settled iteration")
        existing = self.tasks.load_schedule_task(
            run.run_id,
            record.candidate_id,
            iteration.iteration,
        )
        if existing is not None:
            if (
                existing.attempt_commit != iteration.git_head
                or existing.attempt_base_commit != iteration.attempt_base_git_head
                or existing.frozen_spec_id != frozen.frozen_spec_id
                or existing.settlement_id != iteration.settlement_id
                or existing.annotation_task_id != annotation.task_id
                or existing.reward_policy_ref != policies.reward.reference
                or existing.allocation_policy_ref != policies.allocation.reference
            ):
                raise RuntimeError("ScheduleTask identity is immutable")
            self._advance_candidate_index(run, set(existing.reserved_candidate_ids))
            return existing

        try:
            profile = self._resolve_profile(configured)
            profile_error = None
        except Exception as exc:
            profile = None
            profile_error = f"invalid scheduler profile: {type(exc).__name__}: {exc}"
        outer_deadline = self.context.source.read_host_settings().outer_deadline_at
        if outer_deadline:
            deadline_epoch = outer_deadline_epoch(outer_deadline)
            if deadline_epoch is None:
                profile_error = f"invalid {OUTER_DEADLINE_ENV} value"
                profile = None
            elif deadline_epoch <= self.clock.now():
                profile_error = "scheduler outer deadline already expired"
                profile = None
        context = self.context.task_context(run.run_id, fallback=frozen.spec.objective)
        task_context, task_context_source, task_context_ref = (
            context.text,
            context.source,
            context.reference,
        )
        previous = self.tasks.load_schedule_task(
            run.run_id,
            record.candidate_id,
            iteration.iteration - 1,
        )
        allocation_confirmation = requests_allocation_confirmation(previous)
        digest = sha256_text(
            "\0".join(
                (
                    run.run_id,
                    record.candidate_id,
                    str(iteration.iteration),
                    iteration.settlement_id,
                    frozen.frozen_spec_id,
                )
            )
        )[:16]
        now = self.clock.timestamp()
        task = ScheduleTask(
            task_id=(
                f"schedule_{record.candidate_id}_{iteration.iteration:04d}_{digest}"
            ),
            frozen_spec_id=frozen.frozen_spec_id,
            settlement_id=iteration.settlement_id,
            node_id=scheduler_node_id(record.candidate_id, iteration.iteration),
            annotation_task_id=annotation.task_id,
            annotation_state=annotation.state,
            evidence_view=(
                annotation.view.description
                if annotation.state == "completed" and annotation.view is not None
                else None
            ),
            annotation_error_fingerprint=(
                annotation.error_fingerprint
                if annotation.state == "terminal_error"
                else None
            ),
            reward_policy_ref=policies.reward.reference,
            allocation_policy_ref=policies.allocation.reference,
            run_id=run.run_id,
            candidate_id=record.candidate_id,
            iteration=iteration.iteration,
            attempt_base_commit=iteration.attempt_base_git_head,
            attempt_commit=iteration.git_head,
            attempt_changed_files=list(iteration.attempt_changed_files),
            task_context_source=task_context_source,
            task_context_ref=task_context_ref,
            task_context_sha256=sha256_text(task_context),
            reserved_candidate_ids=(
                self.reserve_candidate_ids(run, frozen)
                if allocation_confirmation
                else []
            ),
            profile=profile,
            outer_deadline_at=outer_deadline,
            state="terminal_error" if profile_error else "pending",
            error_fingerprint=sha256_text(profile_error) if profile_error else None,
            last_error=profile_error,
            created_at=now,
            updated_at=now,
        )
        self.tasks.write_schedule_task(task)
        return task

    def ensure_tasks_locked(
        self,
        run: RunRecord,
        frozen: FrozenSpec,
        record: CandidateRecord,
    ) -> dict[int, ScheduleTask]:
        """Update the caller's locked settlement projection; it persists the result."""
        if frozen.spec.strategy.search_scheduler is None:
            return {}
        self._reconcile_candidate_index(run, self.source.read_candidates(run.run_id))
        return self._ensure_candidate_locked(run, frozen, record)

    def _reconcile_candidate_index(
        self, run: RunRecord, records: list[CandidateRecord]
    ) -> None:
        allocated_ids = {item.candidate_id for item in records} | {
            candidate_id
            for task in self.tasks.load_schedule_tasks(run.run_id)
            for candidate_id in task.reserved_candidate_ids
        }
        self._advance_candidate_index(run, allocated_ids)

    def _ensure_candidate_locked(
        self, run: RunRecord, frozen: FrozenSpec, record: CandidateRecord
    ) -> dict[int, ScheduleTask]:
        tasks: dict[int, ScheduleTask] = {}
        for iteration in record.iterations:
            if iteration.iteration <= record.schedule_settlement_watermark:
                continue
            task = self.tasks.load_schedule_task(
                run.run_id,
                record.candidate_id,
                iteration.iteration,
            )
            if task is None:
                annotation = self.annotation.task(
                    run.run_id,
                    record.candidate_id,
                    iteration.iteration,
                )
                if annotation is None or annotation.state not in {
                    "completed",
                    "terminal_error",
                }:
                    break
                if iteration.iteration > 1:
                    previous = self.tasks.load_schedule_task(
                        run.run_id,
                        record.candidate_id,
                        iteration.iteration - 1,
                    )
                    if previous is None or previous.state not in {
                        "completed",
                        "terminal_error",
                    }:
                        break
                task = self.create_task_locked(run, frozen, record, iteration)
            iteration.schedule_task_id = task.task_id
            iteration.scheduler_status = task.state
            tasks[iteration.iteration] = task
        return tasks

    def ensure_tasks(self, run_id: str) -> list[tuple[str, int]]:
        with self.tasks.lock_run(run_id):
            if not self.run_active(run_id):
                return []
            run = self.source.read_run(run_id)
            frozen = self.source.read_frozen_spec(run.frozen_spec_id)
            if frozen.spec.strategy.search_scheduler is None:
                return []
            ensured: list[tuple[str, int]] = []
            records = self.source.read_candidates(run_id)
            self._reconcile_candidate_index(run, records)
            for record in records:
                tasks = self._ensure_candidate_locked(run, frozen, record)
                if tasks:
                    self.source.write_candidate_projection(
                        run_id, self._projection(record)
                    )
                    ensured.extend(
                        (record.candidate_id, iteration) for iteration in tasks
                    )
            self.source.write_candidate_index(run.run_id, run.next_candidate_index)
            return sorted(ensured)

    def unsettled_tasks(self, run_id: str) -> list[tuple[str, int]]:
        if not self.run_active(run_id):
            return []
        return sorted(
            (
                (task.candidate_id, task.iteration)
                for task in self.tasks.load_schedule_tasks(run_id)
                if task.state not in {"completed", "terminal_error"}
            ),
            key=lambda item: (item[0], item[1]),
        )

    def candidate_execution_barrier(self, record: CandidateRecord) -> str | None:
        """Return why the lane cannot advance past an allocation confirmation."""

        if len(record.iterations) < 2:
            return None
        latest = record.iterations[-1]
        previous = self.tasks.load_schedule_task(
            record.task.run_id,
            record.candidate_id,
            latest.iteration - 1,
        )
        if not requests_allocation_confirmation(previous):
            return None
        current = self.tasks.load_schedule_task(
            record.task.run_id,
            record.candidate_id,
            latest.iteration,
        )
        return confirmation_execution_barrier(previous, current)

    def consume_tasks(
        self,
        run_id: str,
        candidate_id: str,
        *,
        through_iteration: int | None = None,
    ) -> None:
        with self.tasks.lock_run(run_id):
            if not self.run_active(run_id):
                return
            run = self.source.read_run(run_id)
            record = self.source.read_candidate(run_id, candidate_id)
            while True:
                next_iteration = record.schedule_settlement_watermark + 1
                if through_iteration is not None and next_iteration > through_iteration:
                    break
                task = self.tasks.load_schedule_task(
                    run_id,
                    candidate_id,
                    next_iteration,
                )
                if task is None or task.state not in {"completed", "terminal_error"}:
                    break
                iteration = next(
                    (
                        item
                        for item in record.iterations
                        if item.iteration == next_iteration
                    ),
                    None,
                )
                if iteration is None or iteration.git_head != task.attempt_commit:
                    raise RuntimeError(
                        "ScheduleTask cannot bind to its hard-settled iteration"
                    )
                iteration.schedule_task_id = task.task_id
                iteration.scheduler_status = task.state
                if task.state == "completed":
                    task = task.model_copy(
                        update={
                            "consumed_at": self.clock.timestamp(),
                        }
                    )
                    self.tasks.write_schedule_task(task)
                else:
                    iteration.scheduler_error = task.last_error
                record.schedule_settlement_watermark = next_iteration
                self.source.write_candidate_projection(run_id, self._projection(record))

            if record.score_report is not None and record.iterations:
                latest = record.iterations[-1]
                latest_task = self.tasks.load_schedule_task(
                    run_id,
                    candidate_id,
                    latest.iteration,
                )
                record.score_report = record.score_report.model_copy(
                    update={
                        "scheduler_status": (
                            latest.scheduler_status if latest_task is not None else None
                        ),
                        "schedule_task_id": latest.schedule_task_id,
                    }
                )
            self.source.write_candidate_projection(run_id, self._projection(record))
            self.source.write_candidate_index(run.run_id, run.next_candidate_index)
