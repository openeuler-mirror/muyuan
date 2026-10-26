"""Persisted operational observation contract, independent of host collection."""

from contextlib import AbstractContextManager
from typing import Any, Protocol
from goal_plus.domain.goal import GoalPlusRecord

OBSERVABILITY_SCHEMA_VERSION = 3


class GoalObservationState(Protocol):
    def read_goal(self, goal_plus_id: str) -> GoalPlusRecord: ...
    def lock_goal(self, goal_plus_id: str) -> AbstractContextManager[None]: ...
    def write_orchestrator_observation(
        self, goal_plus_id: str, observation: dict[str, Any]
    ) -> None: ...
