"""Registered JSON-line host processes with correlated requests and events."""

import json
from io import DEFAULT_BUFFER_SIZE
import os
import select
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

from goal_plus.execution.launch_gate import ProcessLaunchGate
from goal_plus.execution.managed_process import ManagedProcess
from goal_plus.execution.process_output import PROCESS_OUTPUT_LIMIT_BYTES
from goal_plus.execution.sandbox import prepare_sandbox_environment, resolve_sandbox_command
from goal_plus.ports.process import ProcessRegistration
from goal_plus.ports.session import SessionProcessInput


class SessionTransportError(RuntimeError):
    pass


class SessionRequestRejected(SessionTransportError):
    """The native endpoint returned an explicit rejection for an exact request."""


class SessionRpcConnection:
    def __init__(
        self, process: subprocess.Popen,
        on_event: Callable[[dict[str, Any]], None],
        *, on_diagnostic: Callable[[str], None] | None = None,
    ) -> None:
        self.process = process
        self.condition = threading.Condition()
        self._write_lock = threading.Lock()
        self._pending: dict[str, dict[str, Any] | None] = {}
        self._error: Exception | None = None
        self._on_event = on_event
        self._on_diagnostic = on_diagnostic
        self._threads: list[threading.Thread] = []
        self._stderr_tail: deque[str] = deque(maxlen=32)
        self._stderr_lock = threading.Lock()

    def start(self) -> None:
        for target in (self._read_stdout, self._read_stderr):
            thread = threading.Thread(target=target, daemon=True)
            self._threads.append(thread)
            thread.start()

    def _fail(self, error: Exception) -> None:
        with self.condition:
            if self._error is None:
                self._error = error
            self.condition.notify_all()

    def _read_stdout(self) -> None:
        try:
            while line := self.process.stdout.readline(PROCESS_OUTPUT_LIMIT_BYTES + 1):
                size = len(line.encode("utf-8")) if isinstance(line, str) else len(line)
                if size > PROCESS_OUTPUT_LIMIT_BYTES:
                    raise SessionTransportError("native session response exceeds output limit")
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise SessionTransportError("native session response must be an object")
                self._on_event(event)
                identity = event.get("id")
                with self.condition:
                    if isinstance(identity, str) and identity in self._pending:
                        self._pending[identity] = event
                        self.condition.notify_all()
                        continue
        except Exception as exc:
            self._fail(exc)
        finally:
            with self._stderr_lock:
                diagnostic = "".join(self._stderr_tail).strip()
            detail = "native session transport closed"
            if diagnostic:
                # Preserve the transport error class while exposing the
                # bounded launcher/relay stderr that identifies the failure.
                detail = f"{detail}; worker stderr: {diagnostic[-4000:]}"
            self._fail(SessionTransportError(detail))

    def _read_stderr(self) -> None:
        # Drain diagnostics without retaining an unbounded or model-visible log.
        try:
            while chunk := self.process.stderr.read(DEFAULT_BUFFER_SIZE):
                text = chunk if isinstance(chunk, str) else chunk.decode("utf-8", errors="replace")
                with self._stderr_lock:
                    self._stderr_tail.append(text)
                if self._on_diagnostic is not None:
                    self._on_diagnostic(text)
        except (OSError, ValueError):
            pass

    def send(self, payload: dict[str, Any], *, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        if timeout <= 0 or not self._write_lock.acquire(timeout=timeout):
            raise TimeoutError("native session input timed out")
        try:
            descriptor = self.process.stdin.fileno()
            os.set_blocking(descriptor, False)
            data = memoryview((json.dumps(payload, ensure_ascii=False) + "\n").encode())
            while data:
                self.check()
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([], [descriptor], [], remaining)[1]:
                    raise TimeoutError("native session input timed out")
                try:
                    written = os.write(descriptor, data)
                except BlockingIOError:
                    continue
                data = data[written:]
        except TimeoutError as exc:
            # A partial JSON frame cannot be followed by another request safely.
            self._fail(exc)
            raise
        except (OSError, ValueError) as exc:
            self._fail(exc)
            raise SessionTransportError("native session input is closed") from exc
        finally:
            self._write_lock.release()

    def request(self, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        if timeout <= 0:
            raise TimeoutError("native session request deadline expired")
        identity = str(uuid4())
        deadline = time.monotonic() + timeout
        with self.condition:
            self.check()
            self._pending[identity] = None
        try:
            self.send({**payload, "id": identity}, timeout=deadline - time.monotonic())
            with self.condition:
                while self._pending[identity] is None:
                    self.check()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("native session request timed out")
                    self.condition.wait(remaining)
                return self._pending[identity]
        finally:
            with self.condition:
                self._pending.pop(identity, None)

    def check(self) -> None:
        if self._error is not None:
            raise SessionTransportError(str(self._error)) from self._error

    def finish(self, *, timeout: float) -> None:
        """Join readers after the resource owner has stopped the process."""
        self._fail(SessionTransportError("native session is closing"))
        for thread in self._threads:
            thread.join(timeout=timeout)
            if thread.is_alive():
                raise SessionTransportError("native session reader did not exit")


class SessionRpcProcess:
    def __init__(
        self, config: SessionProcessInput, registration: ProcessRegistration,
        on_event: Callable[[dict[str, Any]], None], *, execution_lock: Path | None = None,
    ) -> None:
        self._registration = registration
        self._token: str | None = None
        self._closed = False
        self._close_lock = threading.Lock()
        self._sandbox_cleanup = ExitStack()
        with ExitStack() as setup, ProcessLaunchGate(execution_lock) as gate:
            environment = dict(config.environment)
            sandbox = prepare_sandbox_environment(
                config.sandbox, cwd=config.cwd, environment=environment, cleanup=setup,
            )
            command = resolve_sandbox_command(
                config.command, cwd=config.cwd, sandbox=sandbox,
            )
            self.process = subprocess.Popen(
                gate.command(command), pass_fds=gate.pass_fds,
                cwd=config.cwd, env=environment,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.managed = ManagedProcess(self.process)
            try:
                self.connection = SessionRpcConnection(self.process, on_event)
            except BaseException:
                self.managed.close()
                raise
            try:
                self._token = registration.register(self.process.pid)
                gate.release()
                self.connection.start()
            except BaseException:
                self.close()
                raise
            self._sandbox_cleanup = setup.pop_all()

    def send(self, payload: dict[str, Any], *, timeout: float) -> None:
        self.connection.send(payload, timeout=timeout)

    def request(self, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        return self.connection.request(payload, timeout=timeout)

    def check(self) -> None:
        self.connection.check()

    def fail(self, error: Exception) -> None:
        self.connection._fail(error)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self.managed.close()
            self.connection.finish(timeout=self.managed.kill_timeout)
            if self._token is not None:
                self._registration.finish(self._token)
            self._sandbox_cleanup.close()
            self._closed = True
