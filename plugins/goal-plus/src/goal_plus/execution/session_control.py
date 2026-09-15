"""Invocation control for either native actor, without domain admission or retries."""

from concurrent.futures import Future, ThreadPoolExecutor
from collections.abc import Callable
from dataclasses import dataclass
import math
import threading

from goal_plus.ports.session import NativeSession, SessionCallRequest, SessionCallSnapshot, SessionInvocationResult


def _timeout(value: float, *, allow_zero: bool = False) -> None:
    if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        raise ValueError("timeout must be finite and positive" if not allow_zero else
                         "timeout must be finite and nonnegative")


@dataclass(frozen=True)
class _Call:
    prompt: str
    timeout: float
    future: Future[SessionInvocationResult]
    ready: threading.Event


class NativeSessionControl:
    """The trusted host supplies an admitted actor and owns this controller's lifetime.

    Call IDs belong to the controller; native invocation IDs only arrive in the
    settled result. No operation implicitly schedules a subsequent invocation.
    """

    def __init__(
        self, session: NativeSession, *, stop_action: Callable[[], None] | None = None,
        admit_call: Callable[[SessionCallRequest], None] | None = None,
        reopen_action: Callable[[], None] | None = None,
    ) -> None:
        self.session = session
        self._stop_action = stop_action or session.close
        self._admit_call = admit_call
        self._reopen_action = reopen_action
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="native-session-call")
        self._calls: dict[str, _Call] = {}
        self._current: str | None = None
        self._stopped = False
        self._closed = False

    @property
    def current_call_id(self) -> str | None:
        with self._lock:
            return self._current

    def _snapshot(self, call_id: str) -> SessionCallSnapshot:
        future = self._calls[call_id].future
        if not future.done():
            return SessionCallSnapshot(call_id, self.session.native_session_id, "running")
        try:
            result = future.result()
        except BaseException as exc:
            return SessionCallSnapshot(
                call_id, self.session.native_session_id, "failed", error=f"{type(exc).__name__}: {exc}",
            )
        return SessionCallSnapshot(call_id, self.session.native_session_id, result.state, result)

    def wake(self, prompt: str, *, call_id: str, timeout: float) -> SessionCallSnapshot:
        _timeout(timeout)
        if not call_id or not prompt.strip():
            raise ValueError("call_id and prompt are required")
        with self._lock:
            if previous := self._calls.get(call_id):
                if (previous.prompt, previous.timeout) != (prompt, timeout):
                    raise ValueError("call_id was already used with different input")
                return self._snapshot(call_id)
            if self._stopped or self._closed:
                raise RuntimeError("session execution is stopped")
            if self._current is not None and not self._calls[self._current].future.done():
                raise RuntimeError("session already has an active invocation")
            if self._admit_call is not None:
                self._admit_call(SessionCallRequest(call_id, prompt, timeout))
            self._current = call_id
            ready = threading.Event()
            future = self._executor.submit(self.session.run, prompt, timeout=timeout, on_started=ready.set)
            future.add_done_callback(lambda _: ready.set())
            self._calls[call_id] = _Call(prompt, timeout, future, ready)
            return self._snapshot(call_id)

    def restore_call(
        self, request: SessionCallRequest,
        observation: SessionInvocationResult | Callable[[Callable[[], None]], SessionInvocationResult],
    ) -> None:
        """Attach the host's authorized observer to one exact, already submitted call."""
        _timeout(request.timeout)
        if not request.call_id or not request.prompt.strip():
            raise ValueError("call_id and prompt are required")
        with self._lock:
            if self._calls or self._stopped or self._closed:
                raise RuntimeError("call restoration requires a new controller")
            ready = threading.Event()
            if isinstance(observation, SessionInvocationResult):
                future = Future()
                future.set_result(observation)
            else:
                future = self._executor.submit(observation, ready.set)
            future.add_done_callback(lambda _: ready.set())
            self._current = request.call_id
            self._calls[request.call_id] = _Call(request.prompt, request.timeout, future, ready)

    def wait(self, call_id: str, *, timeout: float) -> SessionCallSnapshot:
        _timeout(timeout, allow_zero=True)
        with self._lock:
            future = self._calls[call_id].future
        try:
            future.result(timeout=timeout)
        except Exception:
            # Execution failures are reported in the exact call's snapshot.
            pass
        with self._lock:
            return self._snapshot(call_id)

    def _active(self, call_id: str, timeout: float) -> bool:
        if call_id not in self._calls:
            raise KeyError(call_id)
        if self._stopped or self._closed:
            raise RuntimeError("session execution is stopped")
        if call_id != self._current:
            raise ValueError("call_id does not identify the current invocation")
        if not self._calls[call_id].ready.wait(timeout):
            raise TimeoutError("native session has not acknowledged the invocation")
        return not self._calls[call_id].future.done()

    def message(self, call_id: str, prompt: str, *, timeout: float) -> None:
        _timeout(timeout)
        with self._lock:
            if not self._active(call_id, timeout):
                raise RuntimeError("messages require an active invocation")
            self.session.message(prompt, timeout=timeout)

    def interrupt(self, call_id: str, *, timeout: float) -> None:
        _timeout(timeout)
        with self._lock:
            if self._active(call_id, timeout):
                self.session.interrupt(timeout=timeout)

    def _fence(self) -> None:
        with self._lock:
            self._stopped = True
        self._executor.shutdown(wait=False)

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._fence()
            self._stop_action()

    def reopen(self) -> None:
        """The host must reauthorize an explicit open before invoking this method."""
        with self._lifecycle_lock, self._lock:
            if self._closed:
                raise RuntimeError("session resources have been closed")
            if not self._stopped:
                return
            if self._reopen_action is None:
                raise RuntimeError("stopped session requires explicit host recovery")
            if any(not call.future.done() for call in self._calls.values()):
                raise RuntimeError("stopped invocation has not finished")
            self._reopen_action()
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="native-session-call")
            self._stopped = False

    def close(self) -> None:
        with self._lifecycle_lock:
            self._fence()
            with self._lock:
                self._closed = True
            self.session.close()
