"""Read the run's internal-agent policy without starting or stopping agents."""

from goal_plus.application.candidate_execution import WORKER_ITERATION_RUN_STATES
from goal_plus.ports.internal_agents import InternalAgentRun, InternalAgentRunSource


class InternalAgentWork:
    def __init__(self, source: InternalAgentRunSource) -> None:
        self._source = source

    def inspect(self, run_id: str) -> InternalAgentRun:
        run = self._source.read_run(run_id)
        frozen = self._source.read_frozen_spec(run.frozen_spec_id)
        return InternalAgentRun(
            host=frozen.spec.strategy.worker_host,
            accepts_work=not run.invalidated_at and run.state in WORKER_ITERATION_RUN_STATES,
            scheduler_enabled=frozen.spec.strategy.search_scheduler is not None,
        )
