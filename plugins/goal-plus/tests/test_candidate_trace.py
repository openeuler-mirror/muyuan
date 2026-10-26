from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading

import pytest

from goal_plus.infrastructure.candidate_trace_export import CandidateTraceExporter
from goal_plus.infrastructure.state.candidate_trace import FileCandidateTraceStore
from goal_plus.tools import SearchTools
from tests.test_global_evidence import _search_with_candidates


def _settle(
    runtime,
    run_id: str,
    candidate_id: str,
    session_id: str,
    workspace: Path,
    value: int,
):
    (workspace / "initial_program.py").write_text(
        f"VALUE = {value}\n", encoding="utf-8"
    )
    return runtime.run_verifier(
        run_id,
        candidate_id,
        agent_session_id=session_id,
        hypothesis=f"Set value to {value}",
    )


def test_global_evidence_snapshots_are_immutable_and_receipted(tmp_path: Path) -> None:
    runtime, run_id, candidates = _search_with_candidates(tmp_path, 2)
    first_id, first_session, _ = candidates[0]
    second_id, second_session, second_workspace = candidates[1]
    assert runtime.get_global_evidence(first_session) == []
    _settle(
        runtime,
        run_id,
        second_id,
        second_session,
        second_workspace,
        4,
    )
    [second_entry] = runtime.get_global_evidence(first_session)

    store = FileCandidateTraceStore(runtime.root_dir)
    events = [
        event
        for event in store.read_events(run_id, first_id)
        if event.kind == "evidence.global_returned"
    ]
    assert len(events) == 2
    assert events[0].payload["snapshot_id"] != events[1].payload["snapshot_id"]
    assert store.read_snapshot(
        run_id, first_id, events[0].payload["snapshot_id"]
    ).content == []
    assert store.read_snapshot(
        run_id, first_id, events[1].payload["snapshot_id"]
    ).content == [second_entry]
    receipts = runtime.candidate_sessions.session(
        first_session
    ).global_evidence_reads
    assert [receipt.operation_id for receipt in receipts] == [
        event.operation_id for event in events
    ]
    assert [receipt.snapshot_id for receipt in receipts] == [
        event.payload["snapshot_id"] for event in events
    ]


def test_auto_and_independent_evidence_trace_exact_return(tmp_path: Path) -> None:
    runtime, run_id, [candidate] = _search_with_candidates(
        tmp_path,
        1,
        strategy_updates={"config": {"global_evidence_mode": "auto"}},
    )
    candidate_id, session_id, workspace = candidate
    (workspace / "initial_program.py").write_text("VALUE = 3\n", encoding="utf-8")
    result = SearchTools(runtime).goal_plus_search_run_verifier(
        run_id,
        candidate_id,
        agent_session_id=session_id,
        hypothesis="Raise the value",
    )
    assert result["global_evidence_injected"] is True
    event = [
        item
        for item in FileCandidateTraceStore(runtime.root_dir).read_events(
            run_id, candidate_id
        )
        if item.kind == "evidence.global_returned"
    ][-1]
    assert event.payload["source"] == "verifier_injected"
    assert event.payload["verifier_attempt_id"] == result["attempt_id"]
    assert event.payload["mode"] == "auto"

    private_runtime, private_run, private_candidates = _search_with_candidates(
        tmp_path / "private",
        2,
        strategy_updates={"config": {"global_evidence_mode": "independent"}},
    )
    for cid, sid, candidate_workspace in private_candidates:
        _settle(
            private_runtime,
            private_run,
            cid,
            sid,
            candidate_workspace,
            2,
        )
    owner_id, owner_session, _ = private_candidates[0]
    returned = private_runtime.get_global_evidence(owner_session)
    assert {entry["candidate_id"] for entry in returned} == {owner_id}
    private_event = [
        item
        for item in FileCandidateTraceStore(private_runtime.root_dir).read_events(
            private_run, owner_id
        )
        if item.kind == "evidence.global_returned"
    ][-1]
    assert FileCandidateTraceStore(private_runtime.root_dir).read_snapshot(
        private_run, owner_id, private_event.payload["snapshot_id"]
    ).content == returned


def test_reference_open_history_keeps_a_b_a(tmp_path: Path) -> None:
    runtime, run_id, candidates = _search_with_candidates(tmp_path, 2)
    source_id, source_session, source_workspace = candidates[0]
    requester_id, requester_session, _ = candidates[1]
    _settle(
        runtime,
        run_id,
        source_id,
        source_session,
        source_workspace,
        1,
    )
    _settle(
        runtime,
        run_id,
        source_id,
        source_session,
        source_workspace,
        2,
    )
    runtime.open_reference_workspace(requester_session, source_id, 1)
    runtime.open_reference_workspace(requester_session, source_id, 2)
    runtime.open_reference_workspace(requester_session, source_id, 1)
    events = [
        event
        for event in FileCandidateTraceStore(runtime.root_dir).read_events(
            run_id, requester_id
        )
        if event.kind == "reference.opened"
    ]
    assert [event.payload["source_iteration"] for event in events] == [1, 2, 1]
    assert len({event.operation_id for event in events}) == 3
    assert len({event.payload["reference_id"] for event in events}) == 1


def test_verifier_trace_keeps_observations_retry_and_single_settlement(
    tmp_path: Path,
) -> None:
    runtime, run_id, [candidate] = _search_with_candidates(tmp_path, 1)
    candidate_id, session_id, workspace = candidate
    (workspace / "initial_program.py").write_text("VALUE = 5\n", encoding="utf-8")
    submitted = threading.Event()
    release = threading.Event()

    def interrupt_after_submission(pending):
        submitted.set()
        assert release.wait(timeout=10)
        raise RuntimeError("controlled interruption")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            runtime.run_verifier,
            run_id,
            candidate_id,
            "process",
            session_id,
            "Set the candidate value",
            None,
            on_submitted=interrupt_after_submission,
        )
        assert submitted.wait(timeout=10)
        pending = runtime.candidate_creation.state.read_candidate(
            run_id, candidate_id
        ).verification
        assert pending is not None
        assert runtime.verifier_result(
            run_id, candidate_id, pending.attempt_id, session_id
        )["state"] == "pending"
        assert runtime.verifier_result(
            run_id, candidate_id, pending.attempt_id, session_id
        )["state"] == "pending"
        release.set()
        with pytest.raises(RuntimeError, match="controlled interruption"):
            future.result(timeout=10)

    assert runtime.verifier_result(
        run_id, candidate_id, pending.attempt_id, session_id
    )["state"] == "needs_recovery"
    report = runtime.run_verifier(
        run_id,
        candidate_id,
        agent_session_id=session_id,
        attempt_id=pending.attempt_id,
    )
    assert report.attempt_id == pending.attempt_id
    assert len(
        runtime.candidate_creation.state.read_candidate(
            run_id, candidate_id
        ).iterations
    ) == 1
    events = FileCandidateTraceStore(runtime.root_dir).read_events(
        run_id, candidate_id
    )
    assert sum(event.kind == "verifier.submitted" for event in events) == 1
    assert sum(event.kind == "verifier.settled" for event in events) == 1
    assert [
        event.payload["status"]
        for event in events
        if event.kind == "verifier.observed"
    ] == ["pending", "pending", "needs_recovery", "retry_requested"]


def test_export_reports_snapshot_corruption_and_sequence_gap_read_only(
    tmp_path: Path,
) -> None:
    runtime, run_id, [candidate] = _search_with_candidates(tmp_path, 1)
    candidate_id, session_id, _ = candidate
    runtime.get_global_evidence(session_id)
    runtime.get_global_evidence(session_id)
    trace_dir = (
        runtime.root_dir / "runs" / run_id / "candidate-traces" / candidate_id
    )
    event_paths = sorted((trace_dir / "events").glob("*.json"))
    first = json.loads(event_paths[0].read_text(encoding="utf-8"))
    snapshot_path = trace_dir / "snapshots" / f"{first['payload']['snapshot_id']}.json"
    snapshot_path.write_text('{"schema_version":1,"snapshot_id":"bad","content":[]}\n', encoding="utf-8")
    event_paths[0].unlink()
    candidate_path = (
        runtime.root_dir
        / "runs"
        / run_id
        / "candidates"
        / candidate_id
        / "candidate.json"
    )
    session_path = (
        runtime.root_dir
        / "runs"
        / run_id
        / "agent_sessions"
        / f"{session_id}.json"
    )
    before = (candidate_path.read_bytes(), session_path.read_bytes())
    summary = CandidateTraceExporter(runtime.root_dir).export(
        run_id, candidate_id=candidate_id
    )
    document = json.loads(
        (
            Path(summary["candidates"][0]["directory"]) / "trace.json"
        ).read_text(encoding="utf-8")
    )
    codes = {item["code"] for item in document["completeness"]["warnings"]}
    assert document["completeness"]["status"] == "partial"
    assert "event_sequence_gap" in codes
    assert "snapshot_unavailable" in codes
    assert "delivery_unknown" in codes
    assert before == (candidate_path.read_bytes(), session_path.read_bytes())
    assert (Path(summary["candidates"][0]["directory"]) / "trace.md").is_file()
