"""Edit-surface projection and ledger-preserving artifact restoration."""

from goal_plus.domain.edit_surface import path_matches
from goal_plus.domain.results_ledger import (
    RESULTS_TSV_RELATIVE_PATH,
    render_results_tsv,
)
from goal_plus.domain.agents import WORKSPACE_LEDGER_PROJECTION_RESULTS_TSV
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.search import FrozenSpec
from goal_plus.ports.state import CandidateRecord, RunRecord

from goal_plus.ports.candidate_artifacts import (
    CandidateArtifactState,
    CandidateArtifactWorkspace,
)
from goal_plus.ports.candidate_ledger import CandidateLedgerWorkspace


class CandidateArtifacts:
    def __init__(
        self, workspace: CandidateArtifactWorkspace, ledger: CandidateLedgerWorkspace
    ) -> None:
        self.workspace, self.ledger = workspace, ledger

    def candidate_artifact_state(
        self,
        run: RunRecord,
        frozen: FrozenSpec,
        record: CandidateRecord,
    ) -> CandidateArtifactState:
        workspace = record.task.workspace
        changed_files = self.workspace.candidate_changed_files(run, record)
        touched_denied = any(
            path_matches(path, frozen.spec.edit_surface.deny) for path in changed_files
        )
        outside_allowed = any(
            not path_matches(path, frozen.spec.edit_surface.allow)
            for path in changed_files
        )
        max_changes = frozen.spec.edit_surface.max_file_changes
        if max_changes is not None and len(changed_files) > max_changes:
            outside_allowed = True
        artifact_ref = self.workspace.current_artifact(record.task)
        git_head = (
            artifact_ref.id
            if artifact_ref is not None and artifact_ref.kind == "git_commit"
            else None
        )
        artifact_status = self.workspace.status(record.task)
        artifact_clean = artifact_ref is not None and not self.workspace.status(
            record.task,
            ignore_runtime_noise=True,
        )
        return CandidateArtifactState(
            changed_files=changed_files,
            touched_denied_files=touched_denied,
            changed_outside_allowed=outside_allowed,
            artifact_hash=self.workspace.artifact_hash(workspace, changed_files),
            artifact_ref=artifact_ref,
            artifact_status=artifact_status,
            artifact_clean=artifact_clean,
            git_head=git_head,
            git_status=artifact_status,
            git_artifact_clean=artifact_clean if git_head is not None else False,
        )

    @staticmethod
    def apply_candidate_artifact_state(
        record: CandidateRecord,
        state: CandidateArtifactState,
    ) -> None:
        record.detected_changed_files = list(state.changed_files)
        record.touched_denied_files = state.touched_denied_files
        record.changed_outside_allowed = state.changed_outside_allowed

    def restore_candidate_artifact(
        self,
        record: CandidateRecord,
        metric_name: str,
        workspace_ledger_projection: str,
        revision: str | ArtifactRef,
        message: str,
    ) -> ArtifactRef:
        preserved_files: dict[str, str] = {}
        if workspace_ledger_projection == WORKSPACE_LEDGER_PROJECTION_RESULTS_TSV:
            self.ledger.check(record, metric_name)
            preserved_files[RESULTS_TSV_RELATIVE_PATH] = render_results_tsv(
                record.results_ledger,
                metric_name,
            )
        artifact_ref = (
            revision
            if isinstance(revision, ArtifactRef)
            else ArtifactRef(
                kind="git_commit",
                id=revision,
                provider=record.task.workspace_provider,
            )
        )
        try:
            restored = self.workspace.restore(
                record.task,
                artifact_ref,
                preserved_files,
                message,
            )
        except RuntimeError as exc:
            raise RuntimeError(
                f"failed to restore candidate artifact from {artifact_ref.id}"
            ) from exc
        if workspace_ledger_projection == WORKSPACE_LEDGER_PROJECTION_RESULTS_TSV:
            self.ledger.check(record, metric_name)
        return restored
