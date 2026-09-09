"""Owned snapshot cleanup admission, durable pins and retry responsibility."""

from typing import Any, Callable
from goal_plus.ports.state import (
    RunRecord,
    ProviderMutationRecord,
    ProviderResourceCleanupRecord,
)
from goal_plus.ports.artifact_lifecycle import ArtifactLifecycleState, SnapshotRemoval
from goal_plus.ports.clock import Clock
from goal_plus.application.artifact_pins import (
    SNAPSHOT_CLEANUP_TERMINAL_RUN_STATES,
    snapshot_id_from_mutation,
    snapshot_refs_in_payload,
    snapshot_recovery_refs_in_payload,
    snapshot_refs_in_provider_identity,
    run_keeps_external_snapshot_refs,
)


class ArtifactLifecycle:
    def __init__(
        self,
        state: ArtifactLifecycleState,
        io: SnapshotRemoval,
        clock: Clock,
        persist_run: Callable[[RunRecord], None],
    ) -> None:
        self.state, self.io, self.clock, self.persist_run = (
            state,
            io,
            clock,
            persist_run,
        )

    def owned_snapshots(
        self,
        run_id: str,
    ) -> dict[str, dict[str, set[str]]]:
        owned: dict[str, dict[str, set[str]]] = {}

        def add(mutation: ProviderMutationRecord, purpose: str) -> None:
            snapshot_id = snapshot_id_from_mutation(mutation)
            if snapshot_id is None:
                return
            entry = owned.setdefault(
                snapshot_id,
                {"purposes": set(), "request_ids": set()},
            )
            entry["purposes"].add(purpose)
            entry["request_ids"].add(mutation.request_id)

        run = self.state.read_run(run_id)
        for mutation in run.provider_mutations:
            add(mutation, mutation.operation)
        for candidate in self.state.read_candidates(run_id):
            for mutation in candidate.provider_mutations:
                add(
                    mutation, f"candidate:{candidate.candidate_id}:{mutation.operation}"
                )
        return owned

    def _external_snapshot_pins(self, run_id: str) -> dict[str, set[str]]:
        pins: dict[str, set[str]] = {}
        for references in self.state.external_references(run_id):
            other_run_id, payload = references.run_id, references.run
            keep_all_refs = run_keeps_external_snapshot_refs(payload)
            referenced = snapshot_refs_in_payload(payload) if keep_all_refs else set()
            referenced.update(snapshot_recovery_refs_in_payload(payload))
            for candidate_payload in references.candidates:
                if keep_all_refs:
                    referenced.update(snapshot_refs_in_payload(candidate_payload))
                referenced.update(snapshot_recovery_refs_in_payload(candidate_payload))
            for snapshot_id in referenced:
                pins.setdefault(snapshot_id, set()).add(f"run:{other_run_id}")
        return pins

    def _thinkthread_snapshot_pins(
        self,
        run: RunRecord,
        snapshot_id: str,
        *,
        external: dict[str, set[str]],
    ) -> list[str]:
        pins = set(external.get(snapshot_id, set()))
        pins.update(
            f"pool:{pool_id}:{state}"
            for pool_id, state in self.state.pool_states(run.run_id)
            if state != "closed"
        )
        annotation = self.state.annotation_state(run.run_id)
        if annotation is not None and annotation != "closed":
            pins.add(f"annotation:{annotation}")
        if not self.state.reports_durable(run.run_id):
            pins.add("report:not_durable")
        outcome = run.publication_outcome
        if outcome is not None and outcome.state in {
            "prepared",
            "applying",
            "recovering",
            "awaiting_main_integration",
        }:
            authoritative = {
                reference.id
                for reference in (
                    outcome.base_artifact_ref,
                    outcome.selected_artifact_ref,
                    outcome.publication_artifact_ref,
                )
                if reference is not None
            }
            if snapshot_id in authoritative:
                pins.add(f"publication:{outcome.state}")
            if outcome.state in {"recovering", "awaiting_main_integration"}:
                for candidate in self.state.read_candidates(run.run_id):
                    if any(
                        iteration.artifact_ref is not None
                        and iteration.artifact_ref.id == snapshot_id
                        for iteration in candidate.iterations
                    ):
                        pins.add("evidence:publication_recovery")
        for mutation in run.provider_mutations:
            if mutation.state not in {
                "platform_mutation_started",
                "needs_recovery",
            }:
                continue
            if snapshot_id in snapshot_refs_in_provider_identity(mutation.identity):
                pins.add(
                    f"provider_recovery:{mutation.operation}:{mutation.request_id}"
                )
        for candidate in self.state.read_candidates(run.run_id):
            for mutation in candidate.provider_mutations:
                if mutation.state not in {
                    "platform_mutation_started",
                    "needs_recovery",
                }:
                    continue
                if snapshot_id in snapshot_refs_in_provider_identity(mutation.identity):
                    pins.add(
                        f"provider_recovery:{mutation.operation}:{mutation.request_id}"
                    )
        if run.state not in SNAPSHOT_CLEANUP_TERMINAL_RUN_STATES:
            pins.add(f"run:{run.state}")
        return sorted(pins)

    def record_cleanup(
        self,
        run_id: str,
        snapshot_id: str,
        *,
        purposes: set[str],
        ownership_request_ids: set[str],
        state: str,
        pins: list[str] | None = None,
        request_id: str | None = None,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        increment_attempts: bool = False,
    ) -> ProviderResourceCleanupRecord:
        with self.state.lock_run(run_id):
            run = self.state.read_run(run_id)
            record = next(
                (
                    item
                    for item in run.provider_cleanup
                    if item.resource_id == snapshot_id
                ),
                None,
            )
            now = self.clock.timestamp()
            if record is None:
                record = ProviderResourceCleanupRecord(
                    resource_id=snapshot_id,
                    purposes=sorted(purposes),
                    ownership_request_ids=sorted(ownership_request_ids),
                    state=state,  # type: ignore[arg-type]
                    pins=list(pins or []),
                    request_id=request_id,
                    attempts=1 if increment_attempts else 0,
                    result=dict(result or {}),
                    error=dict(error or {}),
                    created_at=now,
                    updated_at=now,
                )
                run.provider_cleanup.append(record)
            else:
                record.purposes = sorted(set(record.purposes) | purposes)
                record.ownership_request_ids = sorted(
                    set(record.ownership_request_ids) | ownership_request_ids
                )
                record.state = state  # type: ignore[assignment]
                record.pins = list(pins or [])
                record.request_id = request_id or record.request_id
                record.result = dict(result or {})
                record.error = dict(error or {})
                if increment_attempts:
                    record.attempts += 1
                record.updated_at = now
            self.persist_run(run)
            return record

    def cleanup(self, run_id: str) -> dict[str, Any]:
        """Reclaim only unpinned immutable snapshots proven to belong to this run."""

        self.io.reconcile_closes(run_id)
        owned = self.owned_snapshots(run_id)
        if not owned:
            return {"state": "not_applicable", "removed": [], "retained": []}
        external = self._external_snapshot_pins(run_id)
        removed: list[str] = []
        retained: list[dict[str, Any]] = []
        recovering: list[str] = []
        ordered_resources = sorted(
            owned.items(),
            key=lambda item: (
                "baseline_snapshot" in item[1]["purposes"],
                item[0],
            ),
        )
        for snapshot_id, ownership in ordered_resources:
            existing = next(
                (
                    item
                    for item in self.state.read_run(run_id).provider_cleanup
                    if item.resource_id == snapshot_id
                ),
                None,
            )
            if existing is not None and existing.state in {
                "removed",
                "already_absent",
            }:
                removed.append(snapshot_id)
                continue
            latest = self.state.read_run(run_id)
            pins = self._thinkthread_snapshot_pins(
                latest,
                snapshot_id,
                external=external,
            )
            if pins:
                self.record_cleanup(
                    run_id,
                    snapshot_id,
                    purposes=ownership["purposes"],
                    ownership_request_ids=ownership["request_ids"],
                    state="retained",
                    pins=pins,
                )
                retained.append({"snapshot_id": snapshot_id, "pins": pins})
                continue
            mutation = self.io.prepare_removal(
                run_id=run_id,
                snapshot_id=snapshot_id,
            )
            self.record_cleanup(
                run_id,
                snapshot_id,
                purposes=ownership["purposes"],
                ownership_request_ids=ownership["request_ids"],
                state="removing",
                request_id=mutation.request_id,
                increment_attempts=True,
            )
            try:
                result = self.io.remove(
                    run_id=run_id, snapshot_id=snapshot_id
                )
                if result.request_id != mutation.request_id or result.snapshot_id != snapshot_id:
                    raise RuntimeError("snapshot removal RequestId changed")
            except Exception as exc:
                self.record_cleanup(
                    run_id,
                    snapshot_id,
                    purposes=ownership["purposes"],
                    ownership_request_ids=ownership["request_ids"],
                    state="needs_recovery",
                    request_id=mutation.request_id,
                    error={"type": type(exc).__name__, "message": str(exc)[:2000]},
                )
                recovering.append(snapshot_id)
                continue
            self.record_cleanup(
                run_id,
                snapshot_id,
                purposes=ownership["purposes"],
                ownership_request_ids=ownership["request_ids"],
                state=result.state,
                request_id=mutation.request_id,
                result=result.details,
                error=result.error,
            )
            if result.state in {"removed", "already_absent"}:
                removed.append(snapshot_id)
            else:
                recovering.append(snapshot_id)
        state = (
            "needs_recovery" if recovering else "retained" if retained else "complete"
        )
        return {
            "state": state,
            "removed": removed,
            "retained": retained,
            "recovering": recovering,
        }
