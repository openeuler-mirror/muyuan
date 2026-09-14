from __future__ import annotations
from typing import Any
from goal_plus.application.candidate_ranking import (
    best_iteration_record,
)
from goal_plus.domain.evidence import (
    IterationRecord,
)

from goal_plus.ports.search_inspection import (
    ContextState,
    SearchInspection,
    CandidateContextInputs,
)
from goal_plus.application.candidate_sessions import CandidateSessions
from goal_plus.application.candidate_ledger import CandidateLedger
from goal_plus.application.candidate_artifacts import CandidateArtifacts
from goal_plus.application.references import ReferenceWorkspaces
from goal_plus.application.shared_tools import SharedTools

AGENT_CONTEXT_RECENT_ITERATIONS = 3


def agent_iteration_context(iteration: IterationRecord) -> dict[str, Any]:
    attempt = iteration.metrics.get("_candidate_attempt")
    legacy_attempt = iteration.metrics.get("_thinkthread_settlement")
    attempt_id = (
        attempt.get("id")
        if isinstance(attempt, dict)
        else legacy_attempt.get("key")
        if isinstance(legacy_attempt, dict)
        else None
    )
    return {
        "iteration": iteration.iteration,
        "attempt_id": attempt_id,
        "score": iteration.score,
        "process_passed": iteration.process_passed,
        "git_head": iteration.git_head,
        "artifact_ref": (
            iteration.artifact_ref.model_dump(mode="json")
            if iteration.artifact_ref is not None
            else None
        ),
        "attempt_base_git_head": iteration.attempt_base_git_head,
        "attempt_changed_files": list(iteration.attempt_changed_files),
        "failure_class": iteration.failure_class,
        "hypothesis": iteration.hypothesis,
        "disposition": iteration.disposition,
        "restored_to_iteration": iteration.restored_to_iteration,
        "restored_to_git_head": iteration.restored_to_git_head,
        "workspace_git_head_after_settlement": (
            iteration.workspace_git_head_after_settlement
        ),
        "workspace_artifact_after_settlement": (
            iteration.workspace_artifact_after_settlement.model_dump(mode="json")
            if iteration.workspace_artifact_after_settlement is not None
            else None
        ),
        "created_at": iteration.created_at,
    }


class CandidateContext:
    def __init__(
        self,
        *,
        state: ContextState,
        sessions: CandidateSessions,
        ledger: CandidateLedger,
        inspection: SearchInspection,
        artifacts: CandidateArtifacts,
        references: ReferenceWorkspaces,
        shared_tools: SharedTools,
    ) -> None:
        self.state = state
        self.sessions = sessions
        self.ledger = ledger
        self.inspection = inspection
        self.artifacts = artifacts
        self.references = references
        self.shared_tools = shared_tools

    def get_agent_context(self, agent_session_id: str) -> dict[str, Any]:
        session = self.sessions.session(agent_session_id)
        run = self.state.read_run(session.run_id)
        frozen = self.state.read_frozen_spec(run.frozen_spec_id)
        record = self.state.read_candidate(session.run_id, session.candidate_id)
        previous = self.state.read_agent_sessions(session.run_id)
        initialized = bool(
            record.results_ledger_artifact_ref or record.results_ledger_git_head
        )
        projection = self.ledger.projection(frozen, record)
        results_path = self.ledger.ensure(frozen, record)
        if not initialized:
            self.state.write_candidate(session.run_id, record)
        private = record.task.workspace_provider == "thinkthread"
        workspace_status = (
            [] if private else self.inspection.git_status(record.task.workspace)
        )
        inner_agent = self.inspection.inner_agent(frozen, record.task)
        git_head = None if private else self.inspection.git_head(record.task.workspace)
        changed_files = (
            []
            if private
            else self.artifacts.workspace.candidate_changed_files(run, record)
        )
        references = self.references.list(
            session.run_id,
            requester_agent_session_id=session.agent_session_id,
            open_only=True,
        )
        catalog = (
            self.shared_tools.catalog(session.run_id)
            if record.task.share_out_dir is not None
            else []
        )
        return project_candidate_context(
            CandidateContextInputs(
                session,
                frozen,
                record,
                previous,
                projection,
                results_path,
                workspace_status,
                git_head,
                changed_files,
                inner_agent,
                references,
                catalog,
            )
        )


def project_candidate_context(inputs: CandidateContextInputs) -> dict[str, Any]:
    session, frozen, candidate_record = inputs.session, inputs.frozen, inputs.candidate
    is_thinkthread = candidate_record.task.workspace_provider == "thinkthread"
    workspace_ledger_projection, results_tsv = (
        inputs.ledger_projection,
        inputs.results_path,
    )
    workspace_status = inputs.workspace_status
    previous_sessions: list[dict[str, Any]] = []
    latest_handoff: dict[str, Any] | None = None
    for previous in inputs.previous_sessions:
        if (
            previous.candidate_id != session.candidate_id
            or previous.agent_session_id == session.agent_session_id
        ):
            continue
        metadata = previous.session_handle.metadata
        same_generation = previous.execution_generation == session.execution_generation
        progress_handoff = metadata.get("progress_handoff") if same_generation else None
        if isinstance(progress_handoff, dict):
            latest_handoff = progress_handoff
        assistant_text = metadata.get("assistant_text") if same_generation else None
        error = metadata.get("error")
        previous_sessions.append(
            {
                "agent_session_id": previous.agent_session_id,
                "timed_out": bool(metadata.get("timed_out")),
                "runner_failed": bool(metadata.get("runner_failed")),
                "assistant_summary": (
                    assistant_text[:2000]
                    + ("..." if len(assistant_text) > 2000 else "")
                    if isinstance(assistant_text, str)
                    else None
                ),
                "progress_handoff": progress_handoff
                if isinstance(progress_handoff, dict)
                else None,
                "error": (
                    error[:500] + ("..." if len(error) > 500 else "")
                    if isinstance(error, str)
                    else None
                ),
            }
        )
    dispatch_count = session.session_handle.metadata.get("dispatch_count")
    dispatch_count = dispatch_count if isinstance(dispatch_count, int) else 0
    continuation_mode = session.launch.get("continuation")
    is_native_session_resume = (
        continuation_mode == "native_session" and dispatch_count > 0
    )
    supplemental_enabled = frozen.supplemental_evaluation_enabled
    best_iteration = best_iteration_record(
        candidate_record,
        frozen.spec.metric_direction,
    )
    recent_iterations = candidate_record.iterations[-AGENT_CONTEXT_RECENT_ITERATIONS:]
    task = candidate_record.task
    candidate_task: dict[str, Any] = {
        "hypothesis": task.hypothesis,
        "allowed_files": list(task.allowed_files),
        "denied_files": list(task.denied_files),
        "instructions": list(task.instructions),
    }
    if not is_thinkthread:
        candidate_task["workspace"] = str(task.workspace)
    optional_task_fields = {
        "acceptance": list(task.acceptance),
        "process_environment": dict(task.process_environment),
        "task_skill_paths": [str(path) for path in task.task_skill_paths],
        "share_out_dir": str(task.share_out_dir) if task.share_out_dir else None,
    }
    candidate_task.update(
        {key: value for key, value in optional_task_fields.items() if value}
    )
    inner_agent = inputs.inner_agent
    context: dict[str, Any] = {
        "agent_session_id": session.agent_session_id,
        "execution_generation": session.execution_generation,
        "run_id": session.run_id,
        "candidate_id": session.candidate_id,
        "objective": frozen.spec.objective,
        "metric_name": frozen.spec.metric_name,
        "metric_direction": frozen.spec.metric_direction,
        "candidate_task": candidate_task,
        "inner_agent": inner_agent,
        "results_tsv": str(results_tsv) if results_tsv is not None else None,
        "result_ledger_source": "gp",
        "workspace_ledger_projection": workspace_ledger_projection,
        "result_count": len(candidate_record.results_ledger),
        "latest_result": (
            candidate_record.results_ledger[-1].model_dump(mode="json")
            if candidate_record.results_ledger
            else None
        ),
        "iteration_count": len(candidate_record.iterations),
        "recent_iterations": [
            agent_iteration_context(iteration) for iteration in recent_iterations
        ],
        "best_iteration": (
            agent_iteration_context(best_iteration)
            if best_iteration is not None
            else None
        ),
        "resume": {
            "recovery": candidate_record.recovery.model_dump(mode="json")
            if candidate_record.recovery
            else None,
            "is_redispatch": bool(session.directive.get("state_level_resume")),
            "is_native_session_resume": is_native_session_resume,
            "mode": continuation_mode if is_native_session_resume else None,
            "dispatch_count": dispatch_count,
            "previous_session_count": len(previous_sessions),
            "latest_previous_session": (
                previous_sessions[-1] if previous_sessions else None
            ),
            "latest_handoff": latest_handoff,
            "workspace": {
                "git_head": (None if is_thinkthread else inputs.git_head),
                "artifact_ref": (
                    candidate_record.results_ledger_artifact_ref.model_dump(mode="json")
                    if candidate_record.results_ledger_artifact_ref is not None
                    else candidate_record.task.base_artifact_ref.model_dump(mode="json")
                    if candidate_record.task.base_artifact_ref is not None
                    else None
                ),
                "git_status": workspace_status,
                "dirty": bool(workspace_status),
                "changed_files": (
                    list(best_iteration.changed_files)
                    if is_thinkthread and best_iteration is not None
                    else []
                    if is_thinkthread
                    else inputs.changed_files
                ),
            },
        },
    }
    if supplemental_enabled:
        context["supplemental_evaluation_enabled"] = True
    reference_workspaces = [
        reference.model_dump(mode="json") for reference in inputs.references
    ]
    if reference_workspaces:
        context["reference_workspaces"] = reference_workspaces
    if task.share_out_dir is not None:
        context["tool_family_catalog"] = inputs.tool_catalog
    if is_thinkthread:
        context["agent_harness"] = session.agent_harness
        context["runtime_provider"] = session.runtime_provider
        context["workspace_access"] = task.workspace_access.model_dump(mode="json")
    return context
