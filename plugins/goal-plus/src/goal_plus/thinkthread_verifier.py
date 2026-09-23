"""Host-owned, single-assessment ThinkThread Child, using the shared Message actor."""

from contextlib import contextmanager
import json
import math
from pathlib import Path
import threading
import uuid

from goal_plus.execution.thinkthread_child import ChildDriver
from goal_plus.execution.thinkthread_rpc import (
    PROTOCOL, RESPONSE_RETRY_SECONDS, ReliableRpcChannel, message_payload,
    request_hash, response_hash,
)
from goal_plus.execution.thinkthread_session import ThinkThreadSession
from goal_plus.host_scope import internal_agent_run_admission
from goal_plus.infrastructure.clock import SystemClock
from goal_plus.infrastructure.session_diagnostics import archive_diagnostic_message, collect_child_diagnostics
from goal_plus.infrastructure.state.files import load_json, write_json
from goal_plus.llm_verifier import VerifierExecutionUnavailable
from goal_plus.ports.inference import InferenceInvocation, StructuredInferenceResult
from goal_plus.ports.process import ProcessOutcome
from goal_plus.ports.thinkthread import AgentPosixBridgeError
from goal_plus.ports.thinkthread_session import ThinkThreadSessionHooks, ThinkThreadSessionInput
from goal_plus.thinkthread_agent_posix import AgentPosixSdkClient, validate_thinkthread_root


VERIFIER_TOOLS = frozenset({
    "goal_plus_thinkthread_verifier_input", "goal_plus_thinkthread_verifier_submit",
})


class VerifierRpc:
    """Exact Child/session binding, immutable input and idempotent result delivery."""

    def __init__(self, root, client, clock, record, request, validate, admission, persist):
        self.root, self.client, self.clock = root, client, clock
        self.record, self.request, self.validate = record, request, validate
        self.admission, self.persist = admission, persist
        self.channel = ReliableRpcChannel(client, clock)

    def receive(self, record):
        self.channel.receive(record, self.process, lambda: self.persist(record))
        for key, pending in list(record.get("pending_responses", {}).items()):
            if self.clock.now() >= pending["expires_at_unix"]:
                del record["pending_responses"][key]
            elif self.clock.now() - pending.get("last_attempt_unix", 0) >= RESPONSE_RETRY_SECONDS:
                self.channel.send_response(record, key)
        self.persist(record)

    def process(self, message):
        state = self.record
        if archive_diagnostic_message(self.root, state, message):
            return
        payload = message_payload(message)
        if not payload or payload.get("protocol") != PROTOCOL:
            return
        kind = payload.get("type")
        if kind == "registration":
            if payload.get("registration_nonce") != state["registration_nonce"]:
                raise PermissionError("verifier Child registration mismatch")
            state["registered"] = True
            return
        if kind in {"dispatch_ack", "dispatch_error"}:
            if payload.get("dispatch_nonce") != state["dispatch_nonce"]:
                raise PermissionError("verifier dispatch nonce mismatch")
            if kind == "dispatch_error":
                state["dispatch_error"] = payload
            else:
                message_id = payload.get("message_id")
                if not isinstance(message_id, str) or not message_id or state.get("active_message_id") not in {None, message_id}:
                    raise PermissionError("verifier dispatch Message mismatch")
                state.setdefault("acknowledged_messages", {})[state["dispatch_nonce"]] = message_id
            return
        if kind == "response_ack":
            key = payload.get("request_id")
            settled = state.get("settled_requests", {}).get(key)
            if settled is None or payload.get("response_sha256") != response_hash(settled["response"]):
                raise PermissionError("verifier response acknowledgement mismatch")
            state.setdefault("pending_responses", {}).pop(key, None)
            return
        if kind != "request" or not state.get("registered"):
            raise PermissionError("unregistered verifier RPC request")
        if payload.get("agent_session_id") != state["agent_session_id"]:
            raise PermissionError("verifier RPC session mismatch")
        key, tool, params = payload.get("request_id"), payload.get("tool"), payload.get("params")
        if not isinstance(key, str) or not key or tool not in VERIFIER_TOOLS or not isinstance(params, dict):
            raise PermissionError("verifier RPC tool or request is not allowed")
        digest = request_hash(payload)
        if payload.get("content_sha256") != digest:
            raise ValueError("verifier RPC content hash mismatch")
        deadline = payload.get("deadline_unix_ms")
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline) or deadline <= 0:
            raise ValueError("verifier RPC requires a finite deadline")
        settled = state.setdefault("settled_requests", {})
        prior = settled.get(key)
        if prior is not None and prior["request_hash"] != digest:
            raise ValueError("verifier RPC request identity was reused")
        if prior is None:
            try:
                with self.admission():
                    if self.clock.now() * 1000 >= deadline:
                        raise TimeoutError("verifier RPC deadline elapsed")
                    result = self.dispatch(tool, params)
                response = {"ok": True, "result": result}
            except (ValueError, PermissionError, RuntimeError, TimeoutError) as exc:
                response = {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}
            settled[key] = {"request_hash": digest, "response": response}
        response = settled[key]["response"]
        state.setdefault("pending_responses", {}).setdefault(key, {
            "reply_to_message_id": message.get("messageId"),
            "response_sha256": response_hash(response), "expires_at_unix": deadline / 1000,
        })
        self.persist(state)
        if self.clock.now() * 1000 < deadline:
            self.channel.send_response(state, key)

    def dispatch(self, tool, params):
        if tool == "goal_plus_thinkthread_verifier_input":
            if params:
                raise ValueError("verifier input takes no arguments")
            return {"instructions": self.request.instructions, "prompt": self.request.prompt,
                    "output_schema": self.request.output_schema}
        if set(params) != {"result"}:
            raise ValueError("verifier submit requires exactly result")
        result = self.validate(params["result"]).model_dump(mode="json")
        prior = self.record.get("submitted_result")
        if prior is not None and prior != result:
            raise ValueError("verifier result is already submitted")
        self.record["submitted_result"] = result
        return {"accepted": True}


class ThinkThreadVerifier:
    def __init__(self, root_dir, *, client=None, clock=None):
        self.root = Path(root_dir)
        self.client = client or AgentPosixSdkClient()
        self.clock = clock or SystemClock()
        self.session = None
        self.stopped = threading.Event()

    def terminate(self):
        self.stopped.set()
        if self.session is not None:
            self.session.close()

    def execute(self, task, request, *, baseline_snapshot_id, is_active, validate, transcript=None):
        if request.runtime_provider != "thinkthread" or request.sandbox is not None:
            raise VerifierExecutionUnavailable("ThinkThread verifier must use Child execution")
        directory = self.root / "runs" / task.run_id / "verifier" / "executions"
        path = directory / f"{task.task_id}.json"
        for journal in directory.glob("*.json"):
            if load_json(journal).get("resource_state") != "released":
                raise VerifierExecutionUnavailable("previous verifier Child requires resource cleanup before another attempt")
        invocation_id = str(uuid.uuid4())
        started_at, started = self.clock.timestamp(), self.clock.monotonic()
        record = {
            "run_id": task.run_id, "candidate_id": task.candidate_id, "iteration": task.iteration,
            "task_id": task.task_id, "attempt": task.attempts, "settlement_id": task.subject.settlement_id,
            "role": "verifier", "agent_harness": request.agent_harness,
            "agent_session_id": f"verifier-{invocation_id}", "invocation_id": invocation_id,
            "resource_state": "preparing", "status": "new",
        }

        @contextmanager
        def admission():
            with internal_agent_run_admission(root_dir=self.root, run_id=task.run_id):
                if self.stopped.is_set() or not is_active():
                    raise PermissionError("verifier claim is no longer active")
                yield

        def persist(value):
            write_json(path, value)

        def settle(value):
            if "submitted_result" not in value:
                raise ValueError("ThinkThread verifier ended without a structured result")
            return json.dumps(value["submitted_result"], ensure_ascii=False)

        if transcript is not None:
            transcript.start(request, invocation_id, started_at, request.output_schema)
        try:
            with admission():
                try:
                    root = validate_thinkthread_root(self.client, agent_harness=request.agent_harness)
                except AgentPosixBridgeError as exc:
                    if exc.retryable is False:
                        raise VerifierExecutionUnavailable(str(exc)) from exc
                    raise
                except (RuntimeError, ValueError) as exc:
                    raise VerifierExecutionUnavailable(str(exc)) from exc
                record["root_thinkthread_id"] = root["thinkthreadId"]
                record["model"] = root.get("model")
                persist(record)
            driver = ChildDriver(self.client, self.clock, request.agent_harness)
            rpc = VerifierRpc(self.root, self.client, self.clock, record, request, validate, admission, persist)
            session = ThinkThreadSession(
                ThinkThreadSessionInput(root["thinkthreadId"], baseline_snapshot_id,
                                        agent_harness=request.agent_harness),
                driver, self.clock, record,
                ThinkThreadSessionHooks(admission, persist, lambda _: None, rpc.receive, settle),
            )
            self.session = session
            try:
                outcome = session.run(
                    "goal_plus_thinkthread_role=verifier; "
                    "Call goal_plus_thinkthread_verifier_input for this assessment's instructions, evidence "
                    "and schema. Submit exactly one result with goal_plus_thinkthread_verifier_submit, "
                    "then end the turn. No other tools are permitted.",
                    timeout=request.timeout_seconds,
                )
                if outcome.state != "completed":
                    raise RuntimeError(outcome.error or "ThinkThread verifier did not complete")
                with admission():
                    payload = json.loads(outcome.text)
            finally:
                try:
                    session.close()
                    if request.agent_harness == "pi":
                        collect_child_diagnostics(self.root, record, self.client)
                    if session.native_session_id is not None:
                        driver.release(session.native_session_id, root_id=root["thinkthreadId"],
                                       branch_id=record["fs_branch_id"], timeout=5.0)
                    record["resource_state"] = "released"
                    persist(record)
                except Exception as exc:
                    record["resource_state"] = "cleanup_failed"
                    record["cleanup_error"] = str(exc)
                    persist(record)
                    raise VerifierExecutionUnavailable("verifier Child cleanup is not confirmed") from exc
                finally:
                    self.session = None
            model = root.get("model")
            model_name = (f"{model['provider']}/{model['model']}"
                          if isinstance(model, dict) and model.get("provider") and model.get("model") else None)
            result = StructuredInferenceResult(
                process=ProcessOutcome("process_exited", "", ""), stop_detail=None, pid=None,
                returncode=0, output_text=outcome.text, payload=payload, output_error=None,
                assistant_message=None, usage=outcome.usage,
                invocation=InferenceInvocation(
                    invocation_id, request.agent_harness, model_name, None,
                    started_at, self.clock.timestamp(), max(0.0, self.clock.monotonic() - started),
                    runtime_provider="thinkthread", native_session_id=outcome.native_session_id,
                    native_invocation_id=outcome.invocation_id,
                ),
            )
            if transcript is not None:
                transcript.finish(result)
            return result
        except Exception as exc:
            if transcript is not None:
                transcript.failed(f"{type(exc).__name__}: {exc}")
                from goal_plus.infrastructure.agent_transcript import debug_export_transcripts

                debug_export_transcripts(self.root)
            raise
