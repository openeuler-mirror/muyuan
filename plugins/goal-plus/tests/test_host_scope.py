from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

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
    run_accepts_host_launch,
    run_accepts_internal_agent_launch,
)



def test_scope_fences_new_work_and_releases_registered_process(tmp_path: Path) -> None:
    root = tmp_path / ".gp"
    scope = ensure_execution_scope(
        root_dir=root,
        agent_harness="pi",
        owner_session_id="pi-session",
        run_id="run_test",
    )
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    token = register_execution_process(
        root_dir=root,
        scope_id=scope["scope_id"],
        pid=process.pid,
        label="test-worker",
    )

    closed = close_execution_scope(
        root_dir=root,
        scope_id=scope["scope_id"],
        reason="test",
        timeout_seconds=1,
    )

    assert process.poll() is None
    process.terminate()
    process.wait(timeout=5)
    assert closed["state"] == "closed"
    assert closed["cleanup"]["active_count"] == 0
    assert run_accepts_host_launch(root_dir=root, run_id="run_test") is False
    assert (
        run_accepts_internal_agent_launch(
            root_dir=root,
            run_id="run_test",
        )
        is False
    )
    finish_execution_process(root_dir=root, scope_id=scope["scope_id"], token=token)


@pytest.mark.parametrize(
    "agent_harness,model_field,effort_field,label",
    [
        ("pi", "model_pattern", "thinking_level", "Pi"),
        ("codex", "model", "reasoning_effort", "Codex"),
    ],
)
def test_scope_freezes_non_secret_native_agent_config(
    tmp_path: Path,
    agent_harness,
    model_field,
    effort_field,
    label,
) -> None:
    root = tmp_path / ".gp"
    config = {
        model_field: "native-main/example-model",
        effort_field: "low",
    }
    scope = ensure_execution_scope(
        root_dir=root,
        agent_harness=agent_harness,
        owner_session_id="pi-native-config",
        run_id="run_native",
        native_agent_config=config,
    )

    assert scope["native_agent_config"] == config
    repeated = ensure_execution_scope(
        root_dir=root,
        agent_harness=agent_harness,
        owner_session_id="pi-native-config",
        run_id="run_native",
        native_agent_config=config,
    )
    assert repeated["native_agent_config"] == config

    with pytest.raises(RuntimeError, match="native agent config changed"):
        ensure_execution_scope(
            root_dir=root,
            agent_harness=agent_harness,
            owner_session_id="pi-native-config",
            run_id="run_native",
            native_agent_config={model_field: "changed/model"},
        )
    with pytest.raises(ValueError, match=f"unsupported {label} native agent config"):
        ensure_execution_scope(
            root_dir=root,
            agent_harness=agent_harness,
            owner_session_id="pi-secret-config",
            native_agent_config={
                model_field: "native-main/example-model",
                "api_key": "forbidden",
            },
        )


def test_closed_scope_cannot_be_reopened_implicitly(tmp_path: Path) -> None:
    root = tmp_path / ".gp"
    scope = ensure_execution_scope(
        root_dir=root,
        agent_harness="pi",
        owner_session_id="one-owner",
        run_id="run_one",
    )
    close_execution_scope(
        root_dir=root,
        scope_id=scope["scope_id"],
        reason="aborted",
    )

    with pytest.raises(RuntimeError, match="new work is fenced"):
        ensure_execution_scope(
            root_dir=root,
            agent_harness="pi",
            owner_session_id="one-owner",
            run_id="run_one",
        )

    resumed = ensure_execution_scope(
        root_dir=root,
        agent_harness="pi",
        owner_session_id="one-owner",
        run_id="run_one",
        activation_id="gp_test:3",
    )
    assert resumed["scope_id"] != scope["scope_id"]
    assert resumed["generation"] == scope["generation"] + 1
    # A late operational write on the old scope must not change current ownership.
    finish_execution_process(root_dir=root, scope_id=scope["scope_id"], token="late")
    assert (
        execution_scope_snapshot(root_dir=root, scope_id=scope["scope_id"])["state"]
        == "closed"
    )
    assert (
        execution_scope_for_run(root_dir=root, run_id="run_one")["scope_id"]
        == resumed["scope_id"]
    )
    assert (
        execution_scope_for_owner(
            root_dir=root, agent_harness="pi", owner_session_id="one-owner"
        )["scope_id"]
        == resumed["scope_id"]
    )
    continued = ensure_execution_scope(
        root_dir=root,
        agent_harness="pi",
        owner_session_id="one-owner",
        run_id="run_one",
        activation_id="gp_test:5",
    )
    assert continued["scope_id"] == resumed["scope_id"]
    close_execution_scope(
        root_dir=root, scope_id=continued["scope_id"], reason="aborted"
    )
    with pytest.raises(RuntimeError, match="new work is fenced"):
        ensure_execution_scope(
            root_dir=root,
            agent_harness="pi",
            owner_session_id="one-owner",
            run_id="run_one",
            activation_id="gp_test:5",
        )


@pytest.mark.host_process
def test_selection_fences_only_the_attached_run(tmp_path: Path) -> None:
    root = tmp_path / ".gp"
    scope = ensure_execution_scope(
        root_dir=root,
        agent_harness="pi",
        owner_session_id="selection-owner",
        run_id="run_selection",
    )

    result = close_run_candidates(
        root_dir=root,
        scope_id=scope["scope_id"],
        run_id="run_selection",
    )

    assert result["active_count"] == 0
    assert (
        execution_scope_snapshot(root_dir=root, scope_id=scope["scope_id"])["state"]
        == "open"
    )
    assert run_accepts_host_launch(root_dir=root, run_id="run_selection") is False
    assert (
        run_accepts_internal_agent_launch(
            root_dir=root,
            run_id="run_selection",
        )
        is True
    )
    with internal_agent_run_admission(root_dir=root, run_id="run_selection"):
        pass
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    try:
        register_execution_process(
            root_dir=root,
            scope_id=scope["scope_id"],
            run_id="run_selection",
            pid=process.pid,
            label="annotation",
            kind="internal",
        )
        assert (
            close_run_candidates(
                root_dir=root, scope_id=scope["scope_id"], run_id="run_selection"
            )["active_count"]
            == 0
        )
        assert process.poll() is None
        assert (
            close_run_internal_agents(
                root_dir=root, run_id="run_selection", timeout_seconds=0.1
            )
            == []
        )
        assert process.poll() is None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_fenced_run_rejects_late_process_registration(tmp_path: Path) -> None:
    root = tmp_path / ".gp"
    scope = ensure_execution_scope(
        root_dir=root,
        agent_harness="pi",
        owner_session_id="late-registration-owner",
        run_id="run_late",
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
            label="late-worker",
            run_id="run_late",
        )


def test_thinkthread_cleanup_unknown_fails_scope_and_can_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from goal_plus import thinkthread_annotation

    root = tmp_path / ".gp"
    scope = ensure_execution_scope(
        root_dir=root,
        agent_harness="pi",
        owner_session_id="tt-cleanup-owner",
        run_id="run_tt_cleanup",
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
    with pytest.raises(RuntimeError, match="cleanup is incomplete"):
        ensure_execution_scope(
            root_dir=root,
            agent_harness="pi",
            owner_session_id="tt-cleanup-owner",
            run_id="run_tt_cleanup",
            activation_id="next-authorized-activation",
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


def test_codex_scope_close_requires_native_agents_to_be_terminal(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".gp"
    scope = ensure_execution_scope(
        root_dir=root,
        agent_harness="codex",
        owner_session_id="codex-owner",
        run_id="run_codex",
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
    assert failed["cleanup"]["errors"] == ["codex:active_agents:/root/search_candidate"]
    assert (
        execution_scope_for_codex_agent(
            root_dir=root,
            agent_id="/root/search_candidate",
        )["scope_id"]
        == scope["scope_id"]
    )

    assert not finish_codex_agent(
        root_dir=root,
        scope_id=scope["scope_id"],
        agent_id="/root/search_candidate",
        status="interrupted",
    )
    from goal_plus.host_scope import prepare_codex_agent_tool, record_codex_registry

    prepare_codex_agent_tool(
        root_dir=root,
        scope_id=scope["scope_id"],
        tool="list_agents",
        tool_use_id="recovery-list",
    )
    record_codex_registry(
        root_dir=root,
        scope_id=scope["scope_id"],
        tool_use_id="recovery-list",
        payload={
            "agents": [
                {"agent_name": "/root/search_candidate", "agent_status": "interrupted"}
            ]
        },
    )
    retried = close_execution_scope(
        root_dir=root,
        scope_id=scope["scope_id"],
        reason="normal_stop_retry",
    )
    assert retried["state"] == "closed"
    assert retried["cleanup"]["active_count"] == 0
    newer = ensure_execution_scope(
        root_dir=root,
        agent_harness="codex",
        owner_session_id="another-owner",
        run_id="another-run",
    )
    register_codex_agent(
        root_dir=root,
        scope_id=newer["scope_id"],
        agent_id="/root/search_candidate",
        label="/root/search_candidate",
        run_id="another-run",
    )
    with pytest.raises(RuntimeError, match="ambiguous"):
        execution_scope_for_codex_agent(
            root_dir=root, agent_id="/root/search_candidate"
        )
    assert (
        execution_scope_for_codex_agent(
            root_dir=root,
            agent_id="/root/search_candidate",
            parent_session_id="codex-owner",
        )["scope_id"]
        == scope["scope_id"]
    )
    assert (
        execution_scope_snapshot(root_dir=root, scope_id=newer["scope_id"])[
            "codex_agents"
        ][0]["status"]
        == "active"
    )


def test_codex_run_cleanup_can_retry_after_its_fence(tmp_path: Path) -> None:
    root = tmp_path / ".gp"
    scope = ensure_execution_scope(
        root_dir=root,
        agent_harness="codex",
        owner_session_id="codex-selection-owner",
        run_id="run_retry",
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
