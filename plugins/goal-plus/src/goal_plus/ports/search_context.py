"""Read-only Goal links and trusted process configuration for Search use cases."""

from dataclasses import dataclass
from typing import Literal, Protocol

from goal_plus.domain.goal import GoalPlusRecord


@dataclass(frozen=True)
class SearchHostSettings:
    outer_deadline_at: str | None = None
    annotation_reasoning_effort: str | None = None


@dataclass(frozen=True)
class EvidenceTaskContext:
    text: str
    source: Literal["goal_plus_raw_goal", "frozen_objective"]
    reference: str


class SearchContextSource(Protocol):
    def read_goals(self) -> dict[str, GoalPlusRecord]: ...
    def read_host_settings(self) -> SearchHostSettings: ...
