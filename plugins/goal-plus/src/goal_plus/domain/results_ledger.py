"""Existing ledger text normalization and exact revision prefix selection."""

import math

from goal_plus.domain.evidence import ResultLedgerEntry

RESULTS_TSV_RELATIVE_PATH = "results.tsv"
LEGACY_RESULTS_TSV_RELATIVE_PATH = ".tmp/results.tsv"


def normalize_result_text(value: str) -> str:
    return " ".join(value.replace("\t", " ").split()).strip()


def ledger_at_revision(
    entries: list[ResultLedgerEntry],
    latest_ledger_git_head: str | None,
    settled_commit: str,
) -> list[ResultLedgerEntry]:
    matching_index = next(
        (
            index
            for index, entry in enumerate(entries)
            if entry.ledger_git_head == settled_commit
        ),
        None,
    )
    if matching_index is None:
        if latest_ledger_git_head != settled_commit:
            raise RuntimeError(
                "expansion source ledger revision is not present in candidate history"
            )
        matching_index = len(entries) - 1
    return [entry.model_copy(deep=True) for entry in entries[: matching_index + 1]]


def render_results_tsv(
    entries: list[ResultLedgerEntry],
    metric_name: str,
) -> str:
    normalized_metric_name = normalize_result_text(metric_name) or "metric"
    lines = [f"commit\t{normalized_metric_name}\tstatus\thypothesis"]
    for entry in entries:
        if entry.metric_name != metric_name:
            continue
        lines.append(
            "\t".join(
                [
                    entry.git_head or "",
                    format_result_score(entry.score),
                    normalize_result_text(entry.status) or "unknown",
                    normalize_result_text(entry.hypothesis),
                ]
            )
        )
    return "\n".join(lines) + "\n"


def format_result_score(score: float | None) -> str:
    if score is None:
        return ""
    if math.isfinite(score) and score.is_integer():
        return str(int(score))
    return str(score)


def valid_inner_git_head(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) in {40, 64}
        and all(character in "0123456789abcdef" for character in value)
    )
