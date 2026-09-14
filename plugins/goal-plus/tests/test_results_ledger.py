"""Ledger queries preserve historical decoding without runtime writes or Git."""

import pytest

from goal_plus.domain.evidence import IterationRecord, ResultLedgerEntry
from goal_plus.domain.results_ledger import ledger_at_revision, ledger_through_iteration
from goal_plus.domain.search import CandidateTask
from goal_plus.infrastructure.state.results_ledger import (
    read_candidate_results_ledger,
    read_results_tsv,
)
from goal_plus.ports.state import CandidateRecord


@pytest.fixture
def candidate(tmp_path):
    return CandidateRecord(
        candidate_id="c001",
        status="evaluated",
        task=CandidateTask(
            candidate_id="c001",
            run_id="run_test",
            workspace=tmp_path,
            hypothesis="ledger fixture",
            allowed_files=["main.py"],
            denied_files=["verify.py"],
        ),
        iterations=[
            IterationRecord(
                iteration=index,
                git_head=f"{index}" * 40,
                ledger_git_head=f"ledger-{index}",
                process_passed=index == 1,
                score=float(index),
                hypothesis=f"attempt {index}",
                created_at="2026-09-05T00:00:00Z",
            )
            for index in (1, 2)
        ],
    )


def entry(index):
    return ResultLedgerEntry(
        source_run_id="run_test",
        source_candidate_id="c001",
        iteration=index,
        ledger_git_head=f"ledger-{index}",
        metric_name="metric",
        score=float(index),
        status="pass",
        hypothesis=f"entry {index}",
    )


def test_canonical_ledger_takes_precedence_and_returns_independent_entries(candidate):
    candidate.results_ledger = [entry(1)]
    path = candidate.task.workspace / "results.tsv"
    path.write_text(
        "commit\tmetric\tstatus\thypothesis\nother\t9\tpass\tworkspace row\n"
    )
    before = candidate.model_dump(), path.read_bytes()
    result = read_candidate_results_ledger(candidate)
    assert [item.score for item in result] == [1.0]
    result[0].hypothesis = "local edit"
    assert (candidate.model_dump(), path.read_bytes()) == before


@pytest.mark.parametrize("relative_path", ["results.tsv", ".tmp/results.tsv"])
def test_tsv_reader_binds_abbreviated_commits_and_recovers_unrepresented_iterations(
    candidate, relative_path
):
    path = candidate.task.workspace / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "commit\tmetric\tstatus\thypothesis\n1111111\t3\t pass \t first\t attempt \ninvalid row\n"
    )
    before = candidate.model_dump(), path.read_bytes()
    result = read_candidate_results_ledger(candidate)
    assert [(item.iteration, item.ledger_git_head) for item in result] == [
        (1, "ledger-1"),
        (2, "ledger-2"),
    ]
    assert result[0].git_head == "1111111"
    assert result[0].hypothesis == "first attempt"
    assert result[0].status == "pass" and result[0].score == 3.0
    assert result[1].status == "fail" and result[1].score == 2.0
    assert (candidate.model_dump(), path.read_bytes()) == before
    assert not (candidate.task.workspace / "candidate.json").exists()


@pytest.mark.parametrize(
    "current", ["", "not a header\n", "commit\t\tstatus\thypothesis\n"]
)
def test_existing_invalid_current_tsv_does_not_fall_through_to_legacy(
    candidate, current
):
    legacy = candidate.task.workspace / ".tmp/results.tsv"
    legacy.parent.mkdir()
    legacy.write_text("commit\tmetric\tstatus\thypothesis\nhead\t1\tpass\tlegacy\n")
    (candidate.task.workspace / "results.tsv").write_text(current)
    assert read_results_tsv(candidate) == []


def test_missing_or_unreadable_tsv_returns_no_imported_rows(candidate, monkeypatch):
    assert read_results_tsv(candidate) == []
    path = candidate.task.workspace / "results.tsv"
    path.write_text("commit\tmetric\tstatus\thypothesis\n")

    def unreadable(*args, **kwargs):
        raise OSError("unreadable fixture")

    monkeypatch.setattr(type(path), "read_text", unreadable)
    assert read_results_tsv(candidate) == []


def test_thinkthread_reader_never_reads_local_tsv(candidate):
    candidate.task.workspace_backend = "thinkthread"
    candidate.results_ledger = [entry(1)]
    (candidate.task.workspace / "results.tsv").write_text(
        "commit\tmetric\tstatus\thypothesis\nother\t9\tpass\tlocal\n"
    )
    before = candidate.model_dump()
    assert read_results_tsv(candidate) == [entry(1)]
    assert candidate.model_dump() == before


@pytest.mark.parametrize(
    ("revision", "latest", "expected"),
    [
        ("ledger-1", "ledger-3", [1]),
        ("ledger-2", "ledger-3", [1, 2]),
        ("ledger-3", "ledger-3", [1, 2, 3]),
        ("post-ledger", "post-ledger", [1, 2, 3]),
    ],
)
def test_revision_selection_returns_only_the_exact_ledger_prefix(
    revision, latest, expected
):
    entries = [entry(index) for index in (1, 2, 3)]
    before = [item.model_dump() for item in entries]
    selected = ledger_at_revision(entries, latest, revision)
    assert [item.iteration for item in selected] == expected
    selected[0].hypothesis = "local edit"
    assert [item.model_dump() for item in entries] == before


def test_unknown_ledger_revision_cannot_inherit_all_rows():
    entries = [entry(1)]
    before = entries[0].model_dump()
    with pytest.raises(RuntimeError, match="ledger revision is not present"):
        ledger_at_revision(entries, "current", "unrelated")
    assert entries[0].model_dump() == before
    assert ledger_at_revision([], "initial", "initial") == []


def test_settlement_selection_returns_lineage_through_exact_iteration():
    entries = [
        entry(1).model_copy(
            update={"source_run_id": "run_parent", "source_candidate_id": "c001"}
        ),
        entry(1).model_copy(update={"source_candidate_id": "c001"}),
        entry(2).model_copy(update={"source_candidate_id": "c001"}),
        entry(1).model_copy(update={"source_candidate_id": "c002"}),
    ]

    selected = ledger_through_iteration(entries, "run_test", "c001", 2)

    assert [
        (item.source_run_id, item.source_candidate_id, item.iteration)
        for item in selected
    ] == [
        ("run_parent", "c001", 1),
        ("run_test", "c001", 1),
        ("run_test", "c001", 2),
    ]
    with pytest.raises(RuntimeError, match="settlement is not present"):
        ledger_through_iteration(entries, "run_test", "c003", 1)
