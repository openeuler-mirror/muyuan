"""Launch owners clean up local children when setup or callbacks raise."""

from contextlib import contextmanager
from pathlib import Path
import subprocess
import sys

import pytest

from goal_plus.bootstrap import build_process_monitor, build_structured_inference
from goal_plus.execution.drainer import DetachedDrainer
from goal_plus.execution.managed_process import ManagedProcess
from goal_plus.infrastructure.clock import SystemClock
from goal_plus.infrastructure.state.drainer import FileDrainerState
from goal_plus.ports.inference import StructuredInferenceRequest
from goal_plus.ports.process_monitor import ProcessMonitorIdentity




@pytest.fixture
def local_launch():
    processes = []
    native_popen = subprocess.Popen

    def launch(_command, **options):
        process = native_popen(
            [sys.executable, "-c", "import time; time.sleep(60)"], **options,
        )
        processes.append(process)
        return process

    yield launch, processes
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


@pytest.mark.parametrize("failure", ["active", "monitor", "communicate"])
@pytest.mark.parametrize("exception", [OSError, KeyboardInterrupt])
def test_managed_wait_reaps_child_on_callback_or_io_error(local_launch, monkeypatch, failure, exception):
    launch, processes = local_launch
    process = launch([], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    monitor = build_process_monitor(None, ProcessMonitorIdentity())

    def fail(*_args, **_kwargs):
        raise exception("injected execution failure")

    if failure == "monitor":
        monitor.observe = fail
    if failure == "communicate":
        from goal_plus.execution.process_output import ProcessOutput

        monkeypatch.setattr(ProcessOutput, "communicate", fail)
    with pytest.raises(exception, match="injected execution failure"):
        ManagedProcess(process).wait(
            5, monitor=monitor, is_active=fail if failure == "active" else None,
            timeout_detail="timeout", cancellation_detail="closed",
        )
    assert processes[0].wait(timeout=1) is not None
    assert process.stdout.closed and process.stderr.closed


def test_inference_cleans_up_process_before_removing_its_temporary_contract(local_launch):
    launch, processes = local_launch
    directories = []

    def popen(command, **options):
        directories.append(Path(command[command.index("-C") + 1]))
        return launch(command, **options)

    def fail():
        raise OSError("active query unavailable")

    driver = build_structured_inference(popen=popen)
    request = StructuredInferenceRequest(
        agent_harness="codex", instructions="test", prompt="test", output_schema={"type": "object"},
        model=None, reasoning_effort=None, timeout_seconds=5,
        timeout_detail="timeout", cancellation_detail="closed",
    )
    with pytest.raises(OSError, match="active query unavailable"):
        driver.execute(
            request, monitor=build_process_monitor(None, ProcessMonitorIdentity()),
            is_active=fail, usage=lambda *_: {},
        )
    assert processes[0].wait(timeout=1) is not None
    assert not directories[0].exists()


def test_inference_preserves_failed_cleanup_and_refuses_replacement(local_launch):
    launch, processes = local_launch
    directories = []
    native_terminate = []

    def denied():
        raise PermissionError("stop denied")

    def popen(command, **options):
        directories.append(Path(command[command.index("-C") + 1]))
        process = launch(command, **options)
        native_terminate.append(process.terminate)
        process.terminate = denied
        return process

    def fail():
        raise OSError("active query failed")

    driver = build_structured_inference(popen=popen)
    request = StructuredInferenceRequest(
        agent_harness="codex", instructions="test", prompt="test", output_schema={"type": "object"},
        model=None, reasoning_effort=None, timeout_seconds=5,
        timeout_detail="timeout", cancellation_detail="closed",
    )
    options = dict(
        monitor=build_process_monitor(None, ProcessMonitorIdentity()), is_active=fail, usage=lambda *_: {},
    )
    try:
        with pytest.raises(PermissionError, match="stop denied") as caught:
            driver.execute(request, **options)
        assert isinstance(caught.value.__context__, OSError)
        assert str(caught.value.__context__) == "active query failed"
        assert directories[0].exists()
        assert processes[0].poll() is None
        with pytest.raises(RuntimeError, match="cleanup pending"):
            driver.execute(request, **options)
        assert len(processes) == 1
    finally:
        if processes:
            processes[0].terminate = native_terminate[0]
        driver.terminate()
    assert processes[0].wait(timeout=1) is not None
    assert not directories[0].exists()
    assert processes[0].stdout.closed and processes[0].stderr.closed
    driver.terminate()


def test_managed_terminate_bounds_the_post_kill_wait():
    waits = []

    class Process:
        def poll(self):
            return None

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            waits.append(timeout)
            raise subprocess.TimeoutExpired("local-test", timeout)

    with pytest.raises(subprocess.TimeoutExpired):
        ManagedProcess(Process()).terminate()
    assert len(waits) == 2
    assert all(timeout is not None and 0 < timeout <= 1 for timeout in waits)


@pytest.mark.host_process
@pytest.mark.parametrize("failure", ["write_before", "write_after", "log_close"])
@pytest.mark.parametrize("exception", [OSError, KeyboardInterrupt])
def test_drainer_registration_failure_stops_launched_child(tmp_path, local_launch, failure, exception):
    launch, processes = local_launch

    class State(FileDrainerState):
        def write_worker(self, worker):
            if failure == "write_before":
                raise exception("registration failed")
            super().write_worker(worker)
            if failure == "write_after":
                raise exception("registration failed")

        @contextmanager
        def open_log(self):
            with super().open_log() as log:
                yield log
            if failure == "log_close":
                raise exception("registration failed")

    state = State(tmp_path / "drainer", "worker.log")
    driver = DetachedDrainer(
        state=state, root_dir=tmp_path, run_id="run_test", module="local.test",
        source_root=tmp_path, label="Test", clock=SystemClock(), popen=launch,

    )
    with pytest.raises(exception, match="registration failed"):
        driver.start(has_work=lambda: True, environment=lambda: {})
    assert processes[0].wait(timeout=1) is not None
    record = state.read_worker()
    assert (record is not None) == (failure == "write_after")
    if record is not None:
        assert record.pid == processes[0].pid
