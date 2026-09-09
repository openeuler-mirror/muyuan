"""Read-only Allocation preflight inputs and lock-local prepared values."""

from dataclasses import dataclass
from typing import Protocol

from goal_plus.domain.adaptive.models import AllocationDecision, AllocationReplacement
from goal_plus.domain.evidence import ResultLedgerEntry
from goal_plus.domain.search import AgentSessionRecord, FrozenSpec, SearchPlan
from goal_plus.ports.state import CandidateRecord, RunRecord


@dataclass(frozen=True)
class PreparedAllocationReplacement:
    replacement: AllocationReplacement
    source: CandidateRecord
    plan: SearchPlan
    results_ledger: list[ResultLedgerEntry]


@dataclass(frozen=True)
class PreparedAllocation:
    run: RunRecord
    frozen: FrozenSpec
    decision: AllocationDecision
    replacements: tuple[PreparedAllocationReplacement, ...]


class AllocationPreparationSource(Protocol):
    def read_run(self, run_id: str) -> RunRecord: ...
    def read_frozen_spec(self, frozen_spec_id: str) -> FrozenSpec: ...
    def load_allocation_decision(
        self, run_id: str, decision_id: str
    ) -> AllocationDecision: ...
    def read_candidates(self, run_id: str) -> list[CandidateRecord]: ...
    def read_plan(self, run_id: str, plan_id: str) -> SearchPlan: ...
    def read_agent_sessions(self, run_id: str) -> list[AgentSessionRecord]: ...
    def read_source_ledger(
        self, source: CandidateRecord
    ) -> list[ResultLedgerEntry]: ...
