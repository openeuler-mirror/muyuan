"""Provider-native recovery operations and host-owned approval reads."""

from pathlib import Path
import uuid
from typing import Any
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.search import CandidateRecovery
from goal_plus.infrastructure.state.files import load_json
from goal_plus.ports.state import CandidateRecord
from goal_plus.workspaces import LocalGitWorkspace, require_local_backend


class ProviderCandidateRecovery:
    def new_recovery_id(self) -> str:
        return f"recovery_{uuid.uuid4().hex}"

    def __init__(self, root_dir: Path | str) -> None:
        self.root_dir = Path(root_dir).resolve()

    def require_recoverable(self, record: CandidateRecord) -> None:
        if record.task.workspace_provider == "thinkthread":
            checkpoint = record.results_ledger_artifact_ref
            if checkpoint is None or checkpoint.kind != "fs_snapshot" or checkpoint.provider != "thinkthread":
                raise RuntimeError(
                    "RecoveryCheckpointUnavailable: ThinkThread recovery requires a settled fs_snapshot"
                )
            return
        require_local_backend(record.task.workspace_provider, "recover")

    def read_approval(self, recovery_id: str) -> dict[str, Any]:
        path = self.root_dir / "host-recovery" / "approvals" / f"{recovery_id}.json"
        return load_json(path) if path.exists() else {}

    def restore(
        self,
        record: CandidateRecord,
        recovery: CandidateRecovery,
        preserved_files: dict[str, str],
    ) -> ArtifactRef:
        if record.task.workspace_provider == "thinkthread":
            artifact = recovery.point.artifact_ref
            if artifact.kind != "fs_snapshot" or artifact.provider != "thinkthread":
                raise RuntimeError(
                    "RecoveryArtifactProviderMismatch: ThinkThread recovery requires an fs_snapshot"
                )
            # ThinkThread workspaces are immutable private branches.  The host
            # closes the old Child before this method is called; the next
            # candidate session is created from this exact settled snapshot.
            # There is no local tree to copy or reset here.
            return artifact
        run_dir = self.root_dir / "runs" / record.task.run_id
        return LocalGitWorkspace(record.task.workspace_provider).recover(
            record.task.workspace,
            recovery.point.artifact_ref,
            managed_root=record.task.workspace_managed_root or record.task.workspace,
            recovery_dir=run_dir
            / "candidates"
            / record.candidate_id
            / "recoveries"
            / recovery.recovery_id,
            run_dir=run_dir,
            candidate_id=record.candidate_id,
            provider_handle=record.task.workspace_provider_handle,
            preserved_files=preserved_files,
        )

    def matches_checkpoint(
        self, record: CandidateRecord, expected: ArtifactRef, restored: ArtifactRef
    ) -> bool:
        if record.task.workspace_provider == "thinkthread":
            return expected == restored
        provider = LocalGitWorkspace(record.task.workspace_provider)
        managed = record.task.workspace_managed_root or record.task.workspace
        return not (
            provider.changed_files(managed, expected, restored, ["."])
            or provider.status(managed, ignore_runtime_noise=False)
        )

    def has_unsettled_changes(
        self, record: CandidateRecord, expected: ArtifactRef
    ) -> bool | None:
        if record.task.workspace_provider == "thinkthread":
            return None
        provider = LocalGitWorkspace(record.task.workspace_provider)
        managed = record.task.workspace_managed_root or record.task.workspace
        current = provider.current_artifact(managed)
        return bool(
            provider.status(managed, ignore_runtime_noise=True)
            or current is None
            or provider.changed_files(managed, expected, current, ["."])
        )
