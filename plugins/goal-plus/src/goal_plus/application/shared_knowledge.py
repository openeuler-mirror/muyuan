"""Shared knowledge board read/append use cases.

Admission rules are enforced here so the storage layer stays dumb.  All
writes take the run lock; reads take none -- the fact log is append-only and
a read can at worst miss a concurrent append, which the next read picks up.

Beyond the former shared cache this service enforces the uniform content
format (``type`` first key, ``subject`` second key, fact/think evidence
rules), deduplicates identical facts via ``(section, content_hash)``
idempotency, surfaces same-subject contradictions instead of hiding them,
and filters reads under ``independent`` evidence mode.
"""

from __future__ import annotations

import json
from typing import Any

from goal_plus.application.candidate_sessions import CandidateSessions
from goal_plus.application.candidate_execution import (
    require_worker_iteration_state,
)
from goal_plus.domain.search import FrozenSpec
from goal_plus.domain.shared_knowledge import (
    SharedKnowledgeRecord,
    SharedKnowledgeSpec,
    SharedKnowledgeView,
    new_shared_knowledge_record,
    normalize_content_values,
    project_shared_knowledge,
    validate_entry,
)
from goal_plus.domain.worker_policy import global_evidence_mode
from goal_plus.ports.candidate_settlement import CandidateSettlementState
from goal_plus.ports.clock import Clock
from goal_plus.ports.shared_knowledge import SharedKnowledgeState

_KNOWLEDGE_WRITER_VERIFIER = "verifier"


def _verifier_content_dict(content: str) -> dict[str, str]:
    """Accept section-shaped JSON or plain text as a verifier update payload."""

    try:
        payload = json.loads(content)
    except ValueError:
        payload = None
    if (
        isinstance(payload, dict)
        and payload
        and all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in payload.items()
        )
    ):
        return payload
    return {"text": content}


class SharedKnowledge:
    """Read and append the run-scoped shared knowledge board."""

    def __init__(
        self,
        source: CandidateSettlementState,
        knowledge: SharedKnowledgeState,
        sessions: CandidateSessions,
        clock: Clock,
    ) -> None:
        self.source, self.knowledge, self.sessions = source, knowledge, sessions
        self.clock = clock

    # ------------------------------------------------------------------ read

    def view_for_run(self, run_id: str, frozen: FrozenSpec) -> SharedKnowledgeView:
        """Project the full fact log; used by verifier input and debug tools."""
        records = self.knowledge.load_knowledge_records(run_id)
        return project_shared_knowledge(records, frozen.spec.shared_knowledge)

    def context_snapshot(
        self, frozen: FrozenSpec, run_id: str
    ) -> dict[str, Any]:
        """Template + current entries for agent context and verifier input."""
        spec = frozen.spec.shared_knowledge
        view = project_shared_knowledge(
            self.knowledge.load_knowledge_records(run_id), spec
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
    ) -> tuple[FrozenSpec, SharedKnowledgeView]:
        """Project the board for one candidate session.

        Under ``independent`` evidence mode the reader sees only entries it
        wrote itself, mirroring the Global Evidence visibility contract.
        """
        session = self.sessions.session(agent_session_id)
        frozen = self.source.read_frozen_spec(
            self.source.read_run(session.run_id).frozen_spec_id
        )
        if not frozen.spec.shared_knowledge.enabled:
            raise ValueError(
                "shared knowledge requires shared_knowledge.enabled=true"
            )
        records = self.knowledge.load_knowledge_records(session.run_id)
        if global_evidence_mode(frozen.spec.strategy.config) == "independent":
            own_writer = f"candidate:{session.candidate_id}"
            records = [
                record for record in records if record.writer == own_writer
            ]
        return frozen, project_shared_knowledge(
            records, frozen.spec.shared_knowledge
        )

    # ----------------------------------------------------------------- write

    def append(
        self,
        agent_session_id: str,
        *,
        section: str,
        entry_type: str,
        subject: str,
        content: dict[str, str],
        confidence: str | None = None,
        limits: str | None = None,
        supersedes: str | None = None,
        evidence_ref: str | None = None,
    ) -> dict[str, Any]:
        session = self.sessions.session(agent_session_id)
        with self.source.lock_verifier(session.run_id, session.candidate_id):
            with self.source.lock_run(session.run_id):
                self.sessions.session(session.agent_session_id, session.run_id)
                run = self.source.read_run(session.run_id)
                self.sessions.authorization.require_search_execution(run.run_id)
                require_worker_iteration_state(run, "append shared knowledge")
                frozen = self.source.read_frozen_spec(run.frozen_spec_id)
                spec = frozen.spec.shared_knowledge
                if not spec.enabled:
                    raise ValueError(
                        "knowledge append requires shared_knowledge.enabled=true"
                    )
                # Only facts carry an anchor: validate_entry rejects a think
                # with evidence_ref, so deriving one for settled candidates
                # would make every think append fail after their first
                # settlement.
                if evidence_ref is None and entry_type == "fact":
                    evidence_ref = self._auto_evidence_ref(
                        run.run_id, session.candidate_id
                    )
                record, deduplicated = self._append_locked(
                    run_id=run.run_id,
                    spec=spec,
                    section=section,
                    writer=f"candidate:{session.candidate_id}",
                    entry_type=entry_type,
                    subject=subject,
                    content=content,
                    confidence=confidence,
                    limits=limits,
                    supersedes=supersedes,
                    evidence_ref=evidence_ref,
                    nonce=session.agent_session_id,
                )
                records = self.knowledge.load_knowledge_records(run.run_id)
                view = project_shared_knowledge(records, spec)
                return {
                    "section": section,
                    "writer": f"candidate:{session.candidate_id}",
                    "record_id": record.record_id,
                    "deduplicated": deduplicated,
                    "contradiction_with": self._contradiction_with(records, record),
                    "view": [entry.model_dump(mode="json") for entry in view.entries],
                }

    def append_verifier_updates(
        self,
        run_id: str,
        frozen: FrozenSpec,
        updates: list[dict[str, Any]] | None,
        default_evidence_ref: str | None = None,
    ) -> list[dict[str, Any]]:
        """Apply verifier-proposed knowledge updates (proposal-ledger pattern).

        Called from settlement when a verifier result carries
        ``knowledge_updates``.  Each update is validated independently;
        failures are collected and returned, never raised, so one bad
        proposal cannot abort settlement.  ``default_evidence_ref`` anchors
        verifier facts to the iteration the verdict was delivered for.
        """
        spec = frozen.spec.shared_knowledge
        if not spec.enabled or not spec.verifier_updates or not updates:
            return []
        applied: list[dict[str, Any]] = []
        with self.source.lock_run(run_id):
            existing = {
                record.record_id: record
                for record in self.knowledge.load_knowledge_records(run_id)
            }
            for index, update in enumerate(updates):
                outcome = self._apply_verifier_update(
                    run_id=run_id,
                    spec=spec,
                    existing=existing,
                    update=update,
                    index=index,
                    default_evidence_ref=default_evidence_ref,
                )
                if outcome is not None:
                    applied.append(outcome)
        return applied

    # --------------------------------------------------------------- private

    def _auto_evidence_ref(self, run_id: str, candidate_id: str) -> str | None:
        """Derive the writer's latest settled iteration anchor.

        Writers must not hand-craft iteration anchors: the runtime derives
        the caller's latest settled iteration from the trusted session
        identity. A fact recorded before the first settlement keeps no
        anchor and is rejected with an actionable message by
        ``validate_entry``.
        """
        try:
            candidates = self.source.read_candidates(run_id)
        except Exception:  # noqa: BLE001 - anchor is best effort
            return None
        own = next(
            (
                item
                for item in candidates
                if item.candidate_id == candidate_id
            ),
            None,
        )
        if own is None or not own.iterations:
            return None
        return f"{candidate_id}:{own.iterations[-1].iteration}"

    def _append_locked(
        self,
        *,
        run_id: str,
        spec: SharedKnowledgeSpec,
        section: str,
        writer: str,
        entry_type: str,
        subject: str,
        content: dict[str, str],
        confidence: str | None,
        limits: str | None,
        supersedes: str | None,
        evidence_ref: str | None,
        nonce: str,
    ) -> tuple[SharedKnowledgeRecord, bool]:
        section_spec = spec.section(section)
        if section_spec is None:
            raise ValueError(
                f"unknown knowledge section {section!r}; expected one of "
                f"{[s.key for s in spec.sections]}"
            )
        records = self.knowledge.load_knowledge_records(run_id)
        if supersedes is not None:
            existing = {r.record_id: r for r in records}
            target = existing.get(supersedes)
            if target is None:
                raise ValueError(f"supersedes target {supersedes!r} not found")
            if target.section != section:
                raise ValueError("supersedes target must be in the same section")
        if section_spec.requires_evidence and not evidence_ref:
            raise ValueError(f"knowledge section {section!r} requires evidence_ref")
        content = normalize_content_values(content, max_chars=spec.max_content_chars)
        if not content and entry_type != "subject":
            raise ValueError("knowledge content must not be empty")
        validate_entry(entry_type, subject, content, evidence_ref=evidence_ref)
        if spec.max_records is not None and len(records) >= spec.max_records:
            raise ValueError(f"shared knowledge reached max_records={spec.max_records}")
        record_id = f"{run_id}:{nonce}"
        record = new_shared_knowledge_record(
            record_id=record_id,
            section=section,
            writer=writer,
            entry_type=entry_type,
            subject=subject,
            content=content,
            confidence=confidence,
            limits=limits,
            supersedes=supersedes,
            evidence_ref=evidence_ref,
            created_at=self.clock.timestamp(),
        )
        while True:
            existing_record = next(
                (item for item in records if item.record_id == record.record_id), None
            )
            if existing_record is None:
                break
            if existing_record.content_hash == record.content_hash:
                return existing_record, True
            collision = record.record_id
            suffix = collision.rsplit("-", 1)[-1]
            suffix = str(int(suffix) + 1) if suffix.isdigit() else f"{suffix}-1"
            record = record.model_copy(
                update={"record_id": f"{collision.rsplit('-', 1)[0]}-{suffix}"}
            )
        superseded_ids = {r.supersedes for r in records if r.supersedes is not None}
        for item in records:
            if (
                item.section == section
                and item.content_hash == record.content_hash
                and item.record_id not in superseded_ids
            ):
                return item, True
        self.knowledge.append_knowledge_record(run_id, record)
        return record, False

    def _apply_verifier_update(
        self,
        *,
        run_id: str,
        spec: SharedKnowledgeSpec,
        existing: dict[str, SharedKnowledgeRecord],
        update: dict[str, Any],
        index: int,
        default_evidence_ref: str | None = None,
    ) -> dict[str, Any] | None:
        section = update.get("section")
        content = update.get("content")
        subject = update.get("subject")
        entry_type = update.get("entry_type") or "fact"
        if not isinstance(section, str) or not isinstance(content, str):
            return {
                "index": index,
                "status": "rejected",
                "reason": "section and content must be strings",
            }
        if not isinstance(subject, str) or not subject.strip():
            return {
                "index": index,
                "status": "rejected",
                "reason": (
                    "verifier knowledge updates require a subject "
                    "(the board entry's subject line)"
                ),
            }
        supersedes = update.get("supersedes")
        evidence_ref = update.get("evidence_ref")
        if not isinstance(evidence_ref, str):
            evidence_ref = (
                default_evidence_ref if entry_type == "fact" else None
            )
        try:
            record, deduplicated = self._append_locked(
                run_id=run_id,
                spec=spec,
                section=section,
                writer=_KNOWLEDGE_WRITER_VERIFIER,
                entry_type=entry_type,
                subject=subject,
                content=_verifier_content_dict(content),
                confidence=None,
                limits=None,
                supersedes=supersedes if isinstance(supersedes, str) else None,
                evidence_ref=evidence_ref,
                nonce=f"verifier-{index}",
            )
        except Exception as exc:  # noqa: BLE001 - one bad proposal never aborts settlement
            return {
                "index": index,
                "status": "rejected",
                "reason": f"{type(exc).__name__}: {exc}",
            }
        return {
            "index": index,
            "status": "applied",
            "record_id": record.record_id,
            "deduplicated": deduplicated,
        }

    def _contradiction_with(
        self, records: list[SharedKnowledgeRecord], record: SharedKnowledgeRecord
    ) -> list[str]:
        """Same section + same subject + different content = contradiction."""
        superseded_ids = {
            item.supersedes for item in records if item.supersedes is not None
        }
        return [
            item.record_id
            for item in records
            if item.section == record.section
            and item.record_id != record.record_id
            and item.record_id not in superseded_ids
            and item.subject == record.subject
            and item.content_hash != record.content_hash
        ]

    def _outcome_by_record(
        self, run_id: str, records: list[SharedKnowledgeRecord]
    ) -> dict[str, dict[str, Any]]:
        """Join evidence_ref anchors to settled iteration outcomes."""
        settled: dict[str, dict[str, Any]] = {}
        try:
            candidates = self.source.read_candidates(run_id)
        except Exception:  # noqa: BLE001 - outcome projection is advisory
            return {}
        for candidate in candidates:
            for iteration in candidate.iterations:
                settled[f"{candidate.candidate_id}:{iteration.iteration}"] = {
                    "iteration_ref": (
                        f"{candidate.candidate_id}:{iteration.iteration}"
                    ),
                    "score": iteration.score,
                    "disposition": iteration.disposition,
                }
        outcomes: dict[str, dict[str, Any]] = {}
        for record in records:
            if record.evidence_ref:
                outcome = settled.get(record.evidence_ref)
                if outcome is not None:
                    outcomes[record.record_id] = outcome
        return outcomes

