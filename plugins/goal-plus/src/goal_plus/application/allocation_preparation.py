"""Complete Allocation preflight before any retirement or materialization write."""

from goal_plus.application.candidate_execution import (
    require_candidate_not_recovering,
    require_current_session,
    require_worker_iteration_state,
)
from goal_plus.application.goal_authorization import GoalAuthorization
from goal_plus.domain.results_ledger import ledger_at_revision
from goal_plus.ports.allocation_preparation import (
    AllocationPreparationSource,
    PreparedAllocation,
    PreparedAllocationReplacement,
)


class AllocationPreparation:
    def __init__(
        self, source: AllocationPreparationSource, authorization: GoalAuthorization
    ) -> None:
        self._source = source
        self._authorization = authorization

    def prepare_locked(self, run_id: str, decision_id: str) -> PreparedAllocation:
        """Caller holds the run lock through preparation and its subsequent writes.

        These read values are not a durable authorization token and must not be
        cached or consumed after releasing that lock.
        """
        run = self._source.read_run(run_id)
        self._authorization.require_search_execution(run.run_id)
        require_worker_iteration_state(run, "apply allocation decision")
        frozen = self._source.read_frozen_spec(run.frozen_spec_id)
        if frozen.spec.strategy.search_scheduler is None:
            raise RuntimeError("allocation decisions require strategy.search_scheduler")
        decision = self._source.load_allocation_decision(run_id, decision_id)
        if decision.run_id != run_id:
            raise ValueError("allocation decision belongs to another run")

        records = self._source.read_candidates(run_id)
        records_by_id = {item.candidate_id: item for item in records}
        existing_sessions = {
            item.candidate_id: item
            for item in self._source.read_agent_sessions(run_id)
            if item.directive.get("allocation_decision_id") == decision_id
        }
        missing_child_ids: set[str] = set()
        prepared = []
        for replacement in decision.replacements:
            retired = records_by_id.get(replacement.retire_candidate_id)
            if retired is None:
                raise RuntimeError(
                    "allocation retire candidate is unavailable: "
                    f"{replacement.retire_candidate_id}"
                )
            if retired.retired_by_decision_id not in {None, decision_id}:
                raise RuntimeError(
                    f"candidate {retired.candidate_id} was retired by another decision"
                )
            source = records_by_id.get(replacement.source.candidate_id)
            if source is None:
                raise RuntimeError(
                    "allocation source candidate is unavailable: "
                    f"{replacement.source.candidate_id}"
                )
            if source.task.plan_id is None:
                raise RuntimeError("expansion source has no initial plan")
            iteration = next(
                (
                    item
                    for item in source.iterations
                    if item.iteration == replacement.source.iteration
                ),
                None,
            )
            if (
                iteration is None
                or (
                    iteration.workspace_git_head_after_settlement
                    or iteration.ledger_git_head
                    or iteration.git_head
                )
                != replacement.source.settled_commit
                or iteration.artifact_hash != replacement.source.artifact_hash
            ):
                raise RuntimeError(
                    "allocation source no longer matches settled Evidence"
                )
            plan = self._source.read_plan(run_id, source.task.plan_id)
            entries = ledger_at_revision(
                self._source.read_source_ledger(source),
                source.results_ledger_git_head,
                replacement.source.settled_commit,
            )
            child = records_by_id.get(replacement.new_candidate_id)
            if child is not None:
                if child.task.expansion_source != replacement.source:
                    raise RuntimeError(
                        f"candidate {replacement.new_candidate_id} does not match allocation decision"
                    )
                require_candidate_not_recovering(child)
                session = existing_sessions.get(child.candidate_id)
                if session is not None:
                    require_current_session(session, child)
            else:
                missing_child_ids.add(replacement.new_candidate_id)
            prepared.append(
                PreparedAllocationReplacement(replacement, source, plan, entries)
            )

        max_candidates = frozen.spec.budget.max_candidates
        if (
            max_candidates is not None
            and len(records) + len(missing_child_ids) > max_candidates
        ):
            raise RuntimeError("allocation decision would exceed budget.max_candidates")
        eligible_count = sum(
            item.allocation_eligibility == "eligible" for item in records
        )
        if eligible_count + len(missing_child_ids) > frozen.spec.budget.max_parallel:
            raise RuntimeError("allocation decision would exceed budget.max_parallel")
        return PreparedAllocation(run, frozen, decision, tuple(prepared))
