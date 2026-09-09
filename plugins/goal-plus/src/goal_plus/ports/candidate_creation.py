"""Narrow candidate creation use cases; neither operation launches a host worker."""

from dataclasses import dataclass
from typing import Any, Protocol

from goal_plus.domain.adaptive.models import ExpansionSource
from goal_plus.domain.evidence import ResultLedgerEntry
from goal_plus.domain.search import AgentSessionRecord, CandidateProposal, FrozenSpec, SearchPlan
from goal_plus.ports.state import CandidateRecord, RunRecord


@dataclass(frozen=True)
class CandidateMaterialization:
    """Lock-local input; materialization may update run source coordinates.

    The caller retains the run lock, owns admission and persists run changes.
    The materializer owns the created candidate and its initial ledger.
    """

    run: RunRecord
    frozen: FrozenSpec
    candidate_id: str
    plan: SearchPlan
    proposal: CandidateProposal
    slot: int
    expansion_source: ExpansionSource | None = None
    source_record: CandidateRecord | None = None
    results_ledger: list[ResultLedgerEntry] | None = None


class MaterializeCandidate(Protocol):
    def __call__(self, request: CandidateMaterialization) -> CandidateRecord: ...


class CandidateCreationState(Protocol):
    def read_run(self, run_id: str) -> RunRecord: ...
    def read_frozen_spec(self, frozen_spec_id: str) -> FrozenSpec: ...
    def read_candidate(self, run_id: str, candidate_id: str) -> CandidateRecord: ...
    def read_candidates(self, run_id: str) -> list[CandidateRecord]: ...
    def write_candidate(self, run_id: str, record: CandidateRecord) -> None: ...


class PrepareInitialSession(Protocol):
    """Prepare provenance/launch under current gates, without spawn/wait/kill."""

    def __call__(
        self,
        run_id: str,
        candidate_id: str,
        directive: dict[str, Any] | str | None = None,
        *,
        worker_budget: dict[str, Any] | None = None,
    ) -> AgentSessionRecord: ...
