"""Direct handles are local; historical PIDs never authorize process control."""

import os
import subprocess
import sys
import time

import pytest

from goal_plus.execution.managed_process import ManagedProcess
from goal_plus.infrastructure.process_output import capture_process
from goal_plus.host_scope import (
    ensure_execution_scope,
    register_execution_process,
    execution_scope_snapshot,
    close_execution_scope,
    assert_execution_scope_open,
)


def test_scope_activation_and_release_never_inspect_or_signal_pids(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(os, "kill", lambda *_: pytest.fail("unexpected PID access"))
    monkeypatch.setattr(
        subprocess, "Popen", lambda *_a, **_k: pytest.fail("unexpected supervisor")
    )
    scope = ensure_execution_scope(
        root_dir=tmp_path,
        agent_harness="pi",
        owner_session_id="native",
        run_id="run_test",
    )
    token = register_execution_process(
        root_dir=tmp_path, scope_id=scope["scope_id"], pid=999999999, label="host-owned"
    )
    assert not {"owner_pid", "owner_start_token", "guardian_pid"} & scope.keys()
    record = execution_scope_snapshot(root_dir=tmp_path, scope_id=scope["scope_id"])[
        "processes"
    ][0]
    assert record["token"] == token and record["status"] == "registered"
    assert not {"witness", "start_token", "process_group_id"} & record.keys()
    closed = close_execution_scope(
        root_dir=tmp_path, scope_id=scope["scope_id"], reason="host_close"
    )
    assert closed["state"] == "closed"
    assert closed["processes"][0]["status"] == "released"
    with pytest.raises(RuntimeError, match="not open"):
        assert_execution_scope_open(
            root_dir=tmp_path, scope_id=scope["scope_id"], run_id="run_test"
        )


def test_close_uses_only_its_direct_child_handle():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        managed = ManagedProcess(child)
        managed.close()
        managed.close()
        assert child.returncode is not None
        assert unrelated.poll() is None
    finally:
        for process in (child, unrelated):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def test_output_capture_does_not_wait_for_an_external_pipe_writer():
    read_fd, write_fd = os.pipe()
    process = subprocess.Popen(
        [sys.executable, "-c", "print('complete')"],
        stdout=write_fd,
        stderr=subprocess.PIPE,
    )
    process.stdout = os.fdopen(read_fd, "rb")
    try:
        started = time.monotonic()
        result = capture_process(process, timeout=2)
        assert result.returncode == 0
        assert result.stdout == "complete\n"
        assert time.monotonic() - started < 2
    finally:
        os.close(write_fd)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
