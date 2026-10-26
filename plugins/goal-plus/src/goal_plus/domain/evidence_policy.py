"""Pure eligibility and immutable artifact identity shared by Search and Annotation."""

import math

from goal_plus.domain.annotation import EvidenceComparisonReference
from goal_plus.domain.artifacts import ArtifactRef, WorkspaceProvider
from goal_plus.domain.evidence import IterationRecord


def artifact_iteration_eligible(iteration: IterationRecord) -> bool:
    identity_available = bool(iteration.artifact_ref or iteration.git_head)
    artifact_clean = (
        iteration.artifact_clean
        if iteration.artifact_clean is not None
        else iteration.git_artifact_clean
    )
    return bool(
        iteration.process_passed is True
        and iteration.score is not None
        and math.isfinite(iteration.score)
        and identity_available
        and artifact_clean is True
        and not iteration.touched_denied_files
        and not iteration.changed_outside_allowed
    )


def iteration_artifact_ref(
    iteration: IterationRecord,
    workspace_provider: WorkspaceProvider,
) -> ArtifactRef | None:
    if iteration.artifact_ref is not None:
        return iteration.artifact_ref
    if iteration.git_head is None:
        return None
    return ArtifactRef(
        kind="git_commit",
        id=iteration.git_head,
        provider=workspace_provider,
    )


def iteration_matches_evidence_reference(
    iteration: IterationRecord,
    reference: EvidenceComparisonReference,
    workspace_provider: WorkspaceProvider,
) -> bool:
    return bool(
        iteration.iteration == reference.iteration
        and (reference.commit is None or iteration.git_head == reference.commit)
        and (
            reference.artifact_ref is None
            or iteration_artifact_ref(iteration, workspace_provider)
            == reference.artifact_ref
        )
    )
