"""Codex/Pi native sessions; callers retain admission, budgets and results policy."""

from dataclasses import replace
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from typing import Any

from goal_plus.execution.managed_process import PROCESS_POLL_SECONDS
from goal_plus.execution.process_output import PROCESS_OUTPUT_LIMIT_BYTES
from goal_plus.execution.pi_metrics import collect_pi_metrics
from goal_plus.execution.model_failures import model_identity, normalize_model_failure
from goal_plus.execution.session_rpc import SessionRpcProcess, SessionTransportError, SessionRequestRejected
from goal_plus.ports.process import ProcessRegistration
from goal_plus.ports.session import (
    ProviderDispatchReceipt,
    ProviderDispatchRequest,
    NativeSessionInput,
    SessionInvocationObservation,
    SessionInvocationResult,
    SessionToolBridge,
    SessionProcessInput,
)


def _pi_package_module() -> Path:
    executable = shutil.which("pi")
    if executable is None:
        raise SessionTransportError("Pi executable is unavailable for fork")
    resolved = Path(executable).resolve()
    # The supported Pi launcher is <package>/dist/bundle/cli.js.  Walk upward
    # so checkout-local or user-local installations use their own SDK version.
    for parent in (resolved.parent, *resolved.parents):
        module = parent / "dist" / "index.js"
        if module.is_file():
            return module
    raise SessionTransportError("Pi package SDK is unavailable for fork")


def _fork_pi_session_file(
    *,
    source_path: Path,
    target_cwd: Path,
    session_dir: Path,
    session_id: str,
    parent_session_id: str,
    checkpoint_id: str,
    helper: Path,
    process_input: SessionProcessInput,
    registration: ProcessRegistration,
    timeout: float,
) -> dict[str, Any]:
    """Create one Pi session file with the official SessionManager API."""
    node = shutil.which("node")
    if node is None:
        raise SessionTransportError("Node.js is unavailable for native Pi fork")
    module = _pi_package_module()
    environment = dict(process_input.environment)
    environment.update(
        GP_PI_MODULE=module.as_uri(),
    )
    rpc = SessionRpcProcess(replace(process_input, command=(node, str(helper)), environment=environment),
                            registration, lambda _event: None)
    try:
        result = rpc.request({"sourcePath": str(source_path.resolve()), "targetCwd": str(target_cwd.resolve()),
                              "sessionDir": str(session_dir.resolve()), "sessionId": session_id,
                              "parentSessionId": parent_session_id, "checkpointId": checkpoint_id}, timeout=timeout)
        if result.get("error"):
            raise SessionTransportError(f"Pi SessionManager fork failed: {result['error']}")
        payload = result.get("result")
    finally:
        rpc.close()
    branch_id = payload.get("sessionId") if isinstance(payload, dict) else None
    if branch_id != session_id:
        raise SessionTransportError("Pi SessionManager fork returned an unexpected session identity")
    session_file = payload.get("sessionFile") if isinstance(payload, dict) else None
    if not isinstance(session_file, str) or not Path(session_file).is_file():
        raise SessionTransportError("Pi SessionManager fork did not create a session file")
    return payload


class CodexSession:
    def __init__(
        self, config: NativeSessionInput, registration: ProcessRegistration, *, timeout: float,
        tool_bridge: SessionToolBridge | None = None,
        on_tool_result: Callable[[str, Any, str, str, str, float], None] | None = None,
        on_turn_completed: Callable[[str, str, bool], None] | None = None,
        resume_from: "CodexSession | None" = None,
        execution_lock: Path | None = None,
        attach_existing: bool = False,
    ) -> None:
        if config.agent_harness != "codex" or config.runtime_provider != "direct":
            raise ValueError("Codex session requires the Codex host")
        if resume_from is not None and config.native_session_id != resume_from.native_session_id:
            raise ValueError("Codex continuation must preserve the native session identity")
        self.config = config
        self._condition = threading.Condition()
        self._run_lock = threading.Lock()
        self._session_id = ""
        self._turn_id: str | None = None
        self._completed: dict[str, dict[str, Any]] = {}
        self._messages: dict[str, list[str]] = {}
        self._usage: dict[str, Any] = {}
        self._usage_baseline = dict(resume_from._usage_baseline if resume_from else config.metrics_baseline)
        self._output_bytes = 0
        self._receiving = False
        self._announced_turn_id: str | None = None
        self._deadline = 0.0
        self._tool_bridge = tool_bridge
        self._on_tool_result = on_tool_result
        self._on_turn_completed = on_turn_completed
        self._tool_names = {tool.name for tool in tool_bridge.definitions} if tool_bridge else set()
        self._tool_calls: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        self._tool_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="codex-session-tools")
        self._callbacks: set[Future] = set()
        self._closing = False
        self.rpc = SessionRpcProcess(
            config.process, registration, self._event, execution_lock=execution_lock,
        )
        deadline = time.monotonic() + timeout
        try:
            self._request("initialize", {
                "clientInfo": {"name": "goal_plus", "version": "1"},
                "capabilities": {"experimentalApi": True},
            }, timeout=deadline - time.monotonic())
            self.rpc.send({"method": "initialized", "params": {}}, timeout=deadline - time.monotonic())
            params: dict[str, Any] = {
                "cwd": str(config.process.cwd), "approvalPolicy": "never",
                "config": config.configuration,
            }
            if config.model is not None:
                params["model"] = config.model
            if config.instructions is not None:
                params["developerInstructions"] = config.instructions
            if config.native_session_id is not None:
                params["threadId"] = config.native_session_id
            elif tool_bridge is not None:
                params["dynamicTools"] = [
                    {"type": "function", "name": tool.name, "description": tool.description,
                     "inputSchema": tool.input_schema, "deferLoading": False}
                    for tool in tool_bridge.definitions
                ]
            if attach_existing:
                if not config.native_session_id:
                    raise ValueError("attach_existing requires a native session identity")
                self._session_id = config.native_session_id
            else:
                result = self._request(
                    "thread/resume" if config.native_session_id else "thread/start", params,
                    timeout=deadline - time.monotonic(),
                )
                self._session_id = str(result["thread"]["id"])
                if config.native_session_id and self._session_id != config.native_session_id:
                    raise SessionTransportError("Codex resumed a different native session")
        except BaseException:
            self.close()
            raise

    @property
    def native_session_id(self) -> str:
        return self._session_id

    def _request(self, method: str, params: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        response = self.rpc.request({"method": method, "params": params}, timeout=timeout)
        if "error" in response:
            raise SessionRequestRejected(f"{method}: {response['error']}")
        return response["result"]

    def _event(self, event: dict[str, Any]) -> None:
        if "method" in event and "id" in event:
            if event["method"] == "item/tool/call":
                with self._condition:
                    if self._closing or not self._receiving:
                        raise SessionTransportError("native tool callback has no active invocation")
                    self._output_bytes += len(json.dumps(event).encode("utf-8"))
                    if self._output_bytes > PROCESS_OUTPUT_LIMIT_BYTES:
                        raise SessionTransportError("native session invocation exceeds output limit")
                    callback = self._tool_executor.submit(self._call_tool, event)
                    self._callbacks.add(callback)
                    callback.add_done_callback(self._tool_done)
                return
            # Permissions must be supplied by the trusted launcher, never auto-approved here.
            raise SessionTransportError(f"unsupported Codex callback: {event['method']}")
        params = event.get("params") or {}
        if params.get("threadId") != self._session_id:
            return
        with self._condition:
            if not self._receiving:
                return
            if event.get("method") == "turn/started":
                self._announced_turn_id = params["turn"]["id"]
            if event.get("method") in {"turn/completed", "item/completed", "thread/tokenUsage/updated"}:
                self._output_bytes += len(json.dumps(event).encode("utf-8"))
                if self._output_bytes > PROCESS_OUTPUT_LIMIT_BYTES:
                    raise SessionTransportError("native session invocation exceeds output limit")
            if event.get("method") == "turn/completed":
                turn = params["turn"]
                self._completed[turn["id"]] = turn
            elif event.get("method") == "item/completed":
                item = params["item"]
                if item.get("type") == "agentMessage":
                    self._messages.setdefault(params["turnId"], []).append(item["text"])
            elif event.get("method") == "thread/tokenUsage/updated":
                if isinstance(params.get("turnId"), str):
                    self._usage[params["turnId"]] = params.get("tokenUsage") or {}
            self._condition.notify_all()

    def _tool_done(self, callback: Future) -> None:
        with self._condition:
            self._callbacks.discard(callback)
            if not callback.cancelled() and (error := callback.exception()) is not None:
                self.rpc.fail(error)
            self._condition.notify_all()

    def _call_tool(self, event: dict[str, Any]) -> None:
        params = event.get("params") or {}
        try:
            with self._condition:
                turn_id = self._turn_id or self._announced_turn_id
                if (
                    not self._receiving or turn_id is None
                    or params.get("threadId") != self._session_id
                    or params.get("turnId") != turn_id
                ):
                    raise PermissionError("native tool callback does not belong to the active invocation")
                if self._tool_bridge is None or params.get("tool") not in self._tool_names:
                    raise PermissionError("native tool callback is not an admitted tool")
                if params.get("namespace") is not None or not isinstance(params.get("arguments"), dict):
                    raise ValueError("native tool callback requires an unnamespaced tool and object arguments")
                call_id = params.get("callId")
                if not isinstance(call_id, str) or not call_id:
                    raise ValueError("native tool callback requires callId")
                previous = self._tool_calls.get(call_id)
                if previous is not None and previous[0] != params:
                    raise PermissionError("native tool callback reused callId with different input")
            if previous is not None:
                result = previous[1]
            else:
                try:
                    value = self._tool_bridge.call(params["tool"], params["arguments"])
                    if self._on_tool_result is not None:
                        try:
                            self._on_tool_result(
                                params["tool"], value, self._session_id, turn_id, call_id,
                                max(0.0, self._deadline - time.monotonic()),
                            )
                        except Exception:
                            logging.getLogger(__name__).exception(
                                "candidate replay capture failed"
                            )
                    result = {"success": True, "contentItems": [
                        {"type": "inputText", "text": json.dumps(value, ensure_ascii=False)},
                    ]}
                except Exception as exc:
                    result = {"success": False, "contentItems": [
                        {"type": "inputText", "text": str(exc)},
                    ]}
                with self._condition:
                    self._output_bytes += len(json.dumps(result).encode("utf-8"))
                    if self._output_bytes > PROCESS_OUTPUT_LIMIT_BYTES:
                        raise SessionTransportError("native session invocation exceeds output limit")
                    self._tool_calls[call_id] = (params, result)
        except SessionTransportError:
            raise
        except (PermissionError, ValueError) as exc:
            result = {"success": False, "contentItems": [{"type": "inputText", "text": str(exc)}]}
        self.rpc.send({"id": event["id"], "result": result}, timeout=self._deadline - time.monotonic())

    def run(
        self, prompt: str, *, timeout: float, on_started: Callable[[], None] | None = None,
    ) -> SessionInvocationResult:
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("native session already has an active invocation")
        started = time.monotonic()
        started_at = datetime.now(timezone.utc).isoformat()
        deadline = started + timeout
        try:
            with self._condition:
                self._completed.clear()
                self._messages.clear()
                self._usage = {}
                self._output_bytes = 0
                self._tool_calls.clear()
                self._announced_turn_id = None
                self._deadline = deadline
                self._receiving = True
            params: dict[str, Any] = {
                "threadId": self._session_id, "input": [{"type": "text", "text": prompt}],
            }
            if self.config.reasoning_effort is not None:
                params["effort"] = self.config.reasoning_effort
            turn = self._request("turn/start", params, timeout=timeout)["turn"]
            with self._condition:
                self._turn_id = turn["id"]
                if self._announced_turn_id is not None and self._announced_turn_id != self._turn_id:
                    raise SessionTransportError("Codex announced a different invocation")
                self._condition.notify_all()
                if on_started is not None:
                    on_started()
                while self._turn_id not in self._completed or self._callbacks:
                    self.rpc.check()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("native session invocation timed out")
                    self._condition.wait(min(remaining, PROCESS_POLL_SECONDS))
                self.rpc.check()
                completed = self._completed.pop(self._turn_id)
                state = completed["status"]
                if state not in {"completed", "interrupted", "failed"}:
                    raise SessionTransportError(f"unexpected Codex turn state: {state}")
                if self._on_turn_completed is not None:
                    try:
                        self._on_turn_completed(self._session_id, self._turn_id, state == "completed")
                    except Exception:
                        logging.getLogger(__name__).exception("candidate replay finalization failed")
                total = self._usage.get(self._turn_id, {}).get("total")
                usage = {}
                if isinstance(total, dict):
                    delta = {
                        key: value - self._usage_baseline.get(key, 0)
                        for key, value in total.items()
                        if isinstance(value, int) and not isinstance(value, bool)
                    }
                    required = {"inputTokens", "cachedInputTokens", "outputTokens", "totalTokens"}
                    if (
                        required <= delta.keys() and all(value >= 0 for value in delta.values())
                        and delta["inputTokens"] >= delta["cachedInputTokens"]
                    ):
                        usage = {
                            "scope": "since_last_observation" if self._usage_baseline or not self.config.native_session_id
                            else "session_total_without_baseline",
                            "input": delta["inputTokens"] - delta["cachedInputTokens"],
                            "cacheRead": delta.get("cachedInputTokens"),
                            "output": delta.get("outputTokens"),
                            "totalTokens": delta.get("totalTokens"),
                            "reasoningOutput": delta.get("reasoningOutputTokens"),
                        }
                    self._usage_baseline = dict(total)
                return SessionInvocationResult(
                    self._session_id, self._turn_id, state,
                    "\n".join(self._messages.pop(self._turn_id, [])), usage,
                    str(completed["error"]) if completed.get("error") else None,
                    SessionInvocationObservation(
                        started_at, datetime.now(timezone.utc).isoformat(), time.monotonic() - started,
                        metrics_baseline=dict(self._usage_baseline),
                    ),
                    failure=normalize_model_failure(
                        completed.get("error"), phase="response",
                        model=self.config.model,
                        provider=model_identity(self.config.model)[0],
                        source="codex_app_server",
                    ),
                    agent_harness="codex", runtime_provider="direct",
                )
        except BaseException:
            self.close()
            raise
        finally:
            with self._condition:
                self._turn_id = None
                self._receiving = False
                self._condition.notify_all()
            self._run_lock.release()

    def _active_turn(self, timeout: float) -> str | None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._run_lock.locked() and self._turn_id is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Codex turn identity is not yet known")
                self._condition.wait(min(remaining, PROCESS_POLL_SECONDS))
            return self._turn_id

    def message(self, prompt: str, *, timeout: float) -> None:
        turn = self._active_turn(timeout)
        if turn is None:
            raise RuntimeError("messages require an active invocation; use run for an idle session")
        self._request("turn/steer", {
            "threadId": self._session_id, "expectedTurnId": turn,
            "input": [{"type": "text", "text": prompt}],
        }, timeout=timeout)

    def fork(self, request: ProviderDispatchRequest, *, timeout: float = 60.0) -> ProviderDispatchReceipt:
        """Create a branch from a completed turn without changing this session."""
        if request.checkpoint.parent_native_session_id != self._session_id:
            raise PermissionError("fork checkpoint does not belong to this session")
        if self._run_lock.locked():
            raise RuntimeError("fork requires an idle parent session")
        params: dict[str, Any] = {
            "threadId": self._session_id,
            "lastTurnId": request.checkpoint.checkpoint_id,
            "cwd": str(request.workspace),
            "approvalPolicy": "never",
            "sandbox": "read-only",
            "config": {"sandbox_mode": "read-only"},
        }
        if self.config.model is not None:
            params["model"] = self.config.model
        result = self._request("thread/fork", params, timeout=timeout)
        branch_id = str((result.get("thread") or {}).get("id") or "")
        if not branch_id or branch_id == self._session_id:
            raise SessionTransportError("fork returned an invalid branch identity")
        return ProviderDispatchReceipt(
            provider_session_id=branch_id,
            parent_native_session_id=self._session_id,
            workspace=request.workspace,
            launch_state="started",
            started_at=datetime.now(timezone.utc).isoformat(),
            checkpoint_id=request.checkpoint.checkpoint_id,
            checkpoint_kind=request.checkpoint.checkpoint_kind,
        )

    def interrupt(self, *, timeout: float) -> None:
        turn = self._active_turn(timeout)
        if turn is not None:
            self._request("turn/interrupt", {
                "threadId": self._session_id, "turnId": turn,
            }, timeout=timeout)

    def close(self) -> None:
        with self._condition:
            self._closing = True
        self._tool_executor.shutdown(wait=False, cancel_futures=True)
        self.rpc.close()
        deadline = time.monotonic() + self.rpc.managed.kill_timeout
        with self._condition:
            while self._callbacks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SessionTransportError("native tool callbacks did not settle during close")
                self._condition.wait(remaining)


class PiSession:
    def __init__(
        self, config: NativeSessionInput, registration: ProcessRegistration, *, timeout: float,
        resume_from: "PiSession | None" = None, execution_lock: Path | None = None,
    ) -> None:
        if config.agent_harness != "pi" or config.runtime_provider != "direct":
            raise ValueError("Pi session requires the Pi host")
        if resume_from is not None and config.native_session_id != resume_from.native_session_id:
            raise ValueError("Pi continuation must preserve the native session identity")
        self.config = config
        self._registration = registration
        self._run_lock = threading.Lock()
        self._condition = threading.Condition()
        self._settled = False
        self._started = False
        self._receiving = False
        self._accepted = False
        self._event_revision = 0
        self._control_lock = threading.Lock()
        self._interrupted = False
        self._error: str | None = None
        self._metrics_baseline = dict(resume_from._metrics_baseline if resume_from else config.metrics_baseline)
        command = list(config.process.command)
        if config.model is not None:
            command.extend(["--model", config.model])
        if config.instructions is not None:
            command.extend(["--append-system-prompt", config.instructions])
        self.rpc = SessionRpcProcess(
            replace(config.process, command=tuple(command)), registration, self._event,
            execution_lock=execution_lock,
        )
        deadline = time.monotonic() + timeout
        try:
            state = self._command("get_state", timeout=deadline - time.monotonic())["data"]
            self._session_id = str(state["sessionId"])
            if config.native_session_id and self._session_id != config.native_session_id:
                raise SessionTransportError("Pi resumed a different native session")
            if config.reasoning_effort is not None:
                self._command("set_thinking_level", timeout=deadline - time.monotonic(), level=config.reasoning_effort)
        except BaseException:
            self.close()
            raise

    @property
    def native_session_id(self) -> str:
        return self._session_id

    def _command(self, command: str, *, timeout: float, **params: Any) -> dict[str, Any]:
        response = self.rpc.request({"type": command, **params}, timeout=timeout)
        if response.get("success") is not True:
            raise SessionRequestRejected(f"{command}: {response.get('error')}")
        return response

    def _event(self, event: dict[str, Any]) -> None:
        with self._condition:
            if not self._receiving:
                return
            self._event_revision += 1
            if event.get("type") == "agent_start":
                self._started = True
                self._settled = False
            elif event.get("type") == "agent_settled" and self._started:
                self._settled = True
            elif event.get("type") == "message_end":
                message = event.get("message") or {}
                if message.get("role") == "assistant":
                    self._error = message.get("errorMessage")
            self._condition.notify_all()

    def run(
        self, prompt: str, *, timeout: float, on_started: Callable[[], None] | None = None,
    ) -> SessionInvocationResult:
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("native session already has an active invocation")
        started = time.monotonic()
        started_at = datetime.now(timezone.utc).isoformat()
        deadline = started + timeout
        try:
            with self._condition:
                self._settled = False
                self._interrupted = False
                self._error = None
                self._started = False
                self._receiving = True
                self._accepted = False
            with self._control_lock:
                response = self._command("prompt", message=prompt, timeout=timeout)
                self._accepted = True
                if on_started is not None:
                    on_started()
            while True:
                with self._condition:
                    while not self._settled:
                        self.rpc.check()
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("native session invocation timed out")
                        self._condition.wait(min(remaining, PROCESS_POLL_SECONDS))
                    observed_revision = self._event_revision
                state = self._command("get_state", timeout=deadline - time.monotonic())["data"]
                if not (state.get("isStreaming") or state.get("isCompacting") or state.get("pendingMessageCount")):
                    break
                with self._condition:
                    if self._event_revision == observed_revision:
                        self._settled = False
            text = self._command("get_last_assistant_text", timeout=deadline - time.monotonic())["data"]
            ended_at = datetime.now(timezone.utc).isoformat()
            duration = time.monotonic() - started
            metrics = collect_pi_metrics(
                lambda payload, *, timeout: self._command(
                    payload["type"], timeout=min(timeout, deadline - time.monotonic()),
                    **{key: value for key, value in payload.items() if key != "type"},
                ),
                session_id=self._session_id, metrics_baseline=self._metrics_baseline,
                last_state_data=state, started_at=started_at, ended_at=ended_at,
                duration_seconds=duration,
            )
            self._metrics_baseline = {
                "last_entry_id": metrics["final_last_entry_id"],
                "entry_count": metrics["final_entry_count"],
                "usage_total": metrics["usage_total"] if metrics["usage_total"] is not None
                else self._metrics_baseline.get("usage_total"),
                "duration_seconds": metrics["duration_seconds"],
                "started_at": metrics["started_at"],
                "runtime_start": metrics["runtime_start"],
            }
            return SessionInvocationResult(
                self._session_id, response["id"],
                "interrupted" if self._interrupted else "failed" if self._error else "completed",
                str(text.get("text") or ""),
                {"scope": metrics["usage_delta_scope"], **metrics["usage_delta"]} if metrics["usage_delta"] is not None else {},
                self._error,
                SessionInvocationObservation(started_at, ended_at, duration, metrics, dict(self._metrics_baseline)),
                failure=normalize_model_failure(
                    self._error, phase="response", model=self.config.model,
                    provider=model_identity(self.config.model)[0], source="pi_rpc",
                ),
                agent_harness="pi", runtime_provider="direct",
            )
        except BaseException:
            self.close()
            raise
        finally:
            with self._control_lock:
                with self._condition:
                    self._receiving = False
                    self._accepted = False
                self._run_lock.release()

    def message(self, prompt: str, *, timeout: float) -> None:
        with self._control_lock:
            if not self._accepted or self._settled:
                raise RuntimeError("messages require an active invocation; use run for an idle session")
            self._command("steer", message=prompt, timeout=timeout)

    def fork(self, request: ProviderDispatchRequest, *, timeout: float = 60.0) -> ProviderDispatchReceipt:
        """Fork the current Pi session through Pi's stable SessionManager API.

        Pi RPC intentionally does not expose a ``fork_from`` command.  The
        process reports its persisted session file, while the host invokes the
        installed Pi package's ``SessionManager.forkFrom`` to create a new
        session with the target cwd and the candidate's durable identity.
        """
        if request.checkpoint.parent_native_session_id != self._session_id:
            raise PermissionError("fork checkpoint does not belong to this session")
        if self._run_lock.locked():
            raise RuntimeError("fork requires an idle parent session")
        state = self._command("get_state", timeout=timeout).get("data") or {}
        source_path = state.get("sessionFile")
        if not isinstance(source_path, str) or not source_path:
            raise SessionTransportError("Pi fork source session file is unavailable")
        session_dir = self._session_dir()
        branch_id = str(request.worker_identity.get("agent_session_id") or "")
        if not branch_id:
            raise SessionTransportError("Pi fork requires the candidate session identity")
        helper = self.config.configuration.get("fork_helper")
        if not isinstance(helper, str) or not Path(helper).is_file():
            raise SessionTransportError("Pi fork helper is unavailable")
        payload = _fork_pi_session_file(
            source_path=Path(source_path), target_cwd=request.workspace, session_dir=session_dir,
            session_id=branch_id, parent_session_id=self._session_id,
            checkpoint_id=request.checkpoint.checkpoint_id, helper=Path(helper), timeout=timeout,
            process_input=self.config.process, registration=self._registration,
        )
        if not branch_id or branch_id == self._session_id:
            raise SessionTransportError("Pi fork adapter returned an invalid branch identity")
        return ProviderDispatchReceipt(
            provider_session_id=branch_id,
            parent_native_session_id=self._session_id,
            workspace=request.workspace,
            launch_state="started",
            started_at=datetime.now(timezone.utc).isoformat(),
            context_projection_digest=payload["contextProjectionDigest"],
            native_session_file=payload["sessionFile"],
            checkpoint_id=request.checkpoint.checkpoint_id,
            checkpoint_kind=request.checkpoint.checkpoint_kind,
        )

    def _session_dir(self) -> Path:
        command = self.config.process.command
        try:
            index = command.index("--session-dir")
            return Path(command[index + 1]).resolve()
        except (ValueError, IndexError):
            state = self._command("get_state", timeout=10.0).get("data") or {}
            session_file = state.get("sessionFile")
            if isinstance(session_file, str) and session_file:
                return Path(session_file).resolve().parent
            raise SessionTransportError("Pi fork session directory is unavailable")

    def interrupt(self, *, timeout: float) -> None:
        with self._control_lock:
            if not self._accepted or self._settled:
                return
            self._interrupted = True
            self._command("abort", timeout=timeout)
            state = self._command("get_state", timeout=timeout)["data"]
            if state.get("pendingMessageCount"):
                self.close()
                raise SessionTransportError("Pi retained queued messages after abort; process closed")

    def close(self) -> None:
        self.rpc.close()
