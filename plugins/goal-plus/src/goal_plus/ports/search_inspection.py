"""Read-side host/workspace facts for Search context and Evidence queries."""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from goal_plus.domain.search import (
    AgentSessionRecord,
    CandidateTask,
    FrozenSpec,
)
from goal_plus.ports.state import RunRecord, CandidateRecord
from goal_plus.domain.artifacts import ReferenceWorkspaceRecord


class SearchReadState(Protocol):
    def read_run(self, run_id: str) -> RunRecord: ...
    def read_frozen_spec(self, frozen_spec_id: str) -> FrozenSpec: ...
    def read_candidate(self, run_id: str, candidate_id: str) -> CandidateRecord: ...
    def read_candidates(self, run_id: str) -> list[CandidateRecord]: ...
    def read_agent_sessions(self, run_id: str) -> list[AgentSessionRecord]: ...


class ContextState(SearchReadState, Protocol):
    def write_candidate(self, run_id: str, record: CandidateRecord) -> None: ...


class EvidenceReadState(SearchReadState, Protocol):
    def lock_run(self, run_id: str) -> AbstractContextManager[None]: ...
    def write_agent_session(self, session: AgentSessionRecord) -> None: ...


class SearchInspection(Protocol):
    def git_head(self, workspace: Path) -> str: ...
    def git_status(
        self, workspace: Path, *, ignore_runtime_noise: bool = False
    ) -> list[str]: ...
    def changed_files(self, workspace: Path, base: str, head: str) -> list[str]: ...
    def inner_agent(self, frozen: FrozenSpec, task: CandidateTask) -> str: ...
    def normalize_worker_policy(self, frozen: FrozenSpec) -> dict[str, Any]: ...
    def observability(self, session: AgentSessionRecord) -> dict[str, Any]: ...


class EvidenceAttachments(Protocol):
    def discoverable_tool_ids(self, run_id: str) -> set[str]: ...
    def attach_external_evaluations(
        self, run_id: str, evidence: list[dict[str, Any]]
    ) -> None: ...


@dataclass(frozen=True)
class CandidateContextInputs:
    session: AgentSessionRecord
    frozen: FrozenSpec
    candidate: CandidateRecord
    previous_sessions: list[AgentSessionRecord]
    ledger_projection: str
    results_path: Path | None
    workspace_status: list[str]
    git_head: str | None
    changed_files: list[str]
    inner_agent: str
    references: list[ReferenceWorkspaceRecord]
    tool_catalog: list[dict[str, Any]]
    shared_knowledge: dict[str, Any] | None = None
