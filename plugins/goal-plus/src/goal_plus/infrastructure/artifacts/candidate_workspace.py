"""Materialize admitted candidate workspaces and their optional local layout."""

import time
from pathlib import Path

from goal_plus.domain.artifacts import (
    ArtifactRef,
    WorkspaceProvider,
    WorkspaceOperationTiming,
)
from goal_plus.domain.search import CandidateTask, FrozenSpec
from goal_plus.infrastructure.clock import utc_timestamp
from goal_plus.ports.candidate_creation import CandidateMaterialization
from goal_plus.ports.workspace_creation import (
    PreparedCandidateWorkspace,
    WorkspaceMaterialization,
)
from goal_plus.shared_dir import SHARE_OUT_RELATIVE_PATH, SharedDirManager
from goal_plus.workspaces import LocalGitWorkspace, materialize_candidate_workspace


def materialized_task_skill_paths(frozen: FrozenSpec, workspace: Path) -> list[Path]:
    workspace_root = workspace.resolve()
    paths = []
    for relative_path in frozen.task_skill_paths:
        path = workspace_root / relative_path
        try:
            path.resolve().relative_to(workspace_root)
        except ValueError as exc:
            raise RuntimeError(
                f"materialized workspace Skill file escaped the worktree: {relative_path}"
            ) from exc
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(
                f"Git-tracked task-local Skill is missing from worktree: {relative_path}"
            )
        paths.append(path)
    return paths


class FileCandidateWorkspaceCreation:
    def __init__(self, root_dir: Path | str) -> None:
        self.root_dir = Path(root_dir)

    def requires_relocation(self, workspace_provider: WorkspaceProvider) -> bool:
        return workspace_provider == "git_worktree"

    def current_artifact(self, task: CandidateTask) -> ArtifactRef | None:
        return LocalGitWorkspace(task.workspace_provider).current_artifact(
            task.workspace
        )

    def status(self, task: CandidateTask) -> list[str]:
        return LocalGitWorkspace(task.workspace_provider).status(
            task.workspace, ignore_runtime_noise=True
        )

    def prepare(self, request: CandidateMaterialization) -> PreparedCandidateWorkspace:
        run, frozen = request.run, request.frozen
        run_dir = self.root_dir / "runs" / run.run_id
        workspace = run_dir / "workspace" / request.candidate_id
        if frozen.spec.workspace.provider == "thinkthread":
            baseline = (
                request.expansion_source.attempt_artifact_ref
                if request.expansion_source is not None
                else run.source_artifact_ref
            )
            if (
                baseline is None
                or baseline.kind != "fs_snapshot"
                or baseline.provider != "thinkthread"
            ):
                raise RuntimeError(
                    "ThinkThread candidate requires an exact base snapshot"
                )
            if workspace.exists():
                raise RuntimeError(
                    "ThinkThread candidate logical workspace must not be materialized locally"
                )
            return PreparedCandidateWorkspace(
                WorkspaceMaterialization("thinkthread", workspace, None, baseline.id),
                None,
            )
        started_at, started = utc_timestamp(), time.perf_counter()
        materialization = materialize_candidate_workspace(
            workspace_provider=frozen.spec.workspace.provider,
            run_dir=run_dir,
            source=Path(run.source_path),
            workspace=workspace,
            run_id=run.run_id,
            candidate_id=request.candidate_id,
            base_revision=(
                request.expansion_source.attempt_artifact_ref.id
                if request.expansion_source is not None
                else frozen.source_artifact_ref.id
                if frozen.source_artifact_ref is not None
                else None
            ),
        )
        timing = WorkspaceOperationTiming(
            started_at=started_at,
            completed_at=utc_timestamp(),
            duration_seconds=max(0.0, time.perf_counter() - started),
            outcome="succeeded",
        )
        return PreparedCandidateWorkspace(materialization, timing)

    def task_skill_paths(self, frozen: FrozenSpec, workspace: Path) -> list[Path]:
        return materialized_task_skill_paths(frozen, workspace)

    def initialize_shared_layout(self, run_id: str, workspace: Path) -> Path:
        SharedDirManager(self.root_dir / "runs" / run_id).ensure_layout()
        share_out = workspace / SHARE_OUT_RELATIVE_PATH
        share_out.mkdir(parents=True, exist_ok=True)
        return share_out
