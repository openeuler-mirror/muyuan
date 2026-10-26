"""Candidate creation persistence using the existing Search JSON layout."""

from goal_plus.infrastructure.state.candidate_queries import FileCandidateQueries
from goal_plus.infrastructure.state.files import write_json
from goal_plus.infrastructure.state.results_ledger import resolve_legacy_candidate_ledger
from goal_plus.ports.state import CandidateRecord


class FileCandidateCreationState(FileCandidateQueries):
    def read_candidate(self, run_id: str, candidate_id: str) -> CandidateRecord:
        return resolve_legacy_candidate_ledger(super().read_candidate(run_id, candidate_id))

    def write_candidate(self, run_id: str, record: CandidateRecord) -> None:
        directory = self.candidate_dir(run_id, record.candidate_id)
        write_json(directory / "candidate.json", record.model_dump(mode="json"))
        write_json(directory / "task.json", record.task.model_dump(mode="json"))
