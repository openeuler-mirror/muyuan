"""Decode canonical Goal history and read durable session/pool ownership."""

import json
from pathlib import Path
from typing import Any

from goal_plus.domain.goal import GoalPlusLinkedSearch, GoalPlusRecord, GoalPlusWorkItem
from goal_plus.infrastructure.state.files import load_json

LEGACY_WORK_ITEM_FIELDS = {
    "title",
    "route",
    "depends_on",
    "scope",
    "acceptance",
    "required",
    "search_run_id",
}
_SEARCH_TASK_FIELDS = (
    "goal_revision",
    "frozen_spec_id",
    "run_id",
    "linked_at",
    "selected_candidate_id",
    "report_path",
    "html_report_path",
    "promotion_artifact_path",
    "summary",
    "result_recorded_at",
)


def _merge_search_tasks_from_events(
    record: GoalPlusRecord,
    events: list[dict[str, Any]],
) -> GoalPlusRecord:
    tasks_by_run: dict[str, GoalPlusLinkedSearch] = {}
    run_order: list[str] = []

    def upsert(
        payload: dict[str, Any], *, event_type: str, created_at: str | None
    ) -> None:
        run_id = payload.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            return
        existing = tasks_by_run.get(run_id, GoalPlusLinkedSearch(run_id=run_id))
        updates = {
            field: payload[field]
            for field in _SEARCH_TASK_FIELDS
            if field in payload and payload[field] is not None
        }
        if event_type == "search_linked" and existing.linked_at is None and created_at:
            updates["linked_at"] = created_at
        if event_type == "search_result_recorded" and created_at:
            updates["result_recorded_at"] = created_at
        tasks_by_run[run_id] = existing.model_copy(update=updates)
        if run_id not in run_order:
            run_order.append(run_id)

    for event in events:
        event_type = event.get("event_type")
        payload = event.get("payload")
        if event_type in {"search_linked", "search_result_recorded"} and isinstance(
            payload, dict
        ):
            upsert(
                payload,
                event_type=event_type,
                created_at=event.get("created_at")
                if isinstance(event.get("created_at"), str)
                else None,
            )

    for task in record.search_tasks:
        payload = task.model_dump(mode="json", exclude_none=True)
        upsert(payload, event_type="record", created_at=None)

    current = record.linked_search
    if current is not None:
        payload = current.model_dump(mode="json", exclude_none=True)
        upsert(payload, event_type="record", created_at=None)
        if current.run_id in run_order:
            run_order.remove(current.run_id)
            run_order.append(current.run_id)

    tasks = [tasks_by_run[run_id] for run_id in run_order]
    linked = (
        tasks[-1].model_copy(deep=True)
        if tasks and tasks[-1].goal_revision == record.goal_revision
        else None
    )
    return record.model_copy(update={"search_tasks": tasks, "linked_search": linked})


class FileGoalQueries:
    def __init__(self, root_dir: Path | str) -> None:
        self.root_dir = Path(root_dir).resolve()
        self.goals_dir = self.root_dir / "goal-plus"

    def goal_ids(self) -> list[str]:
        return [
            path.parent.name for path in sorted(self.goals_dir.glob("gp_*/goal.json"))
        ]

    def session_run_ids(self, agent_session_id: str) -> list[str]:
        return [
            path.parents[1].name
            for path in (self.root_dir / "runs").glob("*/agent_sessions/*.json")
            if path.stem == agent_session_id
        ]

    def pool_run_id(self, pool_id: str) -> str | None:
        path = next(
            (
                path
                for path in (self.root_dir / "host-pools" / "pi").glob("*/pool.json")
                if path.parent.name == pool_id
            ),
            None,
        )
        if path is None:
            return None
        try:
            run_id = load_json(path).get("run_id")
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        return run_id if isinstance(run_id, str) else None

    def read_goal(self, goal_plus_id: str) -> GoalPlusRecord:
        payload = load_json(self.goals_dir / goal_plus_id / "goal.json")
        if "control" not in payload:
            # Import old terminal records as historical, never as retryable work.
            session = payload.get("active_session") or {}
            if payload.get("status") in {"blocked", "complete", "abandoned"}:
                payload["control"] = {"state": "closed", "reason": "legacy_terminal"}
            elif session.get("state") == "paused":
                payload["control"] = {"state": "paused", "reason": "legacy_pause"}
                payload["active_session"] = {**session, "state": "attached"}
            else:
                payload["control"] = {"state": "active"}
        raw_items = payload.get("work_items")
        if isinstance(raw_items, list):
            migrated_items = []
            for raw_item in raw_items:
                if not isinstance(raw_item, dict) or not (
                    LEGACY_WORK_ITEM_FIELDS & raw_item.keys()
                ):
                    migrated_items.append(raw_item)
                    continue
                if (
                    raw_item.get("route") in {"main", "search"}
                    or raw_item.get("status") == "planned"
                ):
                    continue
                migrated = {
                    key: value
                    for key, value in raw_item.items()
                    if key in GoalPlusWorkItem.model_fields
                }
                if migrated.get("status") == "blocked":
                    migrated["status"] = "failed"
                migrated_items.append(migrated)
            payload = {**payload, "work_items": migrated_items}
        record = GoalPlusRecord.model_validate(payload)
        return _merge_search_tasks_from_events(record, self.read_events(goal_plus_id))

    def read_events(self, goal_plus_id: str) -> list[dict[str, Any]]:
        event_path = self.goals_dir / goal_plus_id / "events.jsonl"
        if not event_path.exists():
            return []
        events = []
        with event_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events
