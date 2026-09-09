"""Scope-owned local process registration, identity witnesses, and resource cleanup."""

import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal

from goal_plus.execution.process_identity import (
    EXITED_PROCESS_STATES, ProcessInspectionUnavailable, inspect_process,
)
from goal_plus.execution.process_resources import OwnedProcess, stop_processes
from goal_plus.execution.scope import assert_scope_admission
from goal_plus.ports.clock import Clock
from goal_plus.ports.process_identity import ProcessWitness
from goal_plus.ports.scope import ScopeAttachment, ScopeProcessRecord, ScopeRecord, ScopeState


@dataclass(frozen=True)
class ScopeProcessRegistry:
    state: ScopeState
    clock: Clock

    def snapshot(self, *, scope_id: str) -> ScopeRecord:
        with self.state.lock(scope_id):
            return dict(self.state.read(scope_id))

    def register(
        self, *,
        scope_id: str,
        pid: int,
        process_group_id: int | None,
        label: str,
        attachment: ScopeAttachment | None = None,
        kind: Literal["candidate", "internal"] = "candidate",
    ) -> str:
        """Register a process after spawn, failing closed if its owner is closing."""
        if kind not in {"candidate", "internal"} or (kind == "internal" and attachment is None):
            raise ValueError("internal execution resources require a run identity")
        if kind == "internal":
            attachment.require_search_run()
        with self.state.lock(scope_id):
            return self._register_locked(
                scope=self.state.read(scope_id), pid=pid,
                process_group_id=process_group_id, label=label, attachment=attachment, kind=kind,
            )

    def _register_locked(
        self, *, scope: ScopeRecord, pid: int, process_group_id: int | None,
        label: str, attachment: ScopeAttachment | None, kind: Literal["candidate", "internal"],
    ) -> str:
        scope_id = str(scope["scope_id"])
        if scope.get("state") != "open":
            raise RuntimeError(f"execution scope {scope_id} is not open")
        if attachment is not None:
            assert_scope_admission(scope, scope_id=scope_id, attachment=attachment, internal=kind == "internal")
        observed = inspect_process(pid)
        if observed is None or observed.state in EXITED_PROCESS_STATES:
            raise RuntimeError(f"execution process is not alive: {pid}")
        if process_group_id is not None and not (
            process_group_id == pid == observed.process_group_id
        ):
            raise ValueError("execution process must own its registered process group")
        token = hashlib.sha256(
            f"{scope_id}\0{pid}\0{observed.start_token}\0{label}".encode()
        ).hexdigest()[:24]
        resource = OwnedProcess(pid, observed.start_token, process_group_id)
        if not resource.running():
            raise RuntimeError(f"execution resource exited during registration: {pid}")
        processes = scope.setdefault("processes", [])
        processes.append(
            {
                "token": token,
                "pid": pid,
                "process_group_id": process_group_id,
                "start_token": observed.start_token,
                "witness": resource.witness.model_dump(mode="json"),
                "label": label,
                "run_id": attachment.identity if attachment is not None else None,
                **({"kind": "internal"} if kind == "internal" else {}),
                "status": "running",
                "registered_at": self.clock.timestamp(),
            }
        )
        self.state.write(scope)
        return token

    def remember(
        self, *, scope_id: str, token: str, witness: ProcessWitness,
    ) -> None:
        """Persist host-verified identities without changing resource or task state."""
        with self.state.lock(scope_id):
            scope = self.state.read(scope_id)
            for process in scope.get("processes", []):
                if process.get("token") != token or process.get("status") not in {"running", "stop_failed"}:
                    continue
                previous = ProcessWitness.model_validate(process["witness"]) if process.get("witness") else None
                if previous is not None:
                    if previous.boot_id != witness.boot_id:
                        raise ProcessInspectionUnavailable("execution process witness belongs to another boot")
                    witness = witness.model_copy(update={"members": {**previous.members, **witness.members}})
                if witness != previous:
                    process["witness"] = witness.model_dump(mode="json")
                    self.state.write(scope)
                return

    def resource(self, scope_id: str, process: ScopeProcessRecord) -> OwnedProcess:
        return OwnedProcess(
            int(process.get("pid") or 0), process.get("start_token"), process.get("process_group_id"),
            witness=ProcessWitness.model_validate(process["witness"]) if process.get("witness") else None,
            remember=lambda witness: self.remember(
                scope_id=scope_id, token=str(process["token"]), witness=witness,
            ),
            recall=lambda: self.witness(scope_id=scope_id, token=str(process["token"])),
        )

    def witness(self, *, scope_id: str, token: str) -> ProcessWitness | None:
        scope = self.snapshot(scope_id=scope_id)
        for process in scope.get("processes", []):
            if process.get("token") == token:
                return ProcessWitness.model_validate(process["witness"]) if process.get("witness") else None
        return None

    def observe(self, *, scope_id: str) -> None:
        scope = self.snapshot(scope_id=scope_id)
        if scope.get("state") == "closed":
            return
        for process in scope.get("processes", []):
            if process.get("status") in {"running", "stop_failed"}:
                if not self.resource(scope_id, process).running():
                    self.finish(scope_id=scope_id, token=str(process["token"]))

    def finish(
        self, *, scope_id: str, token: str
    ) -> None:
        with self.state.lock(scope_id):
            scope = self.state.read(scope_id)
            for process in scope.get("processes", []):
                if process.get("token") == token:
                    process["status"] = "finished"
                    process["finished_at"] = self.clock.timestamp()
                    break
            self.state.write(scope)

    def stop(
        self, *,
        scope_id: str,
        timeout_seconds: float,
        attachment: ScopeAttachment | None = None,
        kind: Literal["candidate", "internal"] | None = None,
    ) -> tuple[int, list[str]]:
        scope = self.snapshot(scope_id=scope_id)
        targets = [
            process
            for process in scope.get("processes", [])
            if process.get("status") in {"running", "stop_failed"}
            and (attachment is None or process.get("run_id") == attachment.identity)
            and (kind is None or process.get("kind", "candidate") == kind)
        ]

        outcomes = stop_processes(
            [self.resource(scope_id, item) for item in targets],
            term_timeout=timeout_seconds, kill_timeout=min(1.0, max(0.1, timeout_seconds)),
        )
        remaining_tokens = {
            item.get("token") for item, outcome in zip(targets, outcomes, strict=True) if not outcome.stopped
        }
        errors = [error for outcome in outcomes for error in outcome.errors]
        with self.state.lock(scope_id):
            latest = self.state.read(scope_id)
            tokens = {item.get("token") for item in targets}
            for process in latest.get("processes", []):
                if process.get("token") in tokens:
                    stopped = process.get("token") not in remaining_tokens
                    process["status"] = "stopped" if stopped else "stop_failed"
                    process["finished_at"] = self.clock.timestamp() if stopped else None
            self.state.write(latest)
        return len(remaining_tokens), errors

    @contextmanager
    def internal_launch_registration(
        self, *, scope_id: str, attachment: ScopeAttachment, label: str,
    ) -> Iterator[Callable[[int], str]]:
        with self.state.lock(scope_id):
            scope = self.state.read(scope_id)
            assert_scope_admission(scope, scope_id=scope_id, attachment=attachment, internal=True)
            active = True

            def register(pid: int) -> str:
                if not active:
                    raise RuntimeError("internal-agent launch admission has ended")
                return self._register_locked(
                    scope=scope, pid=pid, process_group_id=pid,
                    label=label, attachment=attachment, kind="internal",
                )

            try:
                yield register
            finally:
                active = False
