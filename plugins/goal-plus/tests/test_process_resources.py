"""Owned local process groups, without native agents or historical runtime state."""

from contextlib import contextmanager
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from goal_plus.execution.drainer import DetachedDrainer
from goal_plus.execution import process_identity as inspection
from goal_plus.execution import process_resources as resources
from goal_plus.execution.process_resources import OwnedProcess, stop_processes
from goal_plus.host_scope import close_execution_scope, ensure_execution_scope, register_execution_process
from goal_plus.infrastructure.clock import SystemClock
from goal_plus.infrastructure.state.drainer import FileDrainerState
from goal_plus.ports.drainer import DrainerRecord

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux process group ownership")


@contextmanager
def local_group():
    leader = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], process_group=0)
    member = None
    try:
        member = subprocess.Popen(
            [sys.executable, "-c", "import os,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); signal.signal(signal.SIGUSR1, lambda *_: (os.setsid(), print('escaped', flush=True))); print('ready', flush=True); time.sleep(60)"],
            process_group=leader.pid, stdout=subprocess.PIPE, text=True,
        )
        assert select.select([member.stdout], [], [], 10)[0]
        assert member.stdout.readline().strip() == "ready"
        yield leader, member
    finally:
        for process in (member, leader):
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                if process.stdout:
                    process.stdout.close()


def test_scope_close_stops_members_even_when_the_leader_exits_on_term(tmp_path):
    scope = ensure_execution_scope(
        root_dir=tmp_path, host="pi", owner_session_id="owner", owner_pid=os.getpid(), start_guardian=False,
    )
    with local_group() as (leader, member):
        register_execution_process(
            root_dir=tmp_path, scope_id=scope["scope_id"], pid=leader.pid,
            process_group_id=leader.pid, label="local-worker",
        )
        result = close_execution_scope(
            root_dir=tmp_path, scope_id=scope["scope_id"], reason="test", timeout_seconds=0.1,
        )
        assert result["state"] == "closed"
        assert member.wait(timeout=5) == -signal.SIGKILL
        assert result["cleanup"]["active_count"] == 0


def make_drainer(tmp_path, leader):
    state = FileDrainerState(tmp_path / "drainer", "worker.log")
    state.write_worker(DrainerRecord(pid=leader.pid, generation="current", started_at="before"))
    return DetachedDrainer(
        state=state, root_dir=tmp_path, run_id="run_test", module="local.worker",
        source_root=Path("/unused"), label="Test worker", clock=SystemClock(),
        worker_identity=lambda pid, _run, _generation: inspection.inspect_process(pid) if pid == leader.pid else None,
    )


def test_drainer_close_does_not_drop_members_when_the_leader_exits(tmp_path):
    with local_group() as (leader, member):
        driver = make_drainer(tmp_path, leader)
        assert driver.close(timeout_seconds=0.1).status == "stopped"
        assert member.wait(timeout=5) == -signal.SIGKILL
        assert not (driver.state.directory / "worker.json").exists()


def test_scope_restores_observed_group_after_its_leader_was_reaped(tmp_path):
    scope = ensure_execution_scope(
        root_dir=tmp_path, host="pi", owner_session_id="owner", owner_pid=os.getpid(), start_guardian=False,
    )
    with local_group() as (leader, member):
        register_execution_process(
            root_dir=tmp_path, scope_id=scope["scope_id"], pid=leader.pid,
            process_group_id=leader.pid, label="local-worker",
        )
        leader.terminate()
        leader.wait(timeout=5)
        result = close_execution_scope(
            root_dir=tmp_path, scope_id=scope["scope_id"], reason="test", timeout_seconds=0,
        )
        assert result["state"] == "closed"
        assert result["processes"][0]["status"] == "stopped"
        assert str(member.pid) in result["processes"][0]["witness"]["members"]
        assert member.wait(timeout=5) == -signal.SIGKILL


@pytest.mark.parametrize("operation", ["start", "close"])
def test_drainer_preserves_unknown_group_instead_of_replacing_or_forgetting_it(tmp_path, operation):
    with local_group() as (leader, member):
        driver = make_drainer(tmp_path, leader)
        worker = driver.state.read_worker()
        leader.terminate()
        leader.wait(timeout=5)
        driver.popen = lambda *_, **__: pytest.fail("unknown group was replaced")
        with pytest.raises(RuntimeError):
            if operation == "start":
                driver.start(has_work=lambda: True, environment=lambda: {})
            else:
                driver.close(timeout_seconds=0)
        assert driver.state.read_worker() == worker
        assert member.poll() is None


def test_observed_members_remain_owned_after_leader_reaping_without_touching_another_group():
    outsider = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        with local_group() as (leader, member):
            identity = inspection.inspect_process(leader.pid)
            owned = OwnedProcess(identity.pid, identity.start_token, identity.process_group_id)
            assert owned.running()
            from goal_plus.ports.process_identity import ProcessWitness
            saved = ProcessWitness.model_validate_json(owned.witness.model_dump_json())
            owned = OwnedProcess(identity.pid, identity.start_token, identity.process_group_id, witness=saved)
            member.send_signal(signal.SIGUSR1)
            assert select.select([member.stdout], [], [], 10)[0]
            assert member.stdout.readline().strip() == "escaped"
            leader.terminate()
            leader.wait(timeout=5)
            outcome, = stop_processes([owned], term_timeout=0.05, kill_timeout=1)
            assert outcome.stopped and not outcome.errors
            assert member.wait(timeout=5) == -signal.SIGKILL
            assert outsider.poll() is None
    finally:
        if outsider.poll() is None:
            outsider.kill()
        outsider.wait(timeout=5)


def test_reused_leader_pid_is_not_signalled(monkeypatch):
    replacement = inspection.ProcessSnapshot(1000, "new", 1000, "S")
    monkeypatch.setattr(inspection, "inspect_process", lambda _: replacement)
    monkeypatch.setattr(resources.os, "killpg", lambda *_: pytest.fail("reused process group was signalled"))
    outcome, = stop_processes([OwnedProcess(1000, "old", 1000)], term_timeout=0, kill_timeout=0)
    assert outcome.stopped and not outcome.errors
    from goal_plus.ports.process_identity import ProcessWitness
    previous_boot = ProcessWitness(boot_id="previous-boot", members={1000: "new"})
    outcome, = stop_processes([OwnedProcess(1000, "new", 1000, witness=previous_boot)], term_timeout=0, kill_timeout=0)
    assert outcome.stopped and not outcome.errors


def test_group_signal_permission_failure_is_not_downgraded_to_leader_only_cleanup(monkeypatch):
    leader = inspection.ProcessSnapshot(1000, "current", 1000, "S")
    monkeypatch.setattr(inspection, "inspect_process", lambda _: leader)
    monkeypatch.setattr(inspection, "inspect_process_group", lambda _: (leader,))

    def denied(*_args):
        raise PermissionError("group stop denied")

    monkeypatch.setattr(resources.os, "killpg", denied)
    monkeypatch.setattr(resources.os, "kill", lambda *_: pytest.fail("group failure was downgraded to PID stop"))
    outcome, = stop_processes([OwnedProcess(1000, "current", 1000)], term_timeout=0, kill_timeout=0)
    assert not outcome.stopped
    assert outcome.errors and all("PermissionError" in error for error in outcome.errors)


def test_incomplete_group_observation_never_authorizes_signals(monkeypatch):
    leader = inspection.ProcessSnapshot(1000, "current", 1000, "S")
    monkeypatch.setattr(inspection, "inspect_process", lambda _: leader)

    def unknown(_group):
        raise inspection.ProcessInspectionUnavailable("group observation denied")

    monkeypatch.setattr(inspection, "inspect_process_group", unknown)
    monkeypatch.setattr(resources.os, "killpg", lambda *_: pytest.fail("unobserved group was signalled"))
    outcome, = stop_processes([OwnedProcess(1000, "current", 1000)], term_timeout=0, kill_timeout=0)
    assert not outcome.stopped
    assert outcome.errors == ("group observation denied",)


def test_batch_stop_shares_wait_budgets_across_resources(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    alive = {1000, 1001}
    kill_times = {}

    def inspect(pid):
        return inspection.ProcessSnapshot(pid, "current", pid, "S") if pid in alive else None

    def send(pid, signum):
        if signum == signal.SIGKILL:
            kill_times[pid] = clock.now
            alive.remove(pid)

    def sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr(inspection, "inspect_process", inspect)
    monkeypatch.setattr(resources.os, "kill", send)
    monkeypatch.setattr(resources, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    outcomes = stop_processes(
        [OwnedProcess(pid, "current") for pid in sorted(alive)], term_timeout=0.1, kill_timeout=1,
    )
    assert all(outcome.stopped and not outcome.errors for outcome in outcomes)
    assert kill_times == {1000: pytest.approx(0.1), 1001: pytest.approx(0.1)}
    assert clock.now == pytest.approx(0.1)


@pytest.mark.parametrize("term_timeout,kill_timeout", [(-1, 0), (0, -1), (float("inf"), 0), (0, float("nan"))])
def test_invalid_stop_budget_does_not_signal(monkeypatch, term_timeout, kill_timeout):
    monkeypatch.setattr(resources.os, "kill", lambda *_: pytest.fail("invalid stop was executed"))
    with pytest.raises(ValueError):
        stop_processes([OwnedProcess(1000, "current")], term_timeout=term_timeout, kill_timeout=kill_timeout)


@pytest.mark.parametrize("pid,token,group", [(0, "current", None), (True, "current", None), (1000, 12, 1000), (1000, "current", 1001)])
def test_invalid_resource_identity_is_unknown_even_when_the_pid_cannot_match(monkeypatch, pid, token, group):
    monkeypatch.setattr(inspection, "inspect_process", lambda _: None)
    outcome, = stop_processes([OwnedProcess(pid, token, group)], term_timeout=0, kill_timeout=0)
    assert not outcome.stopped
    assert "ProcessInspectionUnavailable" in outcome.errors[0]


def test_group_inspection_filters_foreign_and_disappearing_processes(monkeypatch):
    root = Path("/proc")
    monkeypatch.setattr(Path, "iterdir", lambda _: iter(root / name for name in ["self", "1000", "1001", "1002"]))

    def group(pid):
        if pid == 1002:
            raise ProcessLookupError("process exited during scan")
        return 1000 if pid == 1000 else 999

    def inspect(pid):
        assert pid == 1000
        return inspection.ProcessSnapshot(pid, "current", 1000, "S")

    monkeypatch.setattr(inspection.os, "getpgid", group)
    monkeypatch.setattr(inspection, "inspect_process", inspect)
    assert inspection.inspect_process_group(1000) == (inspect(1000),)


def test_group_inspection_reports_permission_failure_instead_of_an_empty_group(monkeypatch):
    monkeypatch.setattr(Path, "iterdir", lambda _: iter([Path("/proc/1000")]))

    def denied(_pid):
        raise PermissionError("cannot inspect process group")

    monkeypatch.setattr(inspection.os, "getpgid", denied)
    with pytest.raises(inspection.ProcessInspectionUnavailable, match="group_members"):
        inspection.inspect_process_group(1000)
