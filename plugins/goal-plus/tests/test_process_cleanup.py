"""Local process-group regressions; no native model or historical scope state."""

import os
import select
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from goal_plus import pi_worker
from goal_plus.execution import managed_process
from goal_plus.execution.managed_process import ManagedProcess
from goal_plus.ports.process_identity import ProcessStopOutcome


@pytest.mark.host_process
@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
@pytest.mark.parametrize("leader_exited", [False, True])
def test_pi_cleanup_stops_group_members_after_the_leader_exits(leader_exited):
    leader = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], process_group=0,
    )
    member = None
    try:
        # Direct test children share one group so the test can reap both processes.
        member = subprocess.Popen(
            [
                sys.executable, "-c",
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "print('ready', flush=True); time.sleep(60)",
            ],
            process_group=leader.pid, stdout=subprocess.PIPE, text=True,
        )
        assert select.select([member.stdout], [], [], 10)[0]
        assert member.stdout.readline().strip() == "ready"
        managed = ManagedProcess(leader, term_timeout=0.05, kill_timeout=1)
        managed.own_process_group()
        if leader_exited:
            leader.terminate()
            leader.wait(timeout=5)
        managed.terminate()
        assert member.wait(timeout=5) == -signal.SIGKILL
        assert leader.poll() is not None
    finally:
        for process in (member, leader):
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()


@pytest.mark.host_process
@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_pi_cleanup_accepts_an_empty_group_and_repeated_stop():
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    try:
        managed = ManagedProcess(process)
        managed.own_process_group()
        assert process.wait(timeout=5) == 0
        managed.terminate()
        managed.terminate()
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@pytest.mark.parametrize("errors", [("group ownership denied",), ()])
def test_pi_cleanup_propagates_group_failure_without_leader_only_fallback(monkeypatch, errors):
    monkeypatch.setattr(
        managed_process, "stop_processes",
        lambda *args, **kwargs: (ProcessStopOutcome(stopped=False, errors=errors),),
    )
    managed = ManagedProcess(SimpleNamespace(pid=123))
    managed._process_group = object()
    with pytest.raises(RuntimeError, match="process_group_cleanup_failed"):
        managed.terminate()


def test_pi_cleanup_retains_the_bounded_wait_after_kill(monkeypatch):
    def wait(*, timeout):
        assert timeout == pi_worker.PROCESS_STOP_TIMEOUT_SECONDS == 5
        raise subprocess.TimeoutExpired("owned-process", timeout)

    def stop(resources, *, term_timeout, kill_timeout):
        assert term_timeout == kill_timeout == pi_worker.PROCESS_STOP_TIMEOUT_SECONDS
        return (ProcessStopOutcome(stopped=True),)

    monkeypatch.setattr(
        managed_process, "stop_processes", stop,
    )
    process = SimpleNamespace(pid=123, poll=lambda: None, wait=wait)
    managed = ManagedProcess(
        process, term_timeout=pi_worker.PROCESS_STOP_TIMEOUT_SECONDS,
        kill_timeout=pi_worker.PROCESS_STOP_TIMEOUT_SECONDS,
    )
    managed._process_group = object()

    with pytest.raises(subprocess.TimeoutExpired):
        managed.terminate()
