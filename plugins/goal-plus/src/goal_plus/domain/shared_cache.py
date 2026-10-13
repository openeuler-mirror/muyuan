"""Shared cache domain models and projection logic.

The shared cache is an append-only fact log whose contents are projected at
read time.  Writers (candidates and the verifier) append ``SharedCacheRecord``
entries; readers see the merged projection defined by :class:`SharedCacheSpec`
sections.  Nothing is ever edited in place — corrections supersede earlier
records through ``supersedes`` chains.
"""

from __future__ import annotations

import hashlib

from goal_plus.domain.base import SearchModel


class SharedCacheSectionSpec(SearchModel):
    """One section of the cache template defined by the operator."""

    key: str
    title: str
    requirement: str
    # "any" keeps every distinct entry; "latest" collapses to the newest
    # non-superseded entry.
    cardinality: str = "any"
    requires_evidence: bool = False

    def merge_key(self, content_key: str) -> str:
        if self.cardinality == "latest":
            return self.key
        return f"{self.key}:{content_key}"


class SharedCacheSpec(SearchModel):
    """Cache template frozen together with the SearchSpec."""

    enabled: bool = False
    sections: tuple[SharedCacheSectionSpec, ...] = ()
    # Upper bounds mirroring shared_dir limits; None means framework default.
    max_records: int | None = None
    max_content_chars: int | None = None
    # Whether the verifier always receives a cache snapshot in its input.
    verifier_input: bool = True
    # Whether the verifier may propose cache updates alongside verdicts.
    verifier_updates: bool = True

    def section(self, key: str) -> SharedCacheSectionSpec | None:
        for section in self.sections:
            if section.key == key:
                return section
        return None


class SharedCacheRecord(SearchModel):
    """One append-only entry written by a candidate or the verifier."""

    record_id: str
    section: str
    # "candidate:<id>" or "verifier"
    writer: str
    content: str
    # Record id this entry corrects, if any.
    supersedes: str | None = None
    # Evidence reference required for objective sections.
    evidence_ref: str | None = None
    content_hash: str = ""
    created_at: str = ""

    def derives_content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


def new_shared_cache_record(
    *,
    record_id: str,
    section: str,
    writer: str,
    content: str,
    supersedes: str | None = None,
    evidence_ref: str | None = None,
    created_at: str = "",
) -> SharedCacheRecord:
    """Build a record stamped with its content hash; created_at supplied by caller."""
    record = SharedCacheRecord(
        record_id=record_id,
        section=section,
        writer=writer,
        content=content,
        supersedes=supersedes,
        evidence_ref=evidence_ref,
        created_at=created_at,
    )
    return record.model_copy(
        update={"content_hash": record.derives_content_hash()}
    )


def normalize_content(content: str, *, max_chars: int | None) -> str:
    """Collapse whitespace and enforce the content bound."""
    text = " ".join(content.split())
    if max_chars is not None and len(text) > max_chars:
        raise ValueError(
            f"shared cache content exceeds max_content_chars={max_chars}"
        )
    return text


class SharedCacheViewEntry(SearchModel):
    """A projected entry as shown to readers."""

    key: str
    title: str
    section_key: str
    content: str
    writer: str
    record_id: str
    superseded_by: str | None = None
    evidence_ref: str | None = None


class SharedCacheView(SearchModel):
    """Read-time projection of the fact log for a given reader."""

    entries: tuple[SharedCacheViewEntry, ...] = ()
    total_records: int = 0


def project_shared_cache(
    records: list[SharedCacheRecord],
    spec: SharedCacheSpec,
) -> SharedCacheView:
    """Merge the fact log into the template-shaped view.

    Correction chains: a record whose id appears in some other record's
    ``supersedes`` field is dropped from the view (kept in the log).  For
    ``latest`` cardinality sections only the newest surviving record is kept.
    """
    superseded_ids = {
        record.supersedes
        for record in records
        if record.supersedes is not None
    }
    surviving: list[SharedCacheRecord] = [
        record for record in records if record.record_id not in superseded_ids
    ]
    section_specs = {section.key: section for section in spec.sections}
    view_records: list[SharedCacheRecord] = []
    for section_key in section_specs:
        view_records.extend(
            record
            for record in surviving
            if record.section == section_key
        )
    entries: list[SharedCacheViewEntry] = []
    for section_key, section in section_specs.items():
        section_records = [
            record for record in view_records if record.section == section_key
        ]
        if section.cardinality == "latest":
            section_records = section_records[-1:]
        for record in section_records:
            content_key = (
                hashlib.sha256(record.content.encode("utf-8")).hexdigest()[:16]
            )
            entries.append(
                SharedCacheViewEntry(
                    key=section.merge_key(content_key),
                    title=section.title,
                    section_key=section_key,
                    content=record.content,
                    writer=record.writer,
                    record_id=record.record_id,
                    superseded_by=None,
                    evidence_ref=record.evidence_ref,
                )
            )
    return SharedCacheView(
        entries=tuple(entries),
        total_records=len(records),
    )
