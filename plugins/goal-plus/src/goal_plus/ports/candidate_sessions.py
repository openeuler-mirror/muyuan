"""Session persistence and host payload construction without worker execution."""

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol

from goal_plus.domain.search import AgentSessionRecord, FrozenSpec
from goal_plus.ports.candidate_host import CandidateHostSource
from goal_plus.ports.state import CandidateRecord, RunRecord

MODEL_HANDOFF_RELATIVE_PATH = ".tmp/handoff.json"
MAX_MODEL_HANDOFF_BYTES = 64 * 1024


class CandidateSessionState(CandidateHostSource, Protocol):
    def write_candidate_binding(self, record: CandidateRecord) -> None: ...
    def lock_run(self, run_id: str) -> AbstractContextManager[None]: ...
    def write_session_index(self, run: RunRecord) -> None: ...
    def write_agent_session(self, session: AgentSessionRecord) -> None: ...


class CandidateLaunch(Protocol):
    def prepare(
        self,
        frozen: FrozenSpec,
        candidate_id: str,
        agent_session_id: str,
        directive: dict[str, Any],
        candidate_record: CandidateRecord,
        worker_budget_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...
    def continue_session(
        self,
        frozen: FrozenSpec,
        session: AgentSessionRecord,
        candidate_record: CandidateRecord,
        worker_budget_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...
    def read_handoff(
        self, workspace: Path
    ) -> tuple[dict[str, Any] | None, str | None]: ...
