"""Persist session identity in the existing Search layout."""

from goal_plus.domain.search import AgentSessionRecord
from goal_plus.infrastructure.state.candidate_queries import FileCandidateQueries
from goal_plus.infrastructure.state.files import exclusive_file_lock, write_json
from goal_plus.ports.state import CandidateRecord, RunRecord


class FileCandidateSessionState(FileCandidateQueries):
    def write_execution_generation(self, record: CandidateRecord) -> None:
        current = self.read_candidate(record.task.run_id, record.candidate_id)
        current = CandidateRecord.model_validate({
            **current.model_dump(mode="python"),
            "execution_generation": record.execution_generation,
        })
        write_json(self.candidate_dir(record.task.run_id, record.candidate_id) / "candidate.json",
                   current.model_dump(mode="json"))

    def write_candidate_binding(self, record: CandidateRecord) -> None:
        run_id = record.task.run_id
        current = self.read_candidate(run_id, record.candidate_id)
        current.task.workspace_provider_handle = record.task.workspace_provider_handle
        current.task.workspace_branch = record.task.workspace_branch
        current.task.workspace_access.provider_handle = (
            record.task.workspace_access.provider_handle
        )
        directory = self.candidate_dir(run_id, record.candidate_id)
        write_json(directory / "candidate.json", current.model_dump(mode="json"))
        write_json(directory / "task.json", current.task.model_dump(mode="json"))

    def lock_run(self, run_id: str):
        return exclusive_file_lock(self.run_dir(run_id) / "run.lock")

    def write_session_index(self, run: RunRecord) -> None:
        current = self.read_run(run.run_id)
        current.next_agent_session_index = run.next_agent_session_index
        write_json(
            self.run_dir(run.run_id) / "run.json", current.model_dump(mode="json")
        )

    def write_agent_session(self, session: AgentSessionRecord) -> None:
        write_json(
            self.run_dir(session.run_id)
            / "agent_sessions"
            / f"{session.agent_session_id}.json",
            session.model_dump(mode="json"),
        )
