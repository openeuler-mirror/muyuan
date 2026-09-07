"""Host pump policy, retry, and role separation without starting model agents."""

import os
from types import SimpleNamespace

import pytest

from goal_plus import host_scope, internal_agents
from goal_plus.adaptive_search import scheduler
from goal_plus import evidence_annotator
from goal_plus.application.internal_agents import InternalAgentWork
from goal_plus.domain.search import FrozenSpec, RunState
from goal_plus.infrastructure.state.allocation import FileAllocationState
from goal_plus.infrastructure.state.files import write_json
from goal_plus.ports.state import CandidateRecord, RunRecord
from tests.test_annotation_admission import frozen_spec
from tests.test_annotation_workflow import Clock


@pytest.fixture
def pump(tmp_path, monkeypatch):
    clock = Clock()
    frozen = frozen_spec(clock, adaptive=True)
    store = FileAllocationState(tmp_path / ".gp")
    write_json(store.root_dir / "specs/frozen_test/frozen_spec.json", frozen.model_dump(mode="json"))
    run = RunRecord(
        run_id="run_test", frozen_spec_id=frozen.frozen_spec_id,
        source_path=str(tmp_path), state=RunState.RUNNING, created_at=clock.timestamp(),
    )
    write_json(store.run_dir(run.run_id) / "run.json", run.model_dump(mode="json"))
    calls = []
    monkeypatch.setattr(host_scope, "run_accepts_internal_agent_launch", lambda **kwargs: True)
    monkeypatch.setattr(evidence_annotator, "kick_evidence_annotator", lambda root, run_id, *, host: calls.append(("annotation", run_id, host)) or True)
    monkeypatch.setattr(scheduler, "kick_scheduler", lambda root, run_id, *, host: calls.append(("scheduler", run_id, host)) or True)
    monkeypatch.setattr(host_scope, "close_run_internal_agents", lambda **kwargs: calls.append(("cleanup", kwargs["run_id"])) or [])
    return SimpleNamespace(store=store, work=InternalAgentWork(store), calls=calls, run=run, frozen=frozen)


def json_state(pump):
    return {str(path): path.read_bytes() for path in pump.store.root_dir.rglob("*.json")}


def kick(pump, host=None):
    return internal_agents.kick_internal_agents(pump.store.root_dir, "run_test", host=host or pump.frozen.worker_host)


@pytest.mark.parametrize("state", list(RunState))
def test_internal_work_query_is_read_only_and_not_a_liveness_claim(pump, state):
    pump.run.state = state
    write_json(pump.store.run_dir("run_test") / "run.json", pump.run.model_dump(mode="json"))
    before = json_state(pump)
    work = pump.work.inspect("run_test")
    assert work.accepts_work is (state in {RunState.RUNNING, RunState.WAITING_FOR_WORKERS, RunState.SELECTING, RunState.SELECTION_BLOCKED})
    assert work.host == pump.frozen.worker_host
    assert work.scheduler_enabled is True
    assert json_state(pump) == before
    assert pump.calls == []


def test_host_callback_scans_both_queues_on_each_invocation(pump):
    assert kick(pump) == {"annotation_started": True, "scheduler_started": True}
    assert [item[0] for item in pump.calls] == ["annotation", "scheduler"]
    pump.calls.clear()
    # A fresh invocation discovers durable state; no in-memory wake flag exists.
    assert kick(pump) == {"annotation_started": True, "scheduler_started": True}
    assert [item[0] for item in pump.calls] == ["annotation", "scheduler"]


def test_foreign_host_is_rejected_before_admission_or_cleanup(pump):
    before = json_state(pump)
    with pytest.raises(RuntimeError, match="does not match"):
        kick(pump, "thinkthread")
    assert json_state(pump) == before
    assert pump.calls == []


def test_closed_scope_cannot_readmit_or_launch_work(pump, monkeypatch):
    monkeypatch.setattr(host_scope, "run_accepts_internal_agent_launch", lambda **kwargs: False)
    before = json_state(pump)
    assert kick(pump) == {"annotation_started": False, "scheduler_started": False}
    assert json_state(pump) == before
    assert pump.calls == []


@pytest.mark.parametrize("invalidated", [False, True])
def test_terminal_or_invalidated_run_only_retries_cleanup_without_reopening(pump, monkeypatch, invalidated):
    pump.run.state = RunState.RUNNING if invalidated else RunState.READY_TO_PROMOTE
    if invalidated:
        pump.run.invalidated_at = "2026-09-05T00:00:00Z"
        pump.run.invalidation_reason = "verifier_target_mismatch"
    write_json(pump.store.run_dir("run_test") / "run.json", pump.run.model_dump(mode="json"))
    before = json_state(pump)
    close = host_scope.close_run_internal_agents
    with monkeypatch.context() as patch:
        patch.setattr(host_scope, "close_run_internal_agents", lambda **kwargs: ["annotation:cleanup_incomplete"])
        with pytest.raises(RuntimeError, match="cleanup incomplete"):
            kick(pump)
    assert json_state(pump) == before
    assert host_scope.close_run_internal_agents == close
    assert kick(pump) == {"annotation_started": False, "scheduler_started": False}
    assert pump.calls == [("cleanup", "run_test")]
    assert json_state(pump) == before


def test_annotation_completion_only_wakes_scheduler_under_native_inheritance(pump, monkeypatch):
    monkeypatch.setenv(internal_agents.INTERNAL_AGENT_HOST_ENV, pump.frozen.worker_host)
    internal_agents.wake_scheduler_after_annotation(pump.store.root_dir, "run_test")
    assert [item[0] for item in pump.calls] == ["scheduler"]
    pump.calls.clear()
    monkeypatch.setenv(internal_agents.INTERNAL_AGENT_HOST_ENV, "thinkthread")
    with pytest.raises(RuntimeError, match="matching native host"):
        internal_agents.wake_scheduler_after_annotation(pump.store.root_dir, "run_test")
    assert pump.calls == []


@pytest.mark.parametrize("host", ["codex", "pi-rpc", "thinkthread"])
def test_trusted_pi_pump_selects_only_a_supported_frozen_driver(pump, monkeypatch, host):
    payload = frozen_spec(Clock()).model_dump(mode="json")
    payload["native_host"] = "codex" if host == "codex" else "pi"
    if host == "thinkthread":
        payload["spec"]["strategy"]["worker_launch"] = {}
        payload["spec"]["workspace"]["backend"] = "thinkthread"
    frozen = FrozenSpec.model_validate(payload)
    write_json(pump.store.root_dir / "specs/frozen_test/frozen_spec.json", frozen.model_dump(mode="json"))
    monkeypatch.setattr(internal_agents, "kick_internal_agents", lambda root, run_id, *, host: pump.calls.append(host) or {})
    if host == "codex":
        with pytest.raises(RuntimeError, match="does not own"):
            internal_agents.kick_pi_internal_agents(pump.store.root_dir, "run_test")
        assert pump.calls == []
    else:
        internal_agents.kick_pi_internal_agents(pump.store.root_dir, "run_test")
        assert pump.calls == [host]


def test_host_rescan_settles_scheduler_after_annotation_wake_failure(tmp_path, monkeypatch):
    from goal_plus import runtime as runtime_module
    from tests.adaptive_search.test_scheduler import _start_runtime, _StaticScheduler
    from tests.test_evidence_annotator import RecordingAnnotator

    runtime, run_id, candidates = _start_runtime(tmp_path, max_parallel=1)
    candidate = candidates[0]
    session = runtime.start_agent_session(run_id, candidate.candidate_id)
    candidate.workspace.joinpath("initial_program.py").write_text("VALUE = 1\n")
    runtime.verification.verify(
        run_id, candidate.candidate_id, agent_session_id=session.agent_session_id,
        hypothesis="A focused value change",
    )
    settled = {
        path: path.read_bytes()
        for path in runtime.search_run.state.run_dir(run_id).glob("candidates/*/candidate.json")
    }
    assert settled
    monkeypatch.setenv(internal_agents.INTERNAL_AGENT_HOST_ENV, "codex")

    def unavailable(*args, **kwargs):
        raise RuntimeError("host launch temporarily unavailable")

    def no_runtime(*args, **kwargs):
        pytest.fail("internal-agent host path constructed Search Runtime")

    monkeypatch.setattr(runtime_module, "FileSearchRuntime", no_runtime)
    monkeypatch.setattr(scheduler, "kick_scheduler", unavailable)
    assert evidence_annotator.drain_evidence_annotations(
        runtime.root_dir, run_id, annotator=RecordingAnnotator(),
    ) == 1
    task = runtime.annotation.task(run_id, candidate.candidate_id, 1)
    assert task.state == "completed"
    assert task.attempts == 1
    assert all(path.read_bytes() == value for path, value in settled.items())

    model = _StaticScheduler(["continue"])
    monkeypatch.setattr(
        scheduler, "kick_scheduler",
        lambda root, run_id, *, host: scheduler.drain_schedule_tasks(
            root, run_id, scheduler=model,
        ) > 0,
    )
    assert internal_agents.kick_internal_agents(runtime.root_dir, run_id, host="codex") == {
        "annotation_started": False, "scheduler_started": True,
    }
    completed = scheduler.load_schedule_task(
        runtime.root_dir / "runs" / run_id, candidate.candidate_id, 1,
    )
    assert completed.state == "completed"
    assert len(model.contexts) == 1
    assert internal_agents.kick_internal_agents(runtime.root_dir, run_id, host="codex") == {
        "annotation_started": False, "scheduler_started": False,
    }
    assert len(model.contexts) == 1
    for path, value in settled.items():
        expected = CandidateRecord.model_validate_json(value)
        expected.schedule_settlement_watermark = 1
        expected.iterations[0].schedule_task_id = completed.task_id
        expected.iterations[0].scheduler_status = "completed"
        expected.score_report.schedule_task_id = completed.task_id
        expected.score_report.scheduler_status = "completed"
        assert CandidateRecord.model_validate_json(path.read_bytes()) == expected


@pytest.mark.parametrize("role", ["annotation", "scheduler"])
@pytest.mark.parametrize("rejected_binding", ["generation", "pid"])
def test_role_drainer_rejects_old_owner_then_settles_and_retires_current_worker(
    tmp_path, monkeypatch, role, rejected_binding,
):
    from goal_plus.infrastructure.state.drainer import FileDrainerState
    from goal_plus.ports.drainer import DrainerRecord
    from tests.adaptive_search.test_scheduler import _start_runtime, _StaticScheduler
    from tests.test_evidence_annotator import RecordingAnnotator

    runtime, run_id, [candidate] = _start_runtime(tmp_path, max_parallel=1)
    session = runtime.start_agent_session(run_id, candidate.candidate_id)
    candidate.workspace.joinpath("initial_program.py").write_text("VALUE = 1\n")
    runtime.run_verifier(
        run_id, candidate.candidate_id, agent_session_id=session.agent_session_id,
        hypothesis="A generation-fenced task",
    )
    monkeypatch.setattr(internal_agents, "wake_scheduler_after_annotation", lambda *_: None)
    if role == "scheduler":
        evidence_annotator.drain_evidence_annotations(runtime.root_dir, run_id, annotator=RecordingAnnotator())
        scheduler.SchedulerManager(runtime).ensure_tasks(run_id)
        model = _StaticScheduler(["continue"])

        def drain(generation):
            return scheduler.drain_schedule_tasks(runtime.root_dir, run_id, scheduler=model, generation=generation)

        namespace, log_name = "scheduler", "scheduler.log"
    else:
        model = RecordingAnnotator()

        def drain(generation):
            return evidence_annotator.drain_evidence_annotations(
                runtime.root_dir, run_id, annotator=model, generation=generation,
            )

        namespace, log_name = "evidence-annotator", "annotator.log"
    registration = FileDrainerState(runtime.root_dir / "runs" / run_id / namespace, log_name)
    registration.write_worker(DrainerRecord(
        generation="current", pid=0 if rejected_binding == "pid" else os.getpid(), started_at="before",
    ))
    before = {path: path.read_bytes() for path in runtime.root_dir.rglob("*.json")}
    assert drain("stale" if rejected_binding == "generation" else "current") == 0
    assert {path: path.read_bytes() for path in runtime.root_dir.rglob("*.json")} == before
    registration.write_worker(DrainerRecord(generation="current", pid=os.getpid(), started_at="before"))
    assert drain("current") == 1
    assert registration.read_worker() is None
    assert not (registration.directory / "worker.json").exists()
    if role == "scheduler":
        assert len(model.contexts) == 1
        task = scheduler.load_schedule_task(runtime.root_dir / "runs" / run_id, candidate.candidate_id, 1)
    else:
        assert len(model.commits) == 1
        task = runtime.annotation.task(run_id, candidate.candidate_id, 1)
    assert task.state == "completed"
    assert task.attempts == 1
