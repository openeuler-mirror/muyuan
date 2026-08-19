import csv
from pathlib import Path

import pytest

from aibench.ablation import run_ablation
from aibench.io_util import load_json, repo_root


def test_ablation_fixture_matrix(tmp_path: Path):
    # Production configs are non-mock; unit test uses fixtures only.
    matrix = repo_root() / "tests/fixtures/configs/runs/ablation-matrix.mock.yaml"
    abl_dir = run_ablation(matrix, output_root=tmp_path, allow_weak_grader=True)
    summary = load_json(abl_dir / "ablation_summary.json")
    assert len(summary["runs"]) == 2
    report = (abl_dir / "ablation_report.md").read_text(encoding="utf-8")
    assert "项目效果综述表" in report
    assert report.count("\n| ") >= 3  # header + separator + rows


def test_one_failing_experiment_does_not_void_the_others(tmp_path: Path, monkeypatch):
    """A matrix is hours of paid work; a bad gateway on the last row must not discard the rest."""
    import aibench.ablation as ablation

    original_run = ablation.run_benchmark

    def flaky_run(**kwargs):
        if kwargs.get("run_id") == "abl-mock-b":
            raise RuntimeError("gateway exploded")
        return original_run(**kwargs)

    monkeypatch.setattr(ablation, "run_benchmark", flaky_run)
    matrix = repo_root() / "tests/fixtures/configs/runs/ablation-matrix.mock.yaml"
    abl_dir = ablation.run_ablation(matrix, output_root=tmp_path, allow_weak_grader=True)

    summary = load_json(abl_dir / "ablation_summary.json")
    assert [r["experiment_name"] for r in summary["runs"]] == ["baseline-mock-a"]
    assert [r["experiment_name"] for r in summary["failed_runs"]] == ["baseline-mock-b"]
    report = (abl_dir / "ablation_report.md").read_text(encoding="utf-8")
    assert "执行失败" in report
    assert "gateway exploded" in report


def test_a_candidate_that_measured_nothing_gets_no_lift(tmp_path: Path, monkeypatch):
    """`-100.0pp` against a baseline the row never met is a comparison, not a missing value.

    The baseline side already answered None here. The candidate side read its None as a 0% rate,
    so a run with 0 effective cases and every case an infra error was published as a capability
    result. `ablation_report.md` warns about such rows; `ablation_overview.csv` and the XLSX
    carry neither the warning nor an `effective_case_count` column.
    """
    import aibench.ablation as ablation
    from aibench.io_util import write_json

    original_run = ablation.run_benchmark

    def nothing_measured_on_b(**kwargs):
        run_dir = original_run(**kwargs)
        if kwargs.get("run_id") == "abl-mock-b":
            summary = load_json(run_dir / "summary.json")
            summary["success_rate"] = None
            summary["primary_metric_value"] = None
            summary["success_count"] = 0
            summary["effective_case_count"] = 0
            summary["infra_error_count"] = summary["case_count"]
            write_json(run_dir / "summary.json", summary)
        return run_dir

    monkeypatch.setattr(ablation, "run_benchmark", nothing_measured_on_b)
    matrix = repo_root() / "tests/fixtures/configs/runs/ablation-matrix.mock.yaml"
    abl_dir = ablation.run_ablation(matrix, output_root=tmp_path, allow_weak_grader=True)

    rows = {r["experiment_name"]: r for r in load_json(abl_dir / "ablation_summary.json")["runs"]}
    unmeasured = rows["baseline-mock-b"]
    assert unmeasured["success_rate"] is None
    assert unmeasured["relative_success_lift"] is None
    assert unmeasured["overview_row"]["相对基线收益"] is None
    assert unmeasured["overview_row"]["主指标值"] == "-"
    # The row that did measure something is unaffected.
    assert rows["baseline-mock-a"]["relative_success_lift"] == 0.0


def test_an_unmeasured_row_reads_as_unmeasured_in_every_artifact(tmp_path: Path, monkeypatch):
    """`0.0%` beside `有效Case 0` is a rate for a run that never produced one.

    The markdown warns about such rows further down, but the overview table, the Runs table and
    the XLSX are what get quoted, and the XLSX carries no warning at all.
    """
    import aibench.ablation as ablation
    from aibench.export_results import export_ablation_csv, export_ablation_xlsx
    from aibench.io_util import write_json

    original_run = ablation.run_benchmark

    def nothing_measured_on_b(**kwargs):
        run_dir = original_run(**kwargs)
        if kwargs.get("run_id") == "abl-mock-b":
            summary = load_json(run_dir / "summary.json")
            summary["success_rate"] = None
            summary["primary_metric_value"] = None
            summary["success_count"] = 0
            summary["effective_case_count"] = 0
            summary["infra_error_count"] = summary["case_count"]
            write_json(run_dir / "summary.json", summary)
        return run_dir

    monkeypatch.setattr(ablation, "run_benchmark", nothing_measured_on_b)
    matrix = repo_root() / "tests/fixtures/configs/runs/ablation-matrix.mock.yaml"
    abl_dir = ablation.run_ablation(matrix, output_root=tmp_path, allow_weak_grader=True)

    def cells(line: str) -> list[str]:
        return [c.strip() for c in line.strip().strip("|").split("|")]

    report = (abl_dir / "ablation_report.md").read_text(encoding="utf-8")
    overview = next(ln for ln in report.splitlines() if ln.startswith("| Baseline-repeat |"))
    runs = next(ln for ln in report.splitlines() if ln.startswith("| baseline-mock-b |"))
    assert cells(overview)[6] == "-"  # 主指标值
    assert cells(runs)[2] == "-"  # success_rate
    measured = next(ln for ln in report.splitlines() if ln.startswith("| baseline-mock-a |"))
    assert cells(measured)[2] == "1.000"

    export_ablation_csv(abl_dir)
    csv_rows = list(csv.DictReader((abl_dir / "ablation_overview.csv").read_text().splitlines()))
    by_name = {r["experiment_name"]: r for r in csv_rows}
    assert by_name["baseline-mock-b"]["success_rate"] == ""
    assert by_name["baseline-mock-b"]["relative_success_lift"] == ""

    pytest.importorskip("openpyxl")
    import openpyxl

    export_ablation_xlsx(abl_dir)
    ws = openpyxl.load_workbook(abl_dir / "ablation_overview.xlsx").active
    cells = {row[0]: row[6] for row in ws.iter_rows(values_only=True)}
    assert cells["Baseline-repeat"] == "-"
    assert cells["Baseline"] == "100.0%"


def test_every_experiment_failing_is_still_an_error(tmp_path: Path, monkeypatch):
    import aibench.ablation as ablation

    monkeypatch.setattr(
        ablation, "run_benchmark", lambda **k: (_ for _ in ()).throw(RuntimeError("down"))
    )
    matrix = repo_root() / "tests/fixtures/configs/runs/ablation-matrix.mock.yaml"
    with pytest.raises(RuntimeError, match="every ablation experiment failed"):
        ablation.run_ablation(matrix, output_root=tmp_path, allow_weak_grader=True)


def test_shared_case_sets_are_filtered_before_any_worker_starts(tmp_path: Path, monkeypatch):
    """Filtering rmtree's and repopulates a shared `.ablation-filtered-<set>` directory. Doing
    it inside a worker means two rows on the same set race, and the loser runs against a
    half-copied case set — a wrong number rather than a crash."""
    import aibench.ablation as ablation

    calls: list[str] = []
    real = ablation._filter_unusable_cases

    def counting(case_set, *, skip_weak, skip_invalid):
        calls.append(case_set)
        return real(case_set, skip_weak=skip_weak, skip_invalid=skip_invalid)

    monkeypatch.setattr(ablation, "_filter_unusable_cases", counting)
    matrix = repo_root() / "tests/fixtures/configs/runs/ablation-matrix.mock.yaml"
    ablation.run_ablation(matrix, output_root=tmp_path, allow_weak_grader=True, parallel=2)

    # Both rows share seed-v0; it must be resolved exactly once, up front.
    assert calls.count("seed-v0") == 1, calls


def test_parallel_rows_produce_the_same_rows_as_serial(tmp_path: Path):
    matrix = repo_root() / "tests/fixtures/configs/runs/ablation-matrix.mock.yaml"
    serial = load_json(
        run_ablation(matrix, output_root=tmp_path / "s", allow_weak_grader=True, parallel=1)
        / "ablation_summary.json"
    )
    par = load_json(
        run_ablation(matrix, output_root=tmp_path / "p", allow_weak_grader=True, parallel=2)
        / "ablation_summary.json"
    )
    assert [r["experiment_name"] for r in serial["runs"]] == [
        r["experiment_name"] for r in par["runs"]
    ], "parallel execution must not reorder the matrix"
    assert [r["success_rate"] for r in serial["runs"]] == [r["success_rate"] for r in par["runs"]]
