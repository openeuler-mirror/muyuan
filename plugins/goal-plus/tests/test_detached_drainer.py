"""Detached coordination through its API, without Search Runtime or model calls."""

import json
import os
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

import goal_plus.execution.drainer as execution
from goal_plus.execution.drainer import DetachedDrainer
from goal_plus.infrastructure.clock import SystemClock
from goal_plus.infrastructure.state.drainer import FileDrainerState
from goal_plus.ports.drainer import DrainerRecord
from goal_plus.ports.process_identity import ProcessSnapshot
from goal_plus.execution import process_identity as inspection


@pytest.fixture
def harness(tmp_path, monkeypatch):
    state = FileDrainerState(tmp_path / "runs" / "run_test" / "role", "worker.log")
    calls = []
    live = {}
    read_process, read_group = inspection.inspect_process, inspection.inspect_process_group

    def controlled(pid):
        return pid == 42 or 10000 <= pid < 10000 + len(calls)

    def inspect_process(pid):
        if controlled(pid):
            return ProcessSnapshot(pid, live[pid], pid, "S") if pid in live else None
        return read_process(pid)

    def inspect_group(group):
        if controlled(group):
            item = inspect_process(group)
            return (item,) if item else ()
        return read_group(group)

    monkeypatch.setattr(inspection, "inspect_process", inspect_process)
    monkeypatch.setattr(inspection, "inspect_process_group", inspect_group)

    def popen(command, **kwargs):
        pid = 10000 + len(calls)
        calls.append((command, kwargs))
        live[pid] = command[command.index("--generation") + 1]
        return SimpleNamespace(pid=pid)

    driver = DetachedDrainer(
        state=state, root_dir=tmp_path, run_id="run_test", module="test.role",
        source_root=Path("/source"), label="Test drainer", clock=SystemClock(),
        worker_identity=lambda pid, run_id, generation: inspect_process(pid) if run_id == "run_test" and live.get(pid) == generation else None,
        popen=popen,
    )
    return SimpleNamespace(driver=driver, state=state, calls=calls, live=live)


def test_concurrent_kicks_start_one_registered_process_and_preserve_native_environment(harness):
    environment = {"NATIVE_CONFIG": "host-only", "PYTHONPATH": "/existing"}
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(
            lambda _: harness.driver.start(has_work=lambda: True, environment=lambda: environment),
            range(16),
        ))
    assert sum(results) == 1
    assert len(harness.calls) == 1
    worker = harness.state.read_worker()
    command, options = harness.calls[0]
    assert worker.pid == 10000
    assert command[command.index("--generation") + 1] == worker.generation
    assert command[command.index("--run-id") + 1] == "run_test"
    assert command[command.index("--root") + 1] == str(harness.driver.root_dir)
    assert command[command.index("-m"):][:3] == ["-m", "test.role", "drain"]
    assert options["start_new_session"] is True
    assert options["stdin"] == subprocess.DEVNULL
    assert options["stderr"] == subprocess.STDOUT
    assert options["env"]["PYTHONPATH"] == "/source" + os.pathsep + "/existing"
    assert options["env"]["NATIVE_CONFIG"] == "host-only"
    assert environment["PYTHONPATH"] == "/existing"
    persisted = json.loads((harness.state.directory / "worker.json").read_text())
    assert persisted == worker.model_dump(mode="json")
    assert set(persisted) == {"generation", "pid", "started_at", "witness"}
    assert "host-only" not in json.dumps(persisted)


def test_idle_kick_does_not_resolve_environment_or_launch(harness):
    def environment():
        pytest.fail("idle drainer resolved native configuration")

    assert not harness.driver.start(has_work=lambda: False, environment=environment)
    assert harness.calls == []
    assert harness.state.read_worker() is None


def test_missing_process_is_replaced_without_reusing_its_generation(harness):
    harness.state.write_worker(DrainerRecord(generation="lost", pid=42, started_at="before"))
    assert harness.driver.start(has_work=lambda: True, environment=lambda: {})
    assert harness.state.read_worker().generation != "lost"
    assert len(harness.calls) == 1


@pytest.mark.parametrize("pid_kind,generation,expected", [
    ("foreign", "current", False), ("own", "stale", False), ("own", "current", True),
])
def test_claim_requires_current_generation_and_process(harness, pid_kind, generation, expected):
    worker = DrainerRecord(generation="current", pid=os.getpid() if pid_kind == "own" else 0, started_at="before")
    harness.state.write_worker(worker)
    with harness.driver.claim(generation) as owned:
        assert owned is expected
    assert harness.state.read_worker() == worker


def test_foreground_drain_neither_requires_nor_removes_detached_registration(harness):
    worker = DrainerRecord(generation="other", pid=42, started_at="before")
    harness.state.write_worker(worker)
    with harness.driver.claim(None) as owned:
        assert owned
        assert harness.driver.finish_if_idle(None, has_work=lambda: pytest.fail("unexpected rescan"))
    assert harness.state.read_worker() == worker


def test_final_rescan_retains_current_worker_for_new_work_and_preserves_replacement(harness):
    current = DrainerRecord(generation="current", pid=os.getpid(), started_at="before")
    harness.state.write_worker(current)
    assert not harness.driver.finish_if_idle("current", has_work=lambda: True)
    assert harness.state.read_worker() == current
    replacement = current.model_copy(update={"generation": "next"})
    harness.state.write_worker(replacement)
    assert harness.driver.finish_if_idle("current", has_work=lambda: pytest.fail("stale worker rescanned"))
    assert harness.state.read_worker() == replacement


def test_kick_racing_with_final_exit_starts_the_next_consumer(harness):
    harness.state.write_worker(DrainerRecord(generation="current", pid=os.getpid(), started_at="before"))
    rescanning = Event()
    release = Event()

    def no_work():
        rescanning.set()
        assert release.wait(10)
        return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        exiting = executor.submit(harness.driver.finish_if_idle, "current", has_work=no_work)
        try:
            assert rescanning.wait(10)
            kicking = executor.submit(harness.driver.start, has_work=lambda: True, environment=lambda: {})
        finally:
            release.set()
        assert exiting.result(timeout=10)
        assert kicking.result(timeout=10)
    assert harness.state.read_worker().generation != "current"
    assert len(harness.calls) == 1


@pytest.mark.parametrize("stop_on", [signal.SIGTERM, signal.SIGKILL])
def test_close_escalates_only_as_needed_and_removes_its_registration(harness, monkeypatch, stop_on):
    assert harness.driver.start(has_work=lambda: True, environment=lambda: {})
    worker = harness.state.read_worker()
    sent = []

    def killpg(pid, signum):
        sent.append((pid, signum))
        if signum == stop_on:
            harness.live.pop(pid)

    monkeypatch.setattr(execution.os, "killpg", killpg)
    result = harness.driver.close(timeout_seconds=0)
    assert result.status == "stopped"
    assert result.generation == worker.generation
    assert result.finished_at
    assert sent == [(worker.pid, signal.SIGTERM)] + (
        [(worker.pid, signal.SIGKILL)] if stop_on == signal.SIGKILL else []
    )
    assert harness.state.read_worker() is None
    assert harness.driver.close() is None
    assert not (harness.state.directory / "worker.json").exists()


def test_close_preserves_new_generation_registered_during_wait(harness, monkeypatch):
    assert harness.driver.start(has_work=lambda: True, environment=lambda: {})
    current = harness.state.read_worker()
    replacement = current.model_copy(update={"generation": "replacement", "pid": current.pid + 1})

    def killpg(pid, _signum):
        assert pid == current.pid
        harness.live.pop(pid)
        harness.state.write_worker(replacement)

    monkeypatch.setattr(execution.os, "killpg", killpg)
    assert harness.driver.close().generation == current.generation
    assert harness.state.read_worker() == replacement


def test_failed_cleanup_keeps_handle_for_retry(harness, monkeypatch):
    assert harness.driver.start(has_work=lambda: True, environment=lambda: {})
    worker = harness.state.read_worker()
    monkeypatch.setattr(execution.os, "killpg", lambda *_: None)
    with pytest.raises(RuntimeError, match="did not stop"):
        harness.driver.close(timeout_seconds=0)
    assert harness.state.read_worker() == worker
    harness.live.clear()
    assert harness.driver.close().status == "stale"
    assert harness.state.read_worker() is None


def test_stale_or_invalid_registration_does_not_signal_an_unowned_process(harness, monkeypatch):
    monkeypatch.setattr(execution.os, "killpg", lambda *_: pytest.fail("unowned process signalled"))
    worker = DrainerRecord(generation="old", pid=42, started_at="before")
    harness.state.write_worker(worker)
    with pytest.raises(ValueError):
        harness.driver.close(timeout_seconds=-1)
    assert harness.state.read_worker() == worker
    result = harness.driver.close()
    assert result.model_dump(exclude_none=True) == {**worker.model_dump(exclude={"witness"}), "status": "stale"}
    path = harness.state.directory / "worker.json"
    path.write_text("not-json")
    with pytest.raises(json.JSONDecodeError):
        harness.driver.close()
    with pytest.raises(json.JSONDecodeError):
        harness.driver.start(has_work=lambda: True, environment=lambda: {})
    assert path.read_text() == "not-json"


def test_local_process_is_registered_stopped_and_reaped(harness, monkeypatch):
    from goal_plus.host_scope import (
        close_execution_scope, close_run_candidates, ensure_execution_scope,
        execution_scope_snapshot, internal_agent_launch_registration,
    )

    processes = []

    def popen(_command, **kwargs):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        processes.append(process)
        return process

    harness.driver.popen = popen
    harness.driver.worker_identity = lambda pid, _run, _generation: inspection.inspect_process(pid) if processes[0].pid == pid else None
    try:
        assert harness.driver.start(has_work=lambda: True, environment=lambda: os.environ)
        assert harness.state.read_worker().pid == processes[0].pid
        assert harness.driver.close(timeout_seconds=2).status == "stopped"
        assert processes[0].wait(timeout=5) is not None

        root = harness.driver.root_dir
        scope = ensure_execution_scope(
            root_dir=root, host="pi", owner_session_id="drainer", owner_pid=os.getpid(),
            run_id="run_test", start_guardian=False,
        )
        with internal_agent_launch_registration(root_dir=root, run_id="run_test", label="Test drainer") as register:
            assert harness.driver.start(has_work=lambda: True, environment=lambda: os.environ, register_process=register)
        worker = harness.state.read_worker()
        with pytest.raises(RuntimeError, match="admission has ended"):
            register(worker.pid)
        with monkeypatch.context() as patch:
            patch.setattr(execution.os, "getpid", lambda: worker.pid)
            assert harness.driver.finish_if_idle(worker.generation, has_work=lambda: False)
        assert harness.state.read_worker() is None
        assert harness.driver.close() is None
        recorded = execution_scope_snapshot(root_dir=root, scope_id=scope["scope_id"])["processes"]
        assert len(recorded) == 1 and recorded[0]["pid"] == processes[1].pid
        assert recorded[0]["kind"] == "internal" and recorded[0]["witness"]
        close_run_candidates(root_dir=root, scope_id=scope["scope_id"], run_id="run_test", timeout_seconds=0)
        assert processes[1].poll() is None
        closed = close_execution_scope(root_dir=root, scope_id=scope["scope_id"], reason="test", timeout_seconds=0.1)
        assert closed["state"] == "closed" and closed["cleanup"]["active_count"] == 0
        assert processes[1].wait(timeout=5) is not None
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def test_unknown_process_identity_preserves_registration_and_blocks_launch_or_stop(harness, monkeypatch):
    from goal_plus.execution.process_identity import ProcessInspectionUnavailable

    assert harness.driver.start(has_work=lambda: True, environment=lambda: {})
    worker = harness.state.read_worker()

    def unknown(*_args):
        raise ProcessInspectionUnavailable("process observation denied")

    harness.driver.worker_identity = unknown
    monkeypatch.setattr(execution.os, "killpg", lambda *_: pytest.fail("unknown process was signalled"))
    with pytest.raises(ProcessInspectionUnavailable):
        harness.driver.start(has_work=lambda: True, environment=lambda: {})
    with pytest.raises(ProcessInspectionUnavailable):
        harness.driver.close(timeout_seconds=0)
    assert len(harness.calls) == 1
    assert harness.state.read_worker() == worker
