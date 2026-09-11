"""Darwin wire boundaries and real kernel observations, without model calls."""

import ctypes as C
import errno
import os
import subprocess
import sys
import time
from pathlib import Path
import struct
from types import SimpleNamespace

import pytest

from goal_plus.execution import process_identity as inspection
from goal_plus.execution import process_identity_darwin as darwin


def test_macos_scope_contract_runs_without_procfs_or_macos_libraries(tmp_path, monkeypatch):
    """Exercise the real adapter and scope owner on Linux too; fake only kernel I/O."""
    from goal_plus import host_scope

    pid = 876543
    state = {"status": 3}
    argv = bytes(C.c_int(3)) + b"/bin/python\0\0python\0worker\0space argument\0SECRET=not-argv\0"

    def proc_info(target, flavor, include_zombies, output, size):
        assert (target, flavor, size) == (pid, 3, 136)
        if state["status"] == 5 and not include_zombies:
            C.set_errno(errno.ESRCH)
            return 0
        # SDK proc_bsdinfo wire offsets; do not construct the production ctypes type.
        raw = bytearray(136)
        for offset, value in ((4, state["status"]), (12, pid), (100, pid)):
            struct.pack_into("=I", raw, offset, value)
        struct.pack_into("=QQ", raw, 120, 1_700_000_000, 123456)
        C.memmove(output, bytes(raw), len(raw))
        return len(raw)

    def group(kind, group_id, output, size):
        assert (kind, group_id) == (2, pid)
        if output is not None:
            output[0] = pid
        return C.sizeof(C.c_int)

    def procargs(mib, count, output, size, _new, _length):
        assert list(mib) == [1, 49, pid] and count == 3
        C.memmove(output, argv, len(argv))
        C.cast(size, C.POINTER(C.c_size_t))[0] = len(argv)
        return 0

    def stop(group_id, _signal):
        assert group_id == pid
        state["status"] = 5

    def guard(original):
        def read(path, *args, **kwargs):
            assert not str(path).startswith("/proc"), "macOS path attempted to read procfs"
            return original(path, *args, **kwargs)
        return read

    monkeypatch.setattr(inspection, "_PLATFORM", "darwin")
    monkeypatch.setattr(darwin, "_lib", lambda: SimpleNamespace(
        proc_pidinfo=proc_info, proc_listpids=group, sysctl=procargs,
    ))
    monkeypatch.setattr(darwin, "_named_value", lambda name: {
        b"kern.bootsessionuuid": b"test-boot\0", b"kern.argmax": bytes(C.c_int(len(argv))),
    }[name])
    for name in ("read_text", "read_bytes", "iterdir"):
        monkeypatch.setattr(Path, name, guard(getattr(Path, name)))
    monkeypatch.setattr(os, "getpgid", lambda target: pid if target == pid else pytest.fail("foreign pid"))
    monkeypatch.setattr(os, "killpg", stop)
    monkeypatch.setattr(os, "kill", lambda *_: pytest.fail("unverified PID-only signal"))

    observed = inspection.inspect_command(pid, ["worker", "space argument"])
    assert observed.start_token == "darwin:1700000000:123456"
    assert inspection.inspect_command(pid, ["worker", "space argument", "SECRET=not-argv"]) is None
    assert not inspection.process_alive(pid, "darwin:1700000000:123455")
    scope = host_scope.ensure_execution_scope(
        root_dir=tmp_path, host="pi", owner_session_id="test", owner_pid=pid, start_guardian=False,
    )
    host_scope.register_execution_process(
        root_dir=tmp_path, scope_id=scope["scope_id"], pid=pid, process_group_id=pid, label="test",
    )
    closed = host_scope.close_execution_scope(root_dir=tmp_path, scope_id=scope["scope_id"], reason="test", timeout_seconds=0)
    assert closed["state"] == "closed"
    assert closed["cleanup"]["active_count"] == 0
    assert closed["cleanup"]["errors"] == []


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
