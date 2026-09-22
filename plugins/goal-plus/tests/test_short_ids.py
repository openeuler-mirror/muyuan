"""Sequential run ids and derived agent session ids stay short and unique."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from goal_plus.application.candidate_sessions import CandidateSessions
from goal_plus.infrastructure.state.search_run import FileSearchRunState


def test_new_run_id_is_sequential_and_ignores_legacy_dirs(tmp_path: Path) -> None:
    state = FileSearchRunState(tmp_path)
    assert state.new_run_id() == "run_0001"

    runs = tmp_path / "runs"
    runs.mkdir(exist_ok=True)
    (runs / "run_0001").mkdir()
    (runs / "run_20260918_044059_ab12cd34").mkdir()
    (runs / "run_69893ffba3d184d5a3bf3fa34c96e274").mkdir()
    (runs / "not_a_run").mkdir()
    assert state.new_run_id() == "run_0002"

    (runs / "run_0009").mkdir()
    assert state.new_run_id() == "run_0010"


def test_new_run_id_is_unique_across_concurrent_allocations(tmp_path: Path) -> None:
    state = FileSearchRunState(tmp_path)

    with ThreadPoolExecutor(max_workers=8) as executor:
        ids = list(executor.map(lambda _: state.new_run_id(), range(8)))

    assert sorted(ids) == [f"run_{index:04d}" for index in range(1, 9)]


def test_agent_session_id_is_short_for_new_runs_and_legacy_shape_for_old() -> None:
    make = CandidateSessions._make_agent_session_id
    assert make("run_0007", 2) == "agent_0007_02"
    assert make("run_10000", 1) == "agent_10000_01"
    assert make("run_20260918_044059_ab12cd34", 1) == (
        "agent_20260918_044059_ab12cd34_001"
    )
    assert make("run_69893ffba3d184d5a3bf3fa34c96e274", 2) == (
        "agent_69893ffba3d184d5a3bf3fa34c96e274_002"
    )
