"""Pure contracts and structural invariants."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import (
    Any,
    Literal,
)

from pydantic import (
    Field,
    model_validator,
)

from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.base import SearchModel
from goal_plus.domain.search import VerifierRole
from goal_plus.domain.settlement import (
    SettlementDisposition,
    SettlementReceipt,
)
from goal_plus.domain.shared_tools import (
    SharedToolPublishStatus,
    SharedToolRecord,
    ToolAdoptionRecord,
    ToolizationAdvisory,
    ToolizationDecision,
)
from goal_plus.domain.verifier import VerifierSubmission

IterationDisposition = SettlementDisposition

# These keys are added by trusted runtime adapters to carry provenance that is
# useful for local reconciliation but must not be exposed as candidate-facing
# metric data.  An underscore is otherwise a valid verifier metric name (for
# example ``_coverage``), so prefix matching would silently discard business
# values.
PRIVATE_METRIC_KEYS = frozenset({"_candidate_attempt", "_thinkthread_settlement"})


def same_json_value(left: Any, right: Any) -> bool:
    """Compare JSON values without Python's bool/int/float coercion."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return set(left) == set(right) and all(
            same_json_value(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            same_json_value(item, other) for item, other in zip(left, right)
        )
    return left == right


def _finite_json_tree(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite_json_tree(item) for item in value.values())
    if isinstance(value, list):
        return all(_finite_json_tree(item) for item in value)
    return True


def compact_verifier_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """Drop a fully-covered JSON stdout tail from one metric object."""
    tail = metrics.get("stdout_tail")
    if not isinstance(tail, str):
        return metrics

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate JSON fields")
        return result

    def invalid_constant(value: str) -> Any:
        raise ValueError(value)

    try:
        decoded = json.loads(
            tail, object_pairs_hook=unique_object, parse_constant=invalid_constant,
        )
    except (TypeError, ValueError):
        return metrics
    if not isinstance(decoded, dict) or not decoded or not _finite_json_tree(decoded) or not all(
        key in metrics and key != "stdout_tail"
        and same_json_value(value, metrics[key])
        for key, value in decoded.items()
    ):
        return metrics
    return {key: value for key, value in metrics.items() if key != "stdout_tail"}


def compact_metric_tree(value: Any) -> Any:
    """Recursively compact verifier metric objects without filtering fields."""
    if isinstance(value, dict):
        return {
            key: compact_metric_tree(nested)
            for key, nested in compact_verifier_metrics(value).items()
        }
    if isinstance(value, list):
        return [compact_metric_tree(item) for item in value]
    return value


def public_metric_projection(metrics: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Keep JSON-safe verifier metrics while preserving zero/false/text values."""

    omitted = False

    def value(item: Any) -> Any:
        nonlocal omitted
        if item is None or isinstance(item, (str, bool, int)):
            return item
        if isinstance(item, float):
            if math.isfinite(item):
                return item
            omitted = True
            return None
        if isinstance(item, dict):
            result: dict[str, Any] = {}
            for key, nested in compact_verifier_metrics(item).items():
                if not isinstance(key, str):
                    omitted = True
                    continue
                projected = value(nested)
                if projected is not None or nested is None:
                    result[key] = projected
            return result
        if isinstance(item, (list, tuple)):
            return [value(nested) for nested in item]
        omitted = True
        return None

    projected: dict[str, Any] = {}
    for source, item in compact_verifier_metrics(metrics).items():
        source_name = str(source)
        if source_name in PRIVATE_METRIC_KEYS:
            omitted = True
            continue
        projected_value = value(item)
        if projected_value is not None or item is None:
            projected[source_name] = projected_value
    return projected, omitted


def shared_tool_feedback_projection(payload: dict[str, Any], *, enabled: bool) -> dict[str, Any]:
    """Omit known-empty optional tool fields only for a disabled capability."""
    if enabled is not False:
        return payload
    empty: dict[str, Any] = {
        "shared_tools": [], "shared_tool_errors": [],
        "shared_tool_staging_dir": None, "shared_tool_staged_entries": [],
        "shared_tool_staged_file_count": 0, "shared_tool_staged_bytes": 0,
        "shared_tool_consumed_entries": [], "shared_tool_deduplicated_entries": [],
        "shared_tool_publish_status": "not_staged",
        "toolization_decision": None, "toolization_advisories": [],
    }
    return {
        key: value for key, value in payload.items()
        if key not in empty or not (
            value is None or same_json_value(value, empty[key])
        )
    }


def verifier_feedback_projection(
    payload: dict[str, Any], *, shared_tools_enabled: bool = True,
) -> dict[str, Any]:
    """Remove only verifier log JSON already represented by its metrics.

    Verifier logs remain persisted verbatim.  This projection is for a model
    response only: a JSON object in ``stdout_tail`` is dropped when every
    field is present in the same verifier result's metrics with the same JSON
    type and value.  Truncated, mixed, scalar, malformed, or conflicting
    output stays visible so diagnostics are never silently discarded.
    """

    projected = dict(shared_tool_feedback_projection(payload, enabled=shared_tools_enabled))
    if isinstance(payload.get("score_report"), dict):
        projected["score_report"] = verifier_feedback_projection(
            payload["score_report"], shared_tools_enabled=shared_tools_enabled,
        )
    results = payload.get("verifier_results")
    if not isinstance(results, list):
        return projected
    projected_results: list[Any] = []
    for result in results:
        if not isinstance(result, dict):
            projected_results.append(result)
            continue
        item = dict(result)
        metrics = item.get("metrics")
        tail = metrics.get("stdout_tail") if isinstance(metrics, dict) else None
        if isinstance(tail, str) and isinstance(metrics, dict):
            item["metrics"] = compact_verifier_metrics(metrics)
        projected_results.append(item)
    projected["verifier_results"] = projected_results
    return projected


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
    candidate_action: Literal["end_turn"] = "end_turn"
    attempt_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    agent_session_id: str = Field(min_length=1)
    frozen_spec_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    spec_hash: str | None = Field(default=None, exclude_if=lambda value: value is None)
    hypothesis: str = Field(min_length=1)
    toolization_decision: ToolizationDecision | None = None
    accepted_at: str
    message: str = Field(min_length=1)


class PendingVerification(SearchModel):
    attempt_id: str
    artifact_ref: ArtifactRef
    started_at: str
    state: Literal["running", "needs_recovery"] = "running"
    error: str | None = Field(default=None, exclude_if=lambda value: value is None)


class ScoreReport(SearchModel):
    run_id: str
    candidate_id: str
    frozen_spec_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    spec_hash: str | None = Field(default=None, exclude_if=lambda value: value is None)
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
    verification: PendingVerification | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    disposition: IterationDisposition | None = None
    failure_class: str | None = None
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
    verifier_submission: VerifierSubmission | None = None
    iteration: int
    frozen_spec_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    spec_hash: str | None = Field(default=None, exclude_if=lambda value: value is None)
    settlement_sequence: int = Field(default=0, ge=0)
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
    settlement_receipt: SettlementReceipt | None = Field(
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
    shared_tool_staging_dir: Path | None = None
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

    @model_validator(mode="after")
    def settlement_receipt_matches_iteration(self) -> "IterationRecord":
        receipt = self.settlement_receipt
        if receipt is None:
            return self
        if (
            self.settlement_id != receipt.settlement_id
            or self.frozen_spec_id != receipt.frozen_spec_id
            or self.spec_hash != receipt.spec_hash
            or self.iteration != receipt.iteration
            or self.artifact_ref != receipt.attempt_artifact_ref
            or self.attempt_base_artifact_ref != receipt.attempt_base_artifact_ref
            or self.artifact_hash != receipt.attempt_artifact_hash
            or self.score != receipt.evidence.hard_score
            or self.process_passed != receipt.evidence.process_passed
            or self.disposition != receipt.disposition
            or self.workspace_artifact_after_settlement
            != receipt.ledger_artifact_ref
        ):
            raise ValueError("settlement receipt does not match iteration Evidence")
        return self


class ResultLedgerEntry(SearchModel):
    source_run_id: str
    source_candidate_id: str
    frozen_spec_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    spec_hash: str | None = Field(default=None, exclude_if=lambda value: value is None)
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
