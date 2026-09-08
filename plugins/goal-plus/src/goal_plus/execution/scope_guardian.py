"""Keep scope cleanup responsibility until the owner reports it closed."""

import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path

from goal_plus.execution.launch_gate import ProcessLaunchGate
from goal_plus.execution.python_command import module_arguments
from goal_plus.execution.process_identity import (
    EXITED_PROCESS_STATES, ProcessInspectionUnavailable, inspect_command, inspect_process, process_alive,
)
from goal_plus.ports.scope import ScopeGuardianObservation, ScopeHost, ScopeRecord, ScopeState

# Preserve owner polling; failed cleanup backs off to the existing close grace.
GUARDIAN_POLL_SECONDS = 0.25
GUARDIAN_MAX_RETRY_SECONDS = 5.0
logger = logging.getLogger(__name__)


class ScopeGuardianLauncher:
    def __init__(self, *, state: ScopeState, root_dir: Path, source_root: Path) -> None:
        self.state = state
        self.root_dir = root_dir
        self.source_root = source_root

    def _arguments(self, scope_id: str) -> list[str]:
        return module_arguments("goal_plus.host_scope", ["guard", "--root", str(self.root_dir), "--scope-id", scope_id])

    def write_guarded(self, scope: ScopeRecord, *, start_guardian: bool) -> None:
        """Caller holds the scope lock through durable registration and exec release."""
        scope_id = str(scope["scope_id"])
        with ExitStack() as launch:
            gate = None
            if start_guardian:
                pid, token = int(scope.get("guardian_pid") or 0), scope.get("guardian_start_token")
                alive = process_alive(pid, token) if token else inspect_command(pid, self._arguments(scope_id))
                if not alive:
                    gate = launch.enter_context(ProcessLaunchGate())
                    pid = self._spawn(scope_id, gate)
                    guardian = inspect_process(pid)
                    if guardian is None or guardian.state in EXITED_PROCESS_STATES:
                        raise RuntimeError("execution scope guardian exited before registration")
                    scope["guardian_pid"] = pid
                    scope["guardian_start_token"] = guardian.start_token
            self.state.write(scope)
            if gate is not None:
                gate.release()

    def restore(self, scope_id: str) -> None:
        with self.state.lock(scope_id):
            scope = self.state.read(scope_id)
            if scope.get("state") != "closed":
                self.write_guarded(scope, start_guardian=True)

    def restore_owner(self, *, host: ScopeHost, owner_session_id: str) -> None:
        """Restore cleanup responsibility without opening or reactivating a scope."""
        for scope in self.state.records():
            if scope.get("host") == host and scope.get("owner_session_id") == owner_session_id:
                self.restore(str(scope["scope_id"]))

    def _spawn(self, scope_id: str, gate: ProcessLaunchGate) -> int:
        env = os.environ.copy()
        pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (
            str(self.source_root) if not pythonpath
            else os.pathsep.join((str(self.source_root), pythonpath))
        )
        command = [sys.executable, *self._arguments(scope_id)]
        launch_options = {
            "pass_fds": gate.pass_fds,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
            "env": env,
        }
        try:
            process = subprocess.Popen(gate.command(command), **launch_options, start_new_session=True)
        except PermissionError:
            # Protected ThinkThread Roots may deny setsid; detached stdio still survives the caller.
            process = subprocess.Popen(gate.command(command), **launch_options, start_new_session=False)
        return int(process.pid)


class ScopeGuardian:
    def __init__(
        self, *, read: Callable[[], ScopeGuardianObservation], close: Callable[[str], object],
        owner_alive: Callable[[int, str | None], bool] = process_alive,
        pause: Callable[[float], None] = time.sleep,
        observe_resources: Callable[[], None] | None = None,
    ) -> None:
        self.read = read
        self.close = close
        self.owner_alive = owner_alive
        self.pause = pause
        self.observe_resources = observe_resources

    def run(self) -> int:
        delay = GUARDIAN_POLL_SECONDS
        while True:
            try:
                scope = self.read()
            except FileNotFoundError:
                return 0
            except Exception as exc:
                logger.warning("Execution scope observation unavailable (%s); retrying", type(exc).__name__)
                self.pause(delay)
                delay = min(GUARDIAN_MAX_RETRY_SECONDS, delay * 2)
                continue
            if scope.state == "closed":
                return 0
            unknown_resources = False
            if self.observe_resources is not None:
                try:
                    self.observe_resources()
                except Exception as exc:
                    logger.warning("Execution resource observation unavailable (%s); cleanup responsibility retained", type(exc).__name__)
                    unknown_resources = True

            reason = (scope.close_reason or "cleanup_retry") if scope.state != "open" else None
            unknown_owner = False
            try:
                if not scope.owner_pid or not scope.owner_start_token:
                    raise ProcessInspectionUnavailable("ProcessInspectionUnavailable: missing owner identity")
                alive = self.owner_alive(scope.owner_pid, scope.owner_start_token)
                if alive is False:
                    reason = "owner_process_exited"
                elif alive is not True:
                    raise ProcessInspectionUnavailable("ProcessInspectionUnavailable: unknown owner observation")
            except ProcessInspectionUnavailable:
                unknown_owner = True
            except Exception as exc:
                logger.warning("Execution scope owner observation unavailable (%s); retrying", type(exc).__name__)
                unknown_owner = True

            if reason is not None:
                try:
                    self.close(reason)
                except Exception as exc:
                    logger.warning("Execution scope cleanup failed (%s); retrying", type(exc).__name__)
            retrying = reason is not None or unknown_owner or unknown_resources
            self.pause(delay if retrying else GUARDIAN_POLL_SECONDS)
            delay = min(GUARDIAN_MAX_RETRY_SECONDS, delay * 2) if retrying else GUARDIAN_POLL_SECONDS
