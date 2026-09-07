"""Scope cleanup keeps a retry owner without launching native agents."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
import subprocess
import sys
from threading import Event

import pytest

from goal_plus import host_scope
from goal_plus.execution.process_identity import ProcessInspectionUnavailable
from goal_plus.execution.scope_guardian import GUARDIAN_MAX_RETRY_SECONDS, ScopeGuardian
from goal_plus.execution.scope_processes import ScopeProcessRegistry
from goal_plus.ports.scope import ScopeGuardianObservation


def scope_for(root, *, host="pi", owner_pid=None, run_id=None):
    return host_scope.ensure_execution_scope(
        root_dir=root, host=host, owner_session_id="guardian-test",
        owner_pid=owner_pid or os.getpid(), run_id=run_id, start_guardian=False,
    )


@pytest.mark.parametrize("initial_state", ["closing", "cleanup_failed"])
@pytest.mark.parametrize("owner_unknown", [False, True])
def test_guardian_retries_requested_cleanup_until_native_stop_is_observed(
    tmp_path, monkeypatch, initial_state, owner_unknown,
):
    scope = scope_for(tmp_path, host="codex")
    scope_id = scope["scope_id"]
    host_scope.register_codex_agent(
        root_dir=tmp_path, scope_id=scope_id, agent_id="native-test", label="test",
    )
    close = host_scope.close_execution_scope
    if initial_state == "closing":
        with monkeypatch.context() as patch:
            def failed_stop(_registry, **_kwargs):
                raise OSError("cleanup storage temporarily unavailable")

            patch.setattr(ScopeProcessRegistry, "stop", failed_stop)
            with pytest.raises(OSError):
                close(root_dir=tmp_path, scope_id=scope_id, reason="manual_stop")
    else:
        assert close(root_dir=tmp_path, scope_id=scope_id, reason="manual_stop")["state"] == "cleanup_failed"

    attempts = []

    def retry(**kwargs):
        result = close(**kwargs)
        attempts.append(result["state"])
        if result["state"] == "cleanup_failed":
            host_scope.finish_codex_agent(
                root_dir=tmp_path, scope_id=scope_id, agent_id="native-test", status="interrupted",
            )
        return result

    if owner_unknown:
        def unknown(*_args):
            raise ProcessInspectionUnavailable("owner procfs unavailable")

        monkeypatch.setattr(host_scope, "process_alive", unknown)
    monkeypatch.setattr(host_scope, "close_execution_scope", retry)
    assert host_scope.run_guardian(root_dir=tmp_path, scope_id=scope_id) == 0
    final = host_scope.execution_scope_snapshot(root_dir=tmp_path, scope_id=scope_id)
    assert final["state"] == "closed"
    assert final["close_reason"] == "manual_stop"
    assert final["codex_agents"][0]["status"] == "interrupted"
    assert attempts == ["cleanup_failed", "closed"]
    with pytest.raises(RuntimeError, match="new work is fenced"):
        scope_for(tmp_path, host="codex")


def test_guardian_retries_cleanup_after_confirmed_owner_death(tmp_path, monkeypatch):
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        scope = scope_for(tmp_path, host="codex", owner_pid=owner.pid)
        scope_id = scope["scope_id"]
        host_scope.register_codex_agent(
            root_dir=tmp_path, scope_id=scope_id, agent_id="native-test", label="test",
        )
        owner.terminate()
        owner.wait(timeout=5)
        attempts = []

        def stopping(_registry, **_kwargs):
            attempts.append(True)
            return (1, ["resource observation unavailable"]) if len(attempts) == 1 else (0, [])

        monkeypatch.setattr(ScopeProcessRegistry, "stop", stopping)
        assert host_scope.run_guardian(root_dir=tmp_path, scope_id=scope_id) == 0
        final = host_scope.execution_scope_snapshot(root_dir=tmp_path, scope_id=scope_id)
        assert final["state"] == "closed"
        assert final["close_reason"] == "owner_process_exited"
        assert final["codex_agents"][0]["status"] == "owner_process_exited"
        assert len(attempts) == 2
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=5)


@pytest.mark.parametrize("error", [OSError("read unavailable"), ValueError("invalid scope JSON")])
def test_guardian_retries_transient_scope_read_errors(tmp_path, monkeypatch, error):
    scope_id = scope_for(tmp_path)["scope_id"]
    host_scope.close_execution_scope(root_dir=tmp_path, scope_id=scope_id, reason="test")
    snapshot = host_scope.execution_scope_snapshot
    attempts = []

    def read(**kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise error
        return snapshot(**kwargs)

    monkeypatch.setattr(host_scope, "execution_scope_snapshot", read)
    assert host_scope.run_guardian(root_dir=tmp_path, scope_id=scope_id) == 0
    assert len(attempts) == 2


@pytest.mark.parametrize("candidate_only", [False, True])
def test_scope_and_candidate_cleanup_do_not_overlap(tmp_path, monkeypatch, candidate_only):
    scope_id = scope_for(tmp_path, run_id="run_test")["scope_id"]
    first_entered = Event()
    release = Event()
    second_started = Event()
    overlapped = Event()
    calls = []

    def stop(_registry, **kwargs):
        calls.append(kwargs.get("attachment"))
        if len(calls) == 1:
            first_entered.set()
            assert release.wait(10)
        elif not release.is_set():
            overlapped.set()
        return 0, []

    def first():
        if candidate_only:
            return host_scope.close_run_candidates(root_dir=tmp_path, scope_id=scope_id, run_id="run_test")
        return host_scope.close_execution_scope(root_dir=tmp_path, scope_id=scope_id, reason="first")

    def second():
        second_started.set()
        return host_scope.close_execution_scope(root_dir=tmp_path, scope_id=scope_id, reason="second")

    monkeypatch.setattr(ScopeProcessRegistry, "stop", stop)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_result = executor.submit(first)
        try:
            assert first_entered.wait(10)
            second_result = executor.submit(second)
            assert second_started.wait(10)
            assert not overlapped.wait(0.5)
        finally:
            release.set()
        first_result.result(timeout=10)
        assert second_result.result(timeout=10)["state"] == "closed"
    assert len(calls) == (2 if candidate_only else 1)
    assert host_scope.execution_scope_snapshot(root_dir=tmp_path, scope_id=scope_id)["state"] == "closed"


@pytest.mark.parametrize("candidate_only", [False, True])
def test_waiting_for_cleanup_does_not_delay_launch_fencing(tmp_path, monkeypatch, candidate_only):
    scope_id = scope_for(tmp_path, run_id="run_first")["scope_id"]
    scope_for(tmp_path, run_id="run_later")
    first_entered = Event()
    waiting_for_cleanup = Event()
    release = Event()
    from goal_plus.infrastructure.state import scope as scope_state

    lock = scope_state.exclusive_file_lock

    @contextmanager
    def observed_lock(path):
        if path.name == "cleanup.lock" and first_entered.is_set():
            waiting_for_cleanup.set()
        with lock(path):
            yield

    def stop(_registry, **kwargs):
        if kwargs.get("attachment") == host_scope.ScopeAttachment("search_run", "run_first"):
            first_entered.set()
            assert release.wait(10)
        return 0, []

    def second():
        if candidate_only:
            return host_scope.close_run_candidates(root_dir=tmp_path, scope_id=scope_id, run_id="run_later")
        return host_scope.close_execution_scope(root_dir=tmp_path, scope_id=scope_id, reason="shutdown")

    monkeypatch.setattr(scope_state, "exclusive_file_lock", observed_lock)
    monkeypatch.setattr(ScopeProcessRegistry, "stop", stop)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_result = executor.submit(
            host_scope.close_run_candidates, root_dir=tmp_path, scope_id=scope_id, run_id="run_first",
        )
        try:
            assert first_entered.wait(10)
            second_result = executor.submit(second)
            assert waiting_for_cleanup.wait(10)
            with pytest.raises(RuntimeError):
                host_scope.assert_execution_scope_open(root_dir=tmp_path, scope_id=scope_id, run_id="run_later")
        finally:
            release.set()
        first_result.result(timeout=10)
        second_result.result(timeout=10)


def test_guardian_uses_durable_closed_observation_and_bounded_retry_delays():
    current = ScopeGuardianObservation(
        state="closing", owner_pid=123, owner_start_token="owner", close_reason="shutdown",
    )
    attempts = []
    delays = []

    def close(reason):
        nonlocal current
        attempts.append(reason)
        if len(attempts) == 12:
            current = current.model_copy(update={"state": "closed"})
        return {"state": "closed"}

    def pause(seconds):
        delays.append(seconds)
        assert len(delays) <= 12

    assert ScopeGuardian(read=lambda: current, close=close, owner_alive=lambda *_: True, pause=pause).run() == 0
    assert attempts == ["shutdown"] * 12
    assert all(0 < delay <= GUARDIAN_MAX_RETRY_SECONDS for delay in delays)
    assert max(delays) == GUARDIAN_MAX_RETRY_SECONDS
    assert len(set(delays)) > 1


def test_cleanup_exceptions_including_missing_child_state_are_not_scope_removal():
    current = ScopeGuardianObservation(state="cleanup_failed", close_reason="shutdown")
    errors = [FileNotFoundError("child metadata missing"), RuntimeError("provider unavailable")]
    attempts = []

    def close(reason):
        nonlocal current
        attempts.append(reason)
        if errors:
            raise errors.pop(0)
        current = current.model_copy(update={"state": "closed"})

    def pause(_seconds):
        assert len(attempts) <= 3

    assert ScopeGuardian(read=lambda: current, close=close, pause=pause).run() == 0
    assert attempts == ["shutdown"] * 3


@pytest.mark.parametrize("missing", [False, True])
def test_closed_or_removed_scope_does_not_touch_any_resource(missing):
    def read():
        if missing:
            raise FileNotFoundError("scope was removed")
        return ScopeGuardianObservation(state="closed")

    def unexpected(*_args):
        pytest.fail("closed/removed scope caused a resource operation")

    assert ScopeGuardian(read=read, close=unexpected, owner_alive=unexpected, pause=unexpected).run() == 0


def test_guardian_observation_is_read_only_and_excludes_other_scope_data(tmp_path):
    scope = scope_for(tmp_path, run_id="run_test")
    path = tmp_path / "host-scopes" / scope["scope_id"] / "scope.json"
    before = path.read_bytes()
    observed = host_scope.execution_scope_observation(root_dir=tmp_path, scope_id=scope["scope_id"])
    assert observed.model_dump() == {
        "state": "open", "owner_pid": os.getpid(),
        "owner_start_token": scope["owner_start_token"], "close_reason": None,
    }
    assert path.read_bytes() == before


def test_manual_cleanup_retry_cannot_erase_confirmed_owner_exit(tmp_path, monkeypatch):
    scope_id = scope_for(tmp_path)["scope_id"]
    attempts = []

    def stop(_registry, **_kwargs):
        attempts.append(True)
        return (1, ["resource unavailable"]) if len(attempts) == 1 else (0, [])

    monkeypatch.setattr(ScopeProcessRegistry, "stop", stop)
    assert host_scope.close_execution_scope(
        root_dir=tmp_path, scope_id=scope_id, reason="owner_process_exited",
    )["state"] == "cleanup_failed"
    result = host_scope.close_execution_scope(root_dir=tmp_path, scope_id=scope_id, reason="manual_retry")
    assert result["state"] == "closed"
    assert result["close_reason"] == "owner_process_exited"


@pytest.mark.parametrize("observation", [None, 0, OSError("owner inspection failed")])
def test_unknown_owner_observation_is_not_confirmation_of_exit(observation):
    current = ScopeGuardianObservation(state="open", owner_pid=123, owner_start_token="owner")
    reasons = []

    def owner_alive(*_args):
        if isinstance(observation, Exception):
            raise observation
        return observation

    def pause(_seconds):
        nonlocal current
        current = current.model_copy(update={"state": "closed"})

    assert ScopeGuardian(read=lambda: current, close=reasons.append, owner_alive=owner_alive, pause=pause).run() == 0
    assert reasons == []


def test_invalid_scope_id_is_rejected_before_starting_a_retry_loop(tmp_path):
    with pytest.raises(ValueError, match="invalid scope_id"):
        host_scope.run_guardian(root_dir=tmp_path, scope_id="../foreign")
    assert not (tmp_path / "host-scopes").exists()
