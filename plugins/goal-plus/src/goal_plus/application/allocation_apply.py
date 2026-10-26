"""Execute durable Decisions in order; recovery never re-enters evaluation."""

from goal_plus.application.candidate_execution import require_worker_iteration_state
from goal_plus.domain.results_ledger import ledger_through_iteration
from goal_plus.domain.search import CandidateProposal
from goal_plus.ports.allocation_apply import AllocationApplied, AllocationMaterializationProjection, DispatchRejected
from goal_plus.ports.candidate_creation import CandidateMaterialization


# One dispatch attempt plus one immediate observation of the same durable identity.
DISPATCH_ATTEMPTS = 2


class AllocationApply:
    def __init__(self, *, state, authorization, materialize, start_session, clock) -> None:
        self.state, self.authorization = state, authorization
        self.materialize, self.start_session, self.clock = materialize, start_session, clock

    def apply(self, run_id: str, decision_id: str, *, cancel, dispatch) -> AllocationApplied:
        with self.state.lock_run(run_id):
            target = self.state.load_allocation_decision(run_id, decision_id)
            self._execute_pending(run_id, cancel=cancel, dispatch=dispatch, through=target.verifier_sequence)
            return self._result(self.state.load_allocation_decision(run_id, decision_id))

    def recover(self, run_id: str, *, cancel, dispatch) -> None:
        """Resume durable pending work on host wake/start, without replaying Records."""
        with self.state.lock_run(run_id):
            self._execute_pending(run_id, cancel=cancel, dispatch=dispatch)

    def _execute_pending(self, run_id, *, cancel, dispatch, through=None):
        self.authorization.require_search_execution(run_id)
        require_worker_iteration_state(self.state.read_run(run_id), "execute allocation")
        decisions = sorted(self.state.load_allocation_decisions(run_id),
                           key=lambda d: (d.verifier_sequence, d.decision_id))
        for decision in decisions:
            if through is not None and decision.verifier_sequence > through:
                break
            if decision.status not in {"pending", "interrupted"}:
                continue
            result = self._apply_locked(run_id, decision, cancel=cancel, dispatch=dispatch)
            if result.decision.status == "interrupted":
                # Unknown native outcomes fence later Decisions, not Verifier output.
                break

    def _apply_locked(self, run_id, decision, *, cancel, dispatch):
        run = self.state.read_run(run_id)
        frozen = self.state.read_frozen_spec(run.frozen_spec_id)
        decision_id = decision.decision_id
        if decision.run_id != run_id:
            raise ValueError("allocation decision belongs to another run")
        records = self.state.read_candidates(run_id)
        if decision.selected_retirement is None:
            if frozen.spec.budget.max_candidates is not None and len(records) >= frozen.spec.budget.max_candidates:
                return self._finish(decision, "superseded", "candidate capacity exhausted")
            # Policy already ranked and constrained every option. Only check actual eligibility.
            target = next((c for target_id in decision.retire_candidate_ids for c in records
                           if c.candidate_id == target_id and c.allocation_eligibility == "eligible"
                           and any(target_id in e.eligible_retirements and not self._occupied(e, records)
                                   for e in decision.expansions)), None)
            if target is None:
                return self._finish(decision, "superseded", "no eligible retirement and expansion pair")
            decision.selected_retirement = target.candidate_id
            self.state.write_allocation_decision(decision)
        target_id = decision.selected_retirement
        target = self.state.read_candidate(run_id, target_id)
        if (target.allocation_eligibility == "retired" and target.retired_by_decision_id != decision_id
            or target.retired_by_decision_id not in {None, decision_id}):
            return self._finish(decision, "superseded", "selected target already retired")
        if target.allocation_eligibility == "retired" and target.retired_by_decision_id == decision_id:
            decision.retirement_completed = True
        if not decision.retirement_completed:
            try:
                cancel(decision)
            except (ValueError, PermissionError) as exc:
                return self._finish(decision, "terminal_error", f"retirement rejected: {exc}")
            except Exception as exc:
                return self._finish(decision, "interrupted", f"retirement outcome unknown: {exc}")
            self.state.write_retirement(run_id, target_id, decision_id)
            decision.retirement_completed = True
            decision.status, decision.error = "interrupted", None
            self.state.write_allocation_decision(decision)

        for expansion in decision.expansions:
            if expansion.status == "failed":
                failed_child = self.state.find_candidate(run_id, expansion.new_candidate_id) if expansion.new_candidate_id else None
                if failed_child is not None and failed_child.retired_by_decision_id in {None, decision_id}:
                    self.state.write_retirement(run_id, failed_child.candidate_id, decision_id)
            if expansion.status in {"skipped", "failed"}:
                continue
            records = self.state.read_candidates(run_id)
            child = next((c for c in records if c.candidate_id == expansion.new_candidate_id), None)
            if (target_id not in expansion.eligible_retirements
                or child is not None and child.allocation_eligibility == "retired"
                   and child.retired_by_decision_id != decision_id
                or self._occupied(expansion, records)):
                expansion.status, expansion.error = "skipped", "option no longer eligible"
                self.state.write_allocation_decision(decision)
                continue
            try:
                # A dispatch in doubt resumes the exact prepared session, never materialization.
                session = next((s for s in self.state.read_agent_sessions(run_id)
                                if s.candidate_id == expansion.new_candidate_id
                                and s.directive.get("allocation_decision_id") == decision_id
                                and s.directive.get("expansion_id") == expansion.expansion_id), None)
                if expansion.status not in {"dispatching", "applied"}:
                    active = sum(c.allocation_eligibility == "eligible" for c in records)
                    if (child is None or child.allocation_eligibility != "eligible") and active >= frozen.spec.budget.max_parallel:
                        raise RuntimeError("parallel capacity exhausted")
                    if child is None and frozen.spec.budget.max_candidates is not None and len(records) >= frozen.spec.budget.max_candidates:
                        raise RuntimeError("candidate capacity exhausted")
                    if expansion.new_candidate_id is None:
                        reserved = {e.new_candidate_id for d in self.state.load_allocation_decisions(run_id) for e in d.expansions}
                        used = {c.candidate_id for c in records} | reserved
                        while f"c{run.next_candidate_index:03d}" in used:
                            run.next_candidate_index += 1
                        expansion.new_candidate_id = f"c{run.next_candidate_index:03d}"
                        run.next_candidate_index += 1
                        self.state.write_allocation_decision(decision)
                        self.state.write_candidate_index(run_id, run.next_candidate_index)
                    expansion.status, expansion.error = "materializing", None
                    self.state.write_allocation_decision(decision)
                    source = self.state.read_candidate(run_id, expansion.source.candidate_id)
                    if source.task.plan_id is None:
                        raise RuntimeError("expansion source has no initial plan")
                    proposal = CandidateProposal(intent=expansion.direction_key, metadata={
                        "allocation_decision_id": decision_id, "expansion_id": expansion.expansion_id,
                        "replaces_candidate_id": target_id,
                    })
                    child = self.materialize(CandidateMaterialization(
                        run=run, frozen=frozen, candidate_id=expansion.new_candidate_id,
                        plan=self.state.read_plan(run_id, source.task.plan_id), proposal=proposal,
                        slot=int(target.task.strategy_metadata.get("slot") or 1),
                        expansion_source=expansion.source, source_record=source,
                        results_ledger=ledger_through_iteration(self.state.read_source_ledger(source),
                            run_id, source.candidate_id, expansion.source.iteration),
                    ))
                    directive = {"adaptive_allocation_expansion": True, "allocation_decision_id": decision_id,
                                 "expansion_id": expansion.expansion_id,
                                 "expansion_source": expansion.source.model_dump(mode="json")}
                    session = self.start_session(run_id, child.candidate_id, directive,
                        worker_budget=expansion.worker_budget.model_dump(mode="json") if expansion.worker_budget else None,
                        run_locked=True)
                    expansion.status = "dispatching"
                    self.state.write_allocation_decision(decision)
                if session is None:
                    raise RuntimeError("exact prepared session missing; recovery required")
                for attempt in range(DISPATCH_ATTEMPTS):
                    try:
                        dispatch(session, decision)
                        break
                    except DispatchRejected:
                        raise
                    except Exception:
                        if attempt + 1 == DISPATCH_ATTEMPTS:
                            raise
                        # Refresh persisted host intent before classifying a recovery failure.
                        session = next(s for s in self.state.read_agent_sessions(run_id)
                                       if s.agent_session_id == session.agent_session_id)
                expansion.status, expansion.error = "applied", None
                self.state.write_materialization_projection(run_id, AllocationMaterializationProjection(
                    candidates_total=len(self.state.read_candidates(run_id)),
                    source_artifact_ref=run.source_artifact_ref, source_subdir=run.source_subdir,
                    source_repository=run.source_repository))
                return self._finish(decision, "applied", None)
            except Exception as exc:
                expansion.error = f"{type(exc).__name__}: {exc}"
                if expansion.status in {"dispatching", "applied"} and not isinstance(exc, DispatchRejected):
                    self.state.write_allocation_decision(decision)
                    return self._finish(decision, "interrupted", "dispatch requires same-identity recovery")
                expansion.status = "failed"
                self.state.write_allocation_decision(decision)
                if expansion.new_candidate_id and self.state.find_candidate(run_id, expansion.new_candidate_id):
                    self.state.write_retirement(run_id, expansion.new_candidate_id, decision_id)
        return self._finish(decision, "terminal_error", "all ranked expansion options failed or were occupied")

    @staticmethod
    def _occupied(expansion, records):
        return any(c.task.expansion_source is not None
                   and c.task.expansion_source.node_id == expansion.source.node_id
                   and c.candidate_id != expansion.new_candidate_id
                   and c.allocation_eligibility == "eligible" for c in records)

    def _finish(self, decision, status, error):
        decision.status, decision.error = status, error
        if status == "applied":
            decision.applied_at = self.clock.timestamp()
        self.state.write_allocation_decision(decision)
        return self._result(decision)

    def _result(self, decision):
        child_ids = {e.new_candidate_id for e in decision.expansions if e.status == "applied"}
        return AllocationApplied(decision=decision,
            candidate_tasks=[c.task for c in self.state.read_candidates(decision.run_id) if c.candidate_id in child_ids],
            agent_sessions=[s for s in self.state.read_agent_sessions(decision.run_id)
                            if s.candidate_id in child_ids and s.directive.get("allocation_decision_id") == decision.decision_id])
