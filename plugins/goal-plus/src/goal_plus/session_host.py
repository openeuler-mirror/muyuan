"""Native candidate launch/control assembly for a single host MCP service."""

from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
import threading
import time
import math
import logging

from goal_plus.application.candidate_delivery import CandidateDelivery
from goal_plus.bootstrap import build_scope_process_registry, build_scope_state
from goal_plus.candidate_host import candidate_host_queries
from goal_plus.domain.agents import AgentHarnessKind
from goal_plus.domain.adaptive.models import SteeringTarget
from goal_plus.domain.search import AgentSessionRecord
from goal_plus.execution.native_session import CodexSession, PiSession
from goal_plus.execution.scope import assert_scope_admission
from goal_plus.execution.session_control import NativeSessionControl
from goal_plus.execution.session_rpc import SessionRequestRejected
from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.host_scope import ensure_execution_scope, execution_scope_for_run, execution_scope_snapshot
from goal_plus.infrastructure.native_session_inputs import native_candidate_input
from goal_plus.infrastructure.state.files import exclusive_file_lock
from goal_plus.ports.allocation_apply import DispatchRejected
from goal_plus.ports.process import ProcessRegistration
from goal_plus.ports.scope import ScopeAttachment, ScopeRecord, SCOPE_PROCESS_TERMINAL_STATUSES
from goal_plus.ports.session import SessionOpenResult, SessionOperation
from goal_plus.runtime import FileSearchRuntime
from goal_plus.domain.serialization import canonical_json, sha256_text
from goal_plus.projections.session_tools import session_call_receipt


# Native initialization and MCP discovery, independently of a model invocation.
SESSION_START_TIMEOUT_SECONDS = 60.0
ALLOCATION_INTERRUPT_TIMEOUT_SECONDS = 30.0


_VERIFIER_QUEUES_LOCK = threading.RLock()
_VERIFIER_QUEUES: dict[tuple[Path, str], set[object]] = {}


def _register_verifier_queue(root: Path, run_id: str, queue: object) -> None:
    with _VERIFIER_QUEUES_LOCK:
        _VERIFIER_QUEUES.setdefault((root, run_id), set()).add(queue)


def _unregister_verifier_queue(root: Path, run_id: str, queue: object) -> None:
    with _VERIFIER_QUEUES_LOCK:
        queues = _VERIFIER_QUEUES.get((root, run_id))
        if queues is None:
            return
        queues.discard(queue)
        if not queues:
            _VERIFIER_QUEUES.pop((root, run_id), None)


def close_verifier_queue(*, root_dir: Path | str, run_id: str) -> None:
    """Close every in-process host queue for one run."""

    root = Path(root_dir).resolve()
    with _VERIFIER_QUEUES_LOCK:
        queues = list(_VERIFIER_QUEUES.get((root, run_id), ()))
    errors: list[Exception] = []
    for queue in queues:
        try:
            queue.close()
        except Exception as exc:  # cleanup continues for the remaining hosts
            errors.append(exc)
        finally:
            _unregister_verifier_queue(root, run_id, queue)
    if errors:
        raise RuntimeError(f"verifier queue cleanup failed for {len(errors)} resources") from errors[0]


@dataclass
class _HostedCandidate:
    caller_id: str
    run_id: str
    scope_id: str
    control: NativeSessionControl
    unavailable_calls: frozenset[str] = frozenset()


class _SteeringTransport:
    """A queue owner's exact controller capability, without policy or retry logic."""

    def __init__(self, host: "NativeCandidateHost", caller_id: str) -> None:
        self.host, self.caller_id = host, caller_id

    def _controller(self, session_id: str):
        with self.host._lock:
            hosted = self.host._sessions.get(session_id)
            if self.host._closing or hosted is None or hosted.caller_id != self.caller_id:
                return None
            return hosted

    def active_call(self, session: AgentSessionRecord) -> SteeringTarget | None:
        hosted = self._controller(session.agent_session_id)
        if hosted is None or hosted.run_id != session.run_id:
            return None
        call_id = hosted.control.current_call_id
        if call_id is None:
            return None
        observed = hosted.control.wait(call_id, timeout=0)
        if (observed.state != "running" or not observed.native_session_id
                or observed.native_session_id != session.session_handle.external_id):
            return None
        return SteeringTarget(agent_session_id=session.agent_session_id,
            execution_generation=session.execution_generation,
            native_session_id=observed.native_session_id, call_id=call_id)

    def message(self, target: SteeringTarget, text: str, *, timeout: float,
                before_send: Callable[[], None]) -> None:
        hosted = self._controller(target.agent_session_id)
        if hosted is None:
            raise PermissionError("steering controller unavailable")
        session = self.host.admit(target.agent_session_id, self.caller_id, execution=True,
            scope_id=hosted.scope_id, native_session_id=target.native_session_id)
        if (session.execution_generation != target.execution_generation
                or hosted.run_id != session.run_id
                or hosted.control.session.native_session_id != target.native_session_id):
            raise PermissionError("steering target changed")
        before_send()
        hosted.control.message(target.call_id, text, timeout=timeout)


class _CandidateInvocation:
    def __init__(
        self, host: "NativeCandidateHost", identity: str, caller_id: str,
        scope_id: str, native: CodexSession | PiSession,
    ) -> None:
        self.host, self.identity, self.caller_id = host, identity, caller_id
        self.scope_id, self.native = scope_id, native
        self._has_run = False
        self._resume_required = False

    @property
    def native_session_id(self) -> str:
        return self.native.native_session_id

    def run(self, prompt: str, *, timeout: float, on_started=None):
        started = time.monotonic()
        session = self.host.admit(
            self.identity, self.caller_id, execution=True, scope_id=self.scope_id,
            native_session_id=self.native_session_id,
        )
        sessions = self.host.runtime.candidate_sessions
        if self._has_run:
            session = sessions.prepare_invocation(
                session.run_id, session.candidate_id, agent_harness=session.agent_harness,
                runtime_provider=session.runtime_provider,
                mode="continue", agent_session_id=self.identity,
            )
        self._has_run = True
        contract = session.launch["prompt" if session.agent_harness == "pi" else "message"]
        effective_timeout = sessions.invocation_timeout(self.identity, timeout - (time.monotonic() - started))
        try:
            result = self.native.run(
                contract + "\n\n" + prompt, timeout=effective_timeout, on_started=on_started,
            )
        except BaseException:
            self._resume_required = True
            raise
        # Generation is checked again before settlement; a returned turn cannot
        # authorize supplementing a recovered candidate's workspace.
        self.host.queries.session(self.identity, native_session_id=self.native_session_id)
        sessions.record_native_invocation(self.identity, result)
        review = self.host.delivery.review(session.run_id, session.candidate_id)
        if review.action == "verify":
            self.host.runtime.run_verifier(
                session.run_id, session.candidate_id, agent_session_id=self.identity,
                hypothesis="Host supplemental verification of returned candidate changes",
            )
        self.host.wake_internal_agents(session.run_id, caller_id=self.caller_id)
        return result

    def message(self, prompt: str, *, timeout: float) -> None:
        self.host.admit(
            self.identity, self.caller_id, execution=True, scope_id=self.scope_id,
            native_session_id=self.native_session_id,
        )
        self.native.message(prompt, timeout=timeout)

    def interrupt(self, *, timeout: float) -> None:
        self.native.interrupt(timeout=timeout)

    def close(self) -> None:
        self._resume_required = True
        self.native.close()

    def reopen(self, *, timeout: float = SESSION_START_TIMEOUT_SECONDS) -> None:
        if self._resume_required:
            self.native = self.host.resume_native(self, timeout=timeout)
            self._resume_required = False


class NativeCandidateHost:
    def __init__(
        self, root_dir: Path | str, *, agent_harness: AgentHarnessKind, caller: Callable[[], str],
    ) -> None:
        self.root = Path(root_dir).resolve()
        self.agent_harness, self.caller = agent_harness, caller
        self.runtime = FileSearchRuntime(self.root)
        self.authorization = FileGoalPlusRuntime(self.root).authorization
        self.queries = candidate_host_queries(self.root)
        self.delivery = CandidateDelivery(self.runtime.queries, self.queries)
        self._lock = threading.RLock()
        self._sessions: dict[str, _HostedCandidate] = {}
        self._verifier_queues: dict[str, tuple[str, object]] = {}
        self._closing = False

    def admit(
        self, identity: str, caller_id: str, *, execution: bool,
        scope_id: str | None = None, native_session_id: str | None = None,
    ):
        session = self.queries.source.read_agent_session(identity)
        self._admit_run(session.run_id, caller_id, execution=execution, scope_id=scope_id)
        if session.agent_harness != self.agent_harness:
            raise PermissionError("native session belongs to another host")
        if execution:
            session = self.queries.require_native_execution(
                identity, now=time.time(), native_session_id=native_session_id,
            )
        return session

    def _admit_run(self, run_id: str, caller_id: str, *, execution: bool, scope_id: str | None = None):
        goal = self.authorization.require_main_search_session(
            run_id, agent_harness=self.agent_harness, session_id=caller_id, execution=execution,
        )
        if execution:
            scope = (
                execution_scope_snapshot(root_dir=self.root, scope_id=scope_id)
                if scope_id else execution_scope_for_run(root_dir=self.root, run_id=run_id)
            )
            if (
                scope is None
                and scope_id is None
                and self.queries.runtime_provider(run_id) == "thinkthread"
            ):
                # A ThinkThread Root has no PreToolUse stop hook, so the scope the
                # hook would have created for goal_plus_session_run does not exist
                # yet; ensure it here with the same Main-activation identity.
                scope = ensure_execution_scope(
                    root_dir=self.root,
                    agent_harness=self.agent_harness,
                    owner_session_id=caller_id,
                    run_id=run_id,
                    activation_id=f"{goal.goal_plus_id}:{goal.control.version}",
                )
            if scope is None:
                raise PermissionError("native candidate requires a host execution scope")
            assert_scope_admission(
                scope, scope_id=scope["scope_id"], attachment=ScopeAttachment("search_run", run_id),
            )
            if (
                scope.get("agent_harness") != self.agent_harness
                or scope.get("activation_id") != f"{goal.goal_plus_id}:{goal.control.version}"
                or (self.agent_harness == "codex" and scope.get("owner_session_id") != caller_id)
            ):
                raise PermissionError("native candidate scope does not belong to the current Main activation")

    def _cancel_allocation(self, decision, caller_id: str) -> None:
        """Interrupt the exact retired sessions and persist host receipts."""
        run_id, decision_id = decision.run_id, decision.decision_id
        scopes = build_scope_state(self.root)
        with self._lock:
            self._admit_run(run_id, caller_id, execution=True)
            scope = execution_scope_for_run(root_dir=self.root, run_id=run_id)
            scope_id = scope["scope_id"]
            sessions = [self.queries.current_session(run_id, candidate_id)
                        for candidate_id in [decision.selected_retirement] if candidate_id is not None]
            for session in sessions:
                if session is None:
                    continue
                target = dict(
                    run_id=run_id, decision_id=decision_id, candidate_id=session.candidate_id,
                    agent_session_id=session.agent_session_id,
                    execution_generation=session.execution_generation,
                    native_session_id=session.session_handle.external_id,
                    call_id=session.run_requests[-1].call_id if session.run_requests else None,
                )
                with scopes.lock(scope_id):
                    current = scopes.read(scope_id)
                    receipts = current.setdefault("allocation_cancellations", [])
                    previous = next((item for item in receipts
                                     if all(item.get(key) == value for key, value in target.items())), None)
                    if previous is not None and previous["status"] == "accepted":
                        continue
                    if previous is not None and previous["status"] in {"requested", "unknown"}:
                        try:
                            hosted = self._sessions.get(session.agent_session_id)
                            if hosted is None or hosted.caller_id != caller_id:
                                raise RuntimeError("native controller is unavailable")
                            control = hosted.control
                            if target["call_id"] is None or control.current_call_id != target["call_id"]:
                                raise RuntimeError("native call identity is unavailable")
                            observed = control.wait(target["call_id"], timeout=0)
                            if observed.state not in {"completed", "interrupted"}:
                                raise RuntimeError("native call has no confirmed terminal result")
                        except Exception as exc:
                            raise RuntimeError(
                                "AllocationCancellationRecoveryRequired: native cancellation outcome is unknown"
                            ) from exc
                        # Observe the original call; never resend an uncertain interrupt.
                        previous.update(status="accepted", error=None)
                        scopes.write(current)
                        continue
                    receipt = {**target, "status": "requested", "error": None}
                    if previous is not None:
                        receipts.remove(previous)
                    receipts.append(receipt)
                    scopes.write(current)
                try:
                    hosted = self._sessions.get(session.agent_session_id)
                    if hosted is None:
                        if session.run_requests or session.session_handle.external_id not in {
                            None, "", session.agent_session_id,
                        }:
                            raise RuntimeError("AllocationCancellationRecoveryRequired: native controller is unavailable")
                    else:
                        if hosted.caller_id != caller_id:
                            raise PermissionError("session controller belongs to another Main")
                        self.admit(
                            session.agent_session_id, caller_id, execution=False,
                            scope_id=hosted.scope_id,
                            native_session_id=hosted.control.session.native_session_id,
                        )
                        self.queries.session(
                            session.agent_session_id,
                            native_session_id=hosted.control.session.native_session_id,
                        )
                        control = hosted.control
                        if control.current_call_id != target["call_id"]:
                            raise RuntimeError("AllocationCancellationRecoveryRequired: native call identity changed")
                        if control.current_call_id is not None:
                            observed = control.wait(control.current_call_id, timeout=0)
                            if observed.state == "failed":
                                raise RuntimeError("AllocationCancellationRecoveryRequired: native call failed without a cancellation receipt")
                            if observed.state == "running":
                                control.interrupt(control.current_call_id, timeout=ALLOCATION_INTERRUPT_TIMEOUT_SECONDS)
                except Exception as exc:
                    receipt["status"] = "rejected" if isinstance(exc, (ValueError, PermissionError, SessionRequestRejected)) else "unknown"
                    receipt["error"] = f"{type(exc).__name__}: {exc}"
                    if isinstance(exc, SessionRequestRejected):
                        raise ValueError(str(exc)) from exc
                    raise
                else:
                    receipt["status"] = "accepted"
                finally:
                    with scopes.lock(scope_id):
                        current = scopes.read(scope_id)
                        current["allocation_cancellations"] = [
                            item for item in current.get("allocation_cancellations", [])
                            if not all(item.get(key) == value for key, value in target.items())
                        ] + [receipt]
                        scopes.write(current)

    def _dispatch_allocation(self, session: AgentSessionRecord, decision, caller_id: str) -> None:
        try:
            self._dispatch_allocation_native(session, decision, caller_id)
        except (ValueError, PermissionError, SessionRequestRejected) as exc:
            if getattr(session, "run_requests", ()):
                # Rejection now cannot disprove submission by an earlier attempt.
                raise
            raise DispatchRejected(str(exc)) from exc

    def _dispatch_allocation_native(self, session: AgentSessionRecord, _decision, caller_id: str) -> None:
        """Start and bind the exact prepared native session under the executor lock."""

        call_id = session.directive["expansion_id"]
        hosted = self._sessions.get(session.agent_session_id)
        if hosted is not None:
            if hosted.caller_id != caller_id:
                raise PermissionError("session controller belongs to another Main")
            try:
                hosted.control.wait(call_id, timeout=0)
                return  # The exact dispatch was already submitted, never wake it twice.
            except KeyError:
                pass
        timeout = self.runtime.candidate_sessions.invocation_timeout(session.agent_session_id)
        prompt = "Continue the allocated candidate from its inherited artifact and search contract."
        fingerprint = sha256_text(canonical_json({"prompt": prompt, "expansion_id": call_id}))
        self.runtime.candidate_sessions.record_run_request(
            session.agent_session_id, call_id, fingerprint, run_locked=True,
        )
        self._open(session.agent_session_id, caller_id, run_locked=True)
        self._sessions[session.agent_session_id].control.wake(prompt, call_id=call_id, timeout=timeout)

    def apply_allocation_decision(
        self, run_id: str, decision_id: str, *, caller_id: str | None = None,
    ) -> dict:
        with self._lock:
            owner = caller_id or self.caller()
            self._admit_run(run_id, owner, execution=True)
            return self.runtime.apply_allocation_decision(
                run_id,
                decision_id,
                cancel=lambda decision: self._cancel_allocation(decision, owner),
                dispatch=lambda session, decision: self._dispatch_allocation(
                    session, decision, owner
                ),
            )

    def _verifier_queue(self, run_id: str, caller_id: str):
        from goal_plus.llm_verifier import VerifierQueue

        current = self._verifier_queues.get(run_id)
        if current is not None:
            owner, queue = current
            if owner == caller_id and not queue.stopped.is_set():
                return queue
            if not queue.stopped.is_set():
                queue.close()
            _unregister_verifier_queue(self.root, run_id, queue)
        self.runtime.settlement.verifier_submission.replay(run_id)
        queue = VerifierQueue(
            self.root,
            run_id,
            lambda record: self.runtime.accept_verifier_record(
                record,
                execute=lambda target_run_id, decision_id: self.apply_allocation_decision(
                    target_run_id, decision_id, caller_id=caller_id
                ),
                steering_host=_SteeringTransport(self, caller_id),
            ),
        )
        self._verifier_queues[run_id] = (caller_id, queue)
        _register_verifier_queue(self.root, run_id, queue)
        return queue

    def wake_internal_agents(self, run_id: str, *, caller_id: str | None = None) -> None:
        from goal_plus.internal_agents import kick_internal_agents

        try:
            if caller_id is None:
                owners = {
                    hosted.caller_id
                    for hosted in self._sessions.values()
                    if hosted.run_id == run_id
                }
                caller_id = next(iter(owners)) if len(owners) == 1 else self.caller()
            self._admit_run(run_id, caller_id, execution=True)
            from goal_plus.bootstrap import build_internal_agent_work
            from goal_plus.host_scope import run_accepts_internal_agent_launch

            if (
                not build_internal_agent_work(self.root).inspect(run_id).accepts_work
                or not run_accepts_internal_agent_launch(
                    root_dir=self.root, run_id=run_id
                )
            ):
                return
            self._verifier_queue(run_id, caller_id).start()
        except Exception:
            logging.getLogger(__name__).exception("Verifier wake failed after settlement for %s", run_id)
        # Independent owners: neither annotation failure nor allocation recovery blocks scoring.
        try:
            kick_internal_agents(self.root, run_id, agent_harness=self.agent_harness)
        except Exception:
            logging.getLogger(__name__).exception("Annotation wake failed for %s", run_id)
        try:
            with self._lock:
                self._admit_run(run_id, caller_id, execution=True)
                self.runtime.recover_allocation(
                    run_id,
                    cancel=lambda decision: self._cancel_allocation(decision, caller_id),
                    dispatch=lambda session, decision: self._dispatch_allocation(session, decision, caller_id),
                )
        except Exception:
            logging.getLogger(__name__).exception("Allocation recovery deferred for %s", run_id)

    def open_candidate(self, run_id: str, candidate_id: str) -> SessionOpenResult:
        with self._lock:
            if self._closing:
                raise RuntimeError("native session host is closing")
            self._admit_run(run_id, self.caller(), execution=True)
            self.queries.validate_candidate(run_id, candidate_id)
            session = self.queries.current_session(run_id, candidate_id)
            if session is None:
                session = self.runtime.start_agent_session(run_id, candidate_id)
            return self.open(session.agent_session_id)

    def run_candidate(
        self, run_id: str, candidate_id: str, call_id: str, prompt: str,
        timeout_seconds: float, new_session: bool = False,
    ) -> dict:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout must be finite and positive")
        if not call_id or not prompt.strip():
            raise ValueError("call_id and prompt are required")
        fingerprint = sha256_text(canonical_json({
            "prompt": prompt, "timeout_seconds": timeout_seconds, "new_session": new_session,
        }))
        with self._lock:
            if self._closing:
                raise RuntimeError("native session host is closing")
            self._admit_run(run_id, self.caller(), execution=True)
            previous = next((session for session in self.queries.sessions(run_id, candidate_id)
                             if any(item.call_id == call_id for item in session.run_requests)), None)
            if previous is not None:
                request = next(item for item in previous.run_requests if item.call_id == call_id)
                if request.fingerprint != fingerprint:
                    raise ValueError("SessionRunConflict: call_id was used with different input")
                if request.target_session_id is not None:
                    previous = self.queries.source.read_agent_session(request.target_session_id)
                self.queries.session(previous.agent_session_id)
                hosted = self._sessions.get(previous.agent_session_id)
                if previous.runtime_provider == "direct" and (
                    hosted is None and previous.session_handle.metadata.get("transport")
                    or hosted is not None and call_id in hosted.unavailable_calls
                ):
                    raise RuntimeError("SessionCallHistoryUnavailable: inspect candidate state and continue with a new call_id")
                if hosted is not None:
                    try:
                        hosted.control.wait(call_id, timeout=0)
                    except KeyError:
                        # Preparation may have failed before this call was submitted.
                        self.open(previous.agent_session_id)
                opened = (SessionOpenResult(previous.agent_session_id, hosted.control.session.native_session_id,
                                            self.agent_harness) if hosted is not None
                          else self.open(previous.agent_session_id))
            else:
                current = self.queries.current_session(run_id, candidate_id)
                if new_session and current is not None:
                    hosted = self._sessions.get(current.agent_session_id)
                    if hosted is not None or current.runtime_provider == "thinkthread" or not current.session_handle.metadata.get("transport"):
                        opened = (SessionOpenResult(current.agent_session_id, hosted.control.session.native_session_id,
                                                    self.agent_harness) if hosted is not None
                                  else self.open(current.agent_session_id))
                        control = self(opened.session_id, "wait")
                        if control.current_call_id and control.wait(control.current_call_id, timeout=0).state == "running":
                            raise RuntimeError("session already has an active invocation")
                        control.close()
                    with self._native_session_lock(current.agent_session_id):
                        current = self.queries.session(current.agent_session_id)
                        if current.runtime_provider == "direct":
                            self._finish_previous_native_resources(current)
                        current = self.runtime.candidate_sessions.replace_session(
                            current.agent_session_id, call_id=call_id, fingerprint=fingerprint,
                        )
                    opened = self.open(current.agent_session_id)
                else:
                    self.queries.validate_candidate(run_id, candidate_id)
                    current = current or self.runtime.start_agent_session(run_id, candidate_id)
                    self.admit(current.agent_session_id, self.caller(), execution=True)
                    hosted = self._sessions.get(current.agent_session_id)
                    if hosted is None and (current.runtime_provider == "thinkthread" or current.run_requests
                                           or current.session_handle.metadata.get("transport")):
                        self.open(current.agent_session_id)
                        hosted = self._sessions[current.agent_session_id]
                    if hosted is not None:
                        control = self(current.agent_session_id, "wake")
                        if control.current_call_id and control.wait(control.current_call_id, timeout=0).state == "running":
                            raise RuntimeError("session already has an active invocation")
                    self.runtime.candidate_sessions.record_run_request(current.agent_session_id, call_id, fingerprint)
                    opened = self.open(current.agent_session_id)
                control = self(opened.session_id, "wait")
                if control.current_call_id and control.wait(control.current_call_id, timeout=0).state == "running":
                    raise RuntimeError("session already has an active invocation")
            snapshot = self(opened.session_id, "wake").wake(prompt, call_id=call_id, timeout=timeout_seconds)
            return {"session_id": opened.session_id, **session_call_receipt(snapshot)}

    def open(self, agent_session_id: str) -> SessionOpenResult:
        return self._open(agent_session_id, self.caller())

    def _open(
        self, agent_session_id: str, caller_id: str, *, run_locked: bool = False,
    ) -> SessionOpenResult:
        with self._lock:
            if self._closing:
                raise RuntimeError("native session host is closing")
            session = self.admit(agent_session_id, caller_id, execution=True)
            if existing := self._sessions.get(agent_session_id):
                if existing.caller_id != caller_id:
                    raise PermissionError("session controller belongs to another Main")
                self.queries.session(
                    agent_session_id, native_session_id=existing.control.session.native_session_id,
                )
                existing.control.reopen()
                if isinstance(existing.control.session, _CandidateInvocation):
                    current = existing.control.current_call_id
                    if current is None or existing.control.wait(current, timeout=0).state != "running":
                        existing.control.session.reopen()
                return SessionOpenResult(agent_session_id, existing.control.session.native_session_id, self.agent_harness,
                                         existing.control.current_call_id, session.runtime_provider)
            scope = execution_scope_for_run(root_dir=self.root, run_id=session.run_id)
            scope_id = scope["scope_id"]
            if session.runtime_provider == "thinkthread":
                from goal_plus.infrastructure.thinkthread_sessions import ThinkThreadCandidateSession

                with (
                    nullcontext()
                    if run_locked
                    else self.runtime.candidate_sessions.state.lock_run(session.run_id)
                ):
                    session = self.admit(agent_session_id, caller_id, execution=True, scope_id=scope_id)
                    native = ThinkThreadCandidateSession(self, agent_session_id, caller_id, scope_id)
                    control = NativeSessionControl(native, stop_action=native.stop, admit_call=native.admit_call,
                                                   reopen_action=native.reopen)
                native.restore_control(control)
                self._sessions[agent_session_id] = _HostedCandidate(caller_id, session.run_id, scope_id, control)
                return SessionOpenResult(agent_session_id, native.native_session_id, self.agent_harness, control.current_call_id, session.runtime_provider)
            # Serialize launch/bind with recovery and another MCP process's launch.
            with (
                nullcontext()
                if run_locked
                else self.runtime.candidate_sessions.state.lock_run(session.run_id)
            ):
                session = self.admit(agent_session_id, caller_id, execution=True, scope_id=scope_id)
                initial_pi_handle = (
                    session.agent_harness == "pi"
                    and session.session_handle.external_id == session.agent_session_id
                    and session.session_handle.metadata == {"continuation": "native_session"}
                )
                resume = bool(session.session_handle.external_id and not initial_pi_handle)
                if not resume and any(
                    item.get("status") not in SCOPE_PROCESS_TERMINAL_STATUSES and item.get("label") in {
                        f"pi-rpc:{agent_session_id}", f"native-session:{agent_session_id}",
                    }
                    for item in scope.get("processes", [])
                ):
                    raise RuntimeError("candidate already has a registered execution resource")
                native = self._launch_native(session, caller_id, scope, resume=resume)
                try:
                    self.admit(agent_session_id, caller_id, execution=True, scope_id=scope_id)
                    self.runtime.candidate_sessions.bind_agent_handle(agent_session_id, {
                        "agent_harness": session.agent_harness, "external_id": native.native_session_id,
                        "metadata": {"transport": "app-server" if self.agent_harness == "codex" else "native-rpc",
                                     "scope_id": scope_id},
                    })
                    invocation = _CandidateInvocation(self, agent_session_id, caller_id, scope_id, native)
                    invocation._has_run = resume
                    control = NativeSessionControl(invocation, reopen_action=invocation.reopen)
                    self._sessions[agent_session_id] = _HostedCandidate(
                        caller_id, session.run_id, scope_id, control,
                        frozenset(item.call_id for item in session.run_requests) if resume else frozenset(),
                    )
                except BaseException:
                    native.close()
                    raise
            return SessionOpenResult(agent_session_id, native.native_session_id, self.agent_harness)

    def _launch_native(
        self, session: AgentSessionRecord, caller_id: str, scope: ScopeRecord, *,
        previous: CodexSession | PiSession | None = None, resume: bool = False,
        timeout: float = SESSION_START_TIMEOUT_SECONDS,
    ) -> CodexSession | PiSession:
        agent_session_id, scope_id = session.agent_session_id, scope["scope_id"]
        config = native_candidate_input(
            self.root, session, scope, resume=resume or previous is not None,
            process_environment=self.queries.process_environment(agent_session_id),
        )
        registry = build_scope_process_registry(self.root)

        def register(pid):
            self.admit(agent_session_id, caller_id, execution=True, scope_id=scope_id)
            if resume or previous is not None:
                # Registration runs while the new process holds the execution lease.
                self._finish_previous_native_resources(session)
            return registry.register(
                scope_id=scope_id, pid=pid, label=f"native-session:{agent_session_id}",
                attachment=ScopeAttachment("search_run", session.run_id),
            )

        registration = ProcessRegistration(
            register=register, finish=lambda token: registry.finish(scope_id=scope_id, token=token),
        )
        if self.agent_harness == "codex":
            from goal_plus.mcp_client import McpSessionToolBridge

            return CodexSession(
                config, registration, timeout=timeout, resume_from=previous,
                execution_lock=self.root / "host-sessions" / f"{agent_session_id}.lock",
                tool_bridge=McpSessionToolBridge(
                    self.root, caller_role="candidate", caller_agent_session_id=agent_session_id,
                    on_settled=lambda: self.wake_internal_agents(
                        session.run_id, caller_id=caller_id
                    ),
                ),
            )
        return PiSession(
            config, registration, timeout=timeout, resume_from=previous,
            execution_lock=self.root / "host-sessions" / f"{agent_session_id}.lock",
        )

    def _finish_previous_native_resources(self, session: AgentSessionRecord) -> None:
        metadata = session.session_handle.metadata
        transport = "app-server" if session.agent_harness == "codex" else "native-rpc"
        if metadata.get("transport") != transport or not metadata.get("scope_id"):
            raise RuntimeError("SessionCleanupRequired: native session has no bound execution scope")
        scope = execution_scope_snapshot(root_dir=self.root, scope_id=metadata["scope_id"])
        resources = [item for item in scope.get("processes", [])
                     if item.get("label") == f"native-session:{session.agent_session_id}"
                     and item.get("run_id") == session.run_id and item.get("kind", "candidate") == "candidate"]
        if scope.get("agent_harness") != session.agent_harness or not resources:
            raise RuntimeError("SessionCleanupRequired: native session has no matching execution resource")
        registry = build_scope_process_registry(self.root)
        for resource in resources:
            if resource.get("status") != "finished":
                registry.finish(scope_id=metadata["scope_id"], token=resource["token"])

    @contextmanager
    def _native_session_lock(self, agent_session_id: str):
        try:
            with exclusive_file_lock(
                self.root / "host-sessions" / f"{agent_session_id}.lock", blocking=False,
            ):
                yield
        except BlockingIOError as exc:
            raise RuntimeError("SessionExecutionActive: native session is still running") from exc

    def resume_native(self, invocation: _CandidateInvocation, *, timeout: float) -> CodexSession | PiSession:
        with self._lock:
            if self._closing:
                raise RuntimeError("native session host is closing")
            session = self.admit(
                invocation.identity, invocation.caller_id, execution=True,
                scope_id=invocation.scope_id, native_session_id=invocation.native_session_id,
            )
            invocation.native.close()
            with self.runtime.candidate_sessions.state.lock_run(session.run_id):
                session = self.admit(
                    invocation.identity, invocation.caller_id, execution=True,
                    scope_id=invocation.scope_id, native_session_id=invocation.native_session_id,
                )
                scope = execution_scope_snapshot(root_dir=self.root, scope_id=invocation.scope_id)
                native = self._launch_native(session, invocation.caller_id, scope,
                                             previous=invocation.native, timeout=timeout)
                try:
                    self.admit(invocation.identity, invocation.caller_id, execution=True,
                               scope_id=invocation.scope_id, native_session_id=native.native_session_id)
                except BaseException:
                    native.close()
                    raise
                return native

    def __call__(self, session_id: str, operation: SessionOperation):
        caller_id = self.caller()
        with self._lock:
            hosted = self._sessions.get(session_id)
            if hosted is None:
                self.admit(session_id, caller_id, execution=False)
                raise RuntimeError(
                    "SessionCallHistoryUnavailable: continue the candidate with "
                    "goal_plus_session_run and a new call_id"
                )
            if hosted.caller_id != caller_id:
                raise PermissionError("session controller does not belong to this Main MCP service")
        self.admit(
            session_id, caller_id, execution=operation in {"wake", "message"}, scope_id=hosted.scope_id,
            native_session_id=hosted.control.session.native_session_id,
        )
        return hosted.control

    def shutdown(self) -> None:
        with self._lock:
            self._closing = True
            sessions = list(self._sessions.values())
            queues = list(self._verifier_queues.items())
        errors = []
        for run_id, (_owner, queue) in queues:
            try:
                queue.close()
            except Exception as exc:
                errors.append(exc)
            finally:
                _unregister_verifier_queue(self.root, run_id, queue)
        self._verifier_queues.clear()
        for session in sessions:
            try:
                session.control.close()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise RuntimeError(f"native session cleanup failed for {len(errors)} resources") from errors[0]
