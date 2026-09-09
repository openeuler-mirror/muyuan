"""Exact best-artifact checkpoint projection from committed Evidence."""

from goal_plus.domain.artifacts import (
    ArtifactRef,
)
from goal_plus.domain.evidence import (
    BestArtifactRecord,
    IterationRecord,
)
from goal_plus.domain.search import (
    SearchSpec,
)
from goal_plus.domain.evidence_policy import (
    artifact_iteration_eligible,
)
from goal_plus.ports.state import (
    CandidateRecord,
    RunRecord,
)
from goal_plus.ports.candidate_settlement import CandidateSettlementState


class CandidateCheckpoints:
    def __init__(self, state: CandidateSettlementState) -> None:
        self.state = state

    def write_best(
        self,
        run: RunRecord,
        spec: SearchSpec,
        record: CandidateRecord,
        iteration: IterationRecord,
    ) -> None:
        if not artifact_iteration_eligible(iteration) or not iteration.artifact_hash:
            raise RuntimeError("run best iteration is not artifact-backed")
        workspace = self.state.relative_workspace(run.run_id, record)
        artifact_ref = iteration.artifact_ref
        if artifact_ref is None and iteration.git_head is not None:
            artifact_ref = ArtifactRef(
                kind="git_commit",
                id=iteration.git_head,
                provider=record.task.workspace_backend,
            )
        if artifact_ref is None:
            raise RuntimeError("run best iteration has no artifact identity")
        best = BestArtifactRecord(
            run_id=run.run_id,
            candidate_id=record.candidate_id,
            iteration=iteration.iteration,
            commit=(artifact_ref.id if artifact_ref.kind == "git_commit" else None),
            artifact_ref=artifact_ref,
            score=float(iteration.score),
            metric_name=spec.metric_name,
            metric_direction=spec.metric_direction,
            artifact_hash=str(iteration.artifact_hash),
            workspace=workspace,
            changed_files=iteration.changed_files,
            updated_at=iteration.created_at,
        )
        self.state.write_best(best)
