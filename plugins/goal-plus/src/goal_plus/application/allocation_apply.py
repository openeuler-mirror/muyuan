"""Apply a durable Decision and prepare provenance, without supervising workers."""

from goal_plus.application.allocation_preparation import AllocationPreparation
from goal_plus.application.candidate_execution import require_current_session
from goal_plus.domain.search import AgentSessionRecord, CandidateProposal
from goal_plus.ports.allocation import AllocationCandidateProjection
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

    def apply(self, run_id: str, decision_id: str) -> AllocationApplied:
        with self._state.lock_run(run_id):
            prepared = self._preparation.prepare_locked(run_id, decision_id)
            run, frozen, decision = prepared.run, prepared.frozen, prepared.decision
            for item in prepared.replacements:
                replacement = item.replacement
                self._state.write_retirement(
                    run_id,
                    AllocationCandidateProjection(
                        candidate_id=replacement.retire_candidate_id,
                        eligibility="retired",
                        retired_by_decision_id=decision_id,
                    ),
                )
                child_id = replacement.new_candidate_id
                child = self._state.find_candidate(run_id, child_id)
                if child is not None:
                    if child.task.expansion_source != replacement.source:
                        raise RuntimeError(
                            f"candidate {child_id} does not match allocation decision"
                        )
                    continue
                proposal = CandidateProposal(
                    intent=replacement.exploration_direction,
                    instructions=[
                        "这是 scheduler 从已验证父 Evidence 派生的新 candidate。",
                        "把 expansion_source 视为初始 incumbent，独立执行 scheduler 指定的探索方向，不依赖父 worker transcript。",
                    ],
                    metadata={
                        "allocation_decision_id": decision_id,
                        "expansion_source_candidate_id": replacement.source.candidate_id,
                        "expansion_source_iteration": replacement.source.iteration,
                        "exploration_direction": replacement.exploration_direction,
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
                        expansion_source=replacement.source,
                        source_record=item.source,
                        results_ledger=item.results_ledger,
                    )
                )
                run.candidates_total += 1

            # Candidate records precede the applied anchor; the run count is a
            # recoverable projection. Session preparation happens after unlock.
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
        for replacement in decision.replacements:
            candidate_id = replacement.new_candidate_id
            existing = existing_sessions.get(candidate_id)
            if existing is not None:
                sessions.append(require_current_session(
                    existing, self._state.read_candidate(run_id, candidate_id)
                ))
                continue
            sessions.append(self._start_session(
                run_id,
                candidate_id,
                {
                    "search_scheduler_expansion": True,
                    "allocation_decision_id": decision_id,
                    "expansion_source": replacement.source.model_dump(mode="json"),
                    "exploration_direction": replacement.exploration_direction,
                    "resume_instruction": (
                        "先读取权威 candidate context 和当前 Global Evidence，再从已验证"
                        "初始 incumbent 执行 scheduler 给出的探索方向。"
                    ),
                },
                worker_budget=(
                    replacement.worker_budget.model_dump(mode="json")
                    if replacement.worker_budget is not None else None
                ),
            ))
        return AllocationApplied(
            decision=self._state.load_allocation_decision(run_id, decision_id),
            candidate_tasks=[
                self._state.read_candidate(run_id, item.new_candidate_id).task
                for item in decision.replacements
            ],
            agent_sessions=sessions,
        )
