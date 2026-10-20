"""Pure candidate ranking shared by inheritance, selection and report projections."""

from typing import Literal
from goal_plus.domain.evidence import IterationRecord
from goal_plus.domain.evidence_policy import artifact_iteration_eligible
from goal_plus.domain.search import SearchSpec
from goal_plus.ports.state import CandidateRecord


def record_ranking_score(
    record: CandidateRecord,
    spec: SearchSpec,
) -> float | None:
    best_iteration = best_iteration_record(record, spec.metric_direction)
    if best_iteration is not None:
        return best_iteration.score
    if (
        record.score_report
        and _matches_spec_id(record, record.score_report.frozen_spec_id, None)
        and record.score_report.process_passed
        and record.score_report.aggregate_score is not None
    ):
        return record.score_report.aggregate_score
    return None


def scored_records(
    records: list[CandidateRecord],
    spec: SearchSpec,
) -> list[CandidateRecord]:
    return [
        record for record in records if record_ranking_score(record, spec) is not None
    ]


def top_records(
    records: list[CandidateRecord],
    spec: SearchSpec,
    top_n: int,
) -> list[CandidateRecord]:
    scored = scored_records(records, spec)
    if not scored:
        return records_by_created(records)[:top_n]
    reverse = spec.metric_direction == "maximize"
    return sorted(
        scored,
        key=lambda record: record_ranking_score(record, spec),
        reverse=reverse,
    )[:top_n]  # type: ignore[arg-type,return-value]


def records_by_created(records: list[CandidateRecord]) -> list[CandidateRecord]:
    def created_index(record: CandidateRecord) -> int:
        try:
            return int(record.candidate_id.removeprefix("c"))
        except ValueError:
            return 0

    return sorted(records, key=created_index)


def best_iteration_record(
    record: CandidateRecord,
    metric_direction: Literal["maximize", "minimize"],
    *, frozen_spec_id: str | None = None,
) -> IterationRecord | None:
    scored = [
        iteration
        for iteration in record.iterations
        if iteration.process_passed is True
        and _matches_spec(record, iteration, frozen_spec_id)
        and iteration.score is not None
        and not iteration.touched_denied_files
        and not iteration.changed_outside_allowed
        and iteration.disposition not in {"discard", "failure"}
    ]
    if not scored:
        return None
    reverse = metric_direction == "maximize"
    return sorted(
        reversed(scored),
        key=lambda iteration: iteration.score,
        reverse=reverse,
    )[0]


def best_artifact_iteration_record(
    record: CandidateRecord,
    metric_direction: Literal["maximize", "minimize"],
    *, frozen_spec_id: str | None = None,
) -> IterationRecord | None:
    scored = [
        iteration
        for iteration in record.iterations
        if artifact_iteration_eligible(iteration)
        and _matches_spec(record, iteration, frozen_spec_id)
        and iteration.disposition not in {"discard", "failure"}
    ]
    if not scored:
        return None
    reverse = metric_direction == "maximize"
    return sorted(
        reversed(scored),
        key=lambda iteration: iteration.score,
        reverse=reverse,
    )[0]


def _matches_spec(record: CandidateRecord, iteration: IterationRecord, frozen_spec_id: str | None) -> bool:
    return _matches_spec_id(record, iteration.frozen_spec_id, frozen_spec_id)


def _matches_spec_id(record: CandidateRecord, observed: str | None, frozen_spec_id: str | None) -> bool:
    selected = frozen_spec_id or record.active_frozen_spec_id
    if selected is None:
        return True
    legacy = record.spec_revisions[0].parent_frozen_spec_id if record.spec_revisions else selected
    return (observed or legacy) == selected
