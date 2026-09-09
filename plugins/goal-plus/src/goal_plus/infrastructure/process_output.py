"""Existing bounded capture and process-group cleanup shared by deterministic I/O."""

import os
import signal
import subprocess
import time

OUTPUT_LIMIT_BYTES = 64 * 1024
TERM_GRACE_SECONDS = 0.5


class BoundedOutput:
    def __init__(self, limit: int = OUTPUT_LIMIT_BYTES) -> None:
        self.limit = limit
        self.data = bytearray()
        self.truncated = False

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        if len(chunk) >= self.limit:
            self.data[:] = chunk[-self.limit :]
            self.truncated = True
            return
        overflow = len(self.data) + len(chunk) - self.limit
        if overflow > 0:
            del self.data[:overflow]
            self.truncated = True
        self.data.extend(chunk)

    def text(self) -> str:
        value = self.data.decode("utf-8", errors="replace")
        if self.truncated:
            return "[... output truncated ...]\n" + value
        return value


def terminate_process_group(
    process: subprocess.Popen[bytes],
) -> None:
    if os.name != "posix":  # pragma: no cover - Windows fallback
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=TERM_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        return

    process_group = process.pid

    def group_exists() -> bool:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        pass

    deadline = time.monotonic() + TERM_GRACE_SECONDS
    while group_exists() and time.monotonic() < deadline:
        process.poll()
        time.sleep(0.02)
    if group_exists():
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=TERM_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
