"""Defer exec until the launch owner has durably registered the resource."""

import os
import sys
from collections.abc import Sequence
from pathlib import Path

import fcntl


# EOF cancels an uncommitted launch, including when the launcher is killed.
_WAIT_FOR_REGISTRATION = """
import os, signal, sys
descriptor = int(sys.argv[1])
lease_descriptor = int(sys.argv[2])
registered = os.read(descriptor, 1) == b'1'
os.close(descriptor)
if registered:
    if lease_descriptor >= 0:
        os.set_inheritable(lease_descriptor, True)
    for name in ('SIGPIPE', 'SIGXFZ', 'SIGXFSZ'):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), signal.SIG_DFL)
    os.execvpe(sys.argv[3], sys.argv[3:], os.environ)
"""


class ProcessLaunchGate:
    def __init__(self, lifetime_lock: Path | None = None) -> None:
        self._lifetime_lock = lifetime_lock

    def __enter__(self) -> "ProcessLaunchGate":
        self._reader, self._writer = os.pipe()
        self._lease = None
        try:
            if self._lifetime_lock is not None:
                self._lifetime_lock.parent.mkdir(parents=True, exist_ok=True)
                self._lease = self._lifetime_lock.open("a", encoding="utf-8")
                fcntl.flock(self._lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException as exc:
            self.__exit__()
            if isinstance(exc, BlockingIOError):
                raise RuntimeError("SessionExecutionActive: native session is still running") from exc
            raise
        return self

    @property
    def pass_fds(self) -> tuple[int, ...]:
        return (self._reader,) + ((self._lease.fileno(),) if self._lease is not None else ())

    def command(self, command: Sequence[str]) -> list[str]:
        lease = self._lease.fileno() if self._lease is not None else -1
        return [sys.executable, "-I", "-S", "-c", _WAIT_FOR_REGISTRATION, str(self._reader), str(lease), *command]

    def release(self) -> None:
        os.write(self._writer, b"1")

    def __exit__(self, *_exc: object) -> None:
        try:
            os.close(self._writer)
        finally:
            try:
                os.close(self._reader)
            finally:
                if self._lease is not None:
                    self._lease.close()
