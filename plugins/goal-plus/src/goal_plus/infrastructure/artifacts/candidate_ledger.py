"""Git-backed candidate ledger bytes, integrity checks and checkpoint I/O."""

import subprocess
from pathlib import Path
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.agent_hosts import candidate_inner_agent_binding
from goal_plus.domain.search import FrozenSpec
from goal_plus.domain.results_ledger import LEGACY_RESULTS_TSV_RELATIVE_PATH
from goal_plus.domain.evidence import ResultLedgerEntry
from goal_plus.domain.results_ledger import (
    RESULTS_TSV_RELATIVE_PATH,
    render_results_tsv,
)
from goal_plus.infrastructure.artifacts.git import git_output
from goal_plus.infrastructure.state.files import write_text
from goal_plus.infrastructure.state.results_ledger import read_candidate_results_ledger
from goal_plus.ports.state import CandidateRecord
from goal_plus.workspaces import LocalGitWorkspace, list_files


class FileCandidateLedgerWorkspace:
    def has_legacy_projection(self, record: CandidateRecord) -> bool:
        return any(
            (record.task.workspace / path).is_file()
            for path in (
                RESULTS_TSV_RELATIVE_PATH,
                LEGACY_RESULTS_TSV_RELATIVE_PATH,
            )
        )

    def bound_projection(self, frozen: FrozenSpec, record: CandidateRecord) -> str:
        return candidate_inner_agent_binding(
            frozen, record.task
        ).workspace_ledger_projection

    def read(self, record: CandidateRecord) -> list[ResultLedgerEntry]:
        return read_candidate_results_ledger(record)

    def initialize(
        self, record: CandidateRecord, text: str, message: str
    ) -> ArtifactRef:
        write_text(record.task.workspace / RESULTS_TSV_RELATIVE_PATH, text)
        self._initialize_git(record)
        artifact = self.commit(record, message)
        if artifact is None:
            raise RuntimeError(
                "ResultsLedgerCommitError: failed to commit workspace/results.tsv"
            )
        return artifact

    def check(
        self,
        record: CandidateRecord,
        metric_name: str,
    ) -> Path:
        path = record.task.workspace / RESULTS_TSV_RELATIVE_PATH
        expected = render_results_tsv(record.results_ledger, metric_name)
        try:
            actual = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(
                "ResultsLedgerMutation: workspace/results.tsv is missing; "
                "the runtime-owned ledger may not be deleted or replaced"
            ) from exc
        if actual != expected:
            raise RuntimeError(
                "ResultsLedgerMutation: workspace/results.tsv changed outside "
                "the runtime; existing rows are immutable and only one "
                "runtime-appended row is allowed per returned verifier report"
            )
        provider = LocalGitWorkspace(record.task.workspace_backend)
        if provider.current_artifact(record.task.workspace) is None:
            raise RuntimeError(
                "ResultsLedgerMutation: candidate workspace has no valid scoped "
                "Git history"
            )
        try:
            status = git_output(
                record.task.workspace,
                [
                    "git",
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                    "--",
                    RESULTS_TSV_RELATIVE_PATH,
                ],
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(
                "ResultsLedgerMutation: could not verify the Git state of "
                "workspace/results.tsv"
            ) from exc
        if status.strip():
            raise RuntimeError(
                "ResultsLedgerMutation: workspace/results.tsv must stay clean "
                "between runtime appends"
            )
        return path

    def _initialize_git(self, record: CandidateRecord) -> None:
        workspace = record.task.workspace
        provider = LocalGitWorkspace(record.task.workspace_backend)
        if provider.current_artifact(workspace) is not None:
            return
        if record.task.workspace_backend != "copy":
            raise RuntimeError(
                "ResultsLedgerCommitError: candidate workspace has no usable "
                "Git repository for workspace/results.tsv"
            )
        files = [
            path.relative_to(workspace).as_posix() for path in list_files(workspace)
        ]
        try:
            git_output(workspace, ["git", "init", "-q"])
            git_output(workspace, ["git", "add", "-f", "--", *files])
            git_output(
                workspace,
                [
                    "git",
                    "-c",
                    "user.name=goal-plus",
                    "-c",
                    "user.email=goal-plus@example.invalid",
                    "commit",
                    "-q",
                    "--no-verify",
                    "-m",
                    "search candidate baseline",
                ],
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(
                "ResultsLedgerCommitError: candidate workspace has no usable "
                "Git repository for workspace/results.tsv"
            ) from exc

    def write(self, record: CandidateRecord, text: str) -> None:
        write_text(record.task.workspace / RESULTS_TSV_RELATIVE_PATH, text)

    def commit(
        self,
        record: CandidateRecord,
        message: str,
    ) -> ArtifactRef | None:
        return LocalGitWorkspace(record.task.workspace_backend).checkpoint(
            record.task.workspace,
            message,
            [RESULTS_TSV_RELATIVE_PATH],
        )
