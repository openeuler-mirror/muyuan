"""Internal-agent admission facts, distinct from host process liveness."""

from dataclasses import dataclass
from typing import Protocol

from goal_plus.domain.agents import AgentHarnessKind, AgentRuntimeProvider
from goal_plus.domain.search import FrozenSpec
from goal_plus.ports.state import RunRecord


@dataclass(frozen=True)
class InternalAgentRun:
    agent_harness: AgentHarnessKind
    runtime_provider: AgentRuntimeProvider
    accepts_work: bool
    verifier_enabled: bool


class InternalAgentRunSource(Protocol):
    def read_run(self, run_id: str) -> RunRecord: ...
    def read_frozen_spec(self, frozen_spec_id: str) -> FrozenSpec: ...
