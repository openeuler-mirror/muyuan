"""Goal state writes and serialized mutation locks in the existing layout."""

import json
from pathlib import Path
from uuid import uuid4
from goal_plus.domain.goal import GoalPlusRecord
from goal_plus.infrastructure.state.goal import FileGoalQueries
from goal_plus.infrastructure.state.files import exclusive_file_lock, write_json
from goal_plus.infrastructure.clock import utc_timestamp
from typing import Any


class FileGoalState(FileGoalQueries):
    def lock_goal(self, goal_plus_id: str):
        return exclusive_file_lock(self.goals_dir / goal_plus_id / "goal.lock")

    def lock_entrypoint(self):
        return exclusive_file_lock(self.root_dir / "goal-plus-entrypoint.lock")

    def new_attempt_id(self) -> str:
        return f"attempt_{uuid4().hex}"

    def goal_path(self, goal_plus_id: str) -> Path:
        return self.goals_dir / goal_plus_id / "goal.json"

    def next_goal_id(self) -> str:
        max_index = 0
        for path in self.goals_dir.glob("gp_*"):
            try:
                max_index = max(max_index, int(path.name.removeprefix("gp_")))
            except ValueError:
                continue
        return f"gp_{max_index + 1:04d}"

    def write_goal(self, record: GoalPlusRecord) -> None:
        write_json(self.goal_path(record.goal_plus_id), record.model_dump(mode="json"))

    def append_event(
        self,
        goal_plus_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        event_path = self.goals_dir / goal_plus_id / "events.jsonl"
        event_path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "event_id": f"evt_{uuid4().hex[:12]}",
            "event_type": event_type,
            "created_at": utc_timestamp(),
            "payload": payload,
        }
        with event_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, ensure_ascii=True))
            handle.write("\n")

    def write_orchestrator_observation(
        self, goal_plus_id: str, observation: dict[str, Any]
    ) -> None:
        write_json(
            self.goals_dir / goal_plus_id / "orchestrator-observability.json",
            observation,
        )
