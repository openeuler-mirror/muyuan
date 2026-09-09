"""Execution scope fencing and cleanup, with host services injected at bootstrap."""

from dataclasses import dataclass
from typing import Any

from goal_plus.execution.codex_scope import CodexScopeRegistry, active_codex_agents
from goal_plus.execution.scope_processes import ScopeProcessRegistry
from goal_plus.execution.scope import scope_attachments
from goal_plus.ports.clock import Clock
from goal_plus.ports.scope import ScopeAttachment, ScopeCleanupServices, ScopeRecord, ScopeState


@dataclass(frozen=True)
class ScopeCleanup:
    state: ScopeState
    clock: Clock
    processes: ScopeProcessRegistry
    codex: CodexScopeRegistry
    services: ScopeCleanupServices

    def snapshot(self, *, scope_id: str) -> ScopeRecord:
        with self.state.lock(scope_id):
            return dict(self.state.read(scope_id))

    def close(
        self, *,
        scope_id: str,
        reason: str,
        timeout_seconds: float = 5.0,
    ) -> ScopeRecord:
        """Fence a scope, interrupt its resources, and record cleanup evidence."""
        with self.state.lock(scope_id):
            scope = self.state.read(scope_id)
            if scope.get("state") == "closed":
                return dict(scope)
            scope["state"] = "closing"
            if scope.get("close_reason") != "owner_process_exited":
                scope["close_reason"] = reason
            scope.setdefault("close_requested_at", self.clock.timestamp())
            self.state.write(scope)
        # Fence admission before waiting for another cleanup, without holding scope.lock.
        with self.state.cleanup_lock(scope_id):
            return self._close_resources(
                scope_id=scope_id, timeout_seconds=timeout_seconds,
            )

    def _close_resources(
        self, *, scope_id: str, timeout_seconds: float,
    ) -> ScopeRecord:
        scope = self.snapshot(scope_id=scope_id)
        if scope.get("state") == "closed":
            return scope

        snapshots: list[dict[str, Any]] = []
        errors: list[str] = []
        codex_active_count = 0
        process_count, process_errors = self.processes.stop(
            scope_id=scope_id,
            timeout_seconds=timeout_seconds,
        )
        errors.extend(process_errors)
        for attachment in scope_attachments(scope):
            closed, pool_errors = self.services.close_pools(
                attachment=attachment,
                scope_id=scope_id,
                timeout_seconds=timeout_seconds,
            )
            snapshots.extend(closed)
            errors.extend(pool_errors)
            if attachment.kind == "search_run":
                errors.extend(
                    self.services.close_drainers(
                        run_id=attachment.identity,
                        timeout_seconds=timeout_seconds,
                    )
                )
        if scope.get("host") == "codex":
            if scope.get("close_reason") == "owner_process_exited":
                self.codex.finish_after_owner_exit(scope_id=scope_id)
            else:
                active_agents = active_codex_agents(
                    self.snapshot(scope_id=scope_id)
                )
                codex_active_count = len(active_agents)
                if active_agents:
                    errors.append(
                        "codex:active_agents:"
                        + ",".join(str(item["agent_id"]) for item in active_agents)
                    )

        active_count = process_count + codex_active_count + sum(
            int(item.get("active_count") or 0) for item in snapshots
        )
        with self.state.lock(scope_id):
            latest = self.state.read(scope_id)
            latest["cleanup"] = {
                "active_count": active_count,
                "errors": errors,
                "observed_at": self.clock.timestamp(),
            }
            latest["state"] = "closed" if not errors and active_count == 0 else "cleanup_failed"
            latest["closed_at"] = self.clock.timestamp() if latest["state"] == "closed" else None
            self.state.write(latest)
            return dict(latest)

    def close_candidates(
        self, *, scope_id: str, attachment: ScopeAttachment, timeout_seconds: float = 5.0
    ) -> dict[str, Any]:
        self._fence_run(scope_id=scope_id, attachment=attachment)
        with self.state.cleanup_lock(scope_id):
            return self._close_candidate_resources(
                scope_id=scope_id, attachment=attachment, timeout_seconds=timeout_seconds,
            )

    def _close_candidate_resources(
        self, *, scope_id: str, attachment: ScopeAttachment, timeout_seconds: float,
    ) -> dict[str, Any]:
        run_id = attachment.require_search_run()
        scope = self.snapshot(scope_id=scope_id)
        if scope.get("state") != "open":
            raise RuntimeError(f"execution scope {scope_id} is not open")
        if run_id not in scope.get("run_ids", []):
            raise PermissionError(
                f"execution scope {scope_id} does not own Search run {run_id}"
            )
        snapshots, errors = self.services.close_pools(
            attachment=attachment,
            scope_id=scope_id,
            timeout_seconds=timeout_seconds,
        )
        process_count, process_errors = self.processes.stop(
            scope_id=scope_id,
            timeout_seconds=timeout_seconds,
            attachment=attachment,
            kind="candidate",
        )
        errors.extend(process_errors)
        scope = self.snapshot(scope_id=scope_id)
        codex_active_count = 0
        if scope.get("host") == "codex":
            active_agents = active_codex_agents(scope, attachment=attachment)
            codex_active_count = len(active_agents)
            if active_agents:
                errors.append(
                    "codex:active_agents:"
                    + ",".join(str(item["agent_id"]) for item in active_agents)
                )
            if not self.services.codex_run_is_quiescent(run_id):
                errors.append(f"codex:run_not_quiescent:{run_id}")
        active_count = process_count + codex_active_count + sum(
            int(item.get("active_count") or 0) for item in snapshots
        )
        if errors or active_count:
            raise RuntimeError(
                f"Search run {run_id} did not become quiescent: "
                f"active_count={active_count}, errors={errors}"
            )
        return {"run_id": run_id, "active_count": 0, "pools": snapshots}

    def _fence_run(
        self, *, scope_id: str, attachment: ScopeAttachment
    ) -> None:
        run_id = attachment.require_search_run()
        with self.state.lock(scope_id):
            scope = self.state.read(scope_id)
            if scope.get("state") != "open":
                raise RuntimeError(f"execution scope {scope_id} is not open")
            if run_id not in scope.get("run_ids", []):
                raise PermissionError(
                    f"execution scope {scope_id} does not own Search run {run_id}"
                )
            fenced = scope.setdefault("fenced_run_ids", [])
            if run_id not in fenced:
                fenced.append(run_id)
                self.state.write(scope)
