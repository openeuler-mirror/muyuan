"""File-backed append-only storage for the shared cache fact log."""

from __future__ import annotations

import json
from pathlib import Path

from goal_plus.domain.shared_cache import SharedCacheRecord, SharedCacheSpec
from goal_plus.infrastructure.state.frozen_spec import load_frozen_spec


class FileSharedCacheState:
    """Persists cache records as one JSON file per append.

    Storage layout::

        <root>/runs/<run_id>/shared-cache/<record_id>.json

    Appends are single-file atomic writes (temp file + ``os.replace``), so a
    crash can leave at most a stale temp file, never a torn record.  The
    record set is fully derived by listing the directory, so no separate
    index needs recovery.
    """

    def __init__(self, root_dir: Path | str) -> None:
        self.root_dir = Path(root_dir).resolve()

    def cache_dir(self, run_id: str) -> Path:
        return self.root_dir / "runs" / run_id / "shared-cache"

    def append_cache_record(self, run_id: str, record: SharedCacheRecord) -> None:
        directory = self.cache_dir(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        from goal_plus.infrastructure.state.files import write_json

        write_json(directory / f"{record.record_id}.json", record.model_dump())

    def load_cache_records(self, run_id: str) -> list[SharedCacheRecord]:
        directory = self.cache_dir(run_id)
        if not directory.exists():
            return []
        records = [
            SharedCacheRecord.model_validate(json.loads(path.read_text("utf-8")))
            for path in sorted(directory.glob("*.json"))
        ]
        records.sort(key=lambda record: record.created_at)
        return records

    def read_shared_cache_spec(self, frozen_spec_id: str) -> SharedCacheSpec:
        frozen = load_frozen_spec(
            self.root_dir / "specs" / frozen_spec_id / "frozen_spec.json"
        )
        return frozen.spec.shared_cache
