"""Reference records in the existing Search layout."""

from pathlib import Path
from goal_plus.domain.artifacts import ReferenceWorkspaceRecord
from goal_plus.infrastructure.state.candidate_queries import FileCandidateQueries
from goal_plus.infrastructure.state.results_ledger import resolve_legacy_candidate_ledger
from goal_plus.infrastructure.state.files import load_json, write_json, exclusive_file_lock
from goal_plus.ports.state import CandidateRecord


class FileReferenceState(FileCandidateQueries):
    def lock_run(self, run_id: str):
        return exclusive_file_lock(self.run_dir(run_id) / "run.lock")

    def read_candidate(self, run_id: str, candidate_id: str) -> CandidateRecord:
        return resolve_legacy_candidate_ledger(super().read_candidate(run_id, candidate_id))

    def read(self, run_id: str) -> list[ReferenceWorkspaceRecord]:
        return [
            ReferenceWorkspaceRecord.model_validate(load_json(path))
            for path in sorted((self.run_dir(run_id) / "references").glob("ref_*.json"))
        ]

    def write(self, record: ReferenceWorkspaceRecord) -> None:
        write_json(
            self.run_dir(record.run_id) / "references" / f"{record.reference_id}.json",
            record.model_dump(mode="json"),
        )

    def destination(self, run_id: str, session_id: str) -> Path:
        return (
            self.run_dir(run_id) / "reference-workspaces" / session_id / "ref-workspace"
        )
