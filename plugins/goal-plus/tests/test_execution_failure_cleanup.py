"""Launch owners clean up local children when setup or callbacks raise."""

from contextlib import contextmanager
import os
import select
from pathlib import Path
import subprocess
import sys
import time

import pytest

from goal_plus import host_scope, pi_worker
from goal_plus.bootstrap import build_process_monitor, build_structured_inference
from goal_plus.execution.drainer import DetachedDrainer
from goal_plus.execution.managed_process import ManagedProcess
from goal_plus.execution.process_identity import inspect_process, process_alive
from goal_plus.execution.process_resources import OwnedProcess, stop_processes
from goal_plus.infrastructure.clock import SystemClock
from goal_plus.infrastructure.state.drainer import FileDrainerState
from goal_plus.ports.inference import StructuredInferenceRequest
from goal_plus.ports.process_monitor import ProcessMonitorIdentity


@pytest.mark.host_process
def test_unregistered_child_exits_without_exec_when_launcher_is_killed(tmp_path):
    marker = tmp_path / "executed"
    script = (
        "import subprocess,sys,time\n"
        "from goal_plus.execution.launch_gate import ProcessLaunchGate\n"
        "with ProcessLaunchGate() as gate:\n"
        "    command=[sys.executable,'-c',\"from pathlib import Path; Path(\"+repr(sys.argv[1])+\").touch()\"]\n"
        "    child=subprocess.Popen(gate.command(command),pass_fds=gate.pass_fds,start_new_session=True)\n"
        "    print(child.pid,flush=True)\n"
        "    time.sleep(60)\n"
    )
    launcher = subprocess.Popen([sys.executable, "-c", script, str(marker)], stdout=subprocess.PIPE, text=True)
    identity = None
    try:
        assert select.select([launcher.stdout], [], [], 5)[0]
        child_pid = int(launcher.stdout.readline())
        identity = inspect_process(child_pid)
        assert identity is not None
        assert not marker.exists()
        launcher.kill()
        launcher.wait(timeout=5)
        deadline = time.monotonic() + 5
        while process_alive(child_pid, identity.start_token) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not process_alive(child_pid, identity.start_token)
        assert not marker.exists()
    finally:
        if launcher.poll() is None:
            launcher.kill()
        launcher.wait(timeout=5)
        launcher.stdout.close()
        if identity is not None:
            outcome, = stop_processes(
                [OwnedProcess(identity.pid, identity.start_token, identity.pid)],
                term_timeout=0.1, kill_timeout=0.1,
            )
            assert outcome.stopped


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
        host="codex", instructions="test", prompt="test", output_schema={"type": "object"},
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
        host="codex", instructions="test", prompt="test", output_schema={"type": "object"},
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
        source_root=tmp_path, label="Test", clock=SystemClock(),
        worker_identity=lambda *_: None, popen=launch,
    )
    with pytest.raises(exception, match="registration failed"):
        driver.start(has_work=lambda: True, environment=lambda: {})
    assert processes[0].wait(timeout=1) is not None
    record = state.read_worker()
    assert (record is not None) == (failure == "write_after")
    if record is not None:
        assert record.pid == processes[0].pid


@pytest.mark.host_process
@pytest.mark.pi
@pytest.mark.parametrize("failure", ["construct", "start", "partial_start", "registration"])
@pytest.mark.parametrize("exception", [OSError, KeyboardInterrupt])
def test_pi_rpc_initialization_failure_stops_child_and_keeps_scope_recoverable(
    tmp_path, monkeypatch, local_launch, failure, exception,
):
    launch, processes = local_launch
    root = tmp_path / ".gp"
    scope = host_scope.ensure_execution_scope(
        root_dir=root, host="pi", owner_session_id="test", owner_pid=os.getpid(), start_guardian=False,
    )
    extension = tmp_path / "extension.ts"
    extension.write_text("export default function() {}")
    native_client = pi_worker._RpcClient

    class Client(native_client):
        def __init__(self, **options):
            super().__init__(**options)
            if failure == "construct":
                raise exception("RPC setup failed")

        def start(self):
            if failure == "partial_start":
                super().start()
            raise exception("RPC setup failed")

    if failure == "registration":
        register = host_scope.register_execution_process

        def fail_registration(**kwargs):
            register(**kwargs)
            raise exception("RPC setup failed")

        monkeypatch.setattr(host_scope, "register_execution_process", fail_registration)
    monkeypatch.setattr(pi_worker.subprocess, "Popen", launch)
    monkeypatch.setattr(pi_worker, "_RpcClient", Client)
    with pytest.raises(exception, match="RPC setup failed"):
        pi_worker.run_pi_rpc_worker(
            {
                "role": "ordinary", "agent_session_id": "test", "cwd": str(tmp_path),
                "root": str(root), "budget_control": {"max_runtime_seconds": 5},
                "execution_scope_id": scope["scope_id"], "prompt": "test",
            },
            extension_path=extension,
        )
    assert processes[0].wait(timeout=1) is not None
    result = host_scope.close_execution_scope(
        root_dir=root, scope_id=scope["scope_id"], reason="test cleanup", timeout_seconds=0.1,
    )
    assert result["state"] == "closed"
    assert result["cleanup"]["active_count"] == 0


@pytest.mark.host_process
@pytest.mark.pi
def test_pi_rpc_metrics_interrupt_cannot_bypass_process_cleanup(tmp_path, monkeypatch, local_launch):
    launch, processes = local_launch
    extension = tmp_path / "extension.ts"
    extension.write_text("export default function() {}")

    class Client(pi_worker._RpcClient):
        def command(self, *_args, **_kwargs):
            raise pi_worker.PiRpcError("handshake failed")

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt("metrics interrupted")

    monkeypatch.setattr(pi_worker.subprocess, "Popen", launch)
    monkeypatch.setattr(pi_worker, "_RpcClient", Client)
    monkeypatch.setattr(pi_worker, "_collect_pi_metrics", interrupted)
    with pytest.raises(KeyboardInterrupt, match="metrics interrupted"):
        pi_worker.run_pi_rpc_worker(
            {
                "role": "ordinary", "agent_session_id": "test", "cwd": str(tmp_path),
                "root": str(tmp_path / ".gp"), "budget_control": {"max_runtime_seconds": 5},
                "prompt": "test",
            },
            extension_path=extension,
        )
    assert processes[0].wait(timeout=1) is not None
