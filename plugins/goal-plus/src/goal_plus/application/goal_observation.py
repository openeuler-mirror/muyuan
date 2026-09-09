"""Record already normalized host usage under the Goal observation lock."""

from typing import Any
from goal_plus.ports.clock import Clock
from goal_plus.ports.observability import (
    GoalObservationState,
    OBSERVABILITY_SCHEMA_VERSION,
)


class GoalObservation:
    def __init__(self, state: GoalObservationState, clock: Clock) -> None:
        self.state, self.clock = state, clock

    def record(
        self,
        goal_plus_id: str,
        *,
        host: str,
        usage: dict[str, Any],
        started_at: str | None = None,
    ) -> dict[str, Any]:
        with self.state.lock_goal(goal_plus_id):
            self.state.read_goal(goal_plus_id)
            if host != "pi":
                raise ValueError("only Pi orchestrator usage is recorded by this path")
            observation = {
                "schema_version": OBSERVABILITY_SCHEMA_VERSION,
                "source": "pi_goal_stats",
                "host": host,
                "started_at": started_at,
                "ended_at": self.clock.timestamp(),
                "usage": usage,
            }
            self.state.write_orchestrator_observation(goal_plus_id, observation)
            return observation
