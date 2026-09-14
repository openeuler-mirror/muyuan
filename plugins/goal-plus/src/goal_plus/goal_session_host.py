"""Shared native Goal-role resources; role owners retain admission and results."""

from collections.abc import Callable
from pathlib import Path
import threading

from goal_plus.bootstrap import build_scope_process_registry
from goal_plus.domain.agents import AgentHarnessKind
from goal_plus.execution.native_session import CodexSession, PiSession
from goal_plus.execution.scope import assert_scope_admission
from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.host_scope import execution_scope_for_run, execution_scope_snapshot
from goal_plus.ports.process import ProcessRegistration
from goal_plus.ports.scope import ScopeAttachment, ScopeRecord
from goal_plus.ports.session import NativeSession, NativeSessionInput, SessionToolBridge


NATIVE_GOAL_SESSION_START_TIMEOUT_SECONDS = 60.0


class NativeGoalSessionHost:
    def __init__(self, root_dir: Path | str, *, agent_harness: AgentHarnessKind, caller: Callable[[], str]):
        self.root = Path(root_dir).resolve()
        self.agent_harness, self.caller = agent_harness, caller
        self.runtime = FileGoalPlusRuntime(self.root)
        self._lock = threading.RLock()
        self._resources: list[NativeSession] = []
        self._closing = False

    def admit_goal(self, goal_plus_id: str, caller_id: str, scope_id: str | None = None):
        record = self.runtime.authorization.require_main_goal_session(
            goal_plus_id, agent_harness=self.agent_harness, session_id=caller_id, execution=True,
        )
        scope = (execution_scope_snapshot(root_dir=self.root, scope_id=scope_id) if scope_id
                 else execution_scope_for_run(root_dir=self.root, run_id=goal_plus_id))
        if scope is None:
            raise PermissionError("native Goal role requires a host execution scope")
        assert_scope_admission(scope, scope_id=scope["scope_id"], attachment=ScopeAttachment("goal", goal_plus_id))
        if (
            scope.get("agent_harness") != self.agent_harness
            or scope.get("activation_id") != f"{record.goal_plus_id}:{record.control.version}"
            or (self.agent_harness == "codex" and scope.get("owner_session_id") != caller_id)
        ):
            raise PermissionError("native Goal scope does not belong to the current Main activation")
        return record, scope

    def open_native(
        self, config: NativeSessionInput, scope: ScopeRecord, *, goal_plus_id: str,
        label: str, admit: Callable[[], object], tool_bridge: SessionToolBridge | None = None,
    ) -> NativeSession:
        with self._lock:
            if self._closing:
                raise RuntimeError("native Goal session host is closing")
            scope_id = scope["scope_id"]
            registry = build_scope_process_registry(self.root)

            def register(pid):
                admit()
                return registry.register(
                    scope_id=scope_id, pid=pid, label=label,
                    attachment=ScopeAttachment("goal", goal_plus_id),
                )

            registration = ProcessRegistration(
                register=register,
                finish=lambda token: registry.finish(scope_id=scope_id, token=token),
            )
            native = (CodexSession(config, registration, timeout=NATIVE_GOAL_SESSION_START_TIMEOUT_SECONDS, tool_bridge=tool_bridge)
                      if self.agent_harness == "codex" else PiSession(config, registration, timeout=NATIVE_GOAL_SESSION_START_TIMEOUT_SECONDS))
            self._resources.append(native)
            return native

    def close_native(self, native: NativeSession) -> None:
        native.close()
        with self._lock:
            self._resources = [item for item in self._resources if item is not native]

    def shutdown(self) -> None:
        with self._lock:
            self._closing = True
            sessions = list(self._resources)
        errors = []
        for native in sessions:
            try:
                self.close_native(native)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise RuntimeError("native Goal session cleanup failed") from errors[0]
