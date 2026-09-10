"""Adaptive Search snapshot and reconciliation persistence boundaries."""

from contextlib import AbstractContextManager
from typing import Protocol

from goal_plus.domain.adaptive.models import (
    AdaptiveReconciliationRecord,
    AllocationDecision,
    SearchSnapshot,
)
from goal_plus.domain.search import FrozenSpec
from goal_plus.ports.state import CandidateRecord, RunRecord


class AdaptiveSnapshotState(Protocol):
    def read_run(self, run_id: str) -> RunRecord: ...
    def read_frozen_spec(self, frozen_spec_id: str) -> FrozenSpec: ...
    def read_candidates(self, run_id: str) -> list[CandidateRecord]: ...
    def load_allocation_decisions(self, run_id: str) -> list[AllocationDecision]: ...
    def write_search_snapshot(self, snapshot: SearchSnapshot) -> None: ...


class AdaptiveReconciliationState(AdaptiveSnapshotState, Protocol):
    def lock_adaptive_reconciliation(
        self, run_id: str
    ) -> AbstractContextManager[None]: ...
    def load_adaptive_reconciliation(
        self, run_id: str, verifier_record_id: str
    ) -> AdaptiveReconciliationRecord | None: ...
    def write_adaptive_reconciliation(
        self, record: AdaptiveReconciliationRecord
    ) -> None: ...
    def load_search_snapshot(
        self, run_id: str, snapshot_revision: str
    ) -> SearchSnapshot: ...
    def load_allocation_decision(
        self, run_id: str, decision_id: str
    ) -> AllocationDecision: ...


__all__ = ["AdaptiveReconciliationState", "AdaptiveSnapshotState"]
