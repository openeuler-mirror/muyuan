"""Darwin wire boundaries and real kernel observations, without model calls."""

import ctypes as C
import errno
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from goal_plus.execution import process_identity as inspection
from goal_plus.execution import process_identity_darwin as darwin


def test_procargs_keeps_argument_boundaries_and_excludes_environment():
    raw = bytes(C.c_int(5)) + b"/bin/python\0\0python\0-c\0space and 'quotes'\0\0last\0SECRET=hidden\0"
    assert darwin._decode_arguments(raw) == ["python", "-c", "space and 'quotes'", "", "last"]


@pytest.mark.parametrize("raw", [b"", bytes(C.c_int(-1)), bytes(C.c_int(1)) + b"path\0\0truncated"])
def test_incomplete_procargs_is_not_a_matching_command(raw):
    with pytest.raises(ValueError):
        darwin._decode_arguments(raw)


def test_darwin_permission_failure_is_unknown_not_absent(monkeypatch):
    monkeypatch.setattr(inspection, "_PLATFORM", "darwin")
    def denied(_):
        raise PermissionError("denied")
    monkeypatch.setattr(darwin, "inspect_process", denied)
    with pytest.raises(inspection.ProcessInspectionUnavailable):
        inspection.process_alive(123, "darwin:1:000001")


def test_darwin_command_read_still_fences_pid_reuse(monkeypatch):
    monkeypatch.setattr(inspection, "_PLATFORM", "darwin")
    observations = iter([
        inspection.ProcessSnapshot(123, "darwin:1:000001", 123, "S"),
        inspection.ProcessSnapshot(123, "darwin:1:000002", 123, "S"),
    ])
    monkeypatch.setattr(darwin, "inspect_process", lambda _: next(observations))
    monkeypatch.setattr(darwin, "command_arguments", lambda _: ["python", "worker"])
    assert inspection.inspect_command(123, ["worker"]) is None


@pytest.mark.parametrize("missing", [False, True])
def test_failed_kernel_read_requires_confirmed_absence(monkeypatch, missing):
    def failed(*_args):
        C.set_errno(errno.ESRCH)
        return 0
    def probe(pid, signum):
        assert (pid, signum) == (123, 0)
        if missing:
            raise ProcessLookupError()
    monkeypatch.setattr(darwin, "_lib", lambda: SimpleNamespace(proc_pidinfo=failed))
    monkeypatch.setattr(darwin.os, "kill", probe)
    if missing:
        assert darwin.inspect_process(123) is None
    else:
        with pytest.raises(OSError):
            darwin.inspect_process(123)


@pytest.mark.parametrize("mode", ["denied", "partial", "full"])
def test_group_enumeration_never_accepts_failed_or_truncated_results(monkeypatch, mode):
    def read(_kind, _group, buffer, size):
        if buffer is None:
            return C.sizeof(C.c_int)
        if mode == "denied":
            C.set_errno(errno.EPERM)
            return 0
        return 1 if mode == "partial" else size
    monkeypatch.setattr(darwin, "_lib", lambda: SimpleNamespace(proc_listpids=read))
    with pytest.raises((OSError, ValueError)):
        darwin.group_pids(123)


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin kernel ABI")
def test_darwin_kernel_identity_argv_group_and_exit():
    arguments = ["-c", "import time; print('ready', flush=True); time.sleep(60)", "space 'quote'", "", "last"]
    process = subprocess.Popen([sys.executable, *arguments], stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        assert process.stdout.readline().strip() == "ready"
        snapshot = inspection.inspect_command(process.pid, arguments)
        assert snapshot is not None
        assert snapshot.start_token.startswith("darwin:")
        assert snapshot.process_group_id == process.pid
        assert inspection.process_alive(process.pid, snapshot.start_token)
        assert not inspection.process_alive(process.pid, "darwin:0:000000")
        assert inspection.inspect_command(process.pid, arguments[:-1]) is None
        members = inspection.inspect_process_group(process.pid)
        assert [(item.pid, item.start_token) for item in members] == [(snapshot.pid, snapshot.start_token)]
        assert inspection.boot_identity() == inspection.boot_identity()
        process.terminate()
        deadline = time.monotonic() + 5
        while inspection.process_alive(process.pid, snapshot.start_token) and time.monotonic() < deadline:
            time.sleep(0.01)
        zombie = inspection.inspect_process(process.pid)
        assert zombie.state == "Z"
        assert zombie.start_token == snapshot.start_token
        process.wait(timeout=5)
        assert inspection.inspect_process(process.pid) is None
        assert inspection.inspect_process_group(process.pid) == ()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stdout.close()
