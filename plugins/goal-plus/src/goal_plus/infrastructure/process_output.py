"""Bounded capture and direct subprocess completion for deterministic I/O."""

import os
import selectors
import subprocess
import time

OUTPUT_LIMIT_BYTES = 64 * 1024
TERM_GRACE_SECONDS = 0.5
PIPE_POLL_SECONDS = 0.05


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


def terminate_process(
    process: subprocess.Popen[bytes],
) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=TERM_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=TERM_GRACE_SECONDS)


def capture_process(
    process: subprocess.Popen[bytes],
    *,
    timeout: float | None = None,
    stdout: BoundedOutput | None = None,
) -> subprocess.CompletedProcess[str]:
    """Bound pipe reads after direct-child exit, without inspecting descendants."""
    captures = {
        "stdout": stdout if stdout is not None else BoundedOutput(),
        "stderr": BoundedOutput(),
    }
    deadline = None if timeout is None else time.monotonic() + timeout
    drain_deadline = None
    timed_out = False
    with selectors.DefaultSelector() as selector:
        try:
            for name in captures:
                selector.register(getattr(process, name), selectors.EVENT_READ, name)
            while selector.get_map() or process.poll() is None:
                now = time.monotonic()
                if deadline is not None and now >= deadline and process.poll() is None:
                    timed_out = True
                    terminate_process(process)
                if process.poll() is not None:
                    if drain_deadline is None:
                        drain_deadline = now + TERM_GRACE_SECONDS
                    if now >= drain_deadline:
                        break
                for key, _ in selector.select(PIPE_POLL_SECONDS):
                    chunk = os.read(key.fd, 8192)
                    if chunk:
                        captures[key.data].append(chunk)
                    else:
                        selector.unregister(key.fileobj)
        finally:
            terminate_process(process)
            for name in captures:
                getattr(process, name).close()
    stdout, stderr = captures["stdout"].text(), captures["stderr"].text()
    if timed_out:
        raise subprocess.TimeoutExpired(
            process.args, timeout, output=stdout, stderr=stderr
        )
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
