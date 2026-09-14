"""Immutable facts emitted by the candidate settlement owner."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.base import SearchModel


SettlementDisposition = Literal["keep", "retain", "discard", "failure"]


class SettlementEvidence(SearchModel):
    """Hard-verifier facts about the exact attempt artifact."""

    metric_name: str = Field(min_length=1)
    hard_score: float | None = Field(default=None, allow_inf_nan=False)
    process_passed: bool
    changed_files: list[str] = Field(default_factory=list)
    attempt_changed_files: list[str] = Field(default_factory=list)
    touched_denied_files: bool = False
    changed_outside_allowed: bool = False
    artifact_clean: bool
    artifact_status: list[str] = Field(default_factory=list)
    failure_class: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    log_paths: list[str] = Field(default_factory=list)


class RollbackReceipt(SearchModel):
    """Exact target and resulting artifact for a completed rollback action."""

    target_artifact_ref: ArtifactRef
    restored_artifact_ref: ArtifactRef
    restored_to_iteration: int | None = Field(default=None, ge=1)
    restored_artifact_hash: str = Field(min_length=1)


class SettlementReceipt(SearchModel):
    """Authoritative version settlement for one immutable attempt."""

    schema_version: Literal[1] = 1
    settlement_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    attempt_base_artifact_ref: ArtifactRef | None = None
    attempt_artifact_ref: ArtifactRef
    attempt_artifact_hash: str = Field(min_length=1)
    evidence: SettlementEvidence
    disposition: SettlementDisposition
    rollback: RollbackReceipt | None = None
    post_settlement_artifact_ref: ArtifactRef
    ledger_artifact_ref: ArtifactRef
    created_at: str

    @model_validator(mode="after")
    def artifact_flow_is_consistent(self) -> "SettlementReceipt":
        refs = [
            self.attempt_base_artifact_ref,
            self.attempt_artifact_ref,
            self.post_settlement_artifact_ref,
            self.ledger_artifact_ref,
        ]
        if self.rollback is not None:
            refs.extend(
                [
                    self.rollback.target_artifact_ref,
                    self.rollback.restored_artifact_ref,
                ]
            )
        providers = {item.provider for item in refs if item is not None}
        if providers != {self.attempt_artifact_ref.provider}:
            raise ValueError("settlement receipt artifact providers must match")
        if self.disposition in {"discard", "failure"} and self.rollback is None:
            raise ValueError("discarded or failed settlement requires rollback receipt")
        if self.disposition in {"keep", "retain"} and self.rollback is not None:
            raise ValueError("kept or retained settlement cannot carry rollback receipt")
        if (
            self.rollback is not None
            and self.rollback.restored_artifact_ref
            != self.post_settlement_artifact_ref
        ):
            raise ValueError("rollback result must match post-settlement artifact")
        return self


__all__ = [
    "RollbackReceipt",
    "SettlementDisposition",
    "SettlementEvidence",
    "SettlementReceipt",
]
