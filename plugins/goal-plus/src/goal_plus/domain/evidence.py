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

from goal_plus.domain.adaptive.models import (
    AllocationDecision,
    ScheduleTaskState,
)
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.base import SearchModel
from goal_plus.domain.search import VerifierRole
from goal_plus.domain.shared_tools import (
    SharedToolPublishStatus,
    SharedToolRecord,
    ToolAdoptionRecord,
    ToolizationAdvisory,
    ToolizationDecision,
)

IterationDisposition = Literal["keep", "retain", "discard", "failure"]


class VerifierResult(SearchModel):
    name: str
    role: VerifierRole
    passed: bool
    score: float | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    log_path: Path | None = None
    failure_class: str | None = None


class CandidateAttemptAccepted(SearchModel):
    """Durable acknowledgement for one deferred candidate verification attempt."""

    state: Literal["accepted"] = "accepted"
    attempt_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    agent_session_id: str = Field(min_length=1)
    hypothesis: str = Field(min_length=1)
    toolization_decision: ToolizationDecision | None = None
    accepted_at: str
    message: str = Field(min_length=1)


class ScoreReport(SearchModel):
    run_id: str
    candidate_id: str
    parent_id: str | None = None
    validity_passed: bool
    process_passed: bool
    promotion_passed: bool | None = None
    aggregate_score: float | None = None
    verifier_results: list[VerifierResult]
    touched_denied_files: bool = False
    changed_outside_allowed: bool = False
    hardcoding_suspected: bool = False
    attempt_id: str | None = Field(default=None, min_length=1)
    attempt_iteration: int | None = Field(default=None, ge=1)
    attempt_artifact_ref: ArtifactRef | None = None
    disposition: IterationDisposition | None = None
    best_iteration: int | None = Field(default=None, ge=1)
    best_git_head: str | None = None
    workspace_git_head_after_settlement: str | None = None
    best_artifact_ref: ArtifactRef | None = None
    workspace_artifact_after_settlement: ArtifactRef | None = None
    shared_tool_staged_entries: list[str] | None = None
    shared_tool_staged_file_count: int | None = Field(default=None, ge=0)
    shared_tool_staged_bytes: int | None = Field(default=None, ge=0)
    shared_tool_publish_status: SharedToolPublishStatus | None = None
    shared_tool_errors: list[str] | None = None
    shared_tool_consumed_entries: list[str] | None = None
    shared_tool_deduplicated_entries: list[str] | None = None
    toolization_decision: ToolizationDecision | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    toolization_advisories: list[ToolizationAdvisory] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
    )
    scheduler_status: ScheduleTaskState | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    schedule_task_id: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    allocation_decision: AllocationDecision | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class CandidateAttemptFeedback(SearchModel):
    """Exact verifier feedback delivered after a provider attempt is quiesced."""

    state: Literal["verified"] = "verified"
    attempt_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    agent_session_id: str = Field(min_length=1)
    attempt_iteration: int = Field(ge=1)
    score_report: ScoreReport
    attempt_artifact_ref: ArtifactRef
    workspace_artifact_after_attempt: ArtifactRef
    verified_at: str

    @model_validator(mode="after")
    def report_matches_attempt(self) -> "CandidateAttemptFeedback":
        report = self.score_report
        if (
            report.run_id != self.run_id
            or report.candidate_id != self.candidate_id
            or report.attempt_id != self.attempt_id
            or report.attempt_iteration != self.attempt_iteration
            or report.attempt_artifact_ref != self.attempt_artifact_ref
        ):
            raise ValueError("candidate attempt feedback does not match its ScoreReport")
        workspace_artifact = (
            report.workspace_artifact_after_settlement or report.best_artifact_ref
        )
        if workspace_artifact != self.workspace_artifact_after_attempt:
            raise ValueError(
                "candidate attempt feedback does not match the restored workspace"
            )
        return self


class IterationRecord(SearchModel):
    iteration: int
    settlement_id: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    agent_session_id: str | None = None
    selected_model: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    exact_model_ref: str | None = None
    adapter_version: str | None = None
    model_provenance: dict[str, Any] = Field(default_factory=dict)
    score: float | None = None
    process_passed: bool | None = None
    git_head: str | None = None
    attempt_base_git_head: str | None = None
    artifact_ref: ArtifactRef | None = None
    attempt_base_artifact_ref: ArtifactRef | None = None
    provider_request_ids: list[str] = Field(default_factory=list)
    attempt_changed_files: list[str] = Field(default_factory=list)
    ledger_git_head: str | None = None
    ledger_artifact_ref: ArtifactRef | None = None
    artifact_clean: bool | None = None
    artifact_status: list[str] = Field(default_factory=list)
    git_artifact_clean: bool | None = None
    git_status: list[str] = Field(default_factory=list)
    failure_class: str | None = None
    summary: str = ""
    hypothesis: str = ""
    changed_files: list[str] = Field(default_factory=list)
    touched_denied_files: bool = False
    changed_outside_allowed: bool = False
    artifact_hash: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    log_paths: list[str] = Field(default_factory=list)
    disposition: IterationDisposition | None = None
    schedule_task_id: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    scheduler_status: ScheduleTaskState | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    scheduler_error: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    allocation_decision_id: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    allocation_decision_error: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    restored_to_iteration: int | None = Field(default=None, ge=1)
    restored_to_git_head: str | None = None
    workspace_git_head_after_settlement: str | None = None
    restored_to_artifact_ref: ArtifactRef | None = None
    workspace_artifact_after_settlement: ArtifactRef | None = None
    adopted_tools: list[ToolAdoptionRecord] = Field(default_factory=list, exclude_if=lambda value: not value)
    adoption_confounded: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    shared_tools: list[SharedToolRecord] = Field(default_factory=list)
    shared_tool_errors: list[str] = Field(default_factory=list)
    shared_tool_staged_entries: list[str] = Field(default_factory=list)
    shared_tool_staged_file_count: int = Field(default=0, ge=0)
    shared_tool_staged_bytes: int = Field(default=0, ge=0)
    shared_tool_consumed_entries: list[str] = Field(default_factory=list)
    shared_tool_deduplicated_entries: list[str] = Field(default_factory=list)
    shared_tool_publish_status: SharedToolPublishStatus = "legacy_unknown"
    toolization_decision: ToolizationDecision | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    toolization_advisories: list[ToolizationAdvisory] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
    )
    created_at: str

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_shared_tool_publish_status(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        if "shared_tools" not in payload and "shared_assets" in payload:
            payload["shared_tools"] = payload.pop("shared_assets")
        if "shared_tool_publish_status" not in payload:
            tools = payload.get("shared_tools") or []
            errors = payload.get("shared_tool_errors") or []
            if tools:
                payload["shared_tool_publish_status"] = "partially_published" if errors else "published"
            elif errors:
                payload["shared_tool_publish_status"] = "snapshot_error"
        return payload


class ResultLedgerEntry(SearchModel):
    source_run_id: str
    source_candidate_id: str
    iteration: int | None = Field(default=None, ge=1)
    git_head: str | None = None
    ledger_git_head: str | None = None
    artifact_ref: ArtifactRef | None = None
    ledger_artifact_ref: ArtifactRef | None = None
    metric_name: str = Field(min_length=1)
    score: float | None = None
    status: str = Field(min_length=1)
    hypothesis: str = ""
    failure_class: str | None = None
    created_at: str | None = None


class BestArtifactRecord(SearchModel):
    schema_version: Literal[1, 2] = 2
    run_id: str
    candidate_id: str
    iteration: int = Field(ge=1)
    commit: str | None = Field(default=None, min_length=1)
    artifact_ref: ArtifactRef | None = None
    score: float
    metric_name: str = Field(min_length=1)
    metric_direction: Literal["minimize", "maximize"]
    artifact_hash: str = Field(min_length=1)
    workspace: str = Field(min_length=1)
    changed_files: list[str] = Field(default_factory=list)
    updated_at: str

    @model_validator(mode="after")
    def require_artifact_identity(self) -> "BestArtifactRecord":
        if self.artifact_ref is None and self.commit is None:
            raise ValueError("best artifact requires artifact_ref or legacy commit")
        return self
