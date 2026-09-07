"""Load typed Goal records and only the named host inputs used by Search."""

import os
from pathlib import Path

from goal_plus.domain.goal import GoalPlusRecord
from goal_plus.infrastructure.state.files import load_json
from goal_plus.ports.search_context import SearchHostSettings

OUTER_DEADLINE_ENV = "GOAL_PLUS_OUTER_DEADLINE_AT"
EVIDENCE_ANNOTATOR_REASONING_ENV = "GOAL_PLUS_EVIDENCE_ANNOTATOR_REASONING_EFFORT"


class FileSearchContextSource:
    def __init__(self, root_dir: Path | str) -> None:
        self.root_dir = Path(root_dir).resolve()

    def read_goals(self) -> dict[str, GoalPlusRecord]:
        goals = {}
        for path in sorted((self.root_dir / "goal-plus").glob("*/goal.json")):
            try:
                goals[path.parent.name] = GoalPlusRecord.model_validate(load_json(path))
            except (OSError, ValueError):
                continue
        return goals

    def read_host_settings(self) -> SearchHostSettings:
        return SearchHostSettings(
            outer_deadline_at=os.environ.get(OUTER_DEADLINE_ENV) or None,
            annotation_reasoning_effort=os.environ.get(EVIDENCE_ANNOTATOR_REASONING_ENV)
            or None,
        )
