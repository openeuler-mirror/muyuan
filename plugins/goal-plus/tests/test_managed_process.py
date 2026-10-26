"""Independent local process mechanics; no model, Runtime, or domain store."""

import os
import subprocess
import sys

import pytest

from goal_plus.execution.managed_process import ManagedProcess
from goal_plus.execution.process_monitor import ProcessMonitor
from goal_plus.ports.process_monitor import ProcessMonitorIdentity


class Clock:
    def timestamp(self):
        return "2026-09-06T00:00:00Z"


def wait(owned, *, timeout=5, active=lambda: True, snapshots=None, active_check="before_wait"):
    return owned.wait(
        timeout, is_active=active, timeout_detail="deadline", cancellation_detail="closed",
        active_check=active_check,
        monitor=ProcessMonitor(
            ProcessMonitorIdentity(), clock=Clock(),
            sink=None if snapshots is None else snapshots.append,
        ),
    )


def test_managed_process_reads_large_prompt_with_delayed_stdin_reader(tmp_path):
    prompt = "x" * (128 * 1024)
    path = tmp_path / "prompt.txt"
    path.write_text(prompt)
    script = "import sys,time; time.sleep(1); print(len(sys.stdin.read()),flush=True)"
    with path.open() as stdin:
        process = subprocess.Popen(
            [sys.executable, "-c", script], stdin=stdin,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            result = wait(ManagedProcess(process))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    assert result.state == "process_exited"
    assert result.stdout.strip() == str(len(prompt))
    assert result.stderr == ""
    assert process.returncode == 0


@pytest.mark.parametrize("timed_out", [False, True])
def test_stopping_preserves_partial_output_when_final_drain_is_shorter(timed_out, scripted_process_output):
    snapshots = []

    class Process:
        pid = 123
        returncode = None
        running = True

        def communicate(self, input=None, timeout=None):
            if self.returncode is None:
                self.running = False
                raise subprocess.TimeoutExpired("process", timeout, output=b"complete", stderr=b"error")
            raise subprocess.TimeoutExpired("process", timeout, output=b"short", stderr=None)

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

    process = Process()
    owned = ManagedProcess(process)
    if timed_out:
        # An expired wait must still collect output already available in the pipe.
        process.communicate = lambda **_: ("complete", "error")
    result = wait(owned, timeout=0 if timed_out else 5, active=lambda: process.running, snapshots=snapshots)
    assert result.state == ("timed_out" if timed_out else "terminated")
    assert result.stdout == "complete"
    assert result.stderr == "error"
    assert snapshots[-1].state == result.state
    assert snapshots[-1].detail == ("deadline" if timed_out else "closed")


@pytest.mark.parametrize("stubborn", [False, True])
def test_terminate_reaps_the_owned_process_and_is_idempotent(stubborn):
    class Process:
        returncode = None
        terminated = False
        killed = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            if stubborn and not self.killed:
                raise subprocess.TimeoutExpired("process", timeout)
            self.returncode = -9 if self.killed else -15

    process = Process()
    owned = ManagedProcess(process)
    owned.terminate()
    owned.terminate()
    assert process.terminated
    assert process.killed == stubborn
    assert process.returncode == (-9 if stubborn else -15)


@pytest.mark.parametrize("active_check", ["before_wait", "after_timeout"])
def test_deadline_and_close_race_preserves_caller_failure_classification(monkeypatch, active_check, scripted_process_output):
    import goal_plus.execution.managed_process as process_module

    ticks = [100.0]
    active = [True]
    monkeypatch.setattr(process_module.time, "monotonic", lambda: ticks[0])

    class Process:
        pid = 123
        returncode = None

        def communicate(self, input=None, timeout=None):
            if self.returncode is not None:
                return "final", ""
            ticks[0] += 10
            active[0] = False
            raise subprocess.TimeoutExpired("process", timeout)

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

    result = wait(ManagedProcess(Process()), active=lambda: active[0], active_check=active_check)
    assert result.state == ("timed_out" if active_check == "before_wait" else "terminated")
    assert result.stdout == "final"


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal handling")
@pytest.mark.parametrize("reason", ["timeout", "closed"])
def test_local_process_shutdown_output_is_collected_and_process_is_reaped(reason):
    script = (
        "import signal,sys,time\n"
        "def stop(*_):\n"
        " print('final stdout',flush=True)\n"
        " print('final stderr',file=sys.stderr,flush=True)\n"
        " sys.exit(0)\n"
        "signal.signal(signal.SIGTERM,stop)\n"
        "print('ready',flush=True)\n"
        "time.sleep(30)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdout.readline().strip() == "ready"
        result = wait(ManagedProcess(process), timeout=0 if reason == "timeout" else 5, active=lambda: False)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert result.state == ("timed_out" if reason == "timeout" else "terminated")
    assert result.stdout == "final stdout\n"
    assert result.stderr == "final stderr\n"
    assert process.returncode == 0
