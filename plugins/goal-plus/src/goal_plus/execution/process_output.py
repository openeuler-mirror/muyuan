"""Bounded local pipe collection, without Popen's accumulating output buffers."""

import os
import selectors
import subprocess
import time

# Per stream: cap one invocation's raw capture at 16 MiB across stdout/stderr.
# Read windows match the existing deterministic capture's 64 KiB budget.
PROCESS_OUTPUT_LIMIT_BYTES = 8 * 1024 * 1024
PROCESS_OUTPUT_READ_BYTES = 64 * 1024


class ProcessOutputLimitExceeded(ValueError):
    def __init__(self, stream: str, limit: int, stdout: str, stderr: str) -> None:
        super().__init__(f"ProcessOutputLimitExceeded: {stream} exceeds {limit} bytes")
        self.stdout = stdout
        self.stderr = stderr


class ProcessOutput:
    def __init__(
        self, process: subprocess.Popen[str], *, limit_bytes: int | None = None,
    ) -> None:
        self.process = process
        self.limit = PROCESS_OUTPUT_LIMIT_BYTES if limit_bytes is None else limit_bytes
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or self.limit <= 0:
            raise ValueError("process output limit must be a positive integer")
        self.buffers = {"stdout": bytearray(), "stderr": bytearray()}
        self.selector = selectors.DefaultSelector()
        try:
            for name in self.buffers:
                stream = getattr(process, name)
                if stream is not None:
                    self.selector.register(stream, selectors.EVENT_READ, name)
            if process.stdin is not None:
                process.stdin.close()
        except BaseException:
            self.selector.close()
            raise

    def close(self) -> None:
        self.selector.close()
        if self.process.returncode is not None:
            for name in self.buffers:
                stream = getattr(self.process, name)
                if stream is not None:
                    stream.close()

    def _snapshot(self, *, final: bool = False) -> tuple[str, str]:
        def decode(data: bytearray) -> str:
            text = data.decode(
                self.process.encoding or "utf-8",
                errors=(self.process.errors or "strict") if final else "replace",
            )
            return text.replace("\r\n", "\n").replace("\r", "\n")

        return decode(self.buffers["stdout"]), decode(self.buffers["stderr"])

    def communicate(self, *, input: None = None, timeout: float) -> tuple[str, str]:
        deadline = time.monotonic() + timeout
        while self.selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stdout, stderr = self._snapshot()
                raise subprocess.TimeoutExpired(self.process.args, timeout, output=stdout, stderr=stderr)
            for key, _events in self.selector.select(remaining):
                chunk = os.read(key.fd, PROCESS_OUTPUT_READ_BYTES)
                if not chunk:
                    self.selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                buffer = self.buffers[key.data]
                available = self.limit - len(buffer)
                buffer.extend(chunk[:available])
                if len(chunk) > available:
                    raise ProcessOutputLimitExceeded(key.data, self.limit, *self._snapshot())
        try:
            self.process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            stdout, stderr = self._snapshot()
            raise subprocess.TimeoutExpired(self.process.args, timeout, output=stdout, stderr=stderr) from None
        return self._snapshot(final=True)
