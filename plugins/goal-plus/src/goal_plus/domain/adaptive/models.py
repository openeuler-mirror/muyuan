"""Adaptive Search durable decisions and exact expansion identities."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from goal_plus.domain.adaptive.deterministic import (
    AllocationExpansionIntent,
    AllocationPlan,
    CandidateFeatures,
    FeatureView,
    PriorityReplacePolicySpec,
    SearchSnapshot,
    SnapshotCandidate,
    SnapshotNode,
    compute_allocation_plan,
    compute_feature_view,
    snapshot_identity,
)
from goal_plus.domain.agents import WorkerBudget
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.base import SearchModel
from goal_plus.domain.serialization import sha256_text


AllocationEligibility = Literal["eligible", "retired"]
AllocationDecisionStatus = Literal[
    "pending", "applied", "superseded", "terminal_error"
]
AdaptiveReconciliationStatus = Literal["prepared", "completed"]
AdaptiveReconciliationOutcome = Literal["decision_recorded", "no_decision"]


def adaptive_reconciliation_identity(verifier_record_id: str) -> str:
    return f"adaptive_reconciliation_{sha256_text(verifier_record_id)[:24]}"


class AdaptiveReconciliationRecord(SearchModel):
    """Durable consumption state for one completed VerifierRecord event."""

    schema_version: Literal[1] = 1
    reconciliation_id: str = Field(
        min_length=1, pattern=r"^adaptive_reconciliation_[0-9a-f]{24}$"
    )
    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1, pattern=r"^verifier_[0-9a-f]{24}$")
    verifier_record_id: str = Field(min_length=1)
    state: AdaptiveReconciliationStatus
    snapshot_revision: str = Field(min_length=1)
    plan_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    outcome: AdaptiveReconciliationOutcome | None = None
    decision_id: str | None = Field(
        default=None, pattern=r"^allocation_[0-9a-f]{24}$"
    )

    @model_validator(mode="after")
    def completion_fields_are_consistent(self) -> "AdaptiveReconciliationRecord":
        if self.reconciliation_id != adaptive_reconciliation_identity(
            self.verifier_record_id
        ):
            raise ValueError("adaptive reconciliation identity does not match")
        if self.state == "prepared" and (
            self.outcome is not None or self.decision_id is not None
        ):
            raise ValueError("prepared reconciliation cannot have an outcome")
        if self.state == "completed" and self.outcome is None:
            raise ValueError("completed reconciliation requires an outcome")
        if (self.outcome == "decision_recorded") != (self.decision_id is not None):
            raise ValueError(
                "reconciliation decision outcome and decision id must coexist"
            )
        if self.decision_id is not None and (
            self.plan_hash is None
            or self.decision_id != f"allocation_{self.plan_hash[:24]}"
        ):
            raise ValueError(
                "reconciliation decision identity must match the prepared plan"
            )
        return self


class ExpansionSource(SearchModel):
    """Exact scored attempt used to materialize a derived candidate."""

    candidate_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    settlement_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    verifier_record_id: str = Field(min_length=1)
    attempt_artifact_ref: ArtifactRef
    attempt_artifact_hash: str = Field(min_length=1)
    ledger_artifact_ref: ArtifactRef
    allocation_depth: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def identities_are_consistent(self) -> "ExpansionSource":
        expected_node_id = f"{self.candidate_id}/iteration-{self.iteration:04d}"
        if self.node_id != expected_node_id:
            raise ValueError("expansion source node identity does not match")
        if self.attempt_artifact_ref.provider != self.ledger_artifact_ref.provider:
            raise ValueError("expansion source artifact providers must match")
        return self


class AllocationExpansion(SearchModel):
    expansion_id: str = Field(min_length=1)
    source: ExpansionSource
    direction_key: str = Field(min_length=1, max_length=256)
    rationale: str = Field(min_length=1, max_length=2000)
    worker_budget: WorkerBudget | None = None
    new_candidate_id: str | None = Field(default=None, min_length=1)


class AllocationDecision(SearchModel):
    """Durable execution intent emitted only by deterministic policy."""

    schema_version: Literal[2] = 2
    decision_id: str = Field(min_length=1, pattern=r"^allocation_[0-9a-f]{24}$")
    run_id: str = Field(min_length=1)
    based_on_snapshot_revision: str = Field(min_length=1)
    feature_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_id: str = Field(min_length=1)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    retire_candidate_ids: list[str] = Field(min_length=1)
    expansions: list[AllocationExpansion] = Field(min_length=1)
    reason_codes: list[str] = Field(min_length=1)
    status: AllocationDecisionStatus = "pending"
    error: str | None = None
    created_at: str
    applied_at: str | None = None

    @model_validator(mode="after")
    def state_and_targets_are_consistent(self) -> "AllocationDecision":
        if len(self.retire_candidate_ids) != len(set(self.retire_candidate_ids)):
            raise ValueError("allocation decision cannot retire a candidate twice")
        if len(self.retire_candidate_ids) != len(self.expansions):
            raise ValueError("allocation decision requires one expansion per retirement")
        expansion_ids = [item.expansion_id for item in self.expansions]
        if len(expansion_ids) != len(set(expansion_ids)):
            raise ValueError("allocation decision expansion ids must be unique")
        child_ids = [
            item.new_candidate_id
            for item in self.expansions
            if item.new_candidate_id is not None
        ]
        if len(child_ids) != len(set(child_ids)):
            raise ValueError("allocation decision candidate ids must be unique")
        if self.status in {"superseded", "terminal_error"} and not self.error:
            raise ValueError("terminal allocation decision requires an error")
        if self.status == "applied" and (
            self.applied_at is None
            or any(item.new_candidate_id is None for item in self.expansions)
        ):
            raise ValueError("applied allocation decision requires materialized expansions")
        return self


__all__ = [
    "AdaptiveReconciliationOutcome",
    "AdaptiveReconciliationRecord",
    "AdaptiveReconciliationStatus",
    "adaptive_reconciliation_identity",
    "AllocationDecision",
    "AllocationDecisionStatus",
    "AllocationEligibility",
    "AllocationExpansion",
    "AllocationExpansionIntent",
    "AllocationPlan",
    "CandidateFeatures",
    "ExpansionSource",
    "FeatureView",
    "PriorityReplacePolicySpec",
    "SearchSnapshot",
    "SnapshotCandidate",
    "SnapshotNode",
    "compute_allocation_plan",
    "compute_feature_view",
    "snapshot_identity",
]
