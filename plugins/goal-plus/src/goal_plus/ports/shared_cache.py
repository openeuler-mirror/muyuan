"""Persistence port for the shared cache fact log."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from goal_plus.domain.shared_cache import (
    SharedCacheRecord,
    SharedCacheSpec,
    SharedCacheView,
)


class SharedCacheState(Protocol):
    """Append-only storage for shared cache records.

    The fact log is the source of truth; views are always rebuilt from it.
    """

    def cache_dir(self, run_id: str) -> Path: ...

    def append_cache_record(self, run_id: str, record: SharedCacheRecord) -> None:
        """Persist one record; appending an existing record_id is a no-op."""

    def load_cache_records(self, run_id: str) -> list[SharedCacheRecord]:
        """Return all records in append order."""

    def read_shared_cache_spec(self, frozen_spec_id: str) -> SharedCacheSpec:
        """Read the cache spec frozen alongside the SearchSpec."""
