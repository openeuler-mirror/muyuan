"""Resolve persisted Goal revision context and the effective Search deadline."""

from goal_plus.domain.time import outer_deadline_epoch
from goal_plus.ports.search_context import EvidenceTaskContext, SearchContextSource


class SearchContext:
    def __init__(self, source: SearchContextSource) -> None:
        self.source = source

    def task_context(self, run_id: str, *, fallback: str) -> EvidenceTaskContext:
        matches: list[tuple[str, str]] = []
        for goal in self.source.read_goals().values():
            tasks = [*goal.search_tasks]
            if goal.linked_search is not None:
                tasks.append(goal.linked_search)
            revision = next(
                (
                    task.goal_revision
                    for task in reversed(tasks)
                    if task.run_id == run_id
                ),
                None,
            )
            if revision is None:
                continue
            raw_goal = next(
                (
                    item.raw_goal
                    for item in goal.goal_revisions
                    if item.revision == revision
                ),
                None,
            )
            if not raw_goal or not raw_goal.strip():
                if goal.goal_revision == revision:
                    raw_goal = goal.raw_goal
            if raw_goal and raw_goal.strip():
                matches.append(
                    (raw_goal, f"goal_plus:{goal.goal_plus_id}:revision:{revision}")
                )
        unique = list(dict.fromkeys(matches))
        if len(unique) == 1:
            text, reference = unique[0]
            return EvidenceTaskContext(text, "goal_plus_raw_goal", reference)
        return EvidenceTaskContext(
            fallback, "frozen_objective", f"frozen_objective:{run_id}"
        )

    def outer_deadline(self, run_id: str) -> str | None:
        values: list[str] = []
        host_deadline = self.source.read_host_settings().outer_deadline_at
        if host_deadline:
            values.append(host_deadline)
        for storage_key, goal in self.source.read_goals().items():
            if not storage_key.startswith("gp_"):
                continue
            if (
                goal.status in {"complete", "abandoned"}
                or goal.control.state == "closed"
            ):
                continue
            linked = any(task.run_id == run_id for task in goal.search_tasks) or (
                goal.linked_search is not None and goal.linked_search.run_id == run_id
            )
            if linked and goal.outer_deadline_at:
                values.append(goal.outer_deadline_at)
        if not values:
            return None
        parsed = [(outer_deadline_epoch(value), value) for value in values]
        invalid = next((value for epoch, value in parsed if epoch is None), None)
        if invalid is not None:
            return invalid
        return min(parsed, key=lambda item: float(item[0]))[1]
