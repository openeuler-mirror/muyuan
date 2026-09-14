"""Read-only decoding shared by candidate host and Allocation use cases."""

from pathlib import Path

from goal_plus.domain.adaptive.models import AllocationDecision
from goal_plus.domain.search import AgentSessionRecord, FrozenSpec
from goal_plus.ports.state import CandidateRecord, RunRecord
from goal_plus.infrastructure.state.files import load_json
from goal_plus.infrastructure.state.frozen_spec import load_frozen_spec


class FileCandidateQueries:
    def __init__(self, root_dir: Path | str) -> None:
        self.root_dir = Path(root_dir).resolve()

    def run_dir(self, run_id: str) -> Path:
        return self.root_dir / "runs" / run_id

    def candidate_dir(self, run_id: str, candidate_id: str) -> Path:
        return self.run_dir(run_id) / "candidates" / candidate_id

    def read_run(self, run_id: str) -> RunRecord:
        return RunRecord.model_validate(load_json(self.run_dir(run_id) / "run.json"))

    def read_frozen_spec(self, frozen_spec_id: str) -> FrozenSpec:
        return load_frozen_spec(
            self.root_dir / "specs" / frozen_spec_id / "frozen_spec.json"
        )

    def read_candidate(self, run_id: str, candidate_id: str) -> CandidateRecord:
        return CandidateRecord.model_validate(
            load_json(self.candidate_dir(run_id, candidate_id) / "candidate.json")
        )

    def read_candidates(self, run_id: str) -> list[CandidateRecord]:
        return [
            CandidateRecord.model_validate(load_json(path))
            for path in sorted(self.run_dir(run_id).glob("candidates/*/candidate.json"))
        ]

    def read_agent_sessions(self, run_id: str) -> list[AgentSessionRecord]:
        return [
            AgentSessionRecord.model_validate(load_json(path))
            for path in sorted(
                (self.run_dir(run_id) / "agent_sessions").glob("agent_*.json")
            )
        ]

    def read_agent_session_ids(self) -> list[str]:
        return [
            path.stem
            for path in sorted((self.root_dir / "runs").glob("*/agent_sessions/*.json"))
        ]

    def read_agent_session(
        self,
        agent_session_id: str,
        run_id: str | None = None,
    ) -> AgentSessionRecord:
        if run_id is not None:
            path = (
                self.run_dir(run_id) / "agent_sessions"
            ) / f"{agent_session_id}.json"
            if path.exists():
                session = AgentSessionRecord.model_validate(load_json(path))
                return session
            raise FileNotFoundError(
                f"agent session not found: {agent_session_id} in run {run_id}"
            )

        matches = sorted(
            (self.root_dir / "runs").glob(f"*/agent_sessions/{agent_session_id}.json")
        )
        if len(matches) == 1:
            session = AgentSessionRecord.model_validate(load_json(matches[0]))
            return session
        if len(matches) > 1:
            match_runs = ", ".join(path.parents[1].name for path in matches)
            raise RuntimeError(
                f"ambiguous agent_session_id {agent_session_id}; matched runs: {match_runs}. "
                "Use the exact session_id returned by goal_plus_session_run."
            )
        raise FileNotFoundError(f"agent session not found: {agent_session_id}")

    def load_allocation_decisions(self, run_id: str) -> list[AllocationDecision]:
        return [
            AllocationDecision.model_validate(load_json(path))
            for path in sorted(
                (self.run_dir(run_id) / "allocation-decisions").glob(
                    "allocation_*.json"
                )
            )
        ]
