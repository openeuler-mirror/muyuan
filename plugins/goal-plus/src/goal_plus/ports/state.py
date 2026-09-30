"""Pure contracts and structural invariants."""

from __future__ import annotations

from pathlib import Path
from typing import (
    Any,
    Literal,
)

from pydantic import (
    Field,
    model_validator,
)

from goal_plus.domain.adaptive.models import AllocationEligibility
from goal_plus.domain.agents import SelectedModel
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.base import SearchModel
from goal_plus.domain.evidence import (
    PendingVerification,
    IterationRecord,
    ResultLedgerEntry,
    ScoreReport,
)
from goal_plus.domain.promotion import (
    PromotionEvidence,
    PromotionRecord,
    PublicationOutcome,
    SearchResultState,
)
from goal_plus.domain.search import (
    BootstrapVerification,
    CandidateRecovery,
    CandidateTask,
    RunState,
    VerifierInvalidationReason,
)
from goal_plus.domain.selection import ControllerSelectionDirective
from goal_plus.domain.shared_tools import ToolCopyReceipt, ToolizationDecision
from goal_plus.ports.candidate_artifacts import CandidateArtifactState


class LocalVerificationRequest(SearchModel):
    """Private replay input; the public acknowledgement remains PendingVerification."""

    frozen_spec_id: str
    execution_generation: int
    iteration: int = Field(ge=1)
    attempt: CandidateArtifactState
    base_artifact_ref: ArtifactRef
    changed_files: list[str]
    agent_session_id: str | None
    hypothesis: str | None
    toolization_decision: ToolizationDecision | None
    shared_tool_staging_dir: Path | None = None
    shared_tool_capture_error: str | None = None
    tool_copies: list[ToolCopyReceipt] = Field(default_factory=list)
    report: ScoreReport | None = None


class ProviderMutationRecord(SearchModel):
    """Durable intent/result for a provider mutation owned by one Search run."""

    provider: Literal["thinkthread"] = "thinkthread"
    request_id: str = Field(min_length=1)
    operation: Literal[
        "baseline_snapshot",
        "branch_snapshot",
        "inner_git_settlement",
        "verifier_run",
        "publication_snapshot",
        "apply",
        "snapshot_remove",
    ]
    state: Literal[
        "prepared",
        "platform_mutation_started",
        "succeeded",
        "failed",
        "needs_recovery",
        "closed",
    ] = "prepared"
    identity: dict[str, Any] = Field(default_factory=dict)
    params_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] = Field(default_factory=dict)
    close_state: Literal[
        "not_started",
        "closing",
        "needs_recovery",
        "closed",
    ] = "not_started"
    close_attempts: int = Field(default=0, ge=0)
    close_error: dict[str, Any] = Field(default_factory=dict)
    close_updated_at: str | None = None
    created_at: str
    updated_at: str


class ProviderResourceCleanupRecord(SearchModel):
    """Durable ownership, retention, and cleanup state for a provider resource."""

    provider: Literal["thinkthread"] = "thinkthread"
    resource_kind: Literal["fs_snapshot"] = "fs_snapshot"
    resource_id: str = Field(pattern=r"^fsnap-")
    purposes: list[str] = Field(default_factory=list)
    ownership_request_ids: list[str] = Field(default_factory=list)
    state: Literal[
        "retained",
        "prepared",
        "removing",
        "removed",
        "already_absent",
        "needs_recovery",
        "failed",
    ] = "retained"
    pins: list[str] = Field(default_factory=list)
    request_id: str | None = Field(default=None, min_length=1)
    attempts: int = Field(default=0, ge=0)
    result: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def state_has_retention_or_mutation_evidence(
        self,
    ) -> "ProviderResourceCleanupRecord":
        if self.state == "retained" and not self.pins:
            raise ValueError("retained provider resource requires at least one pin")
        if self.state != "retained" and self.request_id is None:
            raise ValueError("provider cleanup mutation requires a RequestId")
        if self.state in {"removed", "already_absent"} and self.pins:
            raise ValueError("removed provider resource cannot remain pinned")
        return self


class RunRecord(SearchModel):
    run_id: str
    state: RunState
    frozen_spec_id: str
    bootstrap_complete: bool = False
    source_path: str
    source_repository: Path | None = None
    source_subdir: str = "."
    source_artifact_ref: ArtifactRef | None = None
    source_provider_handle: dict[str, Any] = Field(default_factory=dict)
    provider_mutations: list[ProviderMutationRecord] = Field(default_factory=list)
    provider_cleanup: list[ProviderResourceCleanupRecord] = Field(default_factory=list)
    workspace_bootstrap_verification: BootstrapVerification | None = None
    created_at: str
    next_candidate_index: int = 1
    next_plan_index: int = 1
    next_agent_session_index: int = 1
    candidates_total: int = 0
    candidates_evaluated: int = 0
    best_candidate_id: str | None = None
    best_score: float | None = None
    selected_candidate_id: str | None = None
    selected_score: float | None = None
    selected_hard_score: float | None = None
    selected_quality_score: float | None = None
    selection_basis_hard_score: float | None = None
    final_verifier_score: float | None = None
    selection_evidence_source: str | None = None
    controller_selection: ControllerSelectionDirective | None = None
    selection_trace: list[dict[str, Any]] = Field(default_factory=list)
    selection_exclusions: list[dict[str, Any]] = Field(default_factory=list)
    rejected_selection_subjects: list[dict[str, Any]] = Field(default_factory=list)
    selected_iteration: int | None = None
    selected_git_head: str | None = None
    selected_artifact_ref: ArtifactRef | None = None
    selected_artifact_hash: str | None = None
    promotion: PromotionRecord | None = None
    search_result: SearchResultState = "unknown"
    publication_outcome: PublicationOutcome | None = None
    budget_used: dict[str, Any] = Field(default_factory=dict)
    source_run_id: str | None = None
    inherited_research: dict[str, Any] = Field(default_factory=dict)
    invalidated_at: str | None = None
    invalidation_reason: VerifierInvalidationReason | None = None
    invalidation_summary: str | None = None
    invalidation_evidence: list[dict[str, Any]] = Field(default_factory=list)
    replacement_run_id: str | None = None
    selected_models: list[SelectedModel] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
    )


class CandidateRecord(SearchModel):
    candidate_id: str
    status: Literal["created", "evaluated", "failed"]
    task: CandidateTask
    allocation_eligibility: AllocationEligibility = "eligible"
    retired_by_decision_id: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    detected_changed_files: list[str] = Field(default_factory=list)
    touched_denied_files: bool = False
    changed_outside_allowed: bool = False
    score_report: ScoreReport | None = None
    promotion_report: ScoreReport | None = None
    promotion_evidence: PromotionEvidence | None = None
    pending_tool_copies: list[ToolCopyReceipt] = Field(default_factory=list, exclude_if=lambda value: not value)
    iterations: list[IterationRecord] = Field(default_factory=list)
    results_ledger: list[ResultLedgerEntry] = Field(default_factory=list)
    results_ledger_git_head: str | None = None
    results_ledger_artifact_ref: ArtifactRef | None = None
    initial_attempt_base_artifact_ref: ArtifactRef | None = None
    execution_generation: int = Field(default=0, ge=0)
    recovery: CandidateRecovery | None = None
    verification: PendingVerification | None = None
    verification_request: LocalVerificationRequest | None = None
    inner_agent_state: dict[str, Any] = Field(default_factory=dict)
    provider_mutations: list[ProviderMutationRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def recovery_matches_candidate(self) -> "CandidateRecord":
        recovery = self.recovery
        if recovery is None:
            return self
        if recovery.point.artifact_ref.provider != self.task.workspace_provider:
            raise ValueError("recovery provider must match the candidate")
        if recovery.generation > self.execution_generation:
            raise ValueError("recovery generation cannot exceed the candidate execution generation")
        if recovery.state != "restored" and recovery.generation != self.execution_generation:
            raise ValueError("unfinished recovery generation must match the candidate execution generation")
        return self
