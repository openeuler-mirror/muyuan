"""Shared cache read/append use cases.

Admission rules (C-invariants from the design doc) are enforced here so the
storage layer stays dumb.  All writes take the run lock; reads take none —
the fact log is append-only and a read can at worst miss a concurrent
append, which the next read will pick up.
"""

from __future__ import annotations

from typing import Any, Literal

from goal_plus.application.candidate_sessions import CandidateSessions
from goal_plus.application.candidate_execution import (
    require_worker_iteration_state,
)
from goal_plus.domain.search import FrozenSpec
from goal_plus.domain.shared_cache import (
    SharedCacheRecord,
    SharedCacheSpec,
    SharedCacheView,
    new_shared_cache_record,
    normalize_content,
    project_shared_cache,
)
from goal_plus.ports.candidate_settlement import CandidateSettlementState
from goal_plus.ports.clock import Clock
from goal_plus.ports.shared_cache import SharedCacheState

_CACHE_WRITER_VERIFIER = "verifier"


class SharedCache:
    """Read and append the run-scoped shared cache."""

    def __init__(
        self,
        source: CandidateSettlementState,
        cache: SharedCacheState,
        sessions: CandidateSessions,
        clock: Clock,
    ) -> None:
        self.source, self.cache, self.sessions = source, cache, sessions
        self.clock = clock

    # ------------------------------------------------------------------ read

    def view_for_run(self, run_id: str, frozen: FrozenSpec) -> SharedCacheView:
        """Project the full fact log; used by verifier input and debug tools."""
        records = self.cache.load_cache_records(run_id)
        return project_shared_cache(records, frozen.spec.shared_cache)

    def context_snapshot(
        self, frozen: FrozenSpec, run_id: str
    ) -> dict[str, Any]:
        """Template + current entries for agent context and verifier input."""
        spec = frozen.spec.shared_cache
        view = project_shared_cache(
            self.cache.load_cache_records(run_id), spec
        )
        return {
            "sections": [
                {
                    "key": section.key,
                    "title": section.title,
                    "requirement": section.requirement,
                    "cardinality": section.cardinality,
                    "requires_evidence": section.requires_evidence,
                }
                for section in spec.sections
            ],
            "entries": [entry.model_dump(mode="json") for entry in view.entries],
            "total_records": view.total_records,
            "updates_allowed": spec.verifier_updates,
        }

    def view_for_candidate(
        self, agent_session_id: str
    ) -> tuple[FrozenSpec, SharedCacheView]:
        """Project the cache for one candidate session (manual/auto mode)."""
        session = self.sessions.session(agent_session_id)
        frozen = self.source.read_frozen_spec(
            self.source.read_run(session.run_id).frozen_spec_id
        )
        if not frozen.spec.shared_cache.enabled:
            raise ValueError("shared cache requires shared_cache.enabled=true")
        records = self.cache.load_cache_records(session.run_id)
        return frozen, project_shared_cache(records, frozen.spec.shared_cache)

    # ----------------------------------------------------------------- write

    def append(
        self,
        agent_session_id: str,
        *,
        section: str,
        content: str,
        supersedes: str | None = None,
        evidence_ref: str | None = None,
    ) -> dict[str, Any]:
        session = self.sessions.session(agent_session_id)
        with self.source.lock_verifier(session.run_id, session.candidate_id):
            with self.source.lock_run(session.run_id):
                self.sessions.session(session.agent_session_id, session.run_id)
                run = self.source.read_run(session.run_id)
                self.sessions.authorization.require_search_execution(run.run_id)
                require_worker_iteration_state(run, "append shared cache")
                frozen = self.source.read_frozen_spec(run.frozen_spec_id)
                spec = frozen.spec.shared_cache
                if not spec.enabled:
                    raise ValueError(
                        "cache append requires shared_cache.enabled=true"
                    )
                self._append_locked(
                    run_id=run.run_id,
                    spec=spec,
                    section=section,
                    writer=f"candidate:{session.candidate_id}",
                    content=content,
                    supersedes=supersedes,
                    evidence_ref=evidence_ref,
                    nonce=session.agent_session_id,
                )
                view = project_shared_cache(
                    self.cache.load_cache_records(run.run_id), spec
                )
                return {
                    "section": section,
                    "writer": f"candidate:{session.candidate_id}",
                    "view": _view_payload(view),
                }

    def append_verifier_updates(
        self,
        run_id: str,
        frozen: FrozenSpec,
        updates: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        """Apply verifier-proposed cache updates (proposal-ledger pattern).

        Called from settlement when a verifier result carries cache_updates.
        Each update is validated independently; failures are collected and
        returned, never raised, so one bad proposal cannot abort settlement.
        """
        spec = frozen.spec.shared_cache
        if not spec.enabled or not spec.verifier_updates or not updates:
            return []
        applied: list[dict[str, Any]] = []
        with self.source.lock_run(run_id):
            existing = {
                record.record_id: record
                for record in self.cache.load_cache_records(run_id)
            }
            for index, update in enumerate(updates):
                outcome = self._apply_verifier_update(
                    run_id=run_id,
                    spec=spec,
                    existing=existing,
                    update=update,
                    index=index,
                )
                if outcome is not None:
                    applied.append(outcome)
        return applied

    # --------------------------------------------------------------- private

    def _append_locked(
        self,
        *,
        run_id: str,
        spec: SharedCacheSpec,
        section: str,
        writer: str,
        content: str,
        supersedes: str | None,
        evidence_ref: str | None,
        nonce: str,
    ) -> SharedCacheRecord:
        section_spec = spec.section(section)
        if section_spec is None:
            raise ValueError(
                f"unknown cache section {section!r}; expected one of "
                f"{[s.key for s in spec.sections]}"
            )
        if supersedes is not None:
            existing = {
                record.record_id: record
                for record in self.cache.load_cache_records(run_id)
            }
            target = existing.get(supersedes)
            if target is None:
                raise ValueError(f"supersedes target {supersedes!r} not found")
            if target.section != section:
                raise ValueError(
                    "supersedes target must be in the same section"
                )
        if section_spec.requires_evidence and not evidence_ref:
            raise ValueError(
                f"cache section {section!r} requires evidence_ref"
            )
        try:
            normalized = normalize_content(content, max_chars=spec.max_content_chars)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if not normalized:
            raise ValueError("cache content must not be empty")
        records = self.cache.load_cache_records(run_id)
        if spec.max_records is not None and len(records) >= spec.max_records:
            raise ValueError(
                f"shared cache reached max_records={spec.max_records}"
            )
        record_id = f"{run_id}:{nonce}"
        # Idempotency: a retried write with the same id and same content is a
        # no-op; a same-id write with different content advances to a fresh id
        # (we hold the run lock, so ids cannot race).
        record = new_shared_cache_record(
            record_id=record_id,
            section=section,
            writer=writer,
            content=normalized,
            supersedes=supersedes,
            evidence_ref=evidence_ref,
            created_at=self.clock.timestamp(),
        )
        while True:
            existing_record = next(
                (
                    item
                    for item in records
                    if item.record_id == record.record_id
                ),
                None,
            )
            if existing_record is None:
                break
            if existing_record.content_hash == record.content_hash:
                return existing_record
            collision = record.record_id
            suffix = collision.rsplit("-", 1)[-1]
            suffix = str(int(suffix) + 1) if suffix.isdigit() else f"{suffix}-1"
            record = record.model_copy(
                update={"record_id": f"{collision.rsplit('-', 1)[0]}-{suffix}"}
            )
        self.cache.append_cache_record(run_id, record)
        return record

    def _apply_verifier_update(
        self,
        *,
        run_id: str,
        spec: SharedCacheSpec,
        existing: dict[str, SharedCacheRecord],
        update: dict[str, Any],
        index: int,
    ) -> dict[str, Any] | None:
        section = update.get("section")
        content = update.get("content")
        if not isinstance(section, str) or not isinstance(content, str):
            return {
                "index": index,
                "status": "rejected",
                "reason": "section and content must be strings",
            }
        supersedes = update.get("supersedes")
        evidence_ref = update.get("evidence_ref")
        try:
            record = self._append_locked(
                run_id=run_id,
                spec=spec,
                section=section,
                writer=_CACHE_WRITER_VERIFIER,
                content=content,
                supersedes=supersedes if isinstance(supersedes, str) else None,
                evidence_ref=(
                    evidence_ref if isinstance(evidence_ref, str) else None
                ),
                nonce=f"verifier-{index}",
            )
        except ValueError as exc:
            return {
                "index": index,
                "status": "rejected",
                "reason": str(exc),
            }
        return {
            "index": index,
            "status": "applied",
            "record_id": record.record_id,
        }


def _view_payload(view: SharedCacheView) -> list[dict[str, Any]]:
    return [entry.model_dump() for entry in view.entries]

