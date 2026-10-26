"""Persistence port for the shared knowledge fact log."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from goal_plus.domain.shared_knowledge import (
    SharedKnowledgeRecord,
    SharedKnowledgeSpec,
    SharedKnowledgeView,
)


class SharedKnowledgeState(Protocol):
    """Append-only storage for shared knowledge records and the HTML board.

    The fact log is the source of truth; views and the HTML board are always
    rebuilt from it.
    """

    def knowledge_dir(self, run_id: str) -> Path: ...

    def append_knowledge_record(
        self, run_id: str, record: SharedKnowledgeRecord
    ) -> None:
        """Persist one record; appending an existing record_id is a no-op."""

    def load_knowledge_records(self, run_id: str) -> list[SharedKnowledgeRecord]:
        """Return all records in append order."""

    def read_shared_knowledge_spec(self, frozen_spec_id: str) -> SharedKnowledgeSpec:
        """Read the knowledge spec frozen alongside the SearchSpec."""
