"""Read-only recovery checkpoints, workspace observations and launch provenance."""

from typing import Any
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.application.recovery_policy import (
    validate_recovery_ids,
    candidate_recovery_point,
)
from goal_plus.ports.state import CandidateRecord
from goal_plus.ports.candidate_settlement import CandidateSettlementState
from goal_plus.ports.candidate_recovery import CandidateRecoveryWorkspace
from goal_plus.ports.candidate_sessions import CandidateSessionState


class CandidateRecoveryQueries:
    def __init__(
        self,
        state: CandidateSettlementState,
        workspace: CandidateRecoveryWorkspace,
        sessions: CandidateSessionState,
    ) -> None:
        self.state, self.workspace, self.sessions = state, workspace, sessions

    def has_fresh_evidence(self, run_id: str, record: CandidateRecord) -> bool:
        if record.recovery is None:
            return False
        fresh = {
            session.agent_session_id
            for session in self.sessions.read_agent_sessions(run_id)
            if session.candidate_id == record.candidate_id
            and session.execution_generation == record.execution_generation
            and session.session_handle.metadata.get("bound_at")
        }
        return any(
            iteration.iteration > record.recovery.point.iteration
            and iteration.agent_session_id in fresh
            for iteration in record.iterations
        )

    def status(self, run_id: str) -> dict[str, Any]:
        validate_recovery_ids(run_id, "status")
        run = self.state.read_run(run_id)
        candidates = []
        for record in self.state.read_candidates(run_id):
            try:
                point = candidate_recovery_point(record).model_dump(mode="json")
                error = None
            except RuntimeError as exc:
                point, error = None, str(exc)
            unsettled = None
            workspace_error = None
            if point is not None:
                try:
                    unsettled = self.workspace.has_unsettled_changes(
                        record, ArtifactRef.model_validate(point["artifact_ref"])
                    )
                except (RuntimeError, ValueError, OSError) as exc:
                    workspace_error = str(exc)
            candidates.append(
                {
                    "candidate_id": record.candidate_id,
                    "generation": record.execution_generation,
                    "checkpoint": point,
                    "checkpoint_error": error,
                    "verification": record.verification.model_dump(mode="json")
                    if record.verification is not None else None,
                    "unsettled_workspace_changes": unsettled,
                    "workspace_inspection_error": workspace_error,
                    "fresh_recovery_evidence": self.has_fresh_evidence(run_id, record),
                    "recovery": record.recovery.model_dump(mode="json")
                    if record.recovery
                    else None,
                    "sessions": [
                        s.model_dump(
                            mode="json",
                            include={
                                "agent_session_id",
                                "agent_harness",
                                "session_handle",
                                "execution_generation",
                            },
                        )
                        for s in self.sessions.read_agent_sessions(run_id)
                        if s.candidate_id == record.candidate_id
                    ],
                }
            )
        return {"run_id": run_id, "state": run.state, "candidates": candidates}
