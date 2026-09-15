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
from goal_plus.domain.evidence import CandidateAttemptFeedback
from goal_plus.domain.search import CandidateTask, FrozenSpec
from goal_plus.ports.candidate_host import (
    ThinkThreadContinuation,
    ThinkThreadSessionLaunch,
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
        continue_session: PrepareContinuation,
        resolve_binding: Callable[
            [FrozenSpec, CandidateTask], ResolvedInnerAgentBinding
        ],
    ) -> None:
        self.queries = queries
        self.source = queries.source
        self.continue_session = continue_session
        self.resolve_binding = resolve_binding

    def prepare_thinkthread_session(self, agent_session_id: str) -> ThinkThreadSessionLaunch:
        """Project an already-prepared session for the Child adapter, without dispatching it."""
        session = self.queries.session(agent_session_id)
        run = self.source.read_run(session.run_id)
        require_run_not_invalidated(run, "prepare ThinkThread session")
        frozen = self.source.read_frozen_spec(run.frozen_spec_id)
        if session.runtime_provider != "thinkthread" or frozen.spec.workspace.provider != "thinkthread":
            raise ValueError("session does not use the ThinkThread workspace_provider")
        baseline = run.source_artifact_ref
        root_id = run.source_provider_handle.get("root_thinkthread_id")
        if (
            baseline is None or baseline.kind != "fs_snapshot" or baseline.provider != "thinkthread"
            or not isinstance(root_id, str) or not root_id.startswith("tt-")
        ):
            raise RuntimeError("ThinkThread session has no runtime-owned Root and baseline snapshot")
        record = self.source.read_candidate(session.run_id, session.candidate_id)
        binding = self.resolve_binding(frozen, record.task)
        inner_git_binding = None
        if binding.workspace_ledger_projection == "results_tsv":
            backfill_results_ledger_from_iterations(record, frozen.spec.metric_name)
            initial_results = render_thinkthread_inner_git_results_tsv(record, frozen.spec.metric_name)
            inner_git_binding = {
                "protocol": "goal-plus.inner-git.v1", "adapter": "autoresearch-git",
                "inner_agent_id": binding.inner_agent_id,
                "source_subdir": record.task.workspace_access.source_subdir,
                "metric_name": frozen.spec.metric_name,
                "initial_results_base64": base64.b64encode(initial_results.encode("utf-8")).decode("ascii"),
                "initial_result_count": len(record.results_ledger),
            }
        return ThinkThreadSessionLaunch(session, root_id, baseline.id, inner_git_binding)


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
            or bound.runtime_provider != "thinkthread"
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
        message = str(session.launch["message"])
        if attempt_feedback is not None:
            message = append_candidate_attempt_feedback(message, attempt_feedback)
        return ThinkThreadContinuation(
            agent_session_id=session.agent_session_id,
            message=message,
        )
