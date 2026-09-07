"""Defer exec until the launch owner has durably registered the resource."""

import os
import sys
from collections.abc import Sequence


# EOF cancels an uncommitted launch, including when the launcher is killed.
_WAIT_FOR_REGISTRATION = """
import os, signal, sys
descriptor = int(sys.argv[1])
registered = os.read(descriptor, 1) == b'1'
os.close(descriptor)
if registered:
    for name in ('SIGPIPE', 'SIGXFZ', 'SIGXFSZ'):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), signal.SIG_DFL)
    os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
"""


class ProcessLaunchGate:
    def __enter__(self) -> "ProcessLaunchGate":
        self._reader, self._writer = os.pipe()
        return self

    @property
    def pass_fds(self) -> tuple[int]:
        return (self._reader,)

    def command(self, command: Sequence[str]) -> list[str]:
        return [sys.executable, "-I", "-S", "-c", _WAIT_FOR_REGISTRATION, str(self._reader), *command]

    def release(self) -> None:
        os.write(self._writer, b"1")

    def __exit__(self, *_exc: object) -> None:
        try:
            os.close(self._writer)
        finally:
            os.close(self._reader)
