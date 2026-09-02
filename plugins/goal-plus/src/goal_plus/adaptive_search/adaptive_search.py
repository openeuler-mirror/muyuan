"""Adaptive Search integration facade for the Search Scheduler."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeVar

import goal_plus.models as core_models
from goal_plus.models import (
    CandidateRecord,
    FrozenSpec,
    IterationRecord,
    RunRecord,
    ScoreReport,
    SearchModel,
)
from goal_plus.runtime import load_json
from goal_plus.adaptive_search.allocation import (
    AllocationDecision,
    AllocationEligibility,
    AllocationExecutor,
    AllocationTrigger,
    ExpansionSource,
    RewardedSource,
    allocation_decision_paths,
    load_allocation_decisions,
)
from goal_plus.adaptive_search.policies import ResolvedSearchSchedulerPolicies
from goal_plus.adaptive_search.scheduler import (
    ScheduleTask,
    ScheduleTaskState,
    SchedulerManager,
    SchedulerRewardRecord,
    SearchSchedulerSpec,
    load_schedule_task,
    load_schedule_tasks,
    schedule_task_paths,
    scheduler_reward_records,
)


class AdaptiveSearch:
    """Coordinate adaptive Search hooks without owning scheduler internals."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._allocation = AllocationExecutor(runtime)
        self._scheduler = SchedulerManager(runtime)

    def resolve_previous_best_score(
        self,
        record: CandidateRecord,
        *,
        candidate_best_score: float | None,
    ) -> float | None:
        if candidate_best_score is not None:
            return candidate_best_score
        source = record.task.expansion_source
        return source.score if source is not None else None

    def assert_candidate_execution_allowed(
        self,
        record: CandidateRecord,
        operation: Literal[
            "redispatch",
            "start_session",
            "continue_session",
            "run_verifier",
        ],
    ) -> None:
        if record.allocation_eligibility == "eligible":
            return
        candidate_id = record.candidate_id
        messages = {
            "redispatch": f"cannot redispatch retired candidate {candidate_id}",
            "start_session": f"cannot start session for retired candidate {candidate_id}",
            "continue_session": f"cannot continue retired candidate {candidate_id}",
            "run_verifier": (
                f"candidate {candidate_id} was retired by scheduler allocation"
            ),
        }
        raise RuntimeError(messages[operation])

    def list_allocation_decisions(
        self,
        run_id: str,
        status: Literal["pending", "applied"] | None = None,
    ) -> list[AllocationDecision]:
        return self._allocation.list(run_id, status)

    def apply_allocation_decision(
        self,
        run_id: str,
        decision_id: str,
    ) -> dict[str, object]:
        return self._allocation.apply(run_id, decision_id)

    def list_scheduler_rewards(
        self,
        run_id: str,
        candidate_id: str | None = None,
    ) -> list[SchedulerRewardRecord]:
        return self._scheduler.list_rewards(run_id, candidate_id)

    def resolve_scheduler_barrier(
        self,
        run_id: str,
        candidate_id: str,
        report: ScoreReport,
    ) -> ScoreReport:
        self._record_pending_decisions(run_id)
        current = self._runtime._load_candidate_record(run_id, candidate_id)
        if current.allocation_eligibility == "retired":
            return current.score_report or report
        resolved = self._scheduler.resolve_barrier(run_id, candidate_id, report)
        self._record_pending_decisions(run_id)
        refreshed = self._runtime._load_candidate_record(run_id, candidate_id)
        return refreshed.score_report or resolved

    def kick_schedule_tasks(self, run_id: str) -> None:
        self._scheduler.kick(run_id)

    def settle_iteration(
        self,
        run: RunRecord,
        frozen: FrozenSpec,
        record: CandidateRecord,
        iteration: IterationRecord,
    ) -> None:
        """Register a ScheduleTask once the settlement annotation is terminal."""

        if frozen.spec.strategy.search_scheduler is None:
            return
        try:
            tasks = self._scheduler.ensure_tasks_locked(run, frozen, record)
            task = tasks.get(iteration.iteration)
            iteration.scheduler_status = task.state if task is not None else "pending"
        except Exception as exc:
            iteration.scheduler_status = "terminal_error"
            iteration.scheduler_error = (
                f"ScheduleTask registration failed: {type(exc).__name__}: {exc}"
            )
            return

    def _record_pending_decisions(self, run_id: str) -> list[AllocationDecision]:
        decisions: list[AllocationDecision] = []
        for task in self._scheduler.pending_decision_tasks(run_id):
            decision = self._record_decision(task)
            if decision is not None:
                decisions.append(decision)
        return decisions

    def _record_decision(self, task: ScheduleTask) -> AllocationDecision | None:
        if task.output is None or task.output.decision is None:
            return None
        trigger = AllocationTrigger(
            schedule_task_id=task.task_id,
            settlement_id=task.settlement_id,
            candidate_id=task.candidate_id,
            iteration=task.iteration,
            commit=task.attempt_commit,
            reserved_candidate_ids=task.reserved_candidate_ids,
        )
        rewarded_sources = [
            RewardedSource(
                schedule_task_id=reward.schedule_task_id,
                candidate_id=reward.candidate_id,
                iteration=reward.iteration,
                node_id=reward.node_id,
                settlement_id=reward.settlement_id,
            )
            for reward in self._scheduler.list_rewards(task.run_id)
        ]
        decision = self._allocation.record(
            task.run_id,
            trigger,
            task.output.decision,
            rewarded_sources,
        )
        if decision is not None:
            self._scheduler.bind_decision(
                task,
                decision.decision_id,
                [item.new_candidate_id for item in decision.replacements],
            )
        return decision


@dataclass(frozen=True)
class SearchSchedulerViews:
    schedule_tasks: tuple[ScheduleTask, ...]
    reward_records: tuple[SchedulerRewardRecord, ...]
    allocation_decisions: tuple[AllocationDecision, ...]


ModelT = TypeVar("ModelT", bound=SearchModel)


def _load_valid_models(
    paths: list[Path],
    model: type[ModelT],
) -> tuple[ModelT, ...]:
    loaded: list[ModelT] = []
    for path in paths:
        try:
            loaded.append(model.model_validate(load_json(path)))
        except (OSError, ValueError):
            continue
    return tuple(loaded)

def load_search_scheduler_views(
    run_dir: Path,
    *,
    skip_invalid_records: bool = False,
) -> SearchSchedulerViews:
    if skip_invalid_records:
        tasks = _load_valid_models(schedule_task_paths(run_dir), ScheduleTask)
        decisions = _load_valid_models(
            allocation_decision_paths(run_dir),
            AllocationDecision,
        )
    else:
        tasks = tuple(load_schedule_tasks(run_dir))
        decisions = tuple(load_allocation_decisions(run_dir))
    return SearchSchedulerViews(
        schedule_tasks=tasks,
        reward_records=tuple(scheduler_reward_records(list(tasks))),
        allocation_decisions=decisions,
    )

def load_schedule_task_view(
    run_dir: Path,
    candidate_id: str,
    iteration: int,
) -> ScheduleTask | None:
    return load_schedule_task(run_dir, candidate_id, iteration)

def bind_search_scheduler_schemas() -> None:
    """Resolve core forward references after all scheduler schemas exist."""

    types_namespace = {
        "SearchSchedulerSpec": SearchSchedulerSpec,
        "AllocationDecision": AllocationDecision,
        "AllocationEligibility": AllocationEligibility,
        "ExpansionSource": ExpansionSource,
        "ScheduleTaskState": ScheduleTaskState,
        "ResolvedSearchSchedulerPolicies": ResolvedSearchSchedulerPolicies,
    }
    for model in (
        core_models.StrategySpec,
        core_models.SearchSpec,
        core_models.SearchSpecDraft,
        core_models.FrozenSpec,
        core_models.CandidateTask,
        core_models.CandidateWorkOrder,
        core_models.ScoreReport,
        core_models.IterationRecord,
        core_models.CandidateRecord,
    ):
        model.model_rebuild(force=True, _types_namespace=types_namespace)

__all__ = [
    "AdaptiveSearch",
    "SearchSchedulerViews",
    "bind_search_scheduler_schemas",
    "load_schedule_task_view",
    "load_search_scheduler_views",
]
