"""Pure contracts and structural invariants."""

from __future__ import annotations

from typing import (
    Any,
    Literal,
)

from pydantic import (
    Field,
    field_validator,
    model_validator,
)

from goal_plus.domain.agents import AgentHostKind
from goal_plus.domain.artifacts import (
    ArtifactRef,
    WorkspaceBackend,
)
from goal_plus.domain.base import SearchModel
from goal_plus.domain.shared_tools import (
    ToolViewRecord,
    ToolViewRef,
)

MAX_ANNOTATION_ATTEMPTS = 3
ANNOTATION_RETRY_BACKOFF_SECONDS = (30, 120)


class EvidenceAnnotatorSpec(SearchModel):
    host: AgentHostKind | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    timeout_seconds: int = Field(default=1800, gt=0, le=1800)

    @field_validator("model", "reasoning_effort")
    @classmethod
    def values_must_be_nonempty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("annotator option must be non-empty when provided")
        return value


class ResolvedEvidenceAnnotatorProfile(SearchModel):
    host: AgentHostKind = "codex"
    model: str | None = None
    reasoning_effort: str | None = None
    timeout_seconds: int = Field(gt=0, le=1800)
    configuration_source: Literal["host_process"] = "host_process"


EvaluationConfidence = Literal["high", "medium", "low"]


ComparisonRelation = Literal[
    "similar",
    "different",
    "tradeoff",
    "complementary",
    "unknown",
]


class SupplementalDimension(SearchModel):
    name: str = Field(min_length=1, max_length=120)
    finding: str = Field(min_length=1, max_length=1000)
    confidence: EvaluationConfidence
    evidence: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("name", "finding", mode="before")
    @classmethod
    def text_must_be_one_line(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        if "\n" in value or "\r" in value:
            raise ValueError("supplemental evaluation text must be one line")
        return " ".join(value.strip().split())


class EvidenceComparisonReference(SearchModel):
    candidate_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    commit: str | None = Field(default=None, min_length=1)
    artifact_ref: ArtifactRef | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def immutable_identity_is_present(self) -> "EvidenceComparisonReference":
        if self.commit is None and self.artifact_ref is None:
            raise ValueError("evidence comparison requires an immutable artifact")
        if (
            self.commit is not None
            and self.artifact_ref is not None
            and self.artifact_ref.kind == "git_commit"
            and self.artifact_ref.id != self.commit
        ):
            raise ValueError("evidence comparison commit does not match artifact_ref")
        return self


class EvidenceArtifactViewReference(SearchModel):
    """Immutable workspace evidence authorized for one annotation task."""

    view_id: str = Field(min_length=1)
    role: Literal["target", "peer"]
    candidate_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    artifact_ref: ArtifactRef
    workspace_backend: WorkspaceBackend
    display_name: str = Field(min_length=1)


class PeerComparison(EvidenceComparisonReference):
    relation: ComparisonRelation
    rationale: str = Field(min_length=1, max_length=1000)
    evidence: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("rationale", mode="before")
    @classmethod
    def rationale_must_be_one_line(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        if "\n" in value or "\r" in value:
            raise ValueError("peer comparison rationale must be one line")
        return " ".join(value.strip().split())


class SupplementalEvaluation(SearchModel):
    summary: str = Field(min_length=1, max_length=1000)
    dimensions: list[SupplementalDimension] = Field(min_length=1, max_length=8)
    comparisons: list[PeerComparison] = Field(default_factory=list, max_length=8)
    limitations: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("summary", mode="before")
    @classmethod
    def summary_must_be_one_line(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        if "\n" in value or "\r" in value:
            raise ValueError("supplemental evaluation summary must be one line")
        return " ".join(value.strip().split())

    @field_validator("limitations", mode="before")
    @classmethod
    def limitations_must_be_one_line(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        normalized = []
        for item in value:
            if not isinstance(item, str):
                normalized.append(item)
                continue
            if "\n" in item or "\r" in item:
                raise ValueError("supplemental evaluation limitation must be one line")
            normalized.append(" ".join(item.strip().split()))
        return normalized


class EvidenceViewRecord(SearchModel):
    run_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    attempt_commit: str | None = Field(default=None, min_length=1)
    attempt_artifact_ref: ArtifactRef | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    description: str = Field(min_length=1, max_length=1000)
    supplemental_evaluation: SupplementalEvaluation | None = None
    comparison_basis: list[EvidenceComparisonReference] = Field(
        default_factory=list,
        max_length=8,
    )
    tool_views: list[ToolViewRecord] = Field(default_factory=list)
    created_at: str

    @model_validator(mode="after")
    def immutable_identity_is_present(self) -> "EvidenceViewRecord":
        if self.attempt_commit is None and self.attempt_artifact_ref is None:
            raise ValueError("evidence view requires an immutable attempt artifact")
        return self

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        if "\n" in value or "\r" in value:
            raise ValueError("evidence view description must be one line")
        return " ".join(value.strip().split())


class GlobalEvidenceViewReference(SearchModel):
    candidate_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    commit: str | None = Field(default=None, min_length=1)
    artifact_ref: ArtifactRef | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    view_created_at: str
    supplemental_evaluation_present: bool = False

    @model_validator(mode="after")
    def immutable_identity_is_present(self) -> "GlobalEvidenceViewReference":
        if self.commit is None and self.artifact_ref is None:
            raise ValueError("global evidence view requires an immutable artifact")
        return self


class GlobalEvidenceReadRecord(SearchModel):
    read_at: str
    source: Literal["explicit", "verifier_injected"] = "explicit"
    candidate_verifier_iteration: int | None = Field(default=None, ge=1)
    run_verifier_iteration_count: int = Field(default=0, ge=0)
    evidence_count: int = Field(ge=0)
    completed_view_count: int = Field(ge=0)
    completed_supplemental_evaluation_count: int = Field(ge=0)
    completed_views: list[GlobalEvidenceViewReference] = Field(default_factory=list)

    @model_validator(mode="after")
    def counts_match_completed_views(self) -> "GlobalEvidenceReadRecord":
        if self.completed_view_count != len(self.completed_views):
            raise ValueError("completed_view_count must match completed_views")
        supplemental_count = sum(
            item.supplemental_evaluation_present for item in self.completed_views
        )
        if self.completed_supplemental_evaluation_count != supplemental_count:
            raise ValueError(
                "completed_supplemental_evaluation_count must match completed_views"
            )
        if self.completed_view_count > self.evidence_count:
            raise ValueError("completed_view_count cannot exceed evidence_count")
        return self


class EvidenceDetailReadRecord(SearchModel):
    read_at: str
    candidate_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    commit: str | None = Field(default=None, min_length=1)
    artifact_ref: ArtifactRef | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def immutable_identity_is_present(self) -> "EvidenceDetailReadRecord":
        if self.commit is None and self.artifact_ref is None:
            raise ValueError("evidence detail read requires an immutable artifact")
        return self


class EvidenceAnnotationTask(SearchModel):
    task_id: str = Field(min_length=1)
    settlement_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    attempt_base_commit: str | None = Field(default=None, min_length=1)
    attempt_commit: str | None = Field(default=None, min_length=1)
    attempt_base_artifact_ref: ArtifactRef | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    attempt_artifact_ref: ArtifactRef | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    attempt_changed_files: list[str] = Field(default_factory=list)
    task_context_source: Literal[
        "goal_plus_raw_goal",
        "frozen_objective",
    ] | None = None
    task_context_ref: str | None = None
    task_context_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    supplemental_evaluation_enabled: bool = False
    comparison_basis: list[EvidenceComparisonReference] = Field(
        default_factory=list,
        max_length=8,
    )
    artifact_views: list[EvidenceArtifactViewReference] = Field(
        default_factory=list,
        max_length=9,
    )
    profile: ResolvedEvidenceAnnotatorProfile | None = None
    outer_deadline_at: str | None = None
    queue_generation: int = Field(default=1, ge=1)
    queue_revision: int = Field(default=0, ge=0)
    state: Literal[
        "pending",
        "retry_wait",
        "completed",
        "terminal_error",
        "cancelled",
    ] = "pending"
    attempts: int = Field(default=0, ge=0)
    next_attempt_at: str | None = None
    error_fingerprint: str | None = None
    last_error: str | None = None
    attempt_history: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, int | float] = Field(default_factory=dict)
    view: EvidenceViewRecord | None = None
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def terminal_state_matches_view(self) -> "EvidenceAnnotationTask":
        if self.state == "completed" and self.view is None:
            raise ValueError("completed annotation task requires an Evidence View")
        if self.state != "completed" and self.view is not None:
            raise ValueError("only completed annotation task can provide an Evidence View")
        if self.state == "terminal_error" and not self.last_error:
            raise ValueError("terminal annotation task requires an error")
        return self

    @model_validator(mode="after")
    def artifact_views_match_frozen_task(self) -> "EvidenceAnnotationTask":
        if self.attempt_base_commit is None and self.attempt_base_artifact_ref is None:
            raise ValueError("annotation task requires an immutable base artifact")
        if self.attempt_commit is None and self.attempt_artifact_ref is None:
            raise ValueError("annotation task requires an immutable attempt artifact")
        if not self.artifact_views:
            return self
        view_ids = [item.view_id for item in self.artifact_views]
        if len(view_ids) != len(set(view_ids)):
            raise ValueError("annotation Artifact View ids must be unique")
        targets = [item for item in self.artifact_views if item.role == "target"]
        if len(targets) != 1:
            raise ValueError("annotation task requires exactly one target Artifact View")
        target = targets[0]
        if (
            target.candidate_id != self.candidate_id
            or target.iteration != self.iteration
        ):
            raise ValueError("annotation target Artifact View identity does not match task")
        if self.attempt_artifact_ref is not None:
            if target.artifact_ref != self.attempt_artifact_ref:
                raise ValueError("annotation target artifact does not match attempt")
        elif target.artifact_ref.id != self.attempt_commit:
            raise ValueError("annotation target artifact does not match attempt commit")
        peers = [
            (item.candidate_id, item.iteration)
            for item in self.artifact_views
            if item.role == "peer"
        ]
        comparison = [
            (item.candidate_id, item.iteration)
            for item in self.comparison_basis
        ]
        if peers != comparison:
            raise ValueError("annotation peer Artifact Views must match comparison basis")
        return self


class EvidenceAnnotationQueue(SearchModel):
    """Durable admission fence for best-effort Evidence annotation work."""

    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1)
    state: Literal["open", "closed"] = "open"
    generation: int = Field(default=1, ge=1)
    revision: int = Field(default=0, ge=0)
    accepted_tasks: int = Field(default=0, ge=0)
    created_at: str
    updated_at: str
    closed_at: str | None = None
    closed_reason: str | None = None


ToolViewOutput = ToolViewRef


class EvidenceAnnotationOutput(SearchModel):
    description: str = Field(min_length=1, max_length=1000)
    supplemental_evaluation: SupplementalEvaluation | None = None
    tool_views: list[ToolViewOutput] = Field(default_factory=list)

    @field_validator("description", mode="before")
    @classmethod
    def description_must_be_one_line(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        if "\n" in value or "\r" in value:
            raise ValueError("evidence annotation must be one line")
        return " ".join(value.strip().split())


class AnnotationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        usage: dict[str, int | float] | None = None,
    ) -> None:
        super().__init__(message)
        self.usage = dict(usage or {})


class PermanentAnnotationError(AnnotationError):
    pass


class TransientAnnotationError(AnnotationError):
    pass


class AnnotationOutputError(TransientAnnotationError):
    pass


def validate_supplemental_output(
    output: EvidenceAnnotationOutput,
    *,
    enabled: bool,
    comparison_basis: list[dict[str, Any]],
) -> None:
    if not enabled:
        if output.supplemental_evaluation is not None:
            raise AnnotationOutputError(
                "annotation returned supplemental evaluation while disabled"
            )
        return
    evaluation = output.supplemental_evaluation
    if evaluation is None:
        raise AnnotationOutputError(
            "annotation omitted the required supplemental evaluation"
        )
    expected = [
        EvidenceComparisonReference.model_validate(item)
        for item in comparison_basis
    ]
    actual = [
        EvidenceComparisonReference(
            candidate_id=item.candidate_id,
            iteration=item.iteration,
            commit=item.commit,
            artifact_ref=item.artifact_ref,
        )
        for item in evaluation.comparisons
    ]
    if actual != expected:
        raise AnnotationOutputError(
            "annotation peer comparisons do not match the dynamic comparison basis"
        )
