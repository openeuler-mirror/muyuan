from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.domain.goal_commands import GOAL_PLUS_EDIT_ENTRYPOINT_REQUIRED
from goal_plus.domain.goal_authorization import GOAL_PLUS_ENTRYPOINT_REQUIRED
from goal_plus.domain.agents import AgentHostKind
from goal_plus.domain.search import (
    CandidateProposal,
    SearchSpec,
    VerifierInvalidationReason,
)
from goal_plus.domain.goal import (
    GoalPlusNextAction,
    GoalPlusSpecDraft,
    GoalPlusSpecDraftInput,
    GoalPlusTriage,
    GoalPlusWorkEventKind,
)
from goal_plus.domain.shared_tools import ToolizationDecision
from goal_plus.monitor import goal_plus_monitor_snapshot
from goal_plus.runtime import FileSearchRuntime
from goal_plus.application.candidate_creation import format_task_skill_listing
from goal_plus.workspaces import format_workspace_capacity_notice
from goal_plus.projections.goal_tools import (
    goal_receipt, goal_status, reconciliation_summary, work_item_summary,
    GOAL_HISTORY_DEFAULT_LIMIT, GOAL_HISTORY_MAX_LIMIT,
)
from goal_plus.projections.monitor_tools import monitor_tool_summary


ToolCallerRole = Literal["main", "candidate", "final_checker", "ordinary"]

CANDIDATE_TOOL_NAMES = frozenset(
    {
        "search_get_agent_context",
        "search_get_global_evidence",
        "search_reference_open",
        "search_stage_shared_tool",
        "search_copy_shared_tool",
        "search_get_evidence_detail",
        "search_run_verifier",
        "search_list_iterations",
    }
)
FINAL_CHECKER_TOOL_NAMES = frozenset(
    {"goal_plus_status", "goal_plus_submit_final_check"}
)


def require_tool_role(
    role: ToolCallerRole,
    tool_name: str,
    *,
    caller_agent_session_id: str | None = None,
    requested_agent_session_ids: list[str] | None = None,
) -> None:
    """Enforce model-facing role boundaries identified by a trusted host."""

    if role == "main":
        allowed = tool_name != "goal_plus_submit_final_check"
    elif role == "candidate":
        allowed = tool_name in CANDIDATE_TOOL_NAMES
    elif role == "final_checker":
        allowed = tool_name in FINAL_CHECKER_TOOL_NAMES
    else:
        allowed = False
    if not allowed:
        raise PermissionError(f"GoalPlusRoleDenied: {role} cannot call {tool_name}")
    if role == "candidate" and (
        caller_agent_session_id is None
        or set(requested_agent_session_ids or []) != {caller_agent_session_id}
    ):
        raise PermissionError(
            "GoalPlusSessionMismatch: candidate tools require the caller's own "
            "agent_session_id"
        )


class SearchTools:
    """JSON-friendly tool layer shared by tests and the MCP server."""

    def __init__(
        self,
        runtime: FileSearchRuntime,
        goal_runtime: FileGoalPlusRuntime | None = None,
    ) -> None:
        self.runtime = runtime
        self.goal_runtime = goal_runtime

    def _require_target(self, tool_name: str, **tool_input: Any) -> None:
        if self.goal_runtime is not None:
            self.goal_runtime.require_active_tool_target(tool_name, tool_input)

    def _require_search_bootstrap(
        self,
        tool_name: str,
        goal_plus_id: str,
        **tool_input: Any,
    ) -> None:
        goals = self.goal_runtime or FileGoalPlusRuntime(self.runtime.root_dir)
        goals.require_active_tool_target(
            tool_name,
            {"goal_plus_id": goal_plus_id, **tool_input},
        )

    def search_freeze_spec(
        self,
        goal_plus_id: str,
        spec: dict[str, Any] | SearchSpec,
        verifier_artifact_paths: list[str],
    ) -> dict[str, Any]:
        self._require_search_bootstrap("search_freeze_spec", goal_plus_id)
        parsed_spec = spec if isinstance(spec, SearchSpec) else SearchSpec.model_validate(spec)
        source_path = Path(parsed_spec.source_path).expanduser().resolve()
        artifact_paths: list[Path] = []
        for value in verifier_artifact_paths:
            relative = Path(value)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(
                    "verifier_artifact_paths must be source_path-relative"
                )
            resolved = (source_path / relative).resolve()
            if not resolved.is_relative_to(source_path):
                raise ValueError(
                    "verifier_artifact_paths must be source_path-relative"
                )
            artifact_paths.append(resolved)
        frozen = self.runtime.freeze_spec(
            parsed_spec,
            artifact_paths,
            goal_plus_id=goal_plus_id,
        )
        payload = frozen.model_dump(mode="json")
        visible = format_task_skill_listing(frozen.task_skill_paths)
        payload["task_skill_notice"] = (
            "Git-tracked task-local Skills visible from source: "
            + visible
            if frozen.task_skill_paths
            else "No Git-tracked task-local Skills found under .agents/skills."
        )
        if frozen.workspace_capacity_estimate is not None:
            payload["workspace_capacity_notice"] = format_workspace_capacity_notice(
                frozen.workspace_capacity_estimate
            )
        return payload

    def search_create(
        self,
        goal_plus_id: str,
        frozen_spec_id: str,
        source_run_id: str | None = None,
    ) -> dict[str, str]:
        self._require_search_bootstrap(
            "search_create",
            goal_plus_id,
            source_run_id=source_run_id,
        )
        self.runtime.require_frozen_spec_owner(frozen_spec_id, goal_plus_id)
        return {
            "run_id": self.runtime.create_run(
                frozen_spec_id,
                source_run_id=source_run_id,
            )
        }

    def goal_plus_list_models(
        self,
        host: AgentHostKind,
        query: str | None = None,
    ) -> dict[str, Any]:
        return self.runtime.list_available_models(host, query)

    def search_invalidate_run(
        self,
        run_id: str,
        reason: VerifierInvalidationReason,
        summary: str,
        evidence: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self._require_target("search_invalidate_run", run_id=run_id)
        return self.runtime.invalidate_run(
            run_id,
            reason=reason,
            summary=summary,
            evidence=evidence,
        ).model_dump(mode="json")

    def search_status(self, run_id: str) -> dict[str, Any]:
        return self.runtime.status(run_id).model_dump(mode="json")

    def search_recovery_status(self, run_id: str) -> dict[str, Any]:
        self._require_target("search_recovery_status", run_id=run_id)
        return self.runtime.search_recovery_status(run_id)

    def search_prepare_recovery(self, run_id: str, candidate_id: str) -> dict[str, Any]:
        self._require_target("search_prepare_recovery", run_id=run_id)
        return self.runtime.prepare_candidate_recovery(run_id, candidate_id).model_dump(mode="json")

    def search_recover_candidate(
        self, run_id: str, candidate_id: str, recovery_id: str
    ) -> dict[str, Any]:
        from goal_plus.host_recovery import approve_recovery
        self._require_target("search_recover_candidate", run_id=run_id)
        recovery = self.runtime.recovery.request(run_id, candidate_id, recovery_id)
        if recovery.state != "restored":
            approve_recovery(self.runtime.root_dir, recovery)
        return self.runtime.recover_candidate(run_id, candidate_id, recovery_id).model_dump(mode="json")

    def search_list_history(
        self,
        run_id: str,
        top_n: int = 5,
        sort_by: str = "score",
    ) -> dict[str, Any]:
        return self.runtime.list_history(run_id, top_n=top_n, sort_by=sort_by)

    def search_plan_next(self, run_id: str, requested_k: int = 4) -> dict[str, Any]:
        self._require_target("search_plan_next", run_id=run_id)
        return self.runtime.plan_next(run_id, requested_k=requested_k).model_dump(mode="json")

    def search_start_batch(
        self,
        run_id: str,
        plan_id: str,
        proposals: list[CandidateProposal | dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        self._require_target("search_start_batch", run_id=run_id)
        parsed_proposals = (
            [
                proposal
                if isinstance(proposal, CandidateProposal)
                else CandidateProposal.model_validate(proposal)
                for proposal in proposals
            ]
            if proposals is not None
            else None
        )
        payloads = []
        for task in self.runtime.start_batch(run_id, plan_id, parsed_proposals):
            payload = task.model_dump(mode="json")
            payload["task_skill_notice"] = (
                f"Subagent {task.candidate_id} repository Skills: "
                f"{format_task_skill_listing(task.task_skill_paths)}. "
                "External Skills remain host-managed."
            )
            payloads.append(payload)
        return payloads

    def search_start_agent_session(
        self,
        run_id: str,
        candidate_id: str,
        directive: dict[str, Any] | str | None = None,
        worker_budget: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require_target("search_start_agent_session", run_id=run_id)
        return self.runtime.start_agent_session(
            run_id=run_id,
            candidate_id=candidate_id,
            directive=directive,
            worker_budget=worker_budget,
        ).model_dump(mode="json")

    def search_redispatch_candidate(
        self,
        run_id: str,
        candidate_id: str,
        worker_budget: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require_target("search_redispatch_candidate", run_id=run_id)
        return self.runtime.redispatch_candidate(
            run_id=run_id,
            candidate_id=candidate_id,
            worker_budget=worker_budget,
        ).model_dump(mode="json")

    def search_bind_agent_handle(
        self,
        agent_session_id: str,
        handle: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_target(
            "search_bind_agent_handle",
            agent_session_id=agent_session_id,
        )
        return self.runtime.bind_agent_handle(
            agent_session_id=agent_session_id,
            handle=handle,
        ).model_dump(mode="json")

    def search_continue_agent_session(
        self,
        agent_session_id: str,
        worker_budget: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require_target(
            "search_continue_agent_session",
            agent_session_id=agent_session_id,
        )
        return self.runtime.continue_agent_session(
            agent_session_id=agent_session_id,
            worker_budget=worker_budget,
        ).model_dump(mode="json")

    def search_get_agent_context(self, agent_session_id: str) -> dict[str, Any]:
        self._require_target(
            "search_get_agent_context",
            agent_session_id=agent_session_id,
        )
        result = self.runtime.get_agent_context(agent_session_id)
        if result.get("host") != "thinkthread" or not result.get("iteration_count"):
            return result
        try:
            if not self.runtime.should_inject_global_evidence_after_verifier(
                str(result["run_id"])
            ):
                return result
            snapshot = self.runtime.get_global_evidence(
                agent_session_id,
                source="verifier_injected",
            )
        except Exception as exc:
            result["global_evidence_injected"] = False
            result["global_evidence_warning"] = (
                f"Global Evidence injection failed: {type(exc).__name__}: {exc}"
            )
            return result
        result["global_evidence_snapshot"] = snapshot
        result["global_evidence_injected"] = True
        result["global_evidence_entry_count"] = len(snapshot)
        return result

    def search_get_global_evidence(
        self,
        agent_session_id: str,
    ) -> list[dict[str, Any]]:
        self._require_target(
            "search_get_global_evidence",
            agent_session_id=agent_session_id,
        )
        return self.runtime.get_global_evidence(agent_session_id)

    def search_reference_open(
        self,
        agent_session_id: str,
        source_candidate_id: str,
        source_iteration: int,
    ) -> dict[str, Any]:
        self._require_target(
            "search_reference_open",
            agent_session_id=agent_session_id,
        )
        return self.runtime.open_reference_workspace(
            agent_session_id,
            source_candidate_id,
            source_iteration,
        ).model_dump(mode="json")

    def search_copy_shared_tool(
        self,
        agent_session_id: str,
        tool_id: str,
        snapshot_hash: str,
    ) -> dict[str, Any]:
        self._require_target(
            "search_copy_shared_tool",
            agent_session_id=agent_session_id,
        )
        return self.runtime.copy_shared_tool(
            agent_session_id,
            tool_id,
            snapshot_hash,
        )

    def search_stage_shared_tool(
        self,
        agent_session_id: str,
        name: str,
        summary: str,
        entrypoint: str,
        candidate_relative_source_paths: list[str],
        publication_intent: str = "new",
        supersedes_tool_id: str | None = None,
        capability_ids: list[str] | None = None,
        coverage_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        self._require_target(
            "search_stage_shared_tool",
            agent_session_id=agent_session_id,
        )
        return self.runtime.stage_shared_tool(
            agent_session_id=agent_session_id,
            name=name,
            summary=summary,
            entrypoint=entrypoint,
            candidate_relative_source_paths=candidate_relative_source_paths,
            publication_intent=publication_intent,
            supersedes_tool_id=supersedes_tool_id,
            capability_ids=capability_ids,
            coverage_keys=coverage_keys,
        )

    def search_get_evidence_detail(
        self,
        agent_session_id: str,
        candidate_id: str,
        iteration: int,
    ) -> dict[str, Any]:
        self._require_target(
            "search_get_evidence_detail",
            agent_session_id=agent_session_id,
        )
        return self.runtime.get_evidence_detail(
            agent_session_id,
            candidate_id,
            iteration,
        )

    def search_get_agent_observability(self, agent_session_id: str) -> dict[str, Any]:
        return self.runtime.get_agent_observability(agent_session_id)

    def search_run_verifier(
        self,
        run_id: str,
        candidate_id: str,
        scope: Literal["process", "promotion"] = "process",
        agent_session_id: str | None = None,
        hypothesis: str | None = None,
        toolization_decision: ToolizationDecision | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require_target("search_run_verifier", run_id=run_id)
        report = self.runtime.run_verifier(
            run_id,
            candidate_id,
            scope=scope,
            agent_session_id=agent_session_id,
            hypothesis=hypothesis,
            toolization_decision=toolization_decision,
        )
        result = report.model_dump(mode="json")
        if scope != "process" or agent_session_id is None:
            return result
        try:
            if not self.runtime.should_inject_global_evidence_after_verifier(run_id):
                return result
            snapshot = self.runtime.get_global_evidence(
                agent_session_id,
                source="verifier_injected",
            )
        except Exception as exc:
            result["global_evidence_injected"] = False
            result["global_evidence_warning"] = (
                f"Global Evidence injection failed: {type(exc).__name__}: {exc}"
            )
            return result
        result["global_evidence_snapshot"] = snapshot
        result["global_evidence_injected"] = True
        result["global_evidence_entry_count"] = len(snapshot)
        return result

    def search_list_iterations(
        self,
        agent_session_id: str,
    ) -> list[dict[str, Any]]:
        self._require_target(
            "search_list_iterations",
            agent_session_id=agent_session_id,
        )
        return self.runtime.list_agent_iterations(agent_session_id)

    def search_list_allocation_decisions(
        self,
        run_id: str,
        status: Literal["pending", "applied"] | None = None,
    ) -> list[dict[str, Any]]:
        self._require_target("search_list_allocation_decisions", run_id=run_id)
        return [
            item.model_dump(mode="json")
            for item in self.runtime.list_allocation_decisions(run_id, status)
        ]

    def search_apply_allocation_decision(
        self,
        run_id: str,
        decision_id: str,
    ) -> dict[str, Any]:
        self._require_target("search_apply_allocation_decision", run_id=run_id)
        return self.runtime.apply_allocation_decision(run_id, decision_id)

    def search_select(self, run_id: str) -> dict[str, Any]:
        self._require_target("search_select", run_id=run_id)
        return self.runtime.select(run_id)

    def search_report(self, run_id: str) -> dict[str, str]:
        report_path = self.runtime.report(run_id)
        return {
            "report_path": str(report_path),
            "html_report_path": str(report_path.with_suffix(".html")),
        }

    def search_promote(self, run_id: str, candidate_id: str) -> dict[str, Any]:
        self._require_target("search_promote", run_id=run_id)
        artifact_path = self.runtime.promote(run_id, candidate_id)
        promotion = self.runtime.promotion_record(run_id)
        source_path, apply_cwd, apply_directory = (
            self.runtime.promotion_apply_coordinates(run_id)
        )
        return {
            "artifact_path": str(artifact_path),
            "source_path": str(source_path),
            "apply_cwd": str(apply_cwd),
            "apply_directory": apply_directory,
            "promotion_id": promotion.promotion_id,
            "provider": promotion.provider,
            "strategy": promotion.strategy,
            "promotion_mode": promotion.promotion_mode,
            "apply_required": promotion.promotion_mode == "apply",
            "state": promotion.state,
            "publication_artifact_ref": (
                promotion.publication_artifact_ref.model_dump(mode="json")
                if promotion.publication_artifact_ref is not None
                else None
            ),
        }

    def search_apply_promotion(self, run_id: str) -> dict[str, Any]:
        self._require_target("search_apply_promotion", run_id=run_id)
        promotion = self.runtime.apply_promotion(run_id)
        return promotion.model_dump(mode="json")

    def search_main_agent_notifications(self, run_id: str) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "notifications": [
                item.model_dump(mode="json")
                for item in self.runtime.main_agent_notifications(run_id)
            ],
        }

    def search_ack_main_agent_notification(
        self,
        run_id: str,
        notification_id: str,
    ) -> dict[str, Any]:
        return self.runtime.acknowledge_main_agent_notification(
            run_id,
            notification_id,
        ).model_dump(mode="json")

    def goal_plus_monitor_snapshot(
        self,
        goal_plus_id: str | None = None,
        run_id: str | None = None,
        stale_after_seconds: int = 600,
        feature_plugins: list[str] | None = None,
        view: Literal["summary", "full"] = "summary",
    ) -> dict[str, Any]:
        if view not in {"summary", "full"}:
            raise ValueError("view must be summary or full")
        snapshot = goal_plus_monitor_snapshot(
            root_dir=self.runtime.root_dir,
            goal_plus_id=goal_plus_id,
            run_id=run_id,
            stale_after_seconds=stale_after_seconds,
            feature_plugins=feature_plugins,
        )
        return snapshot if view == "full" else monitor_tool_summary(snapshot)


class GoalPlusTools:
    """JSON-friendly goal-plus tool layer shared by tests and the MCP server."""

    def __init__(self, runtime: FileGoalPlusRuntime) -> None:
        self.runtime = runtime

    def _require_target(self, tool_name: str, goal_plus_id: str) -> None:
        self.runtime.require_active_tool_target(
            tool_name,
            {"goal_plus_id": goal_plus_id},
        )

    def goal_plus_create(
        self,
        raw_goal: str,
        source_path: str | None = None,
        policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del raw_goal, source_path, policy
        raise RuntimeError(GOAL_PLUS_ENTRYPOINT_REQUIRED)

    def goal_plus_status(
        self, goal_plus_id: str, view: Literal["summary", "context"] = "summary",
    ) -> dict[str, Any]:
        if view not in {"summary", "context"}:
            raise ValueError("view must be summary or context")
        record = self.runtime.status(goal_plus_id)
        payload = goal_status(record, context=view == "context")
        payload["reconciliation"] = reconciliation_summary(self.runtime.reconciliation(goal_plus_id))
        return payload

    def goal_plus_history(
        self, goal_plus_id: str,
        collection: Literal["goal_revisions", "work_items", "search_tasks", "final_checks", "events"],
        offset: int = 0, limit: int = GOAL_HISTORY_DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        # Explicit detail reads are paged; operational polling never reads this payload.
        if offset < 0 or not 1 <= limit <= GOAL_HISTORY_MAX_LIMIT:
            raise ValueError("offset must be >= 0 and limit must be between 1 and 100")
        if collection not in {"goal_revisions", "work_items", "search_tasks", "final_checks", "events"}:
            raise ValueError("unknown Goal history collection")
        record = self.runtime.status(goal_plus_id)
        items = (self.runtime.list_events(goal_plus_id) if collection == "events"
                 else getattr(record, collection))
        page = items[offset:offset + limit]
        return {
            "goal_plus_id": goal_plus_id, "goal_revision": record.goal_revision,
            "collection": collection, "offset": offset, "total": len(items),
            "next_offset": offset + len(page) if offset + len(page) < len(items) else None,
            "items": [item if isinstance(item, dict) else item.model_dump(mode="json") for item in page],
        }

    def goal_plus_update_goal(
        self,
        goal_plus_id: str,
        raw_goal: str,
        expected_revision: int,
        reason: str | None = None,
    ) -> dict[str, Any]:
        del goal_plus_id, raw_goal, expected_revision, reason
        raise RuntimeError(GOAL_PLUS_EDIT_ENTRYPOINT_REQUIRED)

    def goal_plus_record_triage(
        self,
        goal_plus_id: str,
        triage: GoalPlusTriage | dict[str, Any],
    ) -> dict[str, Any]:
        self._require_target("goal_plus_record_triage", goal_plus_id)
        record = self.runtime.record_triage(
            goal_plus_id,
            triage
            if isinstance(triage, GoalPlusTriage)
            else GoalPlusTriage.model_validate(triage),
        )
        return goal_receipt(record)

    def goal_plus_record_work_event(
        self,
        goal_plus_id: str,
        work_item_id: str,
        event: GoalPlusWorkEventKind,
        summary: str,
        host: str | None = None,
        task_name: str | None = None,
        agent_id: str | None = None,
        transcript_path: str | None = None,
        evidence: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        attempt_id: str | None = None,
        generation: int | None = None,
        launch_ttl_seconds: int = 120,
    ) -> dict[str, Any]:
        self._require_target("goal_plus_record_work_event", goal_plus_id)
        record = self.runtime.record_work_event(
            goal_plus_id,
            work_item_id,
            event,
            summary,
            host=host,
            task_name=task_name,
            agent_id=agent_id,
            transcript_path=transcript_path,
            evidence=evidence,
            metadata=metadata,
            attempt_id=attempt_id,
            generation=generation,
            launch_ttl_seconds=launch_ttl_seconds,
        )
        item = next(item for item in record.work_items if item.work_item_id == work_item_id)
        return {**goal_receipt(record), "work_item": work_item_summary(item)}

    def goal_plus_save_spec_draft(
        self,
        goal_plus_id: str,
        spec_draft: dict[str, Any] | GoalPlusSpecDraft,
    ) -> dict[str, Any]:
        self._require_target("goal_plus_save_spec_draft", goal_plus_id)
        parsed_draft = (
            spec_draft
            if isinstance(spec_draft, GoalPlusSpecDraft)
            else GoalPlusSpecDraftInput.model_validate(spec_draft)
        )
        record = self.runtime.save_spec_draft(
            goal_plus_id,
            parsed_draft,
        )
        return {**goal_receipt(record), "spec_draft_saved": True}

    def goal_plus_link_search_run(
        self,
        goal_plus_id: str,
        frozen_spec_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        self._require_target("goal_plus_link_search_run", goal_plus_id)
        record = self.runtime.link_search_run(
            goal_plus_id,
            frozen_spec_id,
            run_id,
        )
        task = next(task for task in record.search_tasks if task.run_id == run_id)
        return {**goal_receipt(record), "search_task": task.model_dump(mode="json", exclude_none=True)}

    def goal_plus_record_search_result(
        self,
        goal_plus_id: str,
        run_id: str,
        selected_candidate_id: str | None = None,
        report_path: str | None = None,
        promotion_artifact_path: str | None = None,
        summary: str | None = None,
    ) -> dict[str, Any]:
        self._require_target("goal_plus_record_search_result", goal_plus_id)
        record = self.runtime.record_search_result(
            goal_plus_id,
            run_id=run_id,
            selected_candidate_id=selected_candidate_id,
            report_path=report_path,
            promotion_artifact_path=promotion_artifact_path,
            summary=summary,
        )
        task = next(task for task in record.search_tasks if task.run_id == run_id)
        return {**goal_receipt(record), "search_task": task.model_dump(mode="json", exclude_none=True)}

    def goal_plus_prepare_final_check(
        self,
        goal_plus_id: str,
        checker_host: str,
    ) -> dict[str, Any]:
        self._require_target("goal_plus_prepare_final_check", goal_plus_id)
        return self.runtime.prepare_final_check(
            goal_plus_id,
            checker_host=checker_host,  # type: ignore[arg-type]
        )

    def goal_plus_submit_final_check(
        self,
        goal_plus_id: str,
        check_id: str,
        goal_revision: int,
        verdict: str,
        summary: str,
        findings: list[dict[str, Any]] | None = None,
        evidence: list[dict[str, Any]] | None = None,
        checker_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require_target("goal_plus_submit_final_check", goal_plus_id)
        record = self.runtime.submit_final_check(
            goal_plus_id,
            check_id=check_id,
            goal_revision=goal_revision,
            verdict=verdict,
            summary=summary,
            findings=findings,
            evidence=evidence,
            checker_metadata=checker_metadata,
        )
        check = next(check for check in record.final_checks if check.check_id == check_id)
        return {**goal_receipt(record), "final_check": check.model_dump(mode="json", include={
            "check_id", "goal_revision", "status", "summary",
        })}

    def goal_plus_set_status(
        self,
        goal_plus_id: str,
        status: str,
        reason: str | None = None,
        evidence: list[dict[str, Any]] | None = None,
        next_action: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require_target("goal_plus_set_status", goal_plus_id)
        parsed_next_action = (
            GoalPlusNextAction.model_validate(next_action)
            if next_action is not None
            else None
        )
        record = self.runtime.set_status(
            goal_plus_id,
            status=status,  # type: ignore[arg-type]
            reason=reason,
            evidence=evidence,
            next_action=parsed_next_action,
        )
        return goal_receipt(record)

    def goal_plus_gate(
        self,
        goal_plus_id: str,
        event: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        return self.runtime.gate(
            goal_plus_id,
            event=event,  # type: ignore[arg-type]
            context=context,
        ).model_dump(mode="json")
