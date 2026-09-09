"""Pure attempt identity, disposition and best-score policies."""

import hashlib
from typing import Literal
from goal_plus.domain.results_ledger import (
    normalize_result_text,
)
from goal_plus.domain.artifacts import (
    ArtifactRef,
)
from goal_plus.domain.evidence import (
    IterationDisposition,
    IterationRecord,
    ScoreReport,
)
from goal_plus.domain.search import (
    SearchSpec,
)
from goal_plus.domain.evidence_policy import (
    artifact_iteration_eligible,
)
from goal_plus.domain.serialization import canonical_json
from goal_plus.ports.state import (
    CandidateRecord,
    RunRecord,
)


def candidate_attempt_id(
    *,
    run_id: str,
    candidate_id: str,
    iteration: int,
    artifact_ref: ArtifactRef | None,
) -> str:
    identity = canonical_json(
        {
            "run_id": run_id,
            "candidate_id": candidate_id,
            "iteration": iteration,
            "artifact_ref": (
                artifact_ref.model_dump(mode="json")
                if artifact_ref is not None
                else None
            ),
        }
    )
    return f"attempt_{hashlib.sha256(identity.encode()).hexdigest()[:24]}"


def iteration_hypothesis(
    hypothesis: str | None,
    record: CandidateRecord,
    iteration_number: int,
    *,
    scope: str,
    agent_session_id: str | None,
) -> str:
    if hypothesis is not None:
        normalized = normalize_result_text(hypothesis)
        if normalized:
            return normalized
    if agent_session_id is None:
        return f"main {scope} verification"
    if iteration_number == 1:
        candidate_hypothesis = normalize_result_text(record.task.hypothesis)
        if candidate_hypothesis:
            return f"candidate baseline: {candidate_hypothesis}"
    return f"iteration {iteration_number}"


def iteration_disposition(
    iteration: IterationRecord,
    prior_best_score: float | None,
    metric_direction: Literal["maximize", "minimize"],
) -> IterationDisposition:
    if not artifact_iteration_eligible(iteration):
        return "failure"
    if prior_best_score is None:
        return "keep"
    assert iteration.score is not None
    improved = (
        iteration.score > prior_best_score
        if metric_direction == "maximize"
        else iteration.score < prior_best_score
    )
    if improved:
        return "keep"
    if iteration.score == prior_best_score:
        return "retain"
    return "discard"


def update_best_seen(
    run: RunRecord,
    spec: SearchSpec,
    report: ScoreReport,
) -> bool:
    if report.disposition not in {"keep", "retain"} or report.aggregate_score is None:
        return False
    is_best = run.best_score is None or (
        report.aggregate_score >= run.best_score
        if spec.metric_direction == "maximize"
        else report.aggregate_score <= run.best_score
    )
    if not is_best:
        return False
    run.best_score = report.aggregate_score
    run.best_candidate_id = report.candidate_id
    return True


def previous_best_score(
    record: CandidateRecord,
    *,
    candidate_best_score: float | None,
) -> float | None:
    if candidate_best_score is not None:
        return candidate_best_score
    source = record.task.expansion_source
    return source.score if source is not None else None
