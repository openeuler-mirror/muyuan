"""Operational launch receipts; process lifetime belongs to the native host."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from goal_plus.execution.scope import assert_scope_admission
from goal_plus.ports.clock import Clock
from goal_plus.ports.scope import ScopeAttachment, ScopeRecord, ScopeState


@dataclass(frozen=True)
class ScopeProcessRegistry:
    state: ScopeState
    clock: Clock

    def register(
        self,
        *,
        scope_id: str,
        pid: int,
        label: str,
        attachment: ScopeAttachment | None = None,
        kind: Literal["candidate", "internal"] = "candidate",
    ) -> str:
        if kind not in {"candidate", "internal"} or (
            kind == "internal" and attachment is None
        ):
            raise ValueError("internal execution resources require a run identity")
        if kind == "internal":
            attachment.require_search_run()
        with self.state.lock(scope_id):
            return self._register_locked(
                scope=self.state.read(scope_id),
                pid=pid,
                label=label,
                attachment=attachment,
                kind=kind,
            )

    def _register_locked(
        self,
        *,
        scope: ScopeRecord,
        pid: int,
        label: str,
        attachment: ScopeAttachment | None,
        kind: Literal["candidate", "internal"],
    ) -> str:
        scope_id = str(scope["scope_id"])
        if scope.get("state") != "open":
            raise RuntimeError(f"execution scope {scope_id} is not open")
        if attachment is not None:
            assert_scope_admission(
                scope,
                scope_id=scope_id,
                attachment=attachment,
                internal=kind == "internal",
            )
        token = uuid4().hex
        scope.setdefault("processes", []).append(
            {
                "token": token,
                "pid": pid,
                "label": label,
                "run_id": attachment.identity if attachment is not None else None,
                "kind": kind,
                "status": "registered",
                "registered_at": self.clock.timestamp(),
            }
        )
        self.state.write(scope)
        return token

    def finish(self, *, scope_id: str, token: str) -> None:
        with self.state.lock(scope_id):
            scope = self.state.read(scope_id)
            for process in scope.get("processes", []):
                if process.get("token") == token:
                    process["status"] = "finished"
                    process["finished_at"] = self.clock.timestamp()
                    self.state.write(scope)
                    return

    def release(
        self,
        *,
        scope_id: str,
        attachment: ScopeAttachment | None = None,
        kind: Literal["candidate", "internal"] | None = None,
    ) -> None:
        """Release journal entries without asserting that any OS process exited."""
        with self.state.lock(scope_id):
            scope = self.state.read(scope_id)
            for process in scope.get("processes", []):
                if (
                    process.get("status") == "registered"
                    and (
                        attachment is None
                        or process.get("run_id") == attachment.identity
                    )
                    and (kind is None or process.get("kind", "candidate") == kind)
                ):
                    process["status"] = "released"
                    process["finished_at"] = self.clock.timestamp()
            self.state.write(scope)

    @contextmanager
    def internal_launch_registration(
        self,
        *,
        scope_id: str,
        attachment: ScopeAttachment,
        label: str,
    ) -> Iterator[Callable[[int], str]]:
        with self.state.lock(scope_id):
            scope = self.state.read(scope_id)
            assert_scope_admission(
                scope, scope_id=scope_id, attachment=attachment, internal=True
            )
            active = True

            def register(pid: int) -> str:
                if not active:
                    raise RuntimeError("internal-agent launch admission has ended")
                return self._register_locked(
                    scope=scope,
                    pid=pid,
                    label=label,
                    attachment=attachment,
                    kind="internal",
                )

            try:
                yield register
            finally:
                active = False
