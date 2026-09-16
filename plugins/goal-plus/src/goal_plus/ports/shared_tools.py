"""Shared-tool settlement output and immutable publication I/O inputs."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypedDict
from goal_plus.domain.shared_tools import (
    SharedDirSpec,
    SharedToolRecord,
    SharedToolPublishStatus,
    ToolizationAdvisory,
)


@dataclass
class SharedDirSettlement:
    tools: list[SharedToolRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    staged_entries: list[str] = field(default_factory=list)
    staged_file_count: int = 0
    staged_bytes: int = 0
    consumed_entries: list[str] = field(default_factory=list)
    deduplicated_entries: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SharedToolIterationSettlement:
    tools: list[SharedToolRecord]
    errors: list[str]
    staged_entries: list[str]
    staged_file_count: int
    staged_bytes: int
    consumed_entries: list[str]
    deduplicated_entries: list[str]
    publish_status: SharedToolPublishStatus
    toolization_advisories: list[ToolizationAdvisory]


class StagingInspection(TypedDict):
    entries: list[str]
    errors: list[str]


@dataclass(frozen=True)
class ToolDraft:
    name: str
    summary: str
    entrypoint: str
    candidate_relative_source_paths: list[str]
    publication_intent: str
    supersedes_tool_id: str | None
    capability_ids: list[str] | None
    coverage_keys: list[str] | None


class ToolPublicationWorkspace(Protocol):
    def stage(
        self,
        run_id: str,
        workspace: Path,
        share_out: Path,
        limits: SharedDirSpec,
        draft: ToolDraft,
    ) -> dict[str, Any]: ...
    def new_receipt_id(self) -> str: ...
    def materialize_tool(
        self, run_id: str, tool: SharedToolRecord, destination: Path
    ) -> None: ...
    def remove_inbox(self, path: Path) -> None: ...
    def staging_path(
        self, run_id: str, candidate_id: str, generation: int, attempt_id: str
    ) -> Path: ...
    def capture_staging(self, run_id: str, source: Path, destination: Path) -> None: ...
    def publish_committed(self, run_id: str, tools: list[SharedToolRecord]) -> None: ...
    def finish_staging(
        self, run_id: str, staging: Path, share_out: Path, consumed: list[str]
    ) -> None: ...
    def inspect_staging(
        self, run_id: str, share_out_dir: Path, max_tools: int
    ) -> StagingInspection: ...
    def prepare_iteration(
        self,
        *,
        run_id: str,
        candidate_id: str,
        iteration: int,
        source_commit: str | None,
        share_out_dir: Path,
        limits: SharedDirSpec,
        adopted_tool_ids: set[str],
    ) -> SharedDirSettlement: ...
    def tool_family_catalog(
        self, run_id: str, *, max_published_versions_per_family: int
    ) -> list[dict[str, Any]]: ...
    def discoverable_tool_ids(self, run_id: str) -> set[str]: ...
    def archive_staging(
        self,
        run_id: str,
        candidate_id: str,
        staging: Path,
        recovery_id: str,
    ) -> None: ...
