from __future__ import annotations


from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal

from goal_plus.ports.candidate_creation import CandidateMaterialization
from goal_plus.ports.candidate_verification import CandidateVerificationRequest
from goal_plus.application.candidate_execution import (
    require_candidate_not_recovering,
    require_no_pending_verification,
)

from goal_plus.domain.artifacts import (
    ReferenceWorkspaceRecord,
)
from goal_plus.domain.evidence import (
    PendingVerification,
    ScoreReport,
)
from goal_plus.domain.promotion import (
    MainAgentNotification,
    PromotionRecord,
)
from goal_plus.domain.search import (
    AgentSessionRecord,
    CandidateProposal,
    CandidateRecovery,
    CandidateTask,
    CandidateSpecRevisionInput,
    FrozenSpec,
    RunSummary,
    SearchPlan,
    SearchSpec,
    VerifierInvalidationReason,
)
from goal_plus.domain.selection import ControllerSelectionDirective
from goal_plus.domain.shared_cache import SharedCacheView
from goal_plus.domain.shared_tools import (
    ToolizationDecision,
)
from goal_plus.paths import DEFAULT_RUNTIME_ROOT
from goal_plus.ports.state import (
    CandidateRecord,
    RunRecord,
)
from goal_plus.thinkthread_agent_posix import (
    AgentPosixSdkClient,
)

if TYPE_CHECKING:
    from goal_plus.adaptive import (
        AllocationDecision,
        AllocationDecisionStatus,
    )
    from goal_plus.domain.verifier import (
        VerifierRecord,
        VerifierTask,
    )


class FileSearchRuntime:
    def __init__(self, root_dir: Path | str = DEFAULT_RUNTIME_ROOT) -> None:
        from goal_plus.bootstrap import build_search_services

        self.root_dir = Path(root_dir).resolve()
        self.specs_dir = self.root_dir / "specs"
        self.runs_dir = self.root_dir / "runs"
        services = build_search_services(self.root_dir)
        self.verifiers = services.verifiers
        self.candidate_sessions = services.candidate_sessions
        self.candidate_spec_revisions = services.candidate_spec_revisions
        self.research = services.research
        self.search_run = services.search_run
        self.artifacts = services.artifacts
        self.references = services.references
        self.recovery = services.recovery
        self.verification = services.verification
        self.thinkthread_verification = services.thinkthread_verification
        self.thinkthread_attempt_io = services.thinkthread_attempt_io
        self.selection = services.selection
        self.notifications = services.notifications
        self.artifact_lifecycle = services.artifact_lifecycle
        self._verifier = services.verifier
        self._adaptive_search = services.adaptive_search
        self.reporter = services.reporter
        self.goal_authorization = services.goal_authorization
        self.annotation = services.annotation
        self.annotation_admission = services.annotation_admission
        self.search_context = services.search_context
        self.annotation_context = services.annotation_context
        self.provider_mutations = services.provider_mutations
        self.thinkthread_verifiers = services.thinkthread_verifiers
        self.search_bootstrap = services.search_bootstrap
        self.models = services.models
        self.candidate_creation = services.candidate_creation
        self.candidate_ledger = services.candidate_ledger
        self.settlement = services.settlement
        self.checkpoints = services.checkpoints
        self.promotion = services.promotion
        self.publication_apply = services.publication_apply
        self.context = services.context
        self.queries = services.queries
        self.evidence_reads = services.evidence_reads
        self.shared_cache = services.shared_cache
        self.shared_tools = services.shared_tools
        self.trace = services.trace

    def llm_verifier_enabled(self, run_id: str) -> bool:
        return self.queries.llm_verifier_enabled(run_id)

    def list_verifier_tasks(self, run_id: str) -> list[VerifierTask]:
        return self._verifier.list_tasks(run_id)

    def list_verifier_records(self, run_id: str) -> list[VerifierRecord]:
        return self._verifier.list_records(run_id)

    def require_frozen_spec_owner(
        self,
        frozen_spec_id: str,
        goal_plus_id: str,
    ) -> FrozenSpec:
        return self.queries.require_frozen_spec_owner(frozen_spec_id, goal_plus_id)

    def status(self, run_id: str) -> RunSummary:
        return self.queries.status(run_id)

    def list_history(
        self, run_id: str, top_n: int = 5, sort_by: str = "score"
    ) -> dict[str, Any]:
        return self.queries.list_history(run_id, top_n, sort_by)

    def list_iterations(
        self,
        run_id: str,
        candidate_id: str,
    ) -> list[dict[str, Any]]:
        return self.queries.list_iterations(run_id, candidate_id)

    def list_agent_iterations(self, agent_session_id: str) -> list[dict[str, Any]]:
        return self.queries.list_agent_iterations(agent_session_id)

    def list_allocation_decisions(
        self,
        run_id: str,
        status: AllocationDecisionStatus | None = None,
    ) -> list[AllocationDecision]:
        self.queries.require_adaptive_search(run_id)
        return self._adaptive_search.list_allocation_decisions(run_id, status)

    def accept_verifier_record(self, record, *, execute, steering_host=None) -> None:
        frozen = self.queries.state.read_frozen_spec(record.subject.frozen_spec_id)
        shared_cache = getattr(frozen.spec, "shared_cache", None)
        if shared_cache is not None and shared_cache.enabled:
            self._apply_verifier_cache_updates(record, frozen)
        if frozen.spec.strategy.allocation is not None or frozen.spec.strategy.steering is not None:
            self._adaptive_search.accept_verifier_record(record,
                policy=frozen.spec.strategy.allocation, max_parallel=frozen.spec.budget.max_parallel,
                execute=execute, steering_host=steering_host,
                steering_policy=frozen.spec.strategy.steering)

    def _apply_verifier_cache_updates(self, record, frozen) -> None:
        if record.outcome != "completed" or not record.cache_updates:
            return
        updates = [
            update.model_dump(mode="json") if hasattr(update, "model_dump") else update
            for update in record.cache_updates
        ]
        self.shared_cache.append_verifier_updates(
            record.subject.run_id,
            frozen,
            updates,
        )

    def recover_allocation(self, run_id: str, *, cancel, dispatch):
        return self._adaptive_search.recover_allocation(run_id, cancel=cancel, dispatch=dispatch)

    def apply_allocation_decision(self, run_id: str, decision_id: str, *, cancel, dispatch):
        self.queries.require_adaptive_search(run_id)
        return self._adaptive_search.apply_allocation_decision(run_id, decision_id, cancel=cancel, dispatch=dispatch)

    def current_process_evidence(
        self,
        run_id: str,
        candidate_id: str,
    ) -> dict[str, Any] | None:
        return self.queries.current_process_evidence(run_id, candidate_id)

    def candidate_has_unverified_changes(
        self,
        run_id: str,
        candidate_id: str,
    ) -> bool:
        return self.queries.candidate_has_unverified_changes(run_id, candidate_id)

    def get_agent_observability(self, agent_session_id: str) -> dict[str, Any]:
        return self.queries.get_agent_observability(agent_session_id)

    def get_agent_context(self, agent_session_id: str) -> dict[str, Any]:
        return self.context.get_agent_context(agent_session_id)

    def get_global_evidence(
        self,
        agent_session_id: str,
        *,
        source: Literal["explicit", "verifier_injected"] = "explicit",
        verifier_attempt_id: str | None = None,
        known_snapshot_id: str | None = None,
        compact: bool = False,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        return self.evidence_reads.get_global_evidence(
            agent_session_id,
            source=source,
            verifier_attempt_id=verifier_attempt_id,
            known_snapshot_id=known_snapshot_id,
            compact=compact,
        )

    def get_evidence_detail(
        self,
        agent_session_id: str,
        candidate_id: str,
        iteration: int,
    ) -> dict[str, Any]:
        return self.evidence_reads.get_evidence_detail(
            agent_session_id, candidate_id, iteration
        )

    def should_inject_global_evidence_after_verifier(self, run_id: str) -> bool:
        return self.queries.should_inject_global_evidence_after_verifier(run_id)

    def shared_dir_enabled(self, run_id: str) -> bool:
        return self.queries.shared_dir_enabled(run_id)

    def run_verifier(
        self,
        run_id: str,
        candidate_id: str,
        scope: Literal["process", "promotion"] = "process",
        agent_session_id: str | None = None,
        hypothesis: str | None = None,
        toolization_decision: ToolizationDecision | dict[str, Any] | None = None,
        *, on_submitted: Callable[[PendingVerification], None] | None = None,
        attempt_id: str | None = None,
    ) -> ScoreReport:
        if scope not in {"process", "promotion"}:
            raise ValueError("verifier scope must be 'process' or 'promotion'")
        if attempt_id is not None and (scope != "process" or hypothesis is not None or toolization_decision is not None):
            raise ValueError("VerifierQueryInvalid: retry uses only the process attempt identity")
        if attempt_id is None:
            require_no_pending_verification(self.candidate_creation.state.read_candidate(run_id, candidate_id))
        else:
            self.queries.verifier_result(run_id, candidate_id, attempt_id, agent_session_id)
            self._record_verifier_observation(
                run_id,
                agent_session_id,
                attempt_id,
                "retry_requested",
            )
        with self.settlement.state.lock_verifier(run_id, candidate_id, blocking=False) as lease:
            if attempt_id is None:
                require_no_pending_verification(self.candidate_creation.state.read_candidate(run_id, candidate_id))
            require_candidate_not_recovering(
                self.candidate_creation.state.read_candidate(run_id, candidate_id)
            )
            if scope == "process" and agent_session_id is None and attempt_id is None:
                with self.search_run.state.lock_run(run_id):
                    record = self.candidate_creation.state.read_candidate(
                        run_id, candidate_id
                    )
                    if record.pending_tool_copies:
                        raise RuntimeError(
                            "parent process verifier cannot settle a candidate "
                            "with pending tool copies"
                        )
            report = self.verification.resume(
                run_id, candidate_id, attempt_id, on_submitted=on_submitted, execution_lock=lease,
            ) if attempt_id is not None else self.verification.verify(
                run_id,
                candidate_id,
                scope=scope,
                agent_session_id=agent_session_id,
                hypothesis=hypothesis,
                toolization_decision=toolization_decision,
                on_submitted=on_submitted,
                execution_lock=lease,
            )
            adaptive_search_enabled = False
            if scope == "process":
                run = self.search_run.state.read_run(run_id)
                frozen = self.search_run.state.read_frozen_spec(run.frozen_spec_id)
                adaptive_search_enabled = frozen.spec.strategy.adaptive_search_enabled
        if scope == "process" and (
            agent_session_id is not None or adaptive_search_enabled
        ):
            self.evidence_reads.register_annotations(run_id)
        return report

    def verifier_result(
        self, run_id: str, candidate_id: str, attempt_id: str,
        agent_session_id: str | None = None,
    ) -> dict[str, Any]:
        # Validate identity before touching the settlement lock.
        self.queries.verifier_result(run_id, candidate_id, attempt_id, agent_session_id)
        try:
            with self.settlement.state.lock_verifier(run_id, candidate_id, blocking=False):
                result = self.queries.verifier_result(
                    run_id, candidate_id, attempt_id, agent_session_id
                )
        except RuntimeError as exc:
            if not str(exc).startswith("VerifierInProgress:"):
                raise
            result = self.queries.verifier_result(
                run_id,
                candidate_id,
                attempt_id,
                agent_session_id,
                running=True,
            )
        status = "ready" if "aggregate_score" in result else str(result["state"])
        self._record_verifier_observation(
            run_id,
            agent_session_id,
            attempt_id,
            status,
        )
        return result

    def _record_verifier_observation(
        self,
        run_id: str,
        agent_session_id: str | None,
        attempt_id: str,
        status: str,
    ) -> None:
        if agent_session_id is None:
            return
        session = self.candidate_sessions.state.read_agent_session(
            agent_session_id,
            run_id,
        )
        try:
            self.trace.record(
                session,
                kind="verifier.observed",
                operation_id=attempt_id,
                payload={"attempt_id": attempt_id, "status": status},
            )
        except Exception as exc:
            self.trace.failed(
                session,
                operation="verifier_observation",
                operation_id=attempt_id,
                error=exc,
                verified_target={"attempt_id": attempt_id},
            )

    def report(self, run_id: str) -> Path:
        return self.reporter.report(run_id)

    def evidence_annotation_usage(self, run_id: str) -> dict[str, Any]:
        return self.evidence_reads.evidence_annotation_usage(run_id)

    def list_available_models(
        self,
        agent_harness: Literal["codex", "pi"],
        runtime_provider: Literal["direct", "thinkthread"],
        query: str | None = None,
    ) -> dict[str, Any]:
        return self.models.list_available_models(agent_harness, runtime_provider, query=query)

    def freeze_spec(
        self,
        spec: SearchSpec,
        verifier_artifacts: list[Path],
        *,
        goal_plus_id: str | None = None,
        agent_harness: Literal["codex", "pi"] | None = None,
        parent_frozen_spec_id: str | None = None,
        revision_reason: str | None = None,
        dispatch_group_id: str | None = None,
        dispatch_target_index: int | None = None,
    ) -> FrozenSpec:
        return self.search_bootstrap.freeze(
            spec,
            verifier_artifacts,
            goal_plus_id=goal_plus_id,
            agent_harness=agent_harness,
            parent_frozen_spec_id=parent_frozen_spec_id,
            revision_reason=revision_reason,
            dispatch_group_id=dispatch_group_id,
            dispatch_target_index=dispatch_target_index,
        )

    def revise_candidate_spec(
        self, agent_session_id: str, request_id: str, parent_frozen_spec_id: str,
        revision_reason: str, revision: CandidateSpecRevisionInput | dict[str, Any],
    ) -> FrozenSpec:
        return self.candidate_spec_revisions.revise(
            agent_session_id, request_id, parent_frozen_spec_id, revision_reason,
            CandidateSpecRevisionInput.model_validate(revision),
        )

    def materialize_candidate(
        self, request: CandidateMaterialization
    ) -> CandidateRecord:
        return self.candidate_creation.materialize(request)

    def start_agent_session(
        self,
        run_id: str,
        candidate_id: str,
        directive: dict[str, Any] | str | None = None,
        *,
        worker_budget: dict[str, Any] | None = None,
        session_mode: Literal["fresh", "fork"] = "fresh",
    ) -> AgentSessionRecord:
        return self.candidate_sessions.start_agent_session(
            run_id, candidate_id, directive, worker_budget=worker_budget,
            session_mode=session_mode,
        )

    def redispatch_candidate(
        self,
        run_id: str,
        candidate_id: str,
        *,
        worker_budget: dict[str, Any] | None = None,
    ) -> AgentSessionRecord:
        return self.candidate_sessions.redispatch_candidate(
            run_id, candidate_id, worker_budget=worker_budget
        )

    def bind_agent_handle(
        self, agent_session_id: str, handle: dict[str, Any]
    ) -> AgentSessionRecord:
        return self.candidate_sessions.bind_agent_handle(agent_session_id, handle)

    def continue_agent_session(
        self, agent_session_id: str, worker_budget: dict[str, Any] | None = None
    ) -> AgentSessionRecord:
        return self.candidate_sessions.continue_agent_session(
            agent_session_id, worker_budget
        )

    def reconcile_thinkthread_request_closes(
        self, run_id: str, *, client: AgentPosixSdkClient | None = None
    ) -> dict[str, Any]:
        return self.provider_mutations.reconcile_thinkthread_request_closes(
            run_id, client=client
        )

    def create_run(self, frozen_spec_id: str, source_run_id: str | None = None) -> str:
        return self.search_run.create_run(frozen_spec_id, source_run_id)

    def plan_next(self, run_id: str, requested_k: int = 4) -> SearchPlan:
        return self.search_run.plan_next(run_id, requested_k)

    def start_batch(
        self,
        run_id: str,
        plan_id: str,
        proposals: list[CandidateProposal] | None = None,
    ) -> list[CandidateTask]:
        return self.search_run.start_batch(run_id, plan_id, proposals)

    def invalidate_run(
        self,
        run_id: str,
        *,
        reason: VerifierInvalidationReason,
        summary: str,
        evidence: list[dict[str, Any]],
    ) -> RunRecord:
        return self.search_run.invalidate_run(
            run_id, reason=reason, summary=summary, evidence=evidence
        )

    def bind_thinkthread_candidate(
        self,
        *,
        run_id: str,
        candidate_id: str,
        agent_session_id: str,
        thinkthread_id: str,
        branch_id: str,
    ) -> CandidateRecord:
        return self.candidate_sessions.bind_thinkthread_candidate(
            run_id=run_id,
            candidate_id=candidate_id,
            agent_session_id=agent_session_id,
            thinkthread_id=thinkthread_id,
            branch_id=branch_id,
        )

    def prepare_candidate_recovery(
        self, run_id: str, candidate_id: str
    ) -> CandidateRecovery:
        return self.recovery.prepare_candidate_recovery(run_id, candidate_id)

    def recover_candidate(
        self, run_id: str, candidate_id: str, recovery_id: str
    ) -> CandidateRecovery:
        return self.recovery.recover_candidate(run_id, candidate_id, recovery_id)

    def goal_plus_search_recovery_status(self, run_id: str, *, detail: bool = False) -> dict[str, Any]:
        return self.recovery.queries.status(run_id, detail=detail)

    def verify_thinkthread_candidate_attempt(
        self,
        *,
        run_id: str,
        candidate_id: str,
        agent_session_id: str,
        thinkthread_id: str,
        branch_id: str,
        hypothesis: str,
        attempt_id: str,
        inner_git_attempt: dict[str, Any] | None = None,
        toolization_decision: ToolizationDecision | dict[str, Any] | None = None,
        client: AgentPosixSdkClient | None = None,
        recovery_deadline: float | None = None,
    ) -> ScoreReport:
        with self.settlement.state.lock_verifier(run_id, candidate_id, blocking=False):
            return self.thinkthread_verification.verify(
                run_id=run_id,
                candidate_id=candidate_id,
                agent_session_id=agent_session_id,
                thinkthread_id=thinkthread_id,
                branch_id=branch_id,
                hypothesis=hypothesis,
                attempt_id=attempt_id,
                inner_git_attempt=inner_git_attempt,
                toolization_decision=toolization_decision,
                io=self.thinkthread_attempt_io(client),
                recovery_deadline=recovery_deadline,
            )

    def stage_shared_tool(
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
        return self.shared_tools.stage(
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

    def copy_shared_tool(
        self, agent_session_id: str, tool_id: str, snapshot_hash: str
    ) -> dict[str, Any]:
        return self.shared_tools.copy(
            agent_session_id=agent_session_id,
            tool_id=tool_id,
            snapshot_hash=snapshot_hash,
        )

    def read_shared_cache(
        self, agent_session_id: str,
    ) -> tuple[FrozenSpec, SharedCacheView]:
        return self.shared_cache.view_for_candidate(agent_session_id)

    def read_shared_cache_for_run(
        self, run_id: str,
    ) -> tuple[FrozenSpec, SharedCacheView]:
        run = self.queries.state.read_run(run_id)
        frozen = self.queries.state.read_frozen_spec(run.frozen_spec_id)
        if not frozen.spec.shared_cache.enabled:
            return frozen, SharedCacheView()
        return frozen, self.shared_cache.view_for_run(run_id, frozen)

    def append_shared_cache(
        self,
        agent_session_id: str,
        *,
        section: str,
        content: str,
        supersedes: str | None = None,
        evidence_ref: str | None = None,
    ) -> dict[str, Any]:
        return self.shared_cache.append(
            agent_session_id,
            section=section,
            content=content,
            supersedes=supersedes,
            evidence_ref=evidence_ref,
        )

    def open_reference_workspace(
        self,
        agent_session_id: str,
        source_candidate_id: str,
        source_iteration: int,
    ) -> ReferenceWorkspaceRecord:
        return self.references.open(
            agent_session_id=agent_session_id,
            source_candidate_id=source_candidate_id,
            source_iteration=source_iteration,
        )

    def select(self, run_id: str, *, frozen_spec_id: str | None = None) -> dict[str, Any]:
        from goal_plus.search_verification import resolve_verification_requests

        return resolve_verification_requests(
            self.selection.select(run_id, frozen_spec_id=frozen_spec_id), self.run_verifier,
            recheck=self._verify_selection_request,
        )

    def _verify_selection_request(self, request: CandidateVerificationRequest) -> ScoreReport:
        with self.settlement.state.lock_verifier(request.run_id, request.candidate_id, blocking=False) as lease:
            return self.verification.recheck_selection(request, execution_lock=lease)

    def select_for_controller(
        self,
        run_id: str,
        directive: ControllerSelectionDirective,
    ) -> dict[str, Any]:
        """Select one exact subject without exposing a model-facing tool."""

        if not isinstance(directive, ControllerSelectionDirective):
            raise TypeError("controller selection requires ControllerSelectionDirective")
        from goal_plus.search_verification import resolve_verification_requests

        return resolve_verification_requests(
            self.selection.select(
                run_id,
                controller_directive=directive,
            ),
            self.run_verifier,
            recheck=self._verify_selection_request,
        )

    def main_agent_notifications(
        self,
        run_id: str,
        *,
        pending_only: bool = True,
    ) -> list[MainAgentNotification]:
        return self.notifications.main_agent_notifications(
            run_id=run_id,
            pending_only=pending_only,
        )

    def acknowledge_main_agent_notification(
        self,
        run_id: str,
        notification_id: str,
    ) -> MainAgentNotification:
        return self.notifications.acknowledge_main_agent_notification(
            run_id=run_id,
            notification_id=notification_id,
        )

    def promote(self, run_id: str, candidate_id: str) -> Path:
        from goal_plus.search_verification import resolve_verification_requests

        path = resolve_verification_requests(
            self.promotion.promote(run_id, candidate_id), self.run_verifier
        )
        run = self.search_run.state.read_run(run_id)
        if run.promotion.provider != "thinkthread":
            self.reporter.refresh_existing(run_id)
        return path

    def apply_promotion(self, run_id: str) -> PromotionRecord:
        return self.publication_apply.apply_promotion(run_id)

    def promotion_record(self, run_id: str) -> PromotionRecord:
        return self.publication_apply.promotion_record(run_id)

    def promotion_apply_coordinates(self, run_id: str) -> tuple[Path, Path, str]:
        return self.promotion.io.apply_coordinates(
            Path(self.search_run.state.read_run(run_id).source_path)
        )

    def cleanup_thinkthread_snapshots(self, run_id: str) -> dict[str, Any]:
        return self.artifact_lifecycle.cleanup(run_id)
