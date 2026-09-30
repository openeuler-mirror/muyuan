"""Strict candidate execution trace schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.base import SearchModel


CandidateTraceKind = Literal[
    "evidence.global_returned",
    "evidence.detail_returned",
    "reference.opened",
    "verifier.submitted",
    "verifier.observed",
    "verifier.settled",
    "operation.failed",
]


class CandidateTraceIdentity(SearchModel):
    run_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    agent_session_id: str = Field(min_length=1)
    execution_generation: int = Field(ge=0)


class GlobalEvidenceReturnedPayload(SearchModel):
    source: Literal["explicit", "verifier_injected"]
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    entry_count: int = Field(ge=0)
    mode: Literal["manual", "auto", "independent"]
    candidate_iteration: int | None = Field(default=None, ge=1)
    run_iteration_count: int = Field(ge=0)
    verifier_attempt_id: str | None = Field(default=None, min_length=1)
    checkpoint_ref: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def injected_evidence_has_attempt(self) -> "GlobalEvidenceReturnedPayload":
        if self.source == "verifier_injected" and self.verifier_attempt_id is None:
            raise ValueError("verifier-injected Evidence requires its verifier attempt")
        if self.source == "explicit" and self.verifier_attempt_id is not None:
            raise ValueError("explicit Evidence cannot name a verifier attempt")
        return self


class EvidenceDetailReturnedPayload(SearchModel):
    target_candidate_id: str = Field(min_length=1)
    target_iteration: int = Field(ge=1)
    artifact_ref: ArtifactRef
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkpoint_ref: str | None = Field(default=None, min_length=1)


class ReferenceOpenedPayload(SearchModel):
    source_candidate_id: str = Field(min_length=1)
    source_iteration: int = Field(ge=1)
    artifact_ref: ArtifactRef
    reference_id: str = Field(min_length=1)
    workspace_provider: str = Field(min_length=1)
    checkpoint_ref: str | None = Field(default=None, min_length=1)


class VerifierSubmittedPayload(SearchModel):
    attempt_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    artifact_ref: ArtifactRef | None = None
    request_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def artifact_or_request_is_present(self) -> "VerifierSubmittedPayload":
        if self.artifact_ref is None and self.request_id is None:
            raise ValueError("verifier submission requires an artifact or request identity")
        return self


class VerifierObservedPayload(SearchModel):
    attempt_id: str = Field(min_length=1)
    status: Literal[
        "accepted",
        "pending",
        "ready",
        "needs_recovery",
        "retry_requested",
    ]


class VerifierSettledPayload(SearchModel):
    attempt_id: str = Field(min_length=1)
    settlement_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    attempt_base_artifact_ref: ArtifactRef | None = None
    attempt_artifact_ref: ArtifactRef
    post_settlement_artifact_ref: ArtifactRef
    attempt_changed_files: list[str] = Field(default_factory=list)
    process_passed: bool
    score: float | None = None
    failure_class: str | None = None
    disposition: str = Field(min_length=1)
    settled_at: str


class OperationFailedPayload(SearchModel):
    operation: Literal[
        "global_evidence",
        "evidence_detail",
        "reference_open",
        "verifier_submission",
        "verifier_observation",
        "verifier_settlement",
    ]
    verified_target: dict[str, Any] = Field(default_factory=dict)
    error_category: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,127}$")


_PAYLOAD_MODELS = {
    "evidence.global_returned": GlobalEvidenceReturnedPayload,
    "evidence.detail_returned": EvidenceDetailReturnedPayload,
    "reference.opened": ReferenceOpenedPayload,
    "verifier.submitted": VerifierSubmittedPayload,
    "verifier.observed": VerifierObservedPayload,
    "verifier.settled": VerifierSettledPayload,
    "operation.failed": OperationFailedPayload,
}


class CandidateTraceEvent(SearchModel):
    schema_version: Literal[1] = 1
    event_id: str = Field(pattern=r"^trace_[0-9a-f]{32}$")
    sequence: int = Field(ge=1)
    run_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    agent_session_id: str = Field(min_length=1)
    execution_generation: int = Field(ge=0)
    occurred_at: str
    kind: CandidateTraceKind
    operation_id: str = Field(min_length=1)
    host_call_id: str | None = Field(default=None, min_length=1)
    payload: dict[str, Any]

    @model_validator(mode="after")
    def payload_matches_kind(self) -> "CandidateTraceEvent":
        model = _PAYLOAD_MODELS[self.kind]
        self.payload = model.model_validate(self.payload).model_dump(
            mode="json", exclude_none=True
        )
        return self


class CandidateTraceSnapshot(SearchModel):
    schema_version: Literal[1] = 1
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    content: Any


def validate_candidate_trace_payload(
    kind: CandidateTraceKind,
    payload: dict[str, Any] | SearchModel,
) -> dict[str, Any]:
    raw = payload.model_dump(mode="json", exclude_none=True) if isinstance(payload, SearchModel) else payload
    return _PAYLOAD_MODELS[kind].model_validate(raw).model_dump(
        mode="json", exclude_none=True
    )
