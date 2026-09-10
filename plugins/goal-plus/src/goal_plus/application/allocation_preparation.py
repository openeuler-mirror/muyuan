"""Complete Allocation preflight before retirement or materialization writes."""

from goal_plus.application.candidate_execution import (
    require_candidate_not_recovering,
    require_current_session,
    require_worker_iteration_state,
)
from goal_plus.application.goal_authorization import GoalAuthorization
from goal_plus.domain.results_ledger import ledger_through_iteration
from goal_plus.ports.allocation_preparation import (
    AllocationPreparationSource,
    PreparedAllocation,
    PreparedAllocationExpansion,
)


class AllocationPreparation:
    def __init__(
        self, source: AllocationPreparationSource, authorization: GoalAuthorization
    ) -> None:
        self._source = source
        self._authorization = authorization

    def prepare_locked(self, run_id: str, decision_id: str) -> PreparedAllocation:
        """Return values valid only while the caller retains the run lock."""

        run = self._source.read_run(run_id)
        self._authorization.require_search_execution(run.run_id)
        require_worker_iteration_state(run, "apply allocation decision")
        frozen = self._source.read_frozen_spec(run.frozen_spec_id)
        if frozen.spec.strategy.allocation is None:
            raise RuntimeError("allocation decisions require strategy.allocation")
        decision = self._source.load_allocation_decision(run_id, decision_id)
        if decision.run_id != run_id:
            raise ValueError("allocation decision belongs to another run")
        if decision.status not in {"pending", "applied"}:
            raise RuntimeError(
                f"allocation decision is already {decision.status}: {decision.error}"
            )
        try:
            snapshot = self._source.load_search_snapshot(
                run_id, decision.based_on_snapshot_revision
            )
        except FileNotFoundError as exc:
            raise ValueError(
                "allocation decision references a missing immutable snapshot"
            ) from exc
        if (
            snapshot.run_id != run_id
            or snapshot.frozen_spec_id != frozen.frozen_spec_id
            or snapshot.policy_id != decision.policy_id
        ):
            raise ValueError("allocation decision references an invalid snapshot")
        snapshot_candidates = {item.candidate_id: item for item in snapshot.candidates}

        records = self._source.read_candidates(run_id)
        records_by_id = {item.candidate_id: item for item in records}
        existing_sessions = {
            item.candidate_id: item
            for item in self._source.read_agent_sessions(run_id)
            if item.directive.get("allocation_decision_id") == decision_id
        }
        missing_child_count = 0
        prepared: list[PreparedAllocationExpansion] = []
        for retire_candidate_id, expansion in zip(
            decision.retire_candidate_ids, decision.expansions, strict=True
        ):
            retired = records_by_id.get(retire_candidate_id)
            if retired is None:
                raise RuntimeError(
                    f"allocation retire candidate is unavailable: {retire_candidate_id}"
                )
            if retired.retired_by_decision_id not in {None, decision_id}:
                raise RuntimeError(
                    f"candidate {retired.candidate_id} was retired by another decision"
                )
            if (
                decision.status == "pending"
                and retired.allocation_eligibility != "eligible"
                and retired.retired_by_decision_id != decision_id
            ):
                raise RuntimeError(
                    f"candidate {retired.candidate_id} is no longer allocation eligible"
                )
            source = records_by_id.get(expansion.source.candidate_id)
            if source is None:
                raise RuntimeError(
                    "allocation source candidate is unavailable: "
                    f"{expansion.source.candidate_id}"
                )
            if source.task.plan_id is None:
                raise RuntimeError("expansion source has no initial plan")
            iteration = next(
                (
                    item
                    for item in source.iterations
                    if item.iteration == expansion.source.iteration
                    and item.settlement_id == expansion.source.settlement_id
                ),
                None,
            )
            expected_node_id = (
                f"{source.candidate_id}/iteration-{expansion.source.iteration:04d}"
            )
            if (
                iteration is None
                or iteration.settlement_receipt is None
                or expected_node_id != expansion.source.node_id
                or iteration.settlement_receipt.run_id != run_id
                or iteration.settlement_receipt.candidate_id != source.candidate_id
                or iteration.settlement_receipt.attempt_artifact_ref
                != expansion.source.attempt_artifact_ref
                or iteration.settlement_receipt.attempt_artifact_hash
                != expansion.source.attempt_artifact_hash
                or iteration.settlement_receipt.ledger_artifact_ref
                != expansion.source.ledger_artifact_ref
            ):
                raise RuntimeError(
                    "allocation source no longer matches settled Evidence"
                )
            snapshot_source = snapshot_candidates.get(source.candidate_id)
            snapshot_retired = snapshot_candidates.get(retired.candidate_id)
            snapshot_node = next(
                (
                    item
                    for item in (snapshot_source.nodes if snapshot_source else [])
                    if item.iteration == expansion.source.iteration
                    and item.settlement_id == expansion.source.settlement_id
                ),
                None,
            )
            if (
                snapshot_retired is None
                or snapshot_retired.allocation_eligibility != "eligible"
                or snapshot_node is None
                or snapshot_node.node_id != expansion.source.node_id
                or snapshot_node.verifier_record_id
                != expansion.source.verifier_record_id
            ):
                raise ValueError(
                    "allocation decision does not match its immutable snapshot"
                )
            plan = self._source.read_plan(run_id, source.task.plan_id)
            entries = ledger_through_iteration(
                self._source.read_source_ledger(source),
                run_id,
                source.candidate_id,
                expansion.source.iteration,
            )
            child_id = expansion.new_candidate_id
            child = records_by_id.get(child_id) if child_id is not None else None
            if child is not None:
                if child.task.expansion_source != expansion.source:
                    raise RuntimeError(
                        f"candidate {child_id} does not match allocation decision"
                    )
                require_candidate_not_recovering(child)
                session = existing_sessions.get(child.candidate_id)
                if session is not None:
                    require_current_session(session, child)
            else:
                missing_child_count += 1
            prepared.append(
                PreparedAllocationExpansion(
                    expansion, retire_candidate_id, source, plan, entries
                )
            )

        max_candidates = frozen.spec.budget.max_candidates
        if (
            max_candidates is not None
            and len(records) + missing_child_count > max_candidates
        ):
            raise RuntimeError("allocation decision would exceed budget.max_candidates")
        eligible_count = sum(
            item.allocation_eligibility == "eligible" for item in records
        )
        newly_retired = sum(
            records_by_id[candidate_id].allocation_eligibility == "eligible"
            for candidate_id in decision.retire_candidate_ids
        )
        if (
            eligible_count - newly_retired + missing_child_count
            > frozen.spec.budget.max_parallel
        ):
            raise RuntimeError("allocation decision would exceed budget.max_parallel")
        return PreparedAllocation(run, frozen, decision, tuple(prepared))
