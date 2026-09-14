"""Shared candidate provenance preparation, independent of native worker execution."""

import pytest
from dataclasses import replace

from goal_plus.ports.session import SessionInvocationObservation, SessionInvocationResult
from goal_plus.runtime import FileSearchRuntime
from tests._runtime_helpers import make_project, spec_for


@pytest.mark.parametrize("agent_harness", ["codex", "pi"])
@pytest.mark.parametrize("maximum", [None, 1])
def test_sessions_need_no_default_worker_duration(tmp_path, agent_harness, maximum):
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(spec_for(project, max_parallel=1), [project / "evaluator.py"], agent_harness=agent_harness)
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    [task] = runtime.start_batch(run_id, plan.plan_id)
    session = runtime.candidate_sessions.prepare_invocation(
        run_id, task.candidate_id, agent_harness=agent_harness,
        worker_budget={"max_runtime_seconds": maximum} if maximum is not None else None,
    )
    assert runtime.candidate_sessions.invocation_timeout(session.agent_session_id, 120) == (maximum or 120)
    assert "autoresearch_lease" not in session.launch.get("budget_control", {})
    assert "soft_closeout_seconds" not in session.launch.get("budget_control", {})


@pytest.fixture
def candidate_sessions(tmp_path):
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(spec_for(project, max_parallel=2), [project / "evaluator.py"], agent_harness="codex")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    tasks = runtime.start_batch(run_id, plan.plan_id)
    return runtime, run_id, tasks


def test_candidate_invocation_start_continue_and_refresh_preserve_provenance(candidate_sessions):
    runtime, run_id, tasks = candidate_sessions
    sessions = runtime.candidate_sessions
    first = sessions.prepare_invocation(run_id, tasks[0].candidate_id, agent_harness="codex")
    runtime.bind_agent_handle(first.agent_session_id, {"agent_harness": "codex", "external_id": "native-first"})
    continued = sessions.prepare_invocation(run_id, tasks[0].candidate_id, agent_harness="codex", mode="continue")
    assert continued.agent_session_id == first.agent_session_id
    assert continued.workspace == first.workspace
    assert continued.session_handle.external_id == "native-first"
    refreshed = sessions.prepare_invocation(run_id, tasks[0].candidate_id, agent_harness="codex", mode="refresh")
    assert refreshed.agent_session_id != first.agent_session_id
    assert refreshed.workspace == first.workspace
    assert refreshed.session_handle.external_id is None
    assert len(sessions.state.read_agent_sessions(run_id)) == 2


@pytest.mark.parametrize("invalid", ["agent_harness", "peer", "missing", "mode"])
def test_invalid_invocation_preparation_does_not_mutate_domain(candidate_sessions, invalid):
    runtime, run_id, tasks = candidate_sessions
    sessions = runtime.candidate_sessions
    peer = sessions.prepare_invocation(run_id, tasks[1].candidate_id, agent_harness="codex")
    runtime.bind_agent_handle(peer.agent_session_id, {"agent_harness": "codex", "external_id": "native-peer"})
    before = {str(path): path.read_bytes() for path in runtime.root_dir.rglob("*.json")}
    args = {"agent_harness": "codex", "mode": "continue"}
    if invalid == "agent_harness":
        args.update(agent_harness="pi", mode="start")
    elif invalid == "peer":
        args["agent_session_id"] = peer.agent_session_id
    elif invalid == "mode":
        args.update(mode="start", agent_session_id=peer.agent_session_id)
    with pytest.raises(ValueError):
        sessions.prepare_invocation(run_id, tasks[0].candidate_id, **args)
    assert {str(path): path.read_bytes() for path in runtime.root_dir.rglob("*.json")} == before


def test_invocation_timeout_preserves_prepared_limit_and_current_deadline(candidate_sessions, monkeypatch):
    runtime, run_id, tasks = candidate_sessions
    sessions = runtime.candidate_sessions
    session = sessions.start_agent_session(run_id, tasks[0].candidate_id, worker_budget={"max_runtime_seconds": 60})
    before = session.model_dump(mode="json")
    assert sessions.invocation_timeout(session.agent_session_id, 120) == 60
    assert sessions.invocation_timeout(session.agent_session_id, 0.25) == 0.25
    monkeypatch.setattr(sessions.clock, "now", lambda: 1000)
    monkeypatch.setattr(sessions.context, "outer_deadline", lambda _: "1970-01-01T00:16:50Z")
    assert sessions.invocation_timeout(session.agent_session_id, 120) == 10
    for invalid in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            sessions.invocation_timeout(session.agent_session_id, invalid)
    assert sessions.session(session.agent_session_id).model_dump(mode="json") == before


def test_native_observation_is_exact_idempotent_and_does_not_settle(candidate_sessions):
    runtime, run_id, tasks = candidate_sessions
    sessions = runtime.candidate_sessions
    session = sessions.prepare_invocation(run_id, tasks[0].candidate_id, agent_harness="codex")
    sessions.bind_agent_handle(session.agent_session_id, {"external_id": "native"})
    result = SessionInvocationResult(
        "native", "invocation-1", "interrupted", "private model text",
        {"input": 10}, observation=SessionInvocationObservation(
            "2026-09-11T12:00:00Z", "2026-09-11T12:00:05Z", 5,
        ),
        agent_harness="codex", runtime_provider="direct",
    )
    candidate_before = runtime.candidate_creation.state.read_candidate(run_id, tasks[0].candidate_id)
    recorded = sessions.record_native_invocation(session.agent_session_id, result)
    assert recorded.session_handle.metadata["dispatch_count"] == 1
    assert recorded.session_handle.metadata["terminal_state"] == "interrupted"
    assert "private model text" not in recorded.model_dump_json()
    before = {str(path): path.read_bytes() for path in runtime.root_dir.rglob("*.json")}
    assert sessions.record_native_invocation(session.agent_session_id, result) == recorded
    for bad, error in (
        (replace(result, agent_harness="pi"), PermissionError),
        (replace(result, runtime_provider="thinkthread"), PermissionError),
        (replace(result, native_session_id="peer"), PermissionError),
        (replace(result, usage={"input": 100}), ValueError),
    ):
        with pytest.raises(error):
            sessions.record_native_invocation(session.agent_session_id, bad)
    assert {str(path): path.read_bytes() for path in runtime.root_dir.rglob("*.json")} == before
    assert runtime.candidate_creation.state.read_candidate(run_id, tasks[0].candidate_id) == candidate_before
