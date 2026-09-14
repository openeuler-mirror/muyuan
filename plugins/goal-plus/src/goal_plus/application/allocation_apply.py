"""Apply one durable deterministic Allocation decision."""

from goal_plus.application.allocation_preparation import AllocationPreparation
from goal_plus.application.candidate_execution import require_current_session
from goal_plus.domain.search import AgentSessionRecord, CandidateProposal
from goal_plus.ports.allocation_apply import (
    AllocationApplied,
    AllocationApplyState,
    AllocationMaterializationProjection,
)
from goal_plus.ports.candidate_creation import (
    CandidateMaterialization,
    MaterializeCandidate,
    PrepareInitialSession,
)
from goal_plus.ports.clock import Clock


class AllocationApply:
    def __init__(
        self,
        *,
        state: AllocationApplyState,
        preparation: AllocationPreparation,
        materialize: MaterializeCandidate,
        start_session: PrepareInitialSession,
        clock: Clock,
    ) -> None:
        self._state = state
        self._preparation = preparation
        self._materialize = materialize
        self._start_session = start_session
        self._clock = clock

    def _assign_candidate_ids_locked(self, run_id: str, decision_id: str):
        decision = self._state.load_allocation_decision(run_id, decision_id)
        if decision.status != "pending" or all(
            item.new_candidate_id is not None for item in decision.expansions
        ):
            return decision
        run = self._state.read_run(run_id)
        used_ids = {item.candidate_id for item in self._state.read_candidates(run_id)}
        used_ids.update(
            expansion.new_candidate_id
            for item in self._state.load_allocation_decisions(run_id)
            for expansion in item.expansions
            if expansion.new_candidate_id is not None
        )
        expansions = []
        for expansion in decision.expansions:
            candidate_id = expansion.new_candidate_id
            if candidate_id is None:
                while f"c{run.next_candidate_index:03d}" in used_ids:
                    run.next_candidate_index += 1
                candidate_id = f"c{run.next_candidate_index:03d}"
                run.next_candidate_index += 1
                used_ids.add(candidate_id)
            expansions.append(
                expansion.model_copy(update={"new_candidate_id": candidate_id})
            )
        decision = decision.model_copy(update={"expansions": expansions})
        self._state.write_allocation_decision(decision)
        self._state.write_candidate_index(run_id, run.next_candidate_index)
        return decision

    def _mark_failed_locked(
        self, run_id: str, decision_id: str, exc: Exception
    ) -> None:
        try:
            decision = self._state.load_allocation_decision(run_id, decision_id)
        except FileNotFoundError:
            return
        if decision.status != "pending":
            return
        status = "terminal_error" if isinstance(exc, ValueError) else "superseded"
        self._state.write_allocation_decision(
            decision.model_copy(
                update={"status": status, "error": f"{type(exc).__name__}: {exc}"}
            )
        )

    def apply(self, run_id: str, decision_id: str) -> AllocationApplied:
        with self._state.lock_run(run_id):
            try:
                prepared = self._preparation.prepare_locked(run_id, decision_id)
                if any(
                    item.expansion.new_candidate_id is None
                    for item in prepared.expansions
                ):
                    self._assign_candidate_ids_locked(run_id, decision_id)
                    prepared = self._preparation.prepare_locked(run_id, decision_id)
            except Exception as exc:
                self._mark_failed_locked(run_id, decision_id, exc)
                raise
            run, frozen, decision = prepared.run, prepared.frozen, prepared.decision
            for item in prepared.expansions:
                expansion = item.expansion
                self._state.write_retirement(
                    run_id, item.retire_candidate_id, decision_id
                )
                child_id = expansion.new_candidate_id
                assert child_id is not None
                child = self._state.find_candidate(run_id, child_id)
                if child is not None:
                    if child.task.expansion_source != expansion.source:
                        raise RuntimeError(
                            f"candidate {child_id} does not match allocation decision"
                        )
                    continue
                proposal = CandidateProposal(
                    intent=expansion.direction_key,
                    instructions=[
                        "这是确定性 allocation policy 从已验证父 Evidence 派生的新 candidate。",
                        "把 expansion_source 视为初始 incumbent，独立执行指定方向，不依赖父 worker transcript。",
                    ],
                    metadata={
                        "allocation_decision_id": decision_id,
                        "expansion_id": expansion.expansion_id,
                        "expansion_source_candidate_id": expansion.source.candidate_id,
                        "expansion_source_iteration": expansion.source.iteration,
                        "direction_key": expansion.direction_key,
                    },
                )
                self._materialize(
                    CandidateMaterialization(
                        run=run,
                        frozen=frozen,
                        candidate_id=child_id,
                        plan=item.plan,
                        proposal=proposal,
                        slot=int(item.source.task.strategy_metadata.get("slot") or 1),
                        expansion_source=expansion.source,
                        source_record=item.source,
                        results_ledger=item.results_ledger,
                    )
                )
                run.candidates_total += 1

            if decision.status == "pending":
                decision = decision.model_copy(
                    update={"status": "applied", "applied_at": self._clock.timestamp()}
                )
                self._state.write_allocation_decision(decision)
            self._state.write_materialization_projection(
                run_id,
                AllocationMaterializationProjection(
                    candidates_total=max(
                        run.candidates_total, len(self._state.read_candidates(run_id))
                    ),
                    source_artifact_ref=run.source_artifact_ref,
                    source_subdir=run.source_subdir,
                    source_repository=run.source_repository,
                ),
            )

        existing_sessions = {
            item.candidate_id: item
            for item in self._state.read_agent_sessions(run_id)
            if item.directive.get("allocation_decision_id") == decision_id
        }
        sessions: list[AgentSessionRecord] = []
        for expansion in decision.expansions:
            candidate_id = expansion.new_candidate_id
            assert candidate_id is not None
            existing = existing_sessions.get(candidate_id)
            if existing is not None:
                sessions.append(
                    require_current_session(
                        existing, self._state.read_candidate(run_id, candidate_id)
                    )
                )
                continue
            sessions.append(
                self._start_session(
                    run_id,
                    candidate_id,
                    {
                        "adaptive_allocation_expansion": True,
                        "allocation_decision_id": decision_id,
                        "expansion_id": expansion.expansion_id,
                        "expansion_source": expansion.source.model_dump(mode="json"),
                        "direction_key": expansion.direction_key,
                        "resume_instruction": (
                            "先读取权威 candidate context 和当前 Global Evidence，再从"
                            "已验证初始 incumbent 执行确定性 policy 指定的方向。"
                        ),
                    },
                    worker_budget=(
                        expansion.worker_budget.model_dump(mode="json")
                        if expansion.worker_budget is not None
                        else None
                    ),
                )
            )
        return AllocationApplied(
            decision=self._state.load_allocation_decision(run_id, decision_id),
            candidate_tasks=[
                self._state.read_candidate(run_id, item.new_candidate_id).task
                for item in decision.expansions
                if item.new_candidate_id is not None
            ],
            agent_sessions=sessions,
        )
