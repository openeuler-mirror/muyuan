"""Read the existing canonical/legacy TSV ledger without persisting recovery."""

from goal_plus.domain.evidence import ResultLedgerEntry
from goal_plus.domain.results_ledger import (
    LEGACY_RESULTS_TSV_RELATIVE_PATH,
    RESULTS_TSV_RELATIVE_PATH,
    normalize_result_text,
)
from goal_plus.ports.state import CandidateRecord


def read_results_tsv(record: CandidateRecord) -> list[ResultLedgerEntry]:
    if record.task.workspace_backend == "thinkthread":
        return list(record.results_ledger)
    paths = (
        record.task.workspace / RESULTS_TSV_RELATIVE_PATH,
        record.task.workspace / LEGACY_RESULTS_TSV_RELATIVE_PATH,
    )
    path = next((candidate for candidate in paths if candidate.is_file()), None)
    if path is None:
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    if not lines:
        return []
    header = lines[0].split("\t")
    if len(header) < 4 or header[0].strip() != "commit":
        return []
    metric_name = header[1].strip()
    if not metric_name:
        return []

    remaining_iterations = list(record.iterations)

    entries: list[ResultLedgerEntry] = []
    for line in lines[1:]:
        columns = line.split("\t", 3)
        if len(columns) != 4:
            continue
        git_head, score_text, status, hypothesis = columns
        try:
            score = float(score_text) if score_text.strip() else None
        except ValueError:
            score = None
        normalized_git_head = git_head.strip()
        matching_index = next(
            (
                index
                for index, candidate in enumerate(remaining_iterations)
                if candidate.git_head
                and normalized_git_head
                and (
                    candidate.git_head == normalized_git_head
                    or candidate.git_head.startswith(normalized_git_head)
                    or normalized_git_head.startswith(candidate.git_head)
                )
            ),
            None,
        )
        iteration = (
            remaining_iterations.pop(matching_index)
            if matching_index is not None
            else None
        )
        normalized_status = normalize_result_text(status) or "unknown"
        entries.append(
            ResultLedgerEntry(
                source_run_id=record.task.run_id,
                source_candidate_id=record.candidate_id,
                iteration=iteration.iteration if iteration else None,
                git_head=git_head.strip() or None,
                ledger_git_head=iteration.ledger_git_head if iteration else None,
                artifact_ref=(iteration.artifact_ref if iteration else None),
                ledger_artifact_ref=(
                    iteration.ledger_artifact_ref if iteration else None
                ),
                metric_name=metric_name,
                score=score,
                status=normalized_status,
                hypothesis=normalize_result_text(hypothesis),
                failure_class=iteration.failure_class if iteration else None,
                created_at=iteration.created_at if iteration else None,
            )
        )
    for iteration in remaining_iterations:
        hypothesis = normalize_result_text(iteration.hypothesis or iteration.summary)
        entries.append(
            ResultLedgerEntry(
                source_run_id=record.task.run_id,
                source_candidate_id=record.candidate_id,
                iteration=iteration.iteration,
                git_head=iteration.git_head,
                ledger_git_head=iteration.ledger_git_head,
                artifact_ref=iteration.artifact_ref,
                ledger_artifact_ref=iteration.ledger_artifact_ref,
                metric_name=metric_name,
                score=iteration.score,
                status="pass" if iteration.process_passed else "fail",
                hypothesis=hypothesis or f"recovered iteration {iteration.iteration}",
                failure_class=iteration.failure_class,
                created_at=iteration.created_at,
            )
        )
    return entries


def read_candidate_results_ledger(record: CandidateRecord) -> list[ResultLedgerEntry]:
    if record.results_ledger:
        return [entry.model_copy(deep=True) for entry in record.results_ledger]
    return read_results_tsv(record)


def resolve_legacy_candidate_ledger(record: CandidateRecord) -> CandidateRecord:
    if (
        not record.results_ledger
        and record.results_ledger_artifact_ref is None
        and record.results_ledger_git_head is None
    ):
        record.results_ledger = read_results_tsv(record)
    return record
