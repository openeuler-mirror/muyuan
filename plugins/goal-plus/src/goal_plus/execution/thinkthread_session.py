"""One retained Child actor; domain admission, RPC tools and settlement are injected."""

import hashlib
import json
import math
import threading
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import asdict
from typing import Any

from goal_plus.execution.model_failures import model_identity, normalize_model_failure
from goal_plus.execution.thinkthread_child import ChildDriver
from goal_plus.execution.thinkthread_rpc import (
    MESSAGE_RECEIVE_LIMIT,
    PROTOCOL,
    candidate_capabilities_marker,
    message_payload,
)
from goal_plus.ports.clock import Clock
from goal_plus.ports.session import (
    SessionInvocationObservation,
    SessionInvocationResult,
)
from goal_plus.ports.thinkthread import AgentPosixBridgeError, ChildBinding
from goal_plus.ports.thinkthread_session import (
    ThinkThreadSessionHooks,
    ThinkThreadSessionInput,
)

# Poll retained Message outcomes without scheduling subsequent turns.
CHILD_POLL_SECONDS = 0.2
CHILD_STOP_TIMEOUT_SECONDS = 5.0


class ThinkThreadSession:
    def __init__(
        self, config: ThinkThreadSessionInput, driver: ChildDriver, clock: Clock,
        record: dict[str, Any], hooks: ThinkThreadSessionHooks,
        *, domain_bound: bool = False,
    ) -> None:
        self.config, self.driver, self.clock = config, driver, clock
        self.record, self.hooks = record, hooks
        self._lock = threading.RLock()
        self._closing = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._running = False
        self._domain_bound = domain_bound

    @property
    def native_session_id(self) -> str | None:
        return self.record.get("thinkthread_id")

    def _save(self) -> None:
        self.hooks.persist(self.record)

    def _observe(self) -> dict[str, Any] | None:
        if self.native_session_id is None:
            return None
        return self.driver.observe(
            self.native_session_id, root_id=self.config.root_id,
            branch_id=self.record.get("fs_branch_id"),
        )

    def _bind(self, result: dict[str, Any]) -> None:
        binding = self.driver.binding(result, model=self.config.model)
        self.record.update(
            thinkthread_id=binding.child_id, fs_branch_id=binding.branch_id,
            active_message_id=binding.initial_message_id,
        )
        self._save()
        self._observe()

    def _bind_domain(self) -> None:
        if self._domain_bound:
            return
        self.hooks.bind(ChildBinding(
            self.native_session_id, self.record["fs_branch_id"], self.record.get("active_message_id"),
        ))
        self._domain_bound = True
        if self.record.pop("replaced_thinkthread_id", None) is not None:
            self._save()

    def _reap_dead_child(self) -> None:
        """Destroy a stopped Codex Child; its next invocation respawns from the last settled snapshot."""
        if self.config.fork or self.record.get("dispatch_provenance") is not None:
            child = self._observe()
            if (child is None or self.record.get("child_retired") is True
                    or (self.config.agent_harness == "codex" and child.get("executionState") == "absent")):
                raise RuntimeError("ForkContinuationUnavailable: the original ThinkThread Child cannot continue")
            return
        if self.config.agent_harness != "codex":
            return
        replaced = self.record.get("replaced_thinkthread_id")
        if self.native_session_id is None:
            if isinstance(replaced, dict):
                self.driver.release(
                    replaced["thinkthread_id"], root_id=self.config.root_id,
                    branch_id=replaced["fs_branch_id"], timeout=CHILD_STOP_TIMEOUT_SECONDS,
                )
            return
        child = self._observe()
        retired = self.record.get("child_retired") is True
        if child is not None and child.get("executionState") != "absent" and not retired:
            return
        # A Codex Child stays running while retained; once absent (or retired by a
        # diverging settlement) it can never be rewoken, so the only exact
        # continuation is a fresh Child on a new branch.
        replaced = {
            "thinkthread_id": self.native_session_id,
            "fs_branch_id": self.record["fs_branch_id"],
        }
        self.record["replaced_thinkthread_id"] = replaced
        self.record.pop("child_retired", None)
        self._save()
        self.driver.release(
            replaced["thinkthread_id"], root_id=self.config.root_id,
            branch_id=replaced["fs_branch_id"], timeout=CHILD_STOP_TIMEOUT_SECONDS,
        )
        self.record.update(thinkthread_id=None, fs_branch_id=None, active_message_id=None)
        self._domain_bound = False
        self._save()

    def _recover_spawn(self, *, bind_domain: bool = True) -> bool:
        matches = self.driver.recovery_matches(
            before=set(self.record["spawn_intent"]["children_before"]), bound=set(),
            nonce=self.record["registration_nonce"], root_id=self.config.root_id,
        )
        if len(matches) != 1:
            return False
        self._bind(matches[0][0])
        if bind_domain:
            self._bind_domain()
        # Consume from the saved cursor so registration and exact wake ack both
        # pass through the same role bridge, including after a lost spawn reply.
        if bind_domain:
            self.hooks.receive(self.record)
        self.record["spawn_intent"]["state"] = "bound"
        self._save()
        return True

    def _start(self, message: str) -> None:
        with self.hooks.admission():
            if self._closing.is_set():
                raise RuntimeError("ThinkThread session is closing")
            self._reap_dead_child()
            if self.native_session_id is None:
                children = self.driver.client.invoke("thinkthread.list").get("children")
                if not isinstance(children, list):
                    raise RuntimeError("thinkthread.list omitted children")
                self.record["spawn_intent"] = {
                    "state": "started",
                    "children_before": [child["thinkthreadId"] for child in children],
                }
                self._save()
                params = {
                    "profile": "self", "fs": "private",
                    "fsSnapshotId": self.record.get("spawn_snapshot_id") or self.config.baseline_snapshot_id,
                    "capabilities": ["thinkthread.message"], "initialMessage": message,
                }
                if self.config.model is not None:
                    params["model"] = self.config.model
                try:
                    self._bind(self.driver.spawn(params))
                except AgentPosixBridgeError as exc:
                    self.record["spawn_intent"]["state"] = "unknown" if exc.completion_unknown else "rejected"
                    self._save()
                    if not exc.completion_unknown:
                        raise
                else:
                    self.record["spawn_intent"]["state"] = "bound"
                    self._save()
            else:
                if self._observe() is None:
                    raise RuntimeError("ThinkThread session Child no longer exists")
                self.record["wake_intent"] = {"state": "started"}
                self._save()
                try:
                    self.record["active_message_id"] = self.driver.wake(self.native_session_id, message)
                except AgentPosixBridgeError as exc:
                    self.record["wake_intent"]["state"] = "unknown" if exc.completion_unknown else "rejected"
                    self._save()
                    if not exc.completion_unknown:
                        raise
                else:
                    self.record["wake_intent"]["state"] = "sent"
                    self._save()
        if self.native_session_id is not None:
            self._bind_domain()

    def _poll(self, nonce: str, announced: bool, on_started) -> tuple[SessionInvocationResult | None, bool]:
        with self.hooks.admission():
            pass
        if self.native_session_id is None:
            self._recover_spawn()
        if self.native_session_id is None:
            return None, announced
        self.hooks.receive(self.record)
        message_id = self.record.get("active_message_id")
        if message_id is None:
            message_id = self.record.get("acknowledged_messages", {}).get(nonce)
            if message_id:
                self.record["active_message_id"] = message_id
                self._save()
        if message_id is None:
            return None, announced
        if not announced and on_started is not None:
            on_started()
        child = self._observe()
        if child is None:
            raise RuntimeError("ThinkThread Child disappeared during invocation")
        terminal = self.driver.turn_terminal(child, message_id)
        if terminal is None:
            return None, True
        state = "completed" if terminal == "completed" else (
            "interrupted" if terminal in {"cancelled", "interrupted"} else "failed"
        )
        if self.record.get("interrupted_dispatch_nonce") == nonce:
            state = "interrupted"
        error = None
        dispatch_error = self.record.get("dispatch_error")
        if isinstance(dispatch_error, dict) and dispatch_error.get("dispatch_nonce") == nonce:
            state = "failed"
            error = "ChildDispatchError: " + str(dispatch_error.get("message") or "Child startup failed")
        if state == "failed" and error is None:
            completion = child.get("completion")
            wake = child.get("lastWakeOutcome")
            error = str(completion or wake or "ThinkThread Child model request failed")
        configured_model = self.record.get("model")
        if isinstance(configured_model, dict):
            provider = configured_model.get("provider")
            model = configured_model.get("model")
            provider = provider if isinstance(provider, str) else None
            model = model if isinstance(model, str) else None
        else:
            provider, model = model_identity(
                configured_model if isinstance(configured_model, str) else None
            )
        with self.hooks.admission():
            pass
        text = self.hooks.settle(self.record) if state == "completed" else ""
        result = SessionInvocationResult(
            self.native_session_id, message_id, state, text,
            agent_harness=self.config.agent_harness, runtime_provider="thinkthread",
            error=error,
            observation=SessionInvocationObservation(
                self.record["started_at"], self.clock.timestamp(),
                max(0.0, self.clock.now() - self.record["started_unix"]),
            ),
            failure=normalize_model_failure(
                error,
                phase="spawn" if error and error.startswith("ChildDispatchError") else "response",
                model=model,
                provider=provider,
                source="thinkthread_child",
            ),
        )
        self.record.update(status=state, invocation_result=asdict(result))
        self._save()
        return result, True

    def run(
        self, prompt: str, *, timeout: float, on_started: Callable[[], None] | None = None,
    ) -> SessionInvocationResult:
        with self._lock:
            if self._closing.is_set() or self._running:
                raise RuntimeError("ThinkThread session is closed or already running")
            if self.record.get("status") in {"running", "needs_recovery"}:
                raise RuntimeError("ThinkThread invocation requires explicit recovery")
            self._running = True
            self._idle.clear()
        deadline = self.clock.monotonic() + timeout
        try:
            with self.hooks.admission():
                self.record["dispatch_index"] = int(self.record.get("dispatch_index", 0)) + 1
                nonce = str(uuid.uuid4())
                self.record["dispatch_nonce"] = nonce
                self.record.setdefault("dispatch_nonces", []).append(nonce)
                self.record.setdefault("registration_nonce", str(uuid.uuid4()))
                self.record.update(status="running", active_message_id=None, started_at=self.clock.timestamp(),
                                   started_unix=self.clock.now(), execution_deadline_unix=self.clock.now() + timeout)
                for key in ("attempt_request", "attempt_result", "inner_git_attempt", "dispatch_error", "invocation_result", "interrupted_dispatch_nonce"):
                    self.record.pop(key, None)
                self._save()
            message = (
                prompt + f"\n\nThinkThread Goal Plus dispatch: protocol={PROTOCOL}; "
                f"agent_session_id={self.record['agent_session_id']}; "
                f"registration_nonce={self.record['registration_nonce']}; dispatch_nonce={nonce}."
            )
            if self.config.tool_capabilities is not None:
                message += "\n" + candidate_capabilities_marker(self.config.tool_capabilities)
            with self._lock:
                self._start(message)
            announced = False
            if self.record.get("active_message_id") is not None:
                if on_started is not None:
                    on_started()
                announced = True
            while not self._closing.is_set():
                if self.clock.monotonic() >= deadline:
                    raise TimeoutError("ThinkThread invocation exceeded its execution allowance")
                result, announced = self._poll(nonce, announced, on_started)
                if result is not None:
                    return result
                self._closing.wait(CHILD_POLL_SECONDS)
            raise RuntimeError("ThinkThread session closed during invocation")
        except BaseException:
            if not self._closing.is_set():
                self.record["status"] = "needs_recovery"
                self._save()
                if self.native_session_id is not None:
                    self._stop_execution(timeout=CHILD_STOP_TIMEOUT_SECONDS)
            raise
        finally:
            with self._lock:
                self._running = False
                self._idle.set()

    def resume(self, *, on_started: Callable[[], None] | None = None) -> SessionInvocationResult:
        """Observe the saved delivery intent without sending another spawn or wake."""
        with self._lock:
            if self._closing.is_set() or self._running:
                raise RuntimeError("ThinkThread session is closed or already running")
            deadline = self.record["execution_deadline_unix"]
            nonce = self.record["dispatch_nonce"]
            started = self.record["started_unix"]
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                   for value in (started, deadline)) or deadline < started or not isinstance(nonce, str) or not nonce:
                raise ValueError("ThinkThread recovery requires its exact original invocation timing and nonce")
            self._running = True
            self._idle.clear()
        try:
            if self.native_session_id is not None:
                self._bind_domain()
            announced = False
            while not self._closing.is_set():
                result, announced = self._poll(nonce, announced, on_started)
                if result is not None:
                    return result
                if self.clock.now() >= deadline:
                    raise TimeoutError("ThinkThread recovered invocation exhausted its original execution allowance")
                self._closing.wait(CHILD_POLL_SECONDS)
            raise RuntimeError("ThinkThread session closed during recovery")
        except BaseException:
            if not self._closing.is_set():
                self.record["status"] = "needs_recovery"
                self._save()
                if self.native_session_id is not None:
                    self._stop_execution(timeout=CHILD_STOP_TIMEOUT_SECONDS)
            raise
        finally:
            with self._lock:
                self._running = False
                self._idle.set()

    def message(self, prompt: str, *, timeout: float) -> None:
        self._control_message("session_message", prompt, timeout=timeout)

    def _control_message(self, kind: str, prompt: str, *, timeout: float) -> None:
        deadline = self.clock.monotonic() + timeout

        def remaining():
            value = deadline - self.clock.monotonic()
            if value <= 0:
                raise TimeoutError("ThinkThread steering exceeded its timeout")
            return value

        with self._lock, (self.hooks.admission() if kind == "session_message" else nullcontext()):
            if self._closing.is_set() or not self._running or not self.record.get("active_message_id"):
                raise RuntimeError("ThinkThread messages require an active invocation")
            self._observe()
            request_id = str(uuid.uuid4())
            dispatch_nonce = self.record["dispatch_nonce"]
            content = json.dumps({
                "agent_session_id": self.record["agent_session_id"],
                "dispatch_nonce": dispatch_nonce,
                "message_id": self.record["active_message_id"],
                "prompt": prompt,
                "deadline_unix_ms": int((self.clock.now() + timeout) * 1000),
            }, ensure_ascii=True, separators=(",", ":"))
            digest = hashlib.sha256(content.encode()).hexdigest()
            self.driver.client.invoke("message.send", {
                "recipientThinkthreadId": self.native_session_id, "wake": False,
                "text": json.dumps({
                    "protocol": PROTOCOL, "type": kind, "request_id": request_id,
                    "content_json": content, "content_sha256": digest,
                }),
            }, timeout_seconds=remaining())
        cursor = None
        while not self._closing.is_set() and self.clock.monotonic() < deadline:
            params = {"senderThinkthreadId": self.native_session_id, "limit": MESSAGE_RECEIVE_LIMIT}
            if cursor is not None:
                params["after"] = cursor
            page = self.driver.client.invoke("message.receive", params,
                timeout_seconds=remaining())
            if not isinstance(page.get("messages"), list) or not isinstance(page.get("nextCursor"), str):
                raise RuntimeError("ThinkThread steering received an invalid Message page")
            for message in page["messages"]:
                if message.get("senderThinkthreadId") != self.native_session_id:
                    continue
                reply = message_payload(message)
                if not reply or reply.get("protocol") != PROTOCOL or reply.get("type") != kind + "_ack" or reply.get("request_id") != request_id:
                    continue
                if reply.get("content_sha256") != digest:
                    raise RuntimeError("ThinkThread steering acknowledgement content changed")
                if reply.get("accepted") is not True:
                    raise RuntimeError("ThinkThread steering rejected: " + str(reply.get("reason")))
                if kind == "session_interrupt":
                    with self._lock:
                        self.record["interrupted_dispatch_nonce"] = dispatch_nonce
                        self._save()
                return
            cursor = page["nextCursor"]
            if not page.get("hasMore"):
                self._closing.wait(min(CHILD_POLL_SECONDS, max(0, deadline - self.clock.monotonic())))
        raise TimeoutError("ThinkThread steering acknowledgement was not received")

    def interrupt(self, *, timeout: float) -> None:
        self._control_message("session_interrupt", "", timeout=timeout)

    def _stop_execution(self, *, timeout: float) -> None:
        if self.native_session_id is not None and self._observe() is not None:
            self.driver.interrupt(self.native_session_id, timeout)
            child = self._observe()
            if child is not None and child.get("executionState") != "absent":
                raise RuntimeError("ThinkThread Child execution remained active")

    def close(self) -> None:
        self._closing.set()
        with self._lock:
            self._close()
        if not self._idle.wait(CHILD_STOP_TIMEOUT_SECONDS):
            raise RuntimeError("ThinkThread invocation has not left its resource callback")
        with self._lock:
            self.record["status"] = "stopped"
            self._save()

    def reopen(self) -> None:
        with self._lock:
            if self._running or not self._idle.is_set():
                raise RuntimeError("ThinkThread invocation has not finished")
            if not self._closing.is_set() or self.record.get("status") != "stopped":
                raise RuntimeError("ThinkThread execution has not been stopped")
            if self.native_session_id is not None:
                child = self._observe()
                if child is None or child.get("executionState") != "absent":
                    raise RuntimeError("ThinkThread retained Child is missing or execution remains active")
            self._closing.clear()

    def _close(self) -> None:
        if self.native_session_id is None:
            intent = self.record.get("spawn_intent", {})
            if intent.get("state") in {"started", "unknown"} and not self._recover_spawn(bind_domain=False):
                raise RuntimeError("ThinkThread spawn remains unknown; resource cannot be completed")
        if self.native_session_id is not None:
            self._stop_execution(timeout=CHILD_STOP_TIMEOUT_SECONDS)
            child = self._observe()
            if child is not None and child.get("executionState") != "absent":
                raise RuntimeError("ThinkThread Child execution remained active")
