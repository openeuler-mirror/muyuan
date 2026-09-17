from __future__ import annotations
from typing import Any
from goal_plus.application.candidate_ranking import (
    record_ranking_score,
    best_artifact_iteration_record,
)
from goal_plus.domain.worker_policy import (
    global_evidence_mode,
)
from goal_plus.domain.search import (
    FrozenSpec,
    RunSummary,
)
from goal_plus.ports.state import (
    CandidateRecord,
)

from goal_plus.ports.search_inspection import SearchReadState, SearchInspection
from goal_plus.application.candidate_sessions import CandidateSessions
from goal_plus.application.candidate_artifacts import CandidateArtifacts
from goal_plus.application.search_research import SearchResearch


class SearchQueries:
    def __init__(
        self,
        *,
        state: SearchReadState,
        sessions: CandidateSessions,
        research: SearchResearch,
        inspection: SearchInspection,
        artifacts: CandidateArtifacts,
    ) -> None:
        self.state = state
        self.sessions = sessions
        self.research = research
        self.inspection = inspection
        self.artifacts = artifacts

    def status(self, run_id: str) -> RunSummary:
        run = self.state.read_run(run_id)
        records = self.state.read_candidates(run_id)
        evaluated = sum(1 for record in records if record.status == "evaluated")
        return RunSummary(
            run_id=run.run_id,
            state=run.state,
            frozen_spec_id=run.frozen_spec_id,
            candidates_total=len(records),
            candidates_evaluated=evaluated,
            best_candidate_id=run.best_candidate_id,
            best_score=run.best_score,
            budget_used=run.budget_used,
            source_run_id=run.source_run_id,
            invalidated_at=run.invalidated_at,
            invalidation_reason=run.invalidation_reason,
            replacement_run_id=run.replacement_run_id,
        )

    def list_history(
        self, run_id: str, top_n: int = 5, sort_by: str = "score"
    ) -> dict[str, Any]:
        if top_n <= 0:
            raise ValueError("top_n must be > 0")
        if sort_by not in {"score", "created"}:
            raise ValueError("sort_by must be 'score' or 'created'")

        run = self.state.read_run(run_id)
        frozen = self.state.read_frozen_spec(run.frozen_spec_id)
        records = self.state.read_candidates(run_id)

        def score_value(record: CandidateRecord) -> float | None:
            return record_ranking_score(record, frozen.spec)

        def created_index(record: CandidateRecord) -> int:
            try:
                return int(record.candidate_id.removeprefix("c"))
            except ValueError:
                return 0

        if sort_by == "score":
            reverse = frozen.spec.metric_direction == "maximize"

            def score_key(record: CandidateRecord) -> tuple[int, float, int]:
                score = score_value(record)
                if score is None:
                    return (1, 0.0, created_index(record))
                sortable_score = score if reverse else -score
                return (0, -sortable_score, created_index(record))

            ordered = sorted(records, key=score_key)
        else:
            ordered = sorted(records, key=created_index)

        selected = ordered[:top_n]
        candidates = [
            self.research.history_candidate_payload(record, frozen.spec)
            for record in selected
        ]
        research_rollup = self.research.run_research_rollup(
            records,
            frozen.spec,
            visible_candidate_ids=[record.candidate_id for record in selected],
        )

        return {
            "run_id": run.run_id,
            "state": run.state,
            "frozen_spec_id": run.frozen_spec_id,
            "source_run_id": run.source_run_id,
            "inherited_research": run.inherited_research,
            "invalidated_at": run.invalidated_at,
            "invalidation_reason": run.invalidation_reason,
            "invalidation_summary": run.invalidation_summary,
            "invalidation_evidence": run.invalidation_evidence,
            "replacement_run_id": run.replacement_run_id,
            "objective": frozen.spec.objective,
            "metric_name": frozen.spec.metric_name,
            "metric_direction": frozen.spec.metric_direction,
            "strategy": frozen.spec.strategy.model_dump(mode="json"),
            "agent_harness": frozen.agent_harness,
            "runtime_provider": frozen.runtime_provider,
            "worker_policy": self.inspection.normalize_worker_policy(
                frozen
            ),
            "best_candidate_id": run.best_candidate_id,
            "best_score": run.best_score,
            "total_candidates": len(records),
            "returned_candidates": len(candidates),
            "top_n": top_n,
            "sort_by": sort_by,
            "candidates": candidates,
            "feature_ledger": research_rollup["feature_ledger"],
            "pitfalls": research_rollup["pitfalls"],
            "verifier_assessments": research_rollup["verifier_assessments"],
            "research_rollup": research_rollup,
        }

    def list_iterations(
        self,
        run_id: str,
        candidate_id: str,
    ) -> list[dict[str, Any]]:
        record = self.state.read_candidate(run_id, candidate_id)
        return [it.model_dump(mode="json") for it in record.iterations]

    def list_agent_iterations(self, agent_session_id: str) -> list[dict[str, Any]]:
        session = self.sessions.session(agent_session_id)
        return self.list_iterations(session.run_id, session.candidate_id)

    def verifier_result(
        self, run_id: str, candidate_id: str, attempt_id: str,
        agent_session_id: str | None = None, *, running: bool = False,
    ) -> dict[str, Any]:
        if agent_session_id is not None:
            session = self.sessions.session(agent_session_id)
            if (session.run_id, session.candidate_id) != (run_id, candidate_id):
                raise ValueError("GoalPlusSessionMismatch: verifier target differs from candidate session")
        record = self.state.read_candidate(run_id, candidate_id)
        pending, report = record.verification, record.score_report
        if pending is not None and pending.attempt_id != attempt_id:
            pending = None
        if report is not None and report.attempt_id == attempt_id and not running:
            return report.model_copy(update={"verification": pending}).model_dump(mode="json")
        if pending is None and (report is None or report.attempt_id != attempt_id):
            raise ValueError("VerifierAttemptUnavailable: attempt is not pending or the latest result of this candidate")
        state = "needs_recovery" if not running and pending is not None else "pending"
        return {
            "run_id": run_id, "candidate_id": candidate_id, "attempt_id": attempt_id,
            "state": state,
            "verification": pending.model_dump(mode="json") if pending is not None else None,
            "candidate_action": ("retry_verifier" if record.verification_request is not None else "handoff_to_main")
            if state == "needs_recovery" else "wait_for_verifier",
        }

    def current_process_evidence(
        self,
        run_id: str,
        candidate_id: str,
    ) -> dict[str, Any] | None:
        run = self.state.read_run(run_id)
        frozen = self.state.read_frozen_spec(run.frozen_spec_id)
        record = self.state.read_candidate(run_id, candidate_id)
        state = self.artifacts.candidate_artifact_state(run, frozen, record)
        if not state.artifact_clean:
            return None
        iteration = best_artifact_iteration_record(
            record,
            frozen.spec.metric_direction,
        )
        if iteration is None or iteration.artifact_hash != state.artifact_hash:
            return None
        return {
            "run_id": run_id,
            "candidate_id": candidate_id,
            "aggregate_score": iteration.score,
            "process_passed": True,
            "disposition": iteration.disposition,
            "best_iteration": iteration.iteration,
            "best_git_head": iteration.git_head,
            "workspace_git_head_after_settlement": state.git_head,
            "best_artifact_ref": (
                iteration.artifact_ref.model_dump(mode="json")
                if iteration.artifact_ref is not None
                else None
            ),
            "workspace_artifact_after_settlement": (
                state.artifact_ref.model_dump(mode="json")
                if state.artifact_ref is not None
                else None
            ),
            "evidence_agent_session_id": iteration.agent_session_id,
        }

    def candidate_has_unverified_changes(
        self,
        run_id: str,
        candidate_id: str,
    ) -> bool:
        run = self.state.read_run(run_id)
        frozen = self.state.read_frozen_spec(run.frozen_spec_id)
        record = self.state.read_candidate(run_id, candidate_id)
        state = self.artifacts.candidate_artifact_state(run, frozen, record)
        meaningful_status = self.inspection.git_status(
            record.task.workspace,
            ignore_runtime_noise=True,
        )
        settled_head = record.results_ledger_git_head
        if not settled_head or not state.git_head:
            return bool(state.changed_files or meaningful_status)
        if state.git_head != settled_head:
            try:
                if self.inspection.changed_files(
                    record.task.workspace,
                    settled_head,
                    state.git_head,
                ):
                    return True
            except Exception:
                return True
        return bool(meaningful_status)

    def get_agent_observability(self, agent_session_id: str) -> dict[str, Any]:
        """Return normalized, read-only host evidence for one worker session."""
        session = self.sessions.session(agent_session_id, allow_stale=True)
        return self.inspection.observability(session)

    def should_inject_global_evidence_after_verifier(self, run_id: str) -> bool:
        run = self.state.read_run(run_id)
        frozen = self.state.read_frozen_spec(run.frozen_spec_id)
        mode = global_evidence_mode(frozen.spec.strategy.config)
        return mode == "auto"

    def llm_verifier_enabled(self, run_id: str) -> bool:
        run = self.state.read_run(run_id)
        frozen = self.state.read_frozen_spec(run.frozen_spec_id)
        return frozen.spec.strategy.orchestration_mode == "adaptive_search"

    def require_adaptive_search(self, run_id: str) -> FrozenSpec:
        run = self.state.read_run(run_id)
        frozen = self.state.read_frozen_spec(run.frozen_spec_id)
        if not (frozen.spec.strategy.orchestration_mode == "adaptive_search"):
            raise RuntimeError(
                "operation requires strategy.orchestration_mode=adaptive_search"
            )
        return frozen

    def require_frozen_spec_owner(
        self,
        frozen_spec_id: str,
        goal_plus_id: str,
    ) -> FrozenSpec:
        frozen = self.state.read_frozen_spec(frozen_spec_id)
        if frozen.goal_plus_id != goal_plus_id:
            raise RuntimeError(
                "GoalPlusOwnershipMismatch: frozen spec does not belong to "
                f"Goal {goal_plus_id}"
            )
        return frozen
