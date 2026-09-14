"""Typed candidate host queries and launch preparation, without host execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from goal_plus.domain.agents import (
    AgentHarnessKind,
    AgentRuntimeProvider,
    WorkspaceLedgerProjection,
    WORKSPACE_LEDGER_PROJECTION_NONE,
)
from goal_plus.domain.search import AgentSessionRecord, FrozenSpec
from goal_plus.ports.state import RunRecord, CandidateRecord

if TYPE_CHECKING:
    from goal_plus.domain.adaptive.models import AllocationDecision


@dataclass(frozen=True)
class CandidateFileWriteBoundary:
    workspace: Path
    denied_files: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedInnerAgentBinding:
    """Host launch details resolved from one public inner-agent adapter id."""

    inner_agent_id: str
    adapter_version: str
    agent_harness: AgentHarnessKind
    runtime_provider: AgentRuntimeProvider
    host_adapter_version: str
    prompt_asset: str | None = None
    continuation_mode: str = "none"
    workspace_ledger_projection: WorkspaceLedgerProjection = (
        WORKSPACE_LEDGER_PROJECTION_NONE
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "inner_agent_id": self.inner_agent_id,
            "adapter_version": self.adapter_version,
            "agent_harness": self.agent_harness,
            "runtime_provider": self.runtime_provider,
            "host_adapter_version": self.host_adapter_version,
            "prompt_asset": self.prompt_asset,
            "continuation_mode": self.continuation_mode,
            "workspace_ledger_projection": self.workspace_ledger_projection,
        }


@dataclass(frozen=True)
class ThinkThreadSessionLaunch:
    session: AgentSessionRecord
    root_thinkthread_id: str
    baseline_snapshot_id: str
    inner_git_binding: dict[str, Any] | None


@dataclass(frozen=True)
class ThinkThreadContinuation:
    agent_session_id: str
    message: str


class CandidateHostSource(Protocol):
    def read_agent_session_ids(self) -> list[str]: ...
    def read_run(self, run_id: str) -> RunRecord: ...
    def read_frozen_spec(self, frozen_spec_id: str) -> FrozenSpec: ...
    def read_candidate(self, run_id: str, candidate_id: str) -> CandidateRecord: ...
    def read_candidates(self, run_id: str) -> list[CandidateRecord]: ...
    def read_agent_sessions(self, run_id: str) -> list[AgentSessionRecord]: ...
    def read_agent_session(
        self, agent_session_id: str, run_id: str | None = None
    ) -> AgentSessionRecord: ...
    def load_allocation_decisions(self, run_id: str) -> list[AllocationDecision]: ...


class PrepareContinuation(Protocol):
    def __call__(
        self, agent_session_id: str, worker_budget: dict[str, Any] | None = None
    ) -> AgentSessionRecord: ...
