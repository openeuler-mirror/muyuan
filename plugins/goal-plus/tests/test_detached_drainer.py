"""Detached coordination through its API, without Search Runtime or model calls."""

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from goal_plus.execution.drainer import DetachedDrainer
from goal_plus.infrastructure.clock import SystemClock
from goal_plus.infrastructure.state.drainer import FileDrainerState
from goal_plus.ports.drainer import DrainerRecord


@pytest.fixture
def harness(tmp_path, monkeypatch):
    state = FileDrainerState(tmp_path / "runs" / "run_test" / "role", "worker.log")
    calls = []

    def popen(command, **kwargs):
        pid = 10000 + len(calls)
        calls.append((command, kwargs))
        return SimpleNamespace(pid=pid)

    driver = DetachedDrainer(
        state=state,
        root_dir=tmp_path,
        run_id="run_test",
        module="test.role",
        source_root=Path("/source"),
        label="Test drainer",
        clock=SystemClock(),
        popen=popen,
    )
    return SimpleNamespace(driver=driver, state=state, calls=calls)


def test_concurrent_kicks_start_one_registered_process_and_preserve_native_environment(
    harness,
):
    environment = {"NATIVE_CONFIG": "host-only", "PYTHONPATH": "/existing"}
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda _: harness.driver.start(
                    has_work=lambda: True, environment=lambda: environment
                ),
                range(16),
            )
        )
    assert sum(results) == 1
    assert len(harness.calls) == 1
    worker = harness.state.read_worker()
    command, options = harness.calls[0]
    assert worker.pid == 10000
    assert command[command.index("--generation") + 1] == worker.generation
    assert command[command.index("--run-id") + 1] == "run_test"
    assert command[command.index("--root") + 1] == str(harness.driver.root_dir)
    assert command[command.index("-m") :][:3] == ["-m", "test.role", "drain"]
    assert "start_new_session" not in options
    assert options["stdin"] == subprocess.DEVNULL
    assert options["stderr"] == subprocess.STDOUT
    assert options["env"]["PYTHONPATH"] == "/source" + os.pathsep + "/existing"
    assert options["env"]["NATIVE_CONFIG"] == "host-only"
    assert environment["PYTHONPATH"] == "/existing"
    persisted = json.loads((harness.state.directory / "worker.json").read_text())
    assert persisted == worker.model_dump(mode="json")
    assert set(persisted) == {"generation", "pid", "started_at"}
    assert "host-only" not in json.dumps(persisted)


def test_idle_kick_does_not_resolve_environment_or_launch(harness):
    def environment():
        pytest.fail("idle drainer resolved native configuration")

    assert not harness.driver.start(has_work=lambda: False, environment=environment)
    assert harness.calls == []
    assert harness.state.read_worker() is None


def test_registered_generation_requires_explicit_release_before_replacement(
    harness, monkeypatch
):
    monkeypatch.setattr(os, "kill", lambda *_: pytest.fail("PID probing or signalling"))
    harness.state.write_worker(
        DrainerRecord(generation="lost", pid=42, started_at="before")
    )
    assert not harness.driver.start(has_work=lambda: True, environment=lambda: {})
    result = harness.driver.close()
    assert result.status == "released"
    assert result.generation == "lost"
    assert harness.state.read_worker() is None
    assert harness.driver.start(has_work=lambda: True, environment=lambda: {})
    assert harness.state.read_worker().generation != "lost"
    assert len(harness.calls) == 1


@pytest.mark.parametrize(
    "pid_kind,generation,expected",
    [
        ("foreign", "current", True),
        ("own", "stale", False),
        ("own", "current", True),
    ],
)
def test_claim_uses_generation_and_ignores_pid(harness, pid_kind, generation, expected):
    worker = DrainerRecord(
        generation="current",
        pid=os.getpid() if pid_kind == "own" else 0,
        started_at="before",
    )
    harness.state.write_worker(worker)
    with harness.driver.claim(generation) as owned:
        assert owned is expected
    assert harness.state.read_worker() == worker


def test_foreground_drain_neither_requires_nor_removes_detached_registration(harness):
    worker = DrainerRecord(generation="other", pid=42, started_at="before")
    harness.state.write_worker(worker)
    with harness.driver.claim(None) as owned:
        assert owned
        assert harness.driver.finish_if_idle(
            None, has_work=lambda: pytest.fail("unexpected rescan")
        )
    assert harness.state.read_worker() == worker


def test_final_rescan_retains_current_worker_for_new_work_and_preserves_replacement(
    harness,
):
    current = DrainerRecord(generation="current", pid=os.getpid(), started_at="before")
    harness.state.write_worker(current)
    assert not harness.driver.finish_if_idle("current", has_work=lambda: True)
    assert harness.state.read_worker() == current
    replacement = current.model_copy(update={"generation": "next"})
    harness.state.write_worker(replacement)
    assert harness.driver.finish_if_idle(
        "current", has_work=lambda: pytest.fail("stale worker rescanned")
    )
    assert harness.state.read_worker() == replacement


def test_kick_racing_with_final_exit_starts_the_next_consumer(harness):
    harness.state.write_worker(
        DrainerRecord(generation="current", pid=os.getpid(), started_at="before")
    )
    rescanning = Event()
    release = Event()

    def no_work():
        rescanning.set()
        assert release.wait(10)
        return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        exiting = executor.submit(
            harness.driver.finish_if_idle, "current", has_work=no_work
        )
        try:
            assert rescanning.wait(10)
            kicking = executor.submit(
                harness.driver.start, has_work=lambda: True, environment=lambda: {}
            )
        finally:
            release.set()
        assert exiting.result(timeout=10)
        assert kicking.result(timeout=10)
    assert harness.state.read_worker().generation != "current"
    assert len(harness.calls) == 1
