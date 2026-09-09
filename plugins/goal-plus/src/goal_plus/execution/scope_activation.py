"""Host-authorized scope activation and replacement, outside Goal/Search state."""

import hashlib
import time
from typing import Any

from goal_plus.execution.process_identity import (
    EXITED_PROCESS_STATES, ProcessInspectionUnavailable, inspect_process, process_alive,
)
from goal_plus.execution.scope import newest_scope, normalize_native_agent_config
from goal_plus.execution.scope_cleanup import ScopeCleanup
from goal_plus.execution.scope_guardian import ScopeGuardianLauncher
from goal_plus.ports.clock import Clock
from goal_plus.ports.scope import ScopeAttachment, ScopeHost, ScopeRecord, ScopeState


SCOPE_SCHEMA_VERSION = 1


def _scope_id(host: ScopeHost, owner_session_id: str, owner_pid: int, start_token: str) -> str:
    digest = hashlib.sha256(
        f"{host}\0{owner_session_id}\0{owner_pid}\0{start_token}".encode()
    ).hexdigest()[:20]
    return f"scope_{digest}"


class ScopeActivation:
    def __init__(
        self, *, state: ScopeState, clock: Clock, cleanup: ScopeCleanup,
        guardians: ScopeGuardianLauncher,
    ) -> None:
        self.state = state
        self.clock = clock
        self.cleanup = cleanup
        self.guardians = guardians

    def ensure(
        self, *, host: ScopeHost, owner_session_id: str, owner_pid: int,
        attachment: ScopeAttachment | None = None, native_agent_config: dict[str, Any] | None = None,
        start_guardian: bool = True, activation_id: str | None = None,
    ) -> ScopeRecord:
        """Activation is supplied by an authorized host, not inferred from a resume."""
        if not owner_session_id:
            raise ValueError("owner_session_id is required")
        owner = inspect_process(owner_pid)
        if owner is None or owner.state in EXITED_PROCESS_STATES:
            raise RuntimeError(f"execution-scope owner PID is not alive: {owner_pid}")
        normalized_native_config = normalize_native_agent_config(host, native_agent_config)
        scope_id = _scope_id(host, owner_session_id, owner_pid, owner.start_token)
        if activation_id is not None:
            if not activation_id:
                raise ValueError("activation_id must be non-empty")
            scope_id = _scope_id(host, owner_session_id, owner_pid, f"{owner.start_token}\0{activation_id}")
        with self.state.owner_lock(host, owner_session_id):
            previous = [scope for scope in self.state.records() if (
                scope.get("host") == host and scope.get("owner_session_id") == owner_session_id
            )]
            current = newest_scope(iter(previous))
            if activation_id is not None and not self.state.exists(scope_id) and current is not None:
                if (current.get("owner_pid"), current.get("owner_start_token")) == (owner_pid, owner.start_token) and (
                    current.get("state") == "open" or current.get("activation_id") == activation_id
                ):
                    # Pause/resume does not kill running work; its activation stays fenced after close.
                    scope_id = str(current["scope_id"])
            for old in previous:
                if start_guardian and old.get("state") != "closed":
                    self.guardians.restore(str(old["scope_id"]))
            if not self.state.exists(scope_id):
                for old in previous:
                    if old.get("state") == "closed":
                        continue
                    old_token = old.get("owner_start_token")
                    if not old_token:
                        raise ProcessInspectionUnavailable("execution scope owner identity unavailable")
                    old_alive = process_alive(int(old.get("owner_pid") or 0), old_token)
                    if old_alive and (old.get("owner_pid"), old_token) != (owner_pid, owner.start_token):
                        raise RuntimeError("execution scope still belongs to another live owner")
                    closed = self.cleanup.close(
                        scope_id=str(old["scope_id"]),
                        reason="scope_replaced" if old_alive else "owner_process_exited",
                    )
                    if closed["state"] != "closed":
                        raise RuntimeError(f"execution scope {old['scope_id']} cleanup is incomplete; new work is fenced")
            return self._ensure_owner_scope(
                scope_id=scope_id, host=host, owner_session_id=owner_session_id,
                owner_pid=owner_pid, start_token=owner.start_token, attachment=attachment,
                native_agent_config=normalized_native_config, start_guardian=start_guardian,
                generation=max((int(item.get("generation", 0)) for item in previous), default=0) + 1,
                activation_id=activation_id,
            )

    def _ensure_owner_scope(
        self, *, scope_id: str, host: ScopeHost, owner_session_id: str,
        owner_pid: int, start_token: str, attachment: ScopeAttachment | None,
        native_agent_config: dict[str, str] | None, start_guardian: bool,
        generation: int, activation_id: str | None,
    ) -> ScopeRecord:
        with self.state.lock(scope_id):
            if self.state.exists(scope_id):
                scope = self.state.read(scope_id)
                if scope.get("state") != "open":
                    raise RuntimeError(
                        f"execution scope {scope_id} is {scope.get('state')}; new work is fenced"
                    )
                stored_native_config = scope.get("native_agent_config")
                if (
                    native_agent_config is not None
                    and stored_native_config is not None
                    and stored_native_config != native_agent_config
                ):
                    raise RuntimeError(f"execution scope {scope_id} native agent config changed")
                if native_agent_config is not None and stored_native_config is None:
                    scope["native_agent_config"] = native_agent_config
            else:
                now = self.clock.timestamp()
                scope = {
                    "schema_version": SCOPE_SCHEMA_VERSION,
                    "scope_id": scope_id,
                    "host": host,
                    "owner_session_id": owner_session_id,
                    "owner_pid": owner_pid,
                    "owner_start_token": start_token,
                    "generation": generation,
                    "activation_id": activation_id,
                    "state": "open",
                    "run_ids": [],
                    "fenced_run_ids": [],
                    "guardian_pid": None,
                    "created_at": now,
                    "created_at_ns": time.time_ns(),
                    "updated_at": now,
                }
                if native_agent_config is not None:
                    scope["native_agent_config"] = native_agent_config
            if attachment is not None and attachment.identity not in scope["run_ids"]:
                scope["run_ids"].append(attachment.identity)
            if activation_id is not None:
                scope["activation_id"] = activation_id
            self.guardians.write_guarded(scope, start_guardian=start_guardian)
            return dict(scope)
