"""Read-only scheduler and allocation views from canonical records."""

from pathlib import Path
from typing import TypeVar
from goal_plus.ports.report import SearchSchedulerViews
from goal_plus.domain.base import SearchModel
from goal_plus.domain.adaptive.models import AllocationDecision, ScheduleTask, scheduler_reward_records
from goal_plus.infrastructure.state.scheduler import load_schedule_task, load_schedule_tasks, schedule_task_paths
from goal_plus.infrastructure.state.allocation import allocation_decision_paths, load_allocation_decisions
from goal_plus.infrastructure.state.files import load_json


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
