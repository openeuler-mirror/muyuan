"""Prepare candidate launches and exact feedback without supervising workers."""

import base64
import json
from typing import Any, Callable
from goal_plus.application.candidate_execution import require_run_not_invalidated
from goal_plus.application.candidate_queries import CandidateHostQueries
from goal_plus.application.candidate_ledger import (
    backfill_results_ledger_from_iterations,
    render_thinkthread_inner_git_results_tsv,
)
from goal_plus.domain.worker_policy import (
    candidate_worker_budget,
    normalize_worker_budget_override,
)
from goal_plus.domain.evidence import CandidateAttemptFeedback
from goal_plus.domain.search import CandidateTask, FrozenSpec
from goal_plus.ports.candidate_creation import PrepareInitialSession
from goal_plus.ports.candidate_host import (
    ThinkThreadDispatchCandidate,
    ThinkThreadPoolDispatch,
    ThinkThreadContinuation,
    ResolvedInnerAgentBinding,
    PrepareContinuation,
)


ATTEMPT_FEEDBACK_HEADER = "Goal Plus candidate attempt feedback (authoritative JSON):"


def append_candidate_attempt_feedback(
    message: str,
    feedback: CandidateAttemptFeedback,
) -> str:
    payload = json.dumps(
        feedback.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if payload in message:
        return message
    return (
        message.rstrip()
        + "\n\n"
        + ATTEMPT_FEEDBACK_HEADER
        + "\n"
        + payload
        + "\nUse this exact verifier result for the next revision."
    )


class ThinkThreadCandidatePreparation:
    def __init__(
        self,
        queries: CandidateHostQueries,
        *,
        start_session: PrepareInitialSession,
        continue_session: PrepareContinuation,
        resolve_binding: Callable[
            [FrozenSpec, CandidateTask], ResolvedInnerAgentBinding
        ],
    ) -> None:
        self.queries = queries
        self.source = queries.source
        self.start_session = start_session
        self.continue_session = continue_session
        self.resolve_binding = resolve_binding

    def prepare_thinkthread_pool_dispatch(
        self,
        *,
        run_id: str,
        candidate_ids: list[str] | None = None,
        worker_budgets: dict[str, dict[str, Any]] | None = None,
        max_parallel: int | None = None,
    ) -> ThinkThreadPoolDispatch:
        run = self.source.read_run(run_id)
        require_run_not_invalidated(run, "open ThinkThread pool")
        frozen = self.source.read_frozen_spec(run.frozen_spec_id)
        if (
            frozen.spec.strategy.worker_host != "thinkthread"
            or frozen.spec.workspace.backend != "thinkthread"
        ):
            raise ValueError(
                "ThinkThread pools require worker_host='thinkthread' and "
                "workspace.backend='thinkthread'"
            )
        baseline = run.source_artifact_ref
        if (
            baseline is None
            or baseline.kind != "fs_snapshot"
            or baseline.provider != "thinkthread"
        ):
            raise RuntimeError("ThinkThread run has no runtime-owned baseline snapshot")
        records = {
            record.candidate_id: record
            for record in self.source.read_candidates(run_id)
        }
        selected_ids = list(candidate_ids or records)
        if not selected_ids or len(selected_ids) != len(set(selected_ids)):
            raise ValueError("candidate_ids must be non-empty and unique")
        unknown = sorted(set(selected_ids) - set(records))
        if unknown:
            raise ValueError("unknown ThinkThread candidates: " + ", ".join(unknown))
        selected_parallel = int(max_parallel or frozen.spec.budget.max_parallel)
        if selected_parallel > frozen.spec.budget.max_parallel:
            raise ValueError("max_parallel exceeds the frozen Search budget")

        candidates: list[ThinkThreadDispatchCandidate] = []
        for candidate_id in selected_ids:
            record = records[candidate_id]
            if record.status not in {"created", "evaluated"}:
                raise RuntimeError(
                    f"cannot dispatch candidate {candidate_id} in status "
                    f"{record.status}"
                )
            existing = [
                item
                for item in self.source.read_agent_sessions(run_id)
                if item.candidate_id == candidate_id and item.host == "thinkthread"
            ]
            session = (
                self.continue_session(existing[-1].agent_session_id)
                if existing
                else self.start_session(run_id, candidate_id)
            )
            budget = normalize_worker_budget_override(
                worker_host="thinkthread",
                worker_budget={
                    **(candidate_worker_budget(frozen, record.task) or {}),
                    **((worker_budgets or {}).get(candidate_id) or {}),
                },
            )
            max_runtime_seconds = (
                budget.get("max_runtime_seconds") if isinstance(budget, dict) else None
            )
            if not isinstance(max_runtime_seconds, int):
                raise ValueError("ThinkThread pool workers require max_runtime_seconds")
            binding = self.resolve_binding(frozen, record.task)
            inner_git_binding: dict[str, Any] | None = None
            if binding.workspace_ledger_projection == "results_tsv":
                backfill_results_ledger_from_iterations(
                    record,
                    frozen.spec.metric_name,
                )
                initial_results = render_thinkthread_inner_git_results_tsv(
                    record,
                    frozen.spec.metric_name,
                )
                inner_git_binding = {
                    "protocol": "goal-plus.inner-git.v1",
                    "adapter": "autoresearch-git",
                    "inner_agent_id": binding.inner_agent_id,
                    "source_subdir": record.task.workspace_access.source_subdir,
                    "metric_name": frozen.spec.metric_name,
                    "initial_results_base64": base64.b64encode(
                        initial_results.encode("utf-8")
                    ).decode("ascii"),
                    "initial_result_count": len(record.results_ledger),
                }
            candidates.append(
                ThinkThreadDispatchCandidate(
                    candidate_id=candidate_id,
                    agent_session_id=session.agent_session_id,
                    message=str(session.launch["message"]),
                    max_runtime_seconds=max_runtime_seconds,
                    inner_git_binding=inner_git_binding,
                )
            )
        return ThinkThreadPoolDispatch(
            run_id=run_id,
            baseline_snapshot_id=baseline.id,
            max_parallel=selected_parallel,
            candidates=tuple(candidates),
        )

    def prepare_thinkthread_continuation(
        self,
        *,
        run_id: str,
        candidate_id: str,
        agent_session_id: str,
        attempt_feedback: CandidateAttemptFeedback | None = None,
        worker_budget: dict[str, Any] | None = None,
    ) -> ThinkThreadContinuation:
        bound = self.queries.session(agent_session_id)
        if (
            bound.run_id != run_id
            or bound.candidate_id != candidate_id
            or bound.host != "thinkthread"
        ):
            raise ValueError(
                "ThinkThread continuation session does not match its candidate"
            )
        if attempt_feedback is not None and (
            attempt_feedback.run_id != run_id
            or attempt_feedback.candidate_id != candidate_id
            or attempt_feedback.agent_session_id != agent_session_id
        ):
            raise ValueError(
                "ThinkThread continuation feedback does not match its candidate"
            )
        session = self.continue_session(
            agent_session_id,
            worker_budget=worker_budget,
        )
        launch_budget = session.launch.get("worker_budget")
        if not isinstance(launch_budget, dict):
            launch_budget = worker_budget or {}
        max_runtime_seconds = launch_budget.get("max_runtime_seconds")
        if not isinstance(max_runtime_seconds, int):
            run = self.source.read_run(run_id)
            frozen = self.source.read_frozen_spec(run.frozen_spec_id)
            record = self.source.read_candidate(run_id, candidate_id)
            normalized = normalize_worker_budget_override(
                worker_host="thinkthread",
                worker_budget={
                    **(candidate_worker_budget(frozen, record.task) or {}),
                    **(worker_budget or {}),
                },
            )
            max_runtime_seconds = (
                normalized.get("max_runtime_seconds")
                if isinstance(normalized, dict)
                else None
            )
        if not isinstance(max_runtime_seconds, int):
            raise ValueError("ThinkThread continuation requires max_runtime_seconds")
        message = str(session.launch["message"])
        if attempt_feedback is not None:
            message = append_candidate_attempt_feedback(message, attempt_feedback)
        return ThinkThreadContinuation(
            agent_session_id=session.agent_session_id,
            message=message,
            max_runtime_seconds=max_runtime_seconds,
        )

    def recover_legacy_thinkthread_attempt_feedback(
        self,
        *,
        run_id: str,
        candidate_id: str,
        agent_session_id: str,
        attempt_id: str,
        request_id: str,
        verified_at: str,
    ) -> CandidateAttemptFeedback:
        """Project a pre-attempt-schema pool result through the typed boundary."""

        record = self.source.read_candidate(run_id, candidate_id)
        report = record.score_report
        if report is None or not record.iterations:
            raise RuntimeError("legacy ThinkThread result is missing verifier evidence")
        iteration = record.iterations[-1]
        attempt_artifact = report.attempt_artifact_ref or iteration.artifact_ref
        workspace_artifact = (
            report.workspace_artifact_after_settlement
            or report.best_artifact_ref
            or iteration.workspace_artifact_after_settlement
        )
        if attempt_artifact is None or workspace_artifact is None:
            raise RuntimeError("legacy ThinkThread result is missing artifact evidence")
        normalized_report = report.model_copy(
            update={
                "attempt_id": attempt_id,
                "attempt_iteration": iteration.iteration,
                "attempt_artifact_ref": attempt_artifact,
            }
        )
        return CandidateAttemptFeedback(
            attempt_id=attempt_id,
            request_id=request_id,
            run_id=run_id,
            candidate_id=candidate_id,
            agent_session_id=agent_session_id,
            attempt_iteration=iteration.iteration,
            score_report=normalized_report,
            attempt_artifact_ref=attempt_artifact,
            workspace_artifact_after_attempt=workspace_artifact,
            verified_at=verified_at,
        )
