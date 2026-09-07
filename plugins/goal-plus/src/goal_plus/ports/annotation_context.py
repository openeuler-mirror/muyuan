"""Annotation input snapshots and read-only context projection contracts."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypedDict

from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.shared_tools import SharedToolRecord
from goal_plus.ports.annotation import AnnotationCandidateEvidence

# Existing prompt I/O budgets, independent of any model execution driver.
MAX_EVIDENCE_ANNOTATION_DIFF_BYTES = 1024 * 1024
MAX_EVIDENCE_PEER_DIFF_BYTES = 64 * 1024
TOOL_VIEW_MAX_CONTENT_BYTES = 256 * 1024


@dataclass(frozen=True)
class AnnotationCandidateContext(AnnotationCandidateEvidence):
    workspace: Path
    workspace_base_revision: str | None
    base_artifact_ref: ArtifactRef | None


class AnnotationContextSource(Protocol):
    def read_context_candidate(
        self,
        run_id: str,
        candidate_id: str,
    ) -> AnnotationCandidateContext: ...
    def read_context_candidates(
        self, run_id: str
    ) -> list[AnnotationCandidateContext]: ...
    def pending_tool_ids(self, run_id: str) -> set[str]: ...
    def tool_view_input(
        self,
        run_id: str,
        tool: SharedToolRecord,
        *,
        max_content_bytes: int,
    ) -> tuple[dict[str, Any], int]: ...


class AnnotationContext(TypedDict):
    run_id: str
    candidate_id: str
    iteration: int
    agent_summary: str | None
    exact_attempt_commit: str | None
    exact_attempt_artifact_ref: dict[str, Any]
    changed_files: list[str]
    actual_diff: str
    actual_diff_omitted: str | None
    candidate_base_commit: str | None
    candidate_base_artifact_ref: dict[str, Any] | None
    candidate_changed_files: list[str]
    candidate_diff: str
    candidate_diff_omitted: str | None
    diff_context_policy: str
    verifier_result: dict[str, Any]
    relevant_metrics: dict[str, Any]
    verifier_contract: list[dict[str, Any]]
    objective: str
    task_context: str
    task_context_source: str
    supplemental_evaluation_enabled: bool
    peer_evidence: list[dict[str, Any]]
    artifact_views: list[dict[str, Any]]
    comparison_basis: list[dict[str, Any]]
    published_tools: list[dict[str, Any]]
    tool_adoptions: list[dict[str, Any]]
    annotator: dict[str, Any]
    outer_deadline_at: str | None
    runtime_root: str
