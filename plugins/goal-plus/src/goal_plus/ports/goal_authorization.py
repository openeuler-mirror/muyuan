"""Read-only target queries and normalized, non-authorizing tool references."""

from dataclasses import dataclass
from typing import Mapping, Protocol

from goal_plus.domain.goal import GoalPlusRecord
from goal_plus.domain.goal_authorization import tool_matches


@dataclass(frozen=True)
class DispatchPreparation:
    """Exact Goal event authorizing a prepared native context boundary."""

    goal_plus_id: str
    run_id: str
    event_id: str
    created_at: str


@dataclass(frozen=True)
class GoalToolTarget:
    tool_name: str
    goal_plus_id: str | None = None
    run_id: str | None = None
    agent_session_id: str | None = None
    source_run_id: str | None = None
    source_run_requested: bool = False

    @classmethod
    def from_input(
        cls, tool_name: str, tool_input: Mapping[str, object]
    ) -> "GoalToolTarget":
        def reference(name: str) -> str | None:
            value = tool_input.get(name)
            return value if isinstance(value, str) and value else None

        return cls(
            tool_name=tool_name,
            goal_plus_id=reference("goal_plus_id"),
            run_id=reference("run_id"),
            agent_session_id=reference("agent_session_id") or (
                reference("session_id") if any(tool_matches(tool_name, "goal_plus_session_" + action)
                for action in ("wake", "wait", "message", "interrupt", "stop", "close")) else None
            ),
            source_run_id=reference("source_run_id"),
            source_run_requested=tool_input.get("source_run_id") is not None,
        )


class GoalAuthorizationSource(Protocol):
    def goal_ids(self) -> list[str]: ...
    def read_goal(self, goal_plus_id: str) -> GoalPlusRecord: ...
    def read_events(self, goal_plus_id: str) -> list[dict[str, object]]: ...
    def session_run_ids(self, agent_session_id: str) -> list[str]: ...
