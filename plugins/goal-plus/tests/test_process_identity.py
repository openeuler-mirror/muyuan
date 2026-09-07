"""Host process identity checks without launching native agents."""

import importlib
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from goal_plus.execution import process_identity as inspection


@pytest.fixture
def process_files(monkeypatch):
    pid = 876543
    fields = ["0"] * 50
    fields[0], fields[2], fields[19] = "S", str(pid), "12345"
    files = {
        "stat": f"{pid} (process name)) " + " ".join(fields),
        "cmdline": b"",
    }
    read_text, read_bytes, exists = Path.read_text, Path.read_bytes, Path.exists
    kill = os.kill

    def read(path, original, *args, **kwargs):
        if path.parent == Path(f"/proc/{pid}"):
            value = files[path.name]
            if isinstance(value, Exception):
                raise value
            return value
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", lambda path, *a, **kw: read(path, read_text, *a, **kw))
    monkeypatch.setattr(Path, "read_bytes", lambda path: read(path, read_bytes))
    monkeypatch.setattr(Path, "exists", lambda path: True if path.parent == Path(f"/proc/{pid}") else exists(path))
    monkeypatch.setattr(os, "kill", lambda target, sig: None if (target, sig) == (pid, 0) else kill(target, sig))
    return pid, files


@pytest.mark.parametrize("module", [
    "goal_plus.evidence_annotator", "goal_plus.adaptive_search.scheduler",
])
@pytest.mark.parametrize("run_id,generation", [
    ("run_10", "generation"), ("run_1", "generation-next"),
])
def test_worker_identity_rejects_substring_matches(process_files, module, run_id, generation):
    pid, files = process_files
    command = ["python", "-m", module, "drain", "--root", "/root", "--run-id", run_id, "--generation", generation]
    files["cmdline"] = b"\0".join(os.fsencode(arg) for arg in command) + b"\0"
    role = importlib.import_module(module)
    assert not role._drainer(Path("/root"), "run_1").worker_identity(pid, "run_1", "generation")


@pytest.mark.parametrize("module", [
    "goal_plus.evidence_annotator", "goal_plus.adaptive_search.scheduler",
])
def test_worker_identity_does_not_treat_unreadable_command_as_owned(process_files, module):
    pid, files = process_files
    files["cmdline"] = PermissionError("process observation denied")
    role = importlib.import_module(module)
    with pytest.raises(RuntimeError, match="ProcessInspectionUnavailable"):
        role._drainer(Path("/root"), "run_1").worker_identity(pid, "run_1", "generation")


def test_scope_identity_does_not_assume_a_missing_observation_matches(process_files):
    from goal_plus.execution.process_identity import process_alive

    pid, files = process_files
    files["stat"] = PermissionError("process observation denied")
    with pytest.raises(RuntimeError, match="ProcessInspectionUnavailable"):
        process_alive(pid, "12345")


@pytest.mark.parametrize("module", [
    "goal_plus.evidence_annotator", "goal_plus.adaptive_search.scheduler",
])
def test_worker_identity_accepts_only_its_exact_launch_vector(process_files, module):
    pid, files = process_files
    command = ["python", "-m", module, "drain", "--root", "/root", "--run-id", "run_1", "--generation", "generation"]
    files["cmdline"] = b"\0".join(os.fsencode(arg) for arg in command) + b"\0"
    matches = importlib.import_module(module)._drainer(Path("/root"), "run_1").worker_identity
    identity = matches(pid, "run_1", "generation")
    assert identity.pid == pid and identity.start_token == "12345"
    for index, replacement in [(2, module + ".other"), (3, "other"), (5, "/other")]:
        changed = command.copy()
        changed[index] = replacement
        files["cmdline"] = b"\0".join(os.fsencode(arg) for arg in changed) + b"\0"
        assert not matches(pid, "run_1", "generation")
    files["cmdline"] = b"\0".join(os.fsencode(arg) for arg in command + ["--run-id", "other"]) + b"\0"
    assert not matches(pid, "run_1", "generation")


@pytest.mark.parametrize("state", ["Z", "X", "x"])
def test_exited_states_do_not_require_a_readable_command(process_files, state):
    pid, files = process_files
    files["stat"] = files["stat"].replace(") S ", f") {state} ")
    files["cmdline"] = PermissionError("no live command")
    assert not inspection.process_alive(pid, "12345")
    assert not inspection.inspect_worker(pid, "run_1", "generation", module="role", root_dir=Path("/root"))


@pytest.mark.parametrize("stat", [
    "malformed", "876543 (name) S", "876543 (name) " + " ".join(["S"] * 50),
])
def test_malformed_stat_is_unavailable_not_absent(process_files, stat):
    pid, files = process_files
    files["stat"] = stat
    with pytest.raises(inspection.ProcessInspectionUnavailable):
        inspection.inspect_process(pid)


@pytest.mark.parametrize("error", [FileNotFoundError, ProcessLookupError])
def test_live_pid_without_procfs_is_not_a_trusted_observation(process_files, error):
    pid, files = process_files
    files["stat"] = error("procfs unavailable")
    with pytest.raises(inspection.ProcessInspectionUnavailable):
        inspection.inspect_process(pid)


@pytest.mark.parametrize("error", [FileNotFoundError, ProcessLookupError])
def test_missing_pid_is_absent_and_reused_pid_is_not_the_same_owner(process_files, monkeypatch, error):
    pid, files = process_files
    assert inspection.process_alive(pid, "12345")
    assert not inspection.process_alive(pid, "previous")
    files["stat"] = error("process gone")

    def missing(*_args):
        raise ProcessLookupError("process gone")

    monkeypatch.setattr(inspection.os, "kill", missing)
    assert inspection.inspect_process(pid) is None
    assert not inspection.process_alive(pid, "12345")
    assert inspection.inspect_process(0) is None
    assert inspection.inspect_process(-1) is None


def test_pid_reuse_during_command_read_cannot_match_the_original_worker(process_files, monkeypatch):
    pid, files = process_files
    before = inspection.inspect_process(pid)
    after = inspection.ProcessSnapshot(pid, "next", pid, "S")
    files["cmdline"] = b"python\0-m\0role\0drain\0--root\0/root\0--run-id\0run_1\0--generation\0generation\0"
    observations = iter([before, after])
    monkeypatch.setattr(inspection, "inspect_process", lambda _: next(observations))
    assert not inspection.inspect_worker(pid, "run_1", "generation", module="role", root_dir=Path("/root"))


@pytest.mark.parametrize("raw", [b"", b"incomplete"])
def test_live_process_without_complete_command_is_unknown(process_files, raw):
    pid, files = process_files
    files["cmdline"] = raw
    with pytest.raises(inspection.ProcessInspectionUnavailable):
        inspection.inspect_worker(pid, "run_1", "generation", module="role", root_dir=Path("/root"))


@pytest.mark.parametrize("returncode,stdout,stderr,expected", [
    (0, "S Sun Sep  6 09:30:00 2026\n", "", "Sun Sep  6 09:30:00 2026"),
    (0, "Z Sun Sep  6 09:30:00 2026\n", "", None),
    (1, "", "", None),
    (1, "", "denied", "error"),
    (0, "", "", "error"),
    (0, "S", "", "error"),
])
def test_recovery_keeps_its_existing_ps_identity_format(monkeypatch, returncode, stdout, stderr, expected):
    def run(_command, **options):
        assert options["timeout"] == 3
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(inspection.subprocess, "run", run)
    if expected == "error":
        with pytest.raises(inspection.ProcessInspectionUnavailable, match="RecoveryProcessInspectionUnavailable"):
            inspection.process_identity(123)
    else:
        identity = inspection.process_identity(123)
        assert (identity.model_dump() if identity else None) == (
            {"pid": 123, "started": expected} if expected else None
        )


@pytest.mark.skipif(sys.platform != "linux", reason="Linux procfs observation")
def test_current_local_process_is_observable_without_launching_a_host():
    snapshot = inspection.inspect_process(os.getpid())
    assert snapshot.pid == os.getpid()
    assert snapshot.process_group_id == os.getpgrp()
    assert snapshot.start_token.isdecimal()
    assert inspection.process_alive(snapshot.pid, snapshot.start_token)
    assert inspection.process_identity(snapshot.pid).pid == snapshot.pid


def test_scope_unknown_cleanup_keeps_the_handle_and_retry_does_not_signal_a_reused_pid(process_files, tmp_path, monkeypatch):
    from goal_plus import host_scope

    pid, files = process_files
    scope = host_scope.ensure_execution_scope(
        root_dir=tmp_path, host="pi", owner_session_id="owner", owner_pid=os.getpid(), start_guardian=False,
    )
    token = host_scope.register_execution_process(
        root_dir=tmp_path, scope_id=scope["scope_id"], pid=pid, process_group_id=None, label="worker",
    )
    stat = files["stat"]
    files["stat"] = PermissionError("temporarily denied")
    monkeypatch.setattr("goal_plus.execution.process_resources.os.kill", lambda *_: pytest.fail("unverified PID was signalled"))
    failed = host_scope.close_execution_scope(
        root_dir=tmp_path, scope_id=scope["scope_id"], reason="test", timeout_seconds=0,
    )
    assert failed["state"] == "cleanup_failed"
    assert failed["cleanup"]["active_count"] == 1
    assert "ProcessInspectionUnavailable" in failed["cleanup"]["errors"][0]
    assert failed["processes"][0]["token"] == token
    assert failed["processes"][0]["status"] == "stop_failed"
    files["stat"] = stat.replace("12345", "12346")
    retried = host_scope.close_execution_scope(
        root_dir=tmp_path, scope_id=scope["scope_id"], reason="retry", timeout_seconds=0,
    )
    assert retried["state"] == "closed"
    assert retried["processes"][0]["status"] == "stopped"
    assert retried["processes"][0]["start_token"] == "12345"


def test_scope_rejects_registration_for_a_different_process_group_without_state_changes(process_files, tmp_path):
    from goal_plus import host_scope

    pid, _files = process_files
    scope = host_scope.ensure_execution_scope(
        root_dir=tmp_path, host="pi", owner_session_id="owner", owner_pid=os.getpid(), start_guardian=False,
    )
    with pytest.raises(ValueError, match="own its registered process group"):
        host_scope.register_execution_process(
            root_dir=tmp_path, scope_id=scope["scope_id"], pid=pid, process_group_id=pid + 1, label="wrong",
        )
    assert host_scope.execution_scope_snapshot(root_dir=tmp_path, scope_id=scope["scope_id"]) == scope


def test_guardian_keeps_observing_when_owner_identity_is_unknown(monkeypatch):
    from goal_plus import host_scope

    scopes = iter([{"state": "open", "owner_pid": 123, "owner_start_token": "token"}, {"state": "closed"}])
    monkeypatch.setattr(host_scope, "execution_scope_snapshot", lambda **_: next(scopes))
    monkeypatch.setattr(host_scope, "observe_execution_processes", lambda **_: None)

    def unknown(*_args):
        raise inspection.ProcessInspectionUnavailable("temporarily denied")

    monkeypatch.setattr(host_scope, "process_alive", unknown)
    monkeypatch.setattr(host_scope.time, "sleep", lambda _: None)
    monkeypatch.setattr(host_scope, "close_execution_scope", lambda **_: pytest.fail("unknown owner was declared exited"))
    assert host_scope.run_guardian(root_dir=Path("/unused"), scope_id="scope_test") == 0
