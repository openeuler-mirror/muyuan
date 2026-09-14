"""Scope cleanup keeps a retry owner without launching native agents."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event

import pytest

from goal_plus import host_scope
from goal_plus.execution.scope_processes import ScopeProcessRegistry


def scope_for(root, *, agent_harness="pi", run_id=None):
    return host_scope.ensure_execution_scope(
        root_dir=root,
        agent_harness=agent_harness,
        owner_session_id="cleanup-test",
        run_id=run_id,
    )


@pytest.mark.parametrize("candidate_only", [False, True])
def test_scope_and_candidate_cleanup_do_not_overlap(
    tmp_path, monkeypatch, candidate_only
):
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
            return host_scope.close_run_candidates(
                root_dir=tmp_path, scope_id=scope_id, run_id="run_test"
            )
        return host_scope.close_execution_scope(
            root_dir=tmp_path, scope_id=scope_id, reason="first"
        )

    def second():
        second_started.set()
        return host_scope.close_execution_scope(
            root_dir=tmp_path, scope_id=scope_id, reason="second"
        )

    monkeypatch.setattr(ScopeProcessRegistry, "release", stop)
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
    assert (
        host_scope.execution_scope_snapshot(root_dir=tmp_path, scope_id=scope_id)[
            "state"
        ]
        == "closed"
    )


@pytest.mark.parametrize("candidate_only", [False, True])
def test_waiting_for_cleanup_does_not_delay_launch_fencing(
    tmp_path, monkeypatch, candidate_only
):
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
        if kwargs.get("attachment") == host_scope.ScopeAttachment(
            "search_run", "run_first"
        ):
            first_entered.set()
            assert release.wait(10)
        return 0, []

    def second():
        if candidate_only:
            return host_scope.close_run_candidates(
                root_dir=tmp_path, scope_id=scope_id, run_id="run_later"
            )
        return host_scope.close_execution_scope(
            root_dir=tmp_path, scope_id=scope_id, reason="shutdown"
        )

    monkeypatch.setattr(scope_state, "exclusive_file_lock", observed_lock)
    monkeypatch.setattr(ScopeProcessRegistry, "release", stop)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_result = executor.submit(
            host_scope.close_run_candidates,
            root_dir=tmp_path,
            scope_id=scope_id,
            run_id="run_first",
        )
        try:
            assert first_entered.wait(10)
            second_result = executor.submit(second)
            assert waiting_for_cleanup.wait(10)
            with pytest.raises(RuntimeError):
                host_scope.assert_execution_scope_open(
                    root_dir=tmp_path, scope_id=scope_id, run_id="run_later"
                )
        finally:
            release.set()
        first_result.result(timeout=10)
        second_result.result(timeout=10)
