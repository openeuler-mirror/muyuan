"""Candidate use cases and scope-local resource journals for the TT session actor."""

import base64
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
import json
from pathlib import Path

from pydantic import TypeAdapter

from goal_plus.domain.evidence import CandidateAttemptAccepted, CandidateAttemptFeedback
from goal_plus.execution.thinkthread_child import ChildDriver
from goal_plus.execution.thinkthread_session import ThinkThreadSession
from goal_plus.host_scope import execution_scope_admission
from goal_plus.host_services import SearchHostService
from goal_plus.infrastructure.clock import SystemClock
from goal_plus.infrastructure.state.files import exclusive_file_lock, load_json, write_json
from goal_plus.infrastructure.thinkthread_worker_rpc import receive_worker_messages
from goal_plus.infrastructure.session_diagnostics import collect_child_diagnostics
from goal_plus.ports.thinkthread_session import ThinkThreadSessionHooks, ThinkThreadSessionInput
from goal_plus.ports.session import SessionCallRequest, SessionInvocationResult
from goal_plus.thinkthread_agent_posix import AgentPosixSdkClient, validate_thinkthread_root


def session_resource_path(root: Path, scope_id: str, identity: str) -> Path:
    from goal_plus.ports.scope import validate_scope_id

    return root / "host-scopes" / validate_scope_id(scope_id) / "thinkthread-sessions" / (validate_scope_id(identity) + ".json")


def _persist_resource(path, record):
    with exclusive_file_lock(path.with_suffix(".lock")):
        if path.exists() and load_json(path).get("cleanup_state") == "closed":
            record["cleanup_state"] = "closed"
        write_json(path, record)


class ThinkThreadCandidateSession:
    def __init__(self, host, identity: str, caller_id: str, scope_id: str) -> None:
        self.host, self.identity, self.caller_id, self.scope_id = host, identity, caller_id, scope_id
        self.service = SearchHostService(host.root, runtime=host.runtime)
        prepared = self.service.prepare_thinkthread_session(identity)
        self.session = prepared.session
        self.client = AgentPosixSdkClient()
        root = validate_thinkthread_root(self.client)
        if root.get("thinkthreadId") != prepared.root_thinkthread_id:
            raise PermissionError("ThinkThread session Root differs from its frozen source")
        self.path = session_resource_path(host.root, scope_id, identity)
        self.record = {
            "run_id": self.session.run_id, "candidate_id": self.session.candidate_id,
            "agent_session_id": identity, "scope_id": scope_id,
            "root_thinkthread_id": prepared.root_thinkthread_id,
            "baseline_snapshot_id": prepared.baseline_snapshot_id,
            "model": self.session.launch.get("model"), "status": "prepared",
        }
        self.inner_git_binding = prepared.inner_git_binding
        clock = SystemClock()
        self._lease = exclusive_file_lock(self.path.with_suffix(".controller.lock"), blocking=False)
        try:
            self._lease.__enter__()
        except BlockingIOError as exc:
            raise RuntimeError("ThinkThread session already has a live controller") from exc
        try:
            with self._admission():
                if self.path.exists():
                    saved = load_json(self.path)
                    for field in ("run_id", "candidate_id", "agent_session_id", "scope_id", "root_thinkthread_id", "baseline_snapshot_id", "model"):
                        if saved.get(field) != self.record.get(field):
                            raise PermissionError("ThinkThread session resource identity changed")
                    if saved.get("cleanup_state") == "closed":
                        raise RuntimeError("ThinkThread session resources have been closed")
                    if self.session.session_handle.external_id is not None and saved.get("thinkthread_id") != self.session.session_handle.external_id:
                        raise PermissionError("ThinkThread resource no longer belongs to this domain session")
                    if saved.get("thinkthread_id") is not None:
                        child = ChildDriver(self.client, clock).observe(saved["thinkthread_id"],
                            root_id=saved["root_thinkthread_id"], branch_id=saved["fs_branch_id"])
                        if child is None:
                            raise RuntimeError("ThinkThread retained session Child is missing")
                    if saved.get("status") != "prepared" and not isinstance(saved.get("call_request"), dict):
                        raise RuntimeError("ThinkThread session has no exact controller call identity")
                    if saved.get("call_request") is not None:
                        TypeAdapter(SessionCallRequest).validate_python(saved["call_request"])
                    self.record = saved
                else:
                    if self.session.session_handle.external_id is not None:
                        raise RuntimeError("ThinkThread bound session has no exact resource journal")
                    self._persist(self.record)
        except BaseException:
            self._lease.__exit__(None, None, None)
            raise
        self.actor = ThinkThreadSession(
            ThinkThreadSessionInput(prepared.root_thinkthread_id, prepared.baseline_snapshot_id, self.record["model"]),
            ChildDriver(self.client, clock), clock, self.record,
            ThinkThreadSessionHooks(self._admission, self._persist, self._bind, self._receive, self._settle),
            domain_bound=self.session.session_handle.external_id is not None,
        )

    @contextmanager
    def _admission(self):
        self.session = self.host.admit(
            self.identity, self.caller_id, execution=True, scope_id=self.scope_id,
            native_session_id=self.record.get("thinkthread_id") if self.session.session_handle.external_id is not None else None,
        )
        with execution_scope_admission(
            root_dir=self.host.root, scope_id=self.scope_id, run_id=self.session.run_id,
        ):
            yield

    def _persist(self, record):
        _persist_resource(self.path, record)

    def _bind(self, binding):
        self.service.bind_thinkthread_candidate(
            run_id=self.session.run_id, candidate_id=self.session.candidate_id,
            agent_session_id=self.identity, thinkthread_id=binding.child_id, branch_id=binding.branch_id,
        )
        self.session = self.host.runtime.candidate_sessions.session(self.identity)

    def _receive(self, record):
        receive_worker_messages(self.client, self.host.root, record, persist=lambda: self._persist(record))

    def _settle(self, record):
        accepted = CandidateAttemptAccepted.model_validate(record.get("attempt_request"))
        report = self.service.verify_thinkthread_candidate_attempt(
            run_id=self.session.run_id, candidate_id=self.session.candidate_id,
            agent_session_id=self.identity, thinkthread_id=record["thinkthread_id"],
            branch_id=record["fs_branch_id"], hypothesis=accepted.hypothesis,
            attempt_id=accepted.attempt_id, inner_git_attempt=record.get("inner_git_attempt"),
            client=self.client, toolization_decision=accepted.toolization_decision,
        )
        artifact = report.workspace_artifact_after_settlement
        if artifact is None or report.attempt_artifact_ref is None or report.attempt_iteration is None:
            raise RuntimeError("ThinkThread verifier omitted its exact attempted and settled artifacts")
        if artifact != report.attempt_artifact_ref:
            self.actor.driver.reset_branch(
                record["thinkthread_id"], root_id=record["root_thinkthread_id"],
                branch_id=record["fs_branch_id"], snapshot_id=artifact.id,
            )
        feedback = CandidateAttemptFeedback(
            attempt_id=accepted.attempt_id, request_id=accepted.request_id,
            run_id=self.session.run_id, candidate_id=self.session.candidate_id,
            agent_session_id=self.identity, attempt_iteration=report.attempt_iteration,
            score_report=report, attempt_artifact_ref=report.attempt_artifact_ref,
            workspace_artifact_after_attempt=artifact, verified_at=SystemClock().timestamp(),
        )
        record["attempt_result"] = feedback.model_dump(mode="json")
        self._persist(record)
        try:
            from goal_plus.thinkthread_annotation import kick_thinkthread_annotation

            kick_thinkthread_annotation(root_dir=self.host.root, run_id=self.session.run_id, client=self.client)
            self.host.wake_internal_agents(self.session.run_id)
        except Exception as exc:
            record["annotation_host_error"] = {"type": type(exc).__name__, "message": str(exc)}
            self._persist(record)
        return feedback.model_dump_json()

    @property
    def native_session_id(self):
        return self.actor.native_session_id

    def admit_call(self, request: SessionCallRequest):
        with self._admission():
            if self.record.get("status") in {"running", "needs_recovery"}:
                raise RuntimeError("ThinkThread invocation requires recovery of its original call")
            if self.record.get("invocation_result") is not None:
                self._record_saved_result()
            self.record["call_request"] = asdict(request)
            self.record["call_dispatch_index"] = int(self.record.get("dispatch_index", 0)) + 1
            self._persist(self.record)

    def restore_control(self, control):
        try:
            if self.record.get("call_request") is not None:
                request = TypeAdapter(SessionCallRequest).validate_python(self.record["call_request"])
                completed = self.record.get("invocation_result") is not None and (
                    self.record["call_dispatch_index"] == self.record.get("dispatch_index")
                )
                observation = self._record_saved_result() if completed else self._resume
                control.restore_call(request, observation)
        except BaseException:
            self._lease.__exit__(None, None, None)
            raise

    def _record_saved_result(self):
        result = TypeAdapter(SessionInvocationResult).validate_python(self.record["invocation_result"])
        if result.native_session_id != self.native_session_id or result.invocation_id != self.record.get("active_message_id"):
            raise PermissionError("ThinkThread saved result belongs to another invocation")
        child = self.actor.driver.observe(self.native_session_id,
            root_id=self.record["root_thinkthread_id"], branch_id=self.record["fs_branch_id"])
        if child is None or self.actor.driver.turn_terminal(child, result.invocation_id) is None:
            raise RuntimeError("ThinkThread saved invocation is no longer the exact quiescent native turn")
        self.host.runtime.candidate_sessions.record_native_invocation(self.identity, result)
        return result

    def _resume(self, on_started):
        with exclusive_file_lock(self.path.with_suffix(".invocation.lock"), blocking=False):
            with self._admission():
                if self.record["call_dispatch_index"] != self.record.get("dispatch_index"):
                    raise RuntimeError("ThinkThread prior controller exited before submitting this invocation")
            if self.record.get("invocation_result") is not None:
                on_started()
                return self._record_saved_result()
            result = self.actor.resume(on_started=on_started)
            self.host.runtime.candidate_sessions.record_native_invocation(self.identity, result)
            return result

    def run(self, prompt: str, *, timeout: float, on_started=None):
        with exclusive_file_lock(self.path.with_suffix(".invocation.lock"), blocking=False):
            return self._run(prompt, timeout=timeout, on_started=on_started)

    def _run(self, prompt: str, *, timeout: float, on_started=None):
        self.host.admit(self.identity, self.caller_id, execution=True, scope_id=self.scope_id,
                        native_session_id=self.native_session_id)
        if self.record.get("status") == "needs_recovery":
            raise RuntimeError("ThinkThread unfinished invocation requires explicit recovery")
        effective = self.host.runtime.candidate_sessions.invocation_timeout(self.identity, timeout)
        if self.record.get("dispatch_index", 0):
            feedback = (CandidateAttemptFeedback.model_validate(self.record["attempt_result"])
                        if self.record.get("attempt_result") is not None else None)
            contract = self.service.prepare_thinkthread_continuation(
                run_id=self.session.run_id, candidate_id=self.session.candidate_id,
                agent_session_id=self.identity, attempt_feedback=feedback,
            ).message
        else:
            contract = self.session.launch["message"]
        if self.inner_git_binding is not None:
            encoded = base64.urlsafe_b64encode(json.dumps(self.inner_git_binding).encode()).decode().rstrip("=")
            contract += f"\ngoal_plus_inner_git_binding={encoded}\n"
        result = self.actor.run(contract + "\n\n" + prompt, timeout=effective, on_started=on_started)
        self.host.runtime.candidate_sessions.record_native_invocation(self.identity, result)
        return result

    def message(self, prompt: str, *, timeout: float):
        self.actor.message(prompt, timeout=timeout)

    def interrupt(self, *, timeout: float):
        self.actor.interrupt(timeout=timeout)

    def stop(self):
        self.actor.close()

    def reopen(self):
        with self._admission():
            if load_json(self.path).get("cleanup_state") == "closed":
                raise RuntimeError("ThinkThread session resources have been closed")
            self.actor.reopen()

    def close(self):
        self.actor.close()
        release_session_resource(self.path, client=self.client)
        self._lease.__exit__(None, None, None)


def release_session_resource(path: Path, *, client=None, timeout: float = 5.0):
    with exclusive_file_lock(path.with_suffix(".cleanup.lock")):
        try:
            with exclusive_file_lock(path.with_suffix(".invocation.lock"), blocking=False):
                return _release_session_resource(path, client=client, timeout=timeout)
        except BlockingIOError as exc:
            raise RuntimeError("ThinkThread invocation callback is still active; retry resource cleanup after fencing") from exc


def _release_session_resource(path: Path, *, client, timeout: float):
    record = load_json(path)
    if record.get("cleanup_state") == "closed":
        return {"active_count": 0, "state": "closed"}
    sdk = client or AgentPosixSdkClient()
    root = sdk.invoke("self")
    if root.get("parentThinkthreadId") is not None or root.get("thinkthreadId") != record["root_thinkthread_id"]:
        raise PermissionError("ThinkThread session cleanup Root ownership mismatch")
    clock = SystemClock()
    driver = ChildDriver(sdk, clock)
    actor = ThinkThreadSession(
        ThinkThreadSessionInput(record["root_thinkthread_id"], record["baseline_snapshot_id"], record.get("model")),
        driver, clock, record,
        ThinkThreadSessionHooks(nullcontext, lambda value: _persist_resource(path, value), lambda _: None,
                               lambda _: None, lambda _: ""),
    )
    actor.close()
    if actor.native_session_id is not None:
        collect_child_diagnostics(path.parents[3], record, sdk)
        driver.release(actor.native_session_id, root_id=record["root_thinkthread_id"],
                       branch_id=record["fs_branch_id"], timeout=timeout)
    record["cleanup_state"] = "closed"
    _persist_resource(path, record)
    return {"active_count": 0, "state": "closed"}
