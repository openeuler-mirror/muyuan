"""Existing shared-tool snapshot and publication I/O behind a narrow port."""

from pathlib import Path
import uuid
from goal_plus.ports.shared_tools import ToolDraft
from goal_plus.domain.shared_tools import SharedToolRecord
from typing import Any
from goal_plus.domain.shared_tools import SharedDirSpec
from goal_plus.ports.shared_tools import SharedDirSettlement, StagingInspection
from goal_plus.shared_dir import SharedDirManager


class FileToolPublicationWorkspace:
    def new_receipt_id(self) -> str:
        return f"copy_{uuid.uuid4().hex[:24]}"

    def materialize_tool(
        self, run_id: str, tool: SharedToolRecord, destination: Path
    ) -> None:
        self._manager(run_id).materialize_tool(tool, destination)

    def stage(
        self,
        run_id: str,
        workspace: Path,
        share_out: Path,
        limits: SharedDirSpec,
        draft: ToolDraft,
    ) -> dict[str, Any]:
        return self._manager(run_id).stage_tool(
            workspace=workspace,
            share_out_dir=share_out,
            name=draft.name,
            summary=draft.summary,
            entrypoint=draft.entrypoint,
            candidate_relative_source_paths=draft.candidate_relative_source_paths,
            publication_intent=draft.publication_intent,
            supersedes_tool_id=draft.supersedes_tool_id,
            capability_ids=draft.capability_ids,
            coverage_keys=draft.coverage_keys,
            max_tools=limits.max_tools_per_iteration,
            max_files=limits.max_files_per_iteration,
            max_bytes=limits.max_bytes_per_iteration,
            max_path_entries=limits.max_path_entries_per_iteration,
            max_depth=limits.max_depth,
        )

    def remove_inbox(self, path: Path) -> None:
        import shutil

        shutil.rmtree(path, ignore_errors=True)

    def __init__(self, root_dir: Path | str) -> None:
        self.root_dir = Path(root_dir)

    def _manager(self, run_id: str) -> SharedDirManager:
        return SharedDirManager(self.root_dir / "runs" / run_id)

    def inspect_staging(
        self, run_id: str, share_out_dir: Path, max_tools: int
    ) -> StagingInspection:
        inventory = self._manager(run_id).inspect_staging(
            share_out_dir, max_tools=max_tools, deep=False
        )
        return {"entries": inventory["entries"], "errors": inventory["errors"]}

    def settle_iteration(
        self,
        *,
        run_id: str,
        candidate_id: str,
        iteration: int,
        source_commit: str | None,
        share_out_dir: Path,
        limits: SharedDirSpec,
        adopted_tool_ids: set[str],
    ) -> SharedDirSettlement:
        return self._manager(run_id).settle_iteration(
            candidate_id=candidate_id,
            iteration=iteration,
            source_commit=source_commit,
            share_out_dir=share_out_dir,
            max_tools=limits.max_tools_per_iteration,
            max_files=limits.max_files_per_iteration,
            max_bytes=limits.max_bytes_per_iteration,
            max_path_entries=limits.max_path_entries_per_iteration,
            max_depth=limits.max_depth,
            max_pending_revisions_per_family=limits.max_pending_revisions_per_family,
            max_published_versions_per_family=limits.max_published_versions_per_family,
            adopted_tool_ids=adopted_tool_ids,
        )

    def tool_family_catalog(
        self, run_id: str, *, max_published_versions_per_family: int
    ) -> list[dict[str, Any]]:
        return self._manager(run_id).tool_family_catalog(
            max_published_versions_per_family=max_published_versions_per_family
        )

    def discoverable_tool_ids(self, run_id: str) -> set[str]:
        return self._manager(run_id).discoverable_tool_ids()

    def recover_candidate_publications(
        self,
        run_id: str,
        candidate_id: str,
        committed_tool_ids: set[str],
        recovery_id: str,
    ) -> None:
        self._manager(run_id).recover_candidate_publications(
            candidate_id,
            committed_tool_ids,
            self.root_dir
            / "runs"
            / run_id
            / "candidates"
            / candidate_id
            / "recoveries"
            / recovery_id,
        )
