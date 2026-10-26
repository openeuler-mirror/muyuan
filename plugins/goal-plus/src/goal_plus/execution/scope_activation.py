"""Host-authorized session scopes; operating-system processes are not owners."""

import hashlib
import time
from typing import Any

from goal_plus.execution.scope import newest_scope, normalize_native_agent_config
from goal_plus.ports.clock import Clock
from goal_plus.ports.scope import ScopeAttachment, ScopeHost, ScopeRecord, ScopeState


SCOPE_SCHEMA_VERSION = 2


class ScopeActivation:
    def __init__(self, *, state: ScopeState, clock: Clock) -> None:
        self.state = state
        self.clock = clock

    def ensure(
        self,
        *,
        agent_harness: ScopeHost,
        owner_session_id: str,
        attachment: ScopeAttachment | None = None,
        native_agent_config: dict[str, Any] | None = None,
        native_session_file: str | None = None,
        activation_id: str | None = None,
        activation_nonce: str | None = None,
    ) -> ScopeRecord:
        if not owner_session_id:
            raise ValueError("owner_session_id is required")
        if activation_id is not None and not activation_id:
            raise ValueError("activation_id must be non-empty")
        if activation_nonce is not None and not activation_nonce:
            raise ValueError("activation_nonce must be non-empty")
        config = normalize_native_agent_config(agent_harness, native_agent_config)
        if native_session_file is not None and not native_session_file:
            raise ValueError("native_session_file must be non-empty")
        scope_key = f"{agent_harness}\0{owner_session_id}\0{activation_id or ''}"
        if activation_nonce is not None:
            scope_key += f"\0{activation_nonce}"
        digest = hashlib.sha256(scope_key.encode()).hexdigest()[:20]
        scope_id = f"scope_{digest}"
        with self.state.owner_lock(agent_harness, owner_session_id):
            previous = [
                scope
                for scope in self.state.records()
                if (
                    scope.get("agent_harness") == agent_harness
                    and scope.get("owner_session_id") == owner_session_id
                )
            ]
            current = newest_scope(iter(previous))
            if any(
                scope.get("state") in {"closing", "cleanup_failed"}
                for scope in previous
            ):
                raise RuntimeError(
                    "execution scope cleanup is incomplete; new work is fenced"
                )
            if (
                not self.state.exists(scope_id)
                and current is not None
                and activation_nonce is None
                and (
                    current.get("state") == "open"
                    or current.get("activation_id") == activation_id
                )
            ):
                scope_id = str(current["scope_id"])
            with self.state.lock(scope_id):
                if self.state.exists(scope_id):
                    scope = self.state.read(scope_id)
                    if scope.get("state") != "open":
                        raise RuntimeError(
                            f"execution scope {scope_id} is {scope.get('state')}; new work is fenced"
                        )
                    stored = scope.get("native_agent_config")
                    if config is not None and stored is not None and stored != config:
                        raise RuntimeError(
                            f"execution scope {scope_id} native agent config changed"
                        )
                    stored_session_file = scope.get("native_session_file")
                    if (
                        native_session_file is not None
                        and stored_session_file is not None
                        and stored_session_file != native_session_file
                    ):
                        raise RuntimeError(
                            f"execution scope {scope_id} native session file changed"
                        )
                else:
                    now = self.clock.timestamp()
                    scope = {
                        "schema_version": SCOPE_SCHEMA_VERSION,
                        "scope_id": scope_id,
                        "agent_harness": agent_harness,
                        "owner_session_id": owner_session_id,
                        "generation": max(
                            (int(item.get("generation", 0)) for item in previous),
                            default=0,
                        )
                        + 1,
                        "activation_id": activation_id,
                        "state": "open",
                        "run_ids": [],
                        "fenced_run_ids": [],
                        "created_at": now,
                        "created_at_ns": time.time_ns(),
                        "updated_at": now,
                    }
                    if activation_nonce is not None:
                        scope["activation_nonce"] = activation_nonce
                        scope["resume_barrier_id"] = activation_nonce
                if config is not None:
                    scope["native_agent_config"] = config
                if native_session_file is not None:
                    scope["native_session_file"] = native_session_file
                if (
                    attachment is not None
                    and attachment.identity not in scope["run_ids"]
                ):
                    scope["run_ids"].append(attachment.identity)
                if activation_id is not None:
                    scope["activation_id"] = activation_id
                self.state.write(scope)
                return dict(scope)
