"""Persist Policy output once; execution never reevaluates it."""

from goal_plus.domain.adaptive.models import AllocationDecision, AllocationExpansion, AllocationPlan, SearchSnapshot
from goal_plus.domain.serialization import canonical_json, sha256_text
from goal_plus.ports.allocation import AllocationState
from goal_plus.ports.clock import Clock


class AllocationDecisions:
    def __init__(self, *, state: AllocationState, clock: Clock) -> None:
        self.state, self.clock = state, clock

    def record(self, snapshot: SearchSnapshot, plan: AllocationPlan) -> AllocationDecision:
        if plan.based_on_snapshot_revision != snapshot.snapshot_revision or plan.policy_id != snapshot.policy_id:
            raise ValueError("allocation plan does not belong to its snapshot")
        decision_id = f"allocation_{plan.plan_hash[:24]}"
        existing = next((d for d in self.state.load_allocation_decisions(snapshot.run_id)
                         if d.decision_id == decision_id), None)
        if existing is not None:
            return existing
        nodes = {n.node_id: n for c in snapshot.candidates for n in c.nodes}
        expansions = []
        for index, intent in enumerate(plan.expansions):
            node = nodes[intent.source_node_id]
            if node.source is None or node.verifier_record_id != intent.source_verifier_record_id:
                raise ValueError("allocation source has no exact identity")
            expansion_id = sha256_text(canonical_json({"plan": plan.plan_hash, "index": index}))[:24]
            expansions.append(AllocationExpansion(expansion_id=f"expansion_{expansion_id}", source=node.source,
                direction_key=intent.direction_key, rationale=intent.rationale, worker_budget=intent.worker_budget,
                eligible_retirements=intent.eligible_retirements))
        decision = AllocationDecision(decision_id=decision_id, run_id=snapshot.run_id,
            based_on_snapshot_revision=snapshot.snapshot_revision, policy_id=plan.policy_id, plan_hash=plan.plan_hash,
            retire_candidate_ids=plan.retire_candidate_ids, expansions=expansions,
            reason_codes=plan.reason_codes, created_at=self.clock.timestamp(),
            verifier_sequence=snapshot.verifier_cutoff_sequence)
        self.state.write_allocation_decision(decision)
        return decision

    def list(self, run_id, status=None):
        return [d for d in self.state.load_allocation_decisions(run_id) if status is None or d.status == status]
