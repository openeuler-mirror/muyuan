"""Shared knowledge board domain models with structured content.

The record carries structured fields -- entry_type, subject, content dict,
confidence, limits -- instead of a flat string. Storage, agent-facing MCP
tools, and HTML rendering all use the same structure. Goal Plus defines
the container (entry_type enum, subject field, content dict, confidence,
limits) but never knows which keys live inside the content dict; those
are frozen per-run by the benchmark template.
"""

from __future__ import annotations

import hashlib
import json
import re

from goal_plus.domain.base import SearchModel

ENTRY_TYPES = ("fact", "think", "subject", "question")
EVIDENCE_REF_PATTERN = re.compile(r"^c\d+:[1-9]\d*$")


def validate_entry(
    entry_type: str,
    subject: str,
    content: dict[str, str],
    *,
    evidence_ref: str | None,
) -> None:
    """Enforce the structured content contract at admission time."""
    if entry_type not in ENTRY_TYPES:
        raise ValueError(f"entry_type must be one of {ENTRY_TYPES}")
    if not subject or not subject.strip():
        raise ValueError("subject must be non-empty")
    if not isinstance(content, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in content.items()
    ):
        raise ValueError("content must be a dict of string→string")
    if entry_type == "fact":
        if not evidence_ref:
            raise ValueError(
                "entry_type=fact requires evidence_ref=cXXX:iter; "
                "use type=think until your first verifier settlement"
            )
        if not EVIDENCE_REF_PATTERN.match(evidence_ref):
            raise ValueError(f"evidence_ref {evidence_ref!r} must match cXXX:iter")
    elif entry_type == "think" and evidence_ref is not None:
        raise ValueError("entry_type=think must not carry evidence_ref")


class SharedKnowledgeSectionSpec(SearchModel):
    """One section of the knowledge template defined by the operator."""

    key: str
    title: str
    requirement: str
    cardinality: str = "any"
    requires_evidence: bool = False
    content_keys: tuple[str, ...] = ()

    def merge_key(self, content_key: str) -> str:
        if self.cardinality == "latest":
            return self.key
        return f"{self.key}:{content_key}"


class SharedKnowledgeSpec(SearchModel):
    """Knowledge template frozen together with the SearchSpec."""

    enabled: bool = False
    sections: tuple[SharedKnowledgeSectionSpec, ...] = ()
    max_records: int | None = None
    max_content_chars: int | None = None
    verifier_input: bool = True
    verifier_updates: bool = True

    def section(self, key: str) -> SharedKnowledgeSectionSpec | None:
        for section in self.sections:
            if section.key == key:
                return section
        return None


def _canonical_payload(
    entry_type: str,
    subject: str,
    content: dict[str, str],
    confidence: str | None,
    limits: str | None,
) -> str:
    return json.dumps(
        {
            "entry_type": entry_type,
            "subject": subject,
            "content": content,
            "confidence": confidence,
            "limits": limits,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


class SharedKnowledgeRecord(SearchModel):
    """One append-only structured entry written by a candidate or verifier."""

    record_id: str
    section: str
    writer: str
    entry_type: str
    subject: str
    content: dict[str, str]
    confidence: str | None = None
    limits: str | None = None
    supersedes: str | None = None
    evidence_ref: str | None = None
    content_hash: str = ""
    created_at: str = ""

    def derives_content_hash(self) -> str:
        return hashlib.sha256(
            _canonical_payload(
                self.entry_type, self.subject, self.content,
                self.confidence, self.limits,
            ).encode("utf-8")
        ).hexdigest()


def new_shared_knowledge_record(
    *,
    record_id: str,
    section: str,
    writer: str,
    entry_type: str,
    subject: str,
    content: dict[str, str],
    confidence: str | None = None,
    limits: str | None = None,
    supersedes: str | None = None,
    evidence_ref: str | None = None,
    created_at: str = "",
) -> SharedKnowledgeRecord:
    record = SharedKnowledgeRecord(
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
        created_at=created_at,
    )
    return record.model_copy(
        update={"content_hash": record.derives_content_hash()}
    )


def normalize_content_values(content: dict[str, str], *, max_chars: int | None) -> dict[str, str]:
    """Collapse whitespace in each value and enforce the total bound."""
    total = 0
    normalized: dict[str, str] = {}
    for key, value in content.items():
        text = " ".join(value.split())
        total += len(key) + len(text)
        normalized[key] = text
    if max_chars is not None and total > max_chars:
        raise ValueError(f"shared knowledge content exceeds max_content_chars={max_chars}")
    return normalized


class SharedKnowledgeViewEntry(SearchModel):
    """A projected entry as shown to readers."""

    key: str
    title: str
    section_key: str
    entry_type: str
    subject: str
    content: dict[str, str]
    confidence: str | None = None
    limits: str | None = None
    writer: str
    record_id: str
    superseded_by: str | None = None
    evidence_ref: str | None = None


class SharedKnowledgeView(SearchModel):
    entries: tuple[SharedKnowledgeViewEntry, ...] = ()
    total_records: int = 0


def project_shared_knowledge(
    records: list[SharedKnowledgeRecord],
    spec: SharedKnowledgeSpec,
) -> SharedKnowledgeView:
    superseded_ids = {r.supersedes for r in records if r.supersedes is not None}
    surviving = [r for r in records if r.record_id not in superseded_ids]
    section_specs = {s.key: s for s in spec.sections}
    entries: list[SharedKnowledgeViewEntry] = []
    for section_key, section in section_specs.items():
        section_records = [r for r in surviving if r.section == section_key]
        if section.cardinality == "latest":
            section_records = section_records[-1:]
        for record in section_records:
            content_key = record.content_hash[:16]
            entries.append(SharedKnowledgeViewEntry(
                key=section.merge_key(content_key),
                title=section.title,
                section_key=section_key,
                entry_type=record.entry_type,
                subject=record.subject,
                content=record.content,
                confidence=record.confidence,
                limits=record.limits,
                writer=record.writer,
                record_id=record.record_id,
                superseded_by=None,
                evidence_ref=record.evidence_ref,
            ))
    return SharedKnowledgeView(entries=tuple(entries), total_records=len(records))


__all__ = [
    "ENTRY_TYPES",
    "SharedKnowledgeSectionSpec",
    "SharedKnowledgeSpec",
    "SharedKnowledgeRecord",
    "SharedKnowledgeViewEntry",
    "SharedKnowledgeView",
    "validate_entry",
    "new_shared_knowledge_record",
    "normalize_content_values",
    "project_shared_knowledge",
]
