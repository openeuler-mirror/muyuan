"""Search settlement locks and derived best checkpoint in the existing layout."""

from contextlib import ExitStack, contextmanager
from goal_plus.domain.evidence import BestArtifactRecord
from goal_plus.infrastructure.state.candidate_creation import FileCandidateCreationState
from goal_plus.infrastructure.state.files import exclusive_file_lock, write_json
from goal_plus.ports.state import CandidateRecord


class FileCandidateSettlementState(FileCandidateCreationState):
    def lock_run(self, run_id: str):
        return exclusive_file_lock(self.run_dir(run_id) / "run.lock")

    @contextmanager
    def lock_verifier(self, run_id: str, candidate_id: str, *, blocking: bool = True):
        with ExitStack() as stack:
            try:
                stack.enter_context(exclusive_file_lock(
                    self.candidate_dir(run_id, candidate_id) / "verifier.lock", blocking=blocking
                ))
            except BlockingIOError as exc:
                raise RuntimeError("VerifierInProgress: candidate verifier is already active") from exc
            yield

    def relative_workspace(self, run_id: str, record: CandidateRecord) -> str:
        return (
            record.task.workspace.resolve()
            .relative_to(self.run_dir(run_id).resolve())
            .as_posix()
        )

    def write_best(self, best: BestArtifactRecord) -> None:
        write_json(
            self.run_dir(best.run_id) / "best.json", best.model_dump(mode="json")
        )
