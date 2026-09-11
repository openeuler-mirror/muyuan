"""Darwin kernel observations; no ps rendering or second-resolution PID identity.

ABI: macOS SDK libproc.h, sys/proc_info.h, sys/proc.h and sys/sysctl.h.
Only argv is returned from KERN_PROCARGS2; trailing environment is discarded.
"""

import ctypes as C
import errno
import os
from functools import cache

from goal_plus.ports.process_identity import ProcessSnapshot

PROC_PGRP_ONLY = 2
PROC_PIDTBSDINFO = 3
# XNU bsd/kern/proc_info.c: nonzero arg includes zombie records for BSDINFO.
INCLUDE_ZOMBIES = 1
CTL_KERN = 1
KERN_PROCARGS2 = 49
MAXCOMLEN = 16
BSD_STATES = {1: "I", 2: "R", 3: "S", 4: "T", 5: "Z"}
# A full enumeration is required. Bound retries if the group keeps growing.
GROUP_READ_ATTEMPTS = 3


class _BSDInfo(C.Structure):
    _fields_ = [(name, C.c_uint32) for name in (
        "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid",
        "rgid", "svuid", "svgid", "reserved",
    )] + [
        ("comm", C.c_char * MAXCOMLEN), ("name", C.c_char * (2 * MAXCOMLEN)),
        ("nfiles", C.c_uint32), ("pgid", C.c_uint32), ("jobc", C.c_uint32),
        ("tdev", C.c_uint32), ("tpgid", C.c_uint32), ("nice", C.c_int32),
        ("start_sec", C.c_uint64), ("start_usec", C.c_uint64),
    ]


@cache
def _lib():
    lib = C.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    lib.proc_pidinfo.argtypes = [C.c_int, C.c_int, C.c_uint64, C.c_void_p, C.c_int]
    lib.proc_listpids.argtypes = [C.c_uint32, C.c_uint32, C.c_void_p, C.c_int]
    lib.sysctlbyname.argtypes = [C.c_char_p, C.c_void_p, C.POINTER(C.c_size_t), C.c_void_p, C.c_size_t]
    lib.sysctl.argtypes = [C.POINTER(C.c_int), C.c_uint, C.c_void_p, C.POINTER(C.c_size_t), C.c_void_p, C.c_size_t]
    for name in ("proc_pidinfo", "proc_listpids", "sysctlbyname", "sysctl"):
        getattr(lib, name).restype = C.c_int
    return lib


def _error(operation: str) -> OSError:
    code = C.get_errno() or errno.EIO
    return OSError(code, operation)


def _named_value(name: bytes) -> bytes:
    size = C.c_size_t()
    if _lib().sysctlbyname(name, None, C.byref(size), None, 0) != 0:
        raise _error("sysctl size")
    buffer = C.create_string_buffer(size.value)
    if _lib().sysctlbyname(name, buffer, C.byref(size), None, 0) != 0:
        raise _error("sysctl value")
    return buffer.raw[:size.value]


def boot_identity() -> str:
    value = _named_value(b"kern.bootsessionuuid").rstrip(b"\0").decode("ascii")
    if not value:
        raise ValueError("empty boot identity")
    return value


def inspect_process(pid: int) -> ProcessSnapshot | None:
    info = _BSDInfo()
    C.set_errno(0)
    count = _lib().proc_pidinfo(pid, PROC_PIDTBSDINFO, INCLUDE_ZOMBIES, C.byref(info), C.sizeof(info))
    if count != C.sizeof(info):
        error = _error("proc_pidinfo")
        # A failed inspection is absence only if the PID is independently gone.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            pass
        raise error
    if info.pid != pid or info.status not in BSD_STATES or not info.start_sec or info.start_usec >= 1_000_000:
        raise ValueError("invalid BSD process identity")
    return ProcessSnapshot(
        pid, f"darwin:{info.start_sec}:{info.start_usec:06d}", info.pgid, BSD_STATES[info.status],
    )


def _decode_arguments(raw: bytes) -> list[str]:
    # KERN_PROCARGS2: native int argc, executable path, NUL padding, argc strings.
    width = C.sizeof(C.c_int)
    if len(raw) < width:
        raise ValueError("missing argc")
    argc = C.c_int.from_buffer_copy(raw[:width]).value
    if argc <= 0 or argc > len(raw) - width:
        raise ValueError("invalid argc")
    offset = raw.index(b"\0", width) + 1
    while offset < len(raw) and raw[offset] == 0:
        offset += 1
    arguments = []
    for _ in range(argc):
        end = raw.index(b"\0", offset)
        arguments.append(os.fsdecode(raw[offset:end]))
        offset = end + 1
    return arguments


def command_arguments(pid: int) -> list[str]:
    capacity = C.c_int.from_buffer_copy(_named_value(b"kern.argmax")).value
    if capacity <= 0:
        raise ValueError("invalid ARG_MAX")
    buffer = C.create_string_buffer(capacity)
    size = C.c_size_t(capacity)
    mib = (C.c_int * 3)(CTL_KERN, KERN_PROCARGS2, pid)
    if _lib().sysctl(mib, len(mib), buffer, C.byref(size), None, 0) != 0:
        raise _error("KERN_PROCARGS2")
    return _decode_arguments(buffer.raw[:size.value])


def group_pids(group_id: int) -> tuple[int, ...]:
    width = C.sizeof(C.c_int)
    capacity = 0
    for _ in range(GROUP_READ_ATTEMPTS):
        C.set_errno(0)
        needed = _lib().proc_listpids(PROC_PGRP_ONLY, group_id, None, 0)
        if needed < 0 or (needed == 0 and C.get_errno()):
            raise _error("proc_listpids size")
        capacity = max(capacity * 2, needed // width + 1)
        buffer = (C.c_int * capacity)()
        C.set_errno(0)
        count = _lib().proc_listpids(PROC_PGRP_ONLY, group_id, buffer, C.sizeof(buffer))
        if count < 0 or (count == 0 and C.get_errno()):
            raise _error("proc_listpids")
        if count % width:
            raise ValueError("partial process identifier")
        if count < C.sizeof(buffer):
            return tuple(pid for pid in buffer[:count // width] if pid > 0)
    raise OSError(errno.EAGAIN, "process group enumeration remained full")
