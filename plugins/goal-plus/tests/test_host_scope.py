from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from goal_plus.host_scope import (
    close_execution_scope,
    close_run_candidates,
    close_run_internal_agents,
    ensure_execution_scope,
    execution_scope_for_codex_agent,
    execution_scope_for_owner,
    execution_scope_for_run,
    execution_scope_snapshot,
    finish_codex_agent,
    finish_execution_process,
    internal_agent_run_admission,
    register_codex_agent,
    register_execution_process,
    restore_execution_scope_guardians,
    run_accepts_host_launch,
    run_accepts_internal_agent_launch,
)


@pytest.mark.host_process
def test_scope_fences_new_work_and_stops_registered_process(tmp_path: Path) -> None:
    root = tmp_path / ".gp"
    scope = ensure_execution_scope(
        root_dir=root,
        host="pi",
        owner_session_id="pi-session",
        owner_pid=os.getpid(),
        run_id="run_test",
        start_guardian=False,
    )
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    token = register_execution_process(
        root_dir=root,
        scope_id=scope["scope_id"],
        pid=process.pid,
        process_group_id=process.pid,
        label="test-worker",
    )

    closed = close_execution_scope(
        root_dir=root,
        scope_id=scope["scope_id"],
        reason="test",
        timeout_seconds=1,
    )

    process.wait(timeout=5)
    assert closed["state"] == "closed"
    assert closed["cleanup"]["active_count"] == 0
    assert run_accepts_host_launch(root_dir=root, run_id="run_test") is False
    assert run_accepts_internal_agent_launch(
        root_dir=root,
        run_id="run_test",
    ) is False
    finish_execution_process(root_dir=root, scope_id=scope["scope_id"], token=token)


def test_pi_scope_freezes_non_secret_native_agent_config(tmp_path: Path, scope_owner) -> None:
    root = tmp_path / ".gp"
    config = {
        "model_pattern": "native-main/example-model",
        "thinking_level": "low",
    }
    scope = ensure_execution_scope(
        root_dir=root,
        host="pi",
        owner_session_id="pi-native-config",
        owner_pid=os.getpid(),
        run_id="run_native",
        native_agent_config=config,
        start_guardian=False,
    )

    assert scope["native_agent_config"] == config
    repeated = ensure_execution_scope(
        root_dir=root,
        host="pi",
        owner_session_id="pi-native-config",
        owner_pid=os.getpid(),
        run_id="run_native",
        native_agent_config=config,
        start_guardian=False,
    )
    assert repeated["native_agent_config"] == config

    with pytest.raises(RuntimeError, match="native agent config changed"):
        ensure_execution_scope(
            root_dir=root,
            host="pi",
            owner_session_id="pi-native-config",
            owner_pid=os.getpid(),
            run_id="run_native",
            native_agent_config={"model_pattern": "zai/glm-5.3-flash"},
            start_guardian=False,
        )
    with pytest.raises(ValueError, match="unsupported Pi native agent config"):
        ensure_execution_scope(
            root_dir=root,
            host="pi",
            owner_session_id="pi-secret-config",
            owner_pid=os.getpid(),
            native_agent_config={
                "model_pattern": "native-main/example-model",
                "api_key": "forbidden",
            },
            start_guardian=False,
        )


@pytest.mark.host_process
@pytest.mark.skipif(os.name != "posix", reason="requires POSIX owner signaling")
def test_guardian_closes_scope_when_owner_process_exits(tmp_path: Path) -> None:
    root = tmp_path / ".gp"
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    scope = ensure_execution_scope(
        root_dir=root,
        host="pi",
        owner_session_id="dead-owner",
        owner_pid=owner.pid,
        start_guardian=True,
    )

    os.kill(scope["guardian_pid"], signal.SIGKILL)
    deadline = time.monotonic() + 5
    from goal_plus.execution.process_identity import process_alive
    while process_alive(scope["guardian_pid"], scope["guardian_start_token"]) and time.monotonic() < deadline:
        time.sleep(0.05)
    restore_execution_scope_guardians(root_dir=root, host="pi", owner_session_id="dead-owner")
    replacement = ensure_execution_scope(
        root_dir=root, host="pi", owner_session_id="dead-owner", owner_pid=owner.pid,
    )
    assert replacement["scope_id"] == scope["scope_id"]
    assert (replacement["guardian_pid"], replacement["guardian_start_token"]) != (
        scope["guardian_pid"], scope["guardian_start_token"],
    )

    owner.send_signal(signal.SIGTERM)
    owner.wait(timeout=5)
    deadline = time.monotonic() + 5
    observed = execution_scope_snapshot(root_dir=root, scope_id=scope["scope_id"])
    while observed["state"] in {"open", "closing"} and time.monotonic() < deadline:
        time.sleep(0.05)
        observed = execution_scope_snapshot(root_dir=root, scope_id=scope["scope_id"])

    assert observed["state"] == "closed"
    assert observed["close_reason"] == "owner_process_exited"


def test_guardian_retries_when_new_session_is_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope_owner,
) -> None:
    launches: list[dict[str, object]] = []

    class Process:
        pid = 4321

    def launch(_command: list[str], **options: object) -> Process:
        launches.append(options)
        if len(launches) == 1:
            raise PermissionError(1, "Operation not permitted")
        return Process()

    monkeypatch.setattr("goal_plus.execution.scope_guardian.subprocess.Popen", launch)
    from goal_plus.execution.process_identity import inspect_process
    from goal_plus.ports.process_identity import ProcessSnapshot
    monkeypatch.setattr("goal_plus.execution.scope_guardian.inspect_process", lambda pid: (
        ProcessSnapshot(4321, "123", 4321, "S") if pid == 4321 else inspect_process(pid)
    ))

    scope = ensure_execution_scope(
        root_dir=tmp_path / ".gp",
        host="pi",
        owner_session_id="protected-root",
        owner_pid=os.getpid(),
        start_guardian=True,
    )

    assert [launch["start_new_session"] for launch in launches] == [True, False]
    assert scope["guardian_pid"] == 4321
    assert execution_scope_snapshot(
        root_dir=tmp_path / ".gp",
        scope_id=scope["scope_id"],
    )["guardian_pid"] == 4321
    assert scope["guardian_start_token"] == "123"


def test_closed_scope_cannot_be_reopened_implicitly(tmp_path: Path, scope_owner) -> None:
    root = tmp_path / ".gp"
    scope = ensure_execution_scope(
        root_dir=root,
        host="pi",
        owner_session_id="one-owner",
        owner_pid=os.getpid(),
        run_id="run_one",
        start_guardian=False,
    )
    close_execution_scope(
        root_dir=root,
        scope_id=scope["scope_id"],
        reason="aborted",
    )

    with pytest.raises(RuntimeError, match="new work is fenced"):
        ensure_execution_scope(
            root_dir=root,
            host="pi",
            owner_session_id="one-owner",
            owner_pid=os.getpid(),
            run_id="run_one",
            start_guardian=False,
        )

    resumed = ensure_execution_scope(
        root_dir=root, host="pi", owner_session_id="one-owner", owner_pid=os.getpid(),
        run_id="run_one", start_guardian=False, activation_id="gp_test:3",
    )
    assert resumed["scope_id"] != scope["scope_id"]
    assert resumed["generation"] == scope["generation"] + 1
    # A late operational write on the old scope must not change current ownership.
    finish_execution_process(root_dir=root, scope_id=scope["scope_id"], token="late")
    assert execution_scope_snapshot(root_dir=root, scope_id=scope["scope_id"])["state"] == "closed"
    assert execution_scope_for_run(root_dir=root, run_id="run_one")["scope_id"] == resumed["scope_id"]
    assert execution_scope_for_owner(root_dir=root, host="pi", owner_session_id="one-owner")["scope_id"] == resumed["scope_id"]
    continued = ensure_execution_scope(
        root_dir=root, host="pi", owner_session_id="one-owner", owner_pid=os.getpid(),
        run_id="run_one", start_guardian=False, activation_id="gp_test:5",
    )
    assert continued["scope_id"] == resumed["scope_id"]
    close_execution_scope(root_dir=root, scope_id=continued["scope_id"], reason="aborted")
    with pytest.raises(RuntimeError, match="new work is fenced"):
        ensure_execution_scope(
            root_dir=root, host="pi", owner_session_id="one-owner", owner_pid=os.getpid(),
            run_id="run_one", start_guardian=False, activation_id="gp_test:5",
        )


@pytest.mark.host_process
def test_selection_fences_only_the_attached_run(tmp_path: Path) -> None:
    root = tmp_path / ".gp"
    scope = ensure_execution_scope(
        root_dir=root,
        host="pi",
        owner_session_id="selection-owner",
        owner_pid=os.getpid(),
        run_id="run_selection",
        start_guardian=False,
    )

    result = close_run_candidates(
        root_dir=root,
        scope_id=scope["scope_id"],
        run_id="run_selection",
    )

    assert result["active_count"] == 0
    assert execution_scope_snapshot(
        root_dir=root, scope_id=scope["scope_id"]
    )["state"] == "open"
    assert run_accepts_host_launch(root_dir=root, run_id="run_selection") is False
    assert run_accepts_internal_agent_launch(
        root_dir=root,
        run_id="run_selection",
    ) is True
    with internal_agent_run_admission(root_dir=root, run_id="run_selection"):
        pass
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        register_execution_process(
            root_dir=root, scope_id=scope["scope_id"], run_id="run_selection",
            pid=process.pid, process_group_id=process.pid, label="annotation", kind="internal",
        )
        assert close_run_candidates(root_dir=root, scope_id=scope["scope_id"], run_id="run_selection")["active_count"] == 0
        assert process.poll() is None
        assert close_run_internal_agents(root_dir=root, run_id="run_selection", timeout_seconds=0.1) == []
        assert process.wait(timeout=5) is not None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_fenced_run_rejects_late_process_registration(tmp_path: Path, scope_owner) -> None:
    root = tmp_path / ".gp"
    scope = ensure_execution_scope(
        root_dir=root,
        host="pi",
        owner_session_id="late-registration-owner",
        owner_pid=os.getpid(),
        run_id="run_late",
        start_guardian=False,
    )
    close_run_candidates(
        root_dir=root,
        scope_id=scope["scope_id"],
        run_id="run_late",
    )

    with pytest.raises(RuntimeError, match="fenced against new host work"):
        register_execution_process(
            root_dir=root,
            scope_id=scope["scope_id"],
            pid=os.getpid(),
            process_group_id=None,
            label="late-worker",
            run_id="run_late",
        )


def test_thinkthread_cleanup_unknown_fails_scope_and_can_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope_owner,
) -> None:
    from goal_plus import thinkthread_annotation

    root = tmp_path / ".gp"
    scope = ensure_execution_scope(
        root_dir=root,
        host="pi",
        owner_session_id="tt-cleanup-owner",
        owner_pid=os.getpid(),
        run_id="run_tt_cleanup",
        start_guardian=False,
    )
    outcomes = iter(("needs_recovery", "closed"))

    def close_annotation(**_kwargs: object) -> dict[str, str]:
        return {"status": next(outcomes)}

    monkeypatch.setattr(
        thinkthread_annotation,
        "close_thinkthread_annotation",
        close_annotation,
    )

    failed = close_execution_scope(
        root_dir=root,
        scope_id=scope["scope_id"],
        reason="first-close",
    )
    retried = close_execution_scope(
        root_dir=root,
        scope_id=scope["scope_id"],
        reason="retry-close",
    )

    assert failed["state"] == "cleanup_failed"
    assert failed["cleanup"]["errors"] == [
        "thinkthread-annotation:cleanup_incomplete:needs_recovery"
    ]
    assert retried["state"] == "closed"
    assert retried["cleanup"] == {
        "active_count": 0,
        "errors": [],
        "observed_at": retried["cleanup"]["observed_at"],
    }


def test_codex_scope_close_requires_native_agents_to_be_terminal(tmp_path: Path, scope_owner) -> None:
    root = tmp_path / ".gp"
    scope = ensure_execution_scope(
        root_dir=root,
        host="codex",
        owner_session_id="codex-owner",
        owner_pid=os.getpid(),
        run_id="run_codex",
        start_guardian=False,
    )
    register_codex_agent(
        root_dir=root,
        scope_id=scope["scope_id"],
        agent_id="/root/search_candidate",
        label="/root/search_candidate",
        run_id="run_codex",
        tool_use_id="spawn-with-lost-return",
        parent_session_id="codex-owner",
    )

    failed = close_execution_scope(
        root_dir=root,
        scope_id=scope["scope_id"],
        reason="normal_stop",
    )

    assert failed["state"] == "cleanup_failed"
    assert failed["cleanup"]["active_count"] == 1
    assert failed["cleanup"]["errors"] == [
        "codex:active_agents:/root/search_candidate"
    ]
    assert execution_scope_for_codex_agent(
        root_dir=root,
        agent_id="/root/search_candidate",
    )["scope_id"] == scope["scope_id"]

    assert not finish_codex_agent(
        root_dir=root,
        scope_id=scope["scope_id"],
        agent_id="/root/search_candidate",
        status="interrupted",
    )
    from goal_plus.host_scope import prepare_codex_agent_tool, record_codex_registry
    prepare_codex_agent_tool(root_dir=root, scope_id=scope["scope_id"], tool="list_agents", tool_use_id="recovery-list")
    record_codex_registry(
        root_dir=root, scope_id=scope["scope_id"], tool_use_id="recovery-list",
        payload={"agents": [{"agent_name": "/root/search_candidate", "agent_status": "interrupted"}]},
    )
    retried = close_execution_scope(
        root_dir=root,
        scope_id=scope["scope_id"],
        reason="normal_stop_retry",
    )
    assert retried["state"] == "closed"
    assert retried["cleanup"]["active_count"] == 0
    newer = ensure_execution_scope(
        root_dir=root, host="codex", owner_session_id="another-owner", owner_pid=os.getpid(),
        run_id="another-run", start_guardian=False,
    )
    register_codex_agent(
        root_dir=root, scope_id=newer["scope_id"], agent_id="/root/search_candidate",
        label="/root/search_candidate", run_id="another-run",
    )
    with pytest.raises(RuntimeError, match="ambiguous"):
        execution_scope_for_codex_agent(root_dir=root, agent_id="/root/search_candidate")
    assert execution_scope_for_codex_agent(
        root_dir=root, agent_id="/root/search_candidate", parent_session_id="codex-owner",
    )["scope_id"] == scope["scope_id"]
    assert execution_scope_snapshot(root_dir=root, scope_id=newer["scope_id"])["codex_agents"][0]["status"] == "active"


def test_codex_owner_exit_marks_native_agents_terminal(tmp_path: Path, monkeypatch, scope_owner) -> None:
    root = tmp_path / ".gp"
    monkeypatch.setattr("goal_plus.host_scope._close_run_internal_drainers", lambda **_: pytest.fail("Goal routed to Search cleanup"))
    scope = ensure_execution_scope(
        root_dir=root,
        host="codex",
        owner_session_id="dead-codex-owner",
        owner_pid=os.getpid(),
        run_id="gp_0001",
        start_guardian=False,
    )
    register_codex_agent(
        root_dir=root,
        scope_id=scope["scope_id"],
        agent_id="/root/owned_child",
        label="owned_child",
        run_id="gp_0001",
    )

    closed = close_execution_scope(
        root_dir=root,
        scope_id=scope["scope_id"],
        reason="owner_process_exited",
    )

    assert closed["state"] == "closed"
    assert closed["cleanup"]["active_count"] == 0
    assert closed["codex_agents"][0]["status"] == "owner_process_exited"


@pytest.mark.host_process
@pytest.mark.skipif(os.name != "posix", reason="requires POSIX owner signaling")
def test_guardian_marks_codex_agents_terminal_after_owner_death(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".gp"
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    scope = ensure_execution_scope(
        root_dir=root,
        host="codex",
        owner_session_id="codex-dead-owner",
        owner_pid=owner.pid,
        run_id="run_codex_guardian",
        start_guardian=True,
    )
    register_codex_agent(
        root_dir=root,
        scope_id=scope["scope_id"],
        agent_id="/root/owned_child",
        label="owned_child",
        run_id="run_codex_guardian",
    )

    owner.send_signal(signal.SIGTERM)
    owner.wait(timeout=5)
    deadline = time.monotonic() + 5
    observed = execution_scope_snapshot(root_dir=root, scope_id=scope["scope_id"])
    while observed["state"] in {"open", "closing"} and time.monotonic() < deadline:
        time.sleep(0.05)
        observed = execution_scope_snapshot(root_dir=root, scope_id=scope["scope_id"])

    assert observed["state"] == "closed"
    assert observed["close_reason"] == "owner_process_exited"
    assert observed["codex_agents"][0]["status"] == "owner_process_exited"


def test_codex_run_cleanup_can_retry_after_its_fence(tmp_path: Path, scope_owner) -> None:
    root = tmp_path / ".gp"
    scope = ensure_execution_scope(
        root_dir=root,
        host="codex",
        owner_session_id="codex-selection-owner",
        owner_pid=os.getpid(),
        run_id="run_retry",
        start_guardian=False,
    )
    register_codex_agent(
        root_dir=root,
        scope_id=scope["scope_id"],
        agent_id="/root/search_worker",
        label="/root/search_worker",
        run_id="run_retry",
    )

    with pytest.raises(RuntimeError, match="did not become quiescent"):
        close_run_candidates(
            root_dir=root,
            scope_id=scope["scope_id"],
            run_id="run_retry",
        )

    finish_codex_agent(
        root_dir=root,
        scope_id=scope["scope_id"],
        agent_id="/root/search_worker",
        status="interrupted",
    )
    retried = close_run_candidates(
        root_dir=root,
        scope_id=scope["scope_id"],
        run_id="run_retry",
    )
    assert retried["active_count"] == 0
