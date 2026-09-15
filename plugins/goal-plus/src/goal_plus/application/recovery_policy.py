"""Exact settled recovery checkpoint and identifier invariants."""

from pathlib import Path
from goal_plus.domain.search import CandidateRecoveryPoint
from goal_plus.ports.state import CandidateRecord


def validate_recovery_ids(run_id: str, candidate_id: str) -> None:
    if any(
        not value or Path(value).name != value or value in {".", ".."}
        for value in (run_id, candidate_id)
    ):
        raise ValueError("invalid recovery identity")


def candidate_recovery_point(record: CandidateRecord) -> CandidateRecoveryPoint:
    last = record.iterations[-1] if record.iterations else None
    artifact = (
        last.workspace_artifact_after_settlement
        if last is not None
        else record.results_ledger_artifact_ref
    )
    if artifact is None or artifact.provider != record.task.workspace_provider:
        raise RuntimeError("RecoveryCheckpointUnavailable: no settled artifact")
    if last is not None and (
        last.ledger_artifact_ref != artifact
        or record.results_ledger_artifact_ref != artifact
        or record.score_report is None
        or record.score_report.workspace_artifact_after_settlement != artifact
    ):
        raise RuntimeError("RecoveryCheckpointInconsistent: settlement/ledger mismatch")
    return CandidateRecoveryPoint(
        artifact_ref=artifact,
        iteration=last.iteration if last else 0,
        settlement_id=last.settlement_id if last else None,
        result_count=len(record.results_ledger),
    )
