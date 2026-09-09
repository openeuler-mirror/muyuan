"""Authorized publication apply, exact request recovery and pending integration."""

from pathlib import Path
from typing import Any, Callable, Literal
from goal_plus.domain.artifacts import WorkspaceOperationTiming
from goal_plus.domain.evidence_policy import artifact_iteration_eligible
from goal_plus.domain.promotion import (
    PendingIntegrationRecord,
    PromotionRecord,
    PublicationRecoveryRecord,
)
from goal_plus.domain.search import RunState
from goal_plus.ports.state import RunRecord
from goal_plus.ports.clock import Clock
from goal_plus.ports.publication import (
    PublicationApplyState,
    LocalPublicationApply,
    ThinkThreadPublicationApply,
    PreparedPromotion,
)
from goal_plus.application.goal_authorization import GoalAuthorization
from goal_plus.application.publication_contracts import (
    publication_apply_intent,
)
from goal_plus.application.publication_notifications import PublicationNotifications


class PublicationApply:
    def __init__(
        self,
        *,
        state: PublicationApplyState,
        io: LocalPublicationApply,
        thinkthread: ThinkThreadPublicationApply,
        clock: Clock,
        persist_run: Callable[[RunRecord], None],
        authorization: GoalAuthorization,
        notifications: PublicationNotifications,
    ) -> None:
        self.state, self.io, self.clock, self.persist_run = (
            state,
            io,
            clock,
            persist_run,
        )
        self.authorization, self.notifications = authorization, notifications
        self.thinkthread = thinkthread

    def _write_pending_integration(
        self,
        *,
        run: RunRecord,
        promotion: PromotionRecord,
        apply_outcome: Literal["conflict", "policy_rejected", "failed"],
        provider_error: dict[str, Any],
    ) -> Path:
        publication = promotion.publication_artifact_ref
        request_id = promotion.apply_request_id
        if publication is None or request_id is None:
            raise RuntimeError(
                "ThinkThread promotion omitted publication apply identity"
            )
        candidate = self.state.read_candidate(run.run_id, promotion.candidate_id)
        evidence_refs = [
            f"{candidate.candidate_id}:iteration:{item.iteration}"
            for item in candidate.iterations
            if item.artifact_ref == promotion.selected_artifact_ref
            and artifact_iteration_eligible(item)
        ]
        record = PendingIntegrationRecord(
            run_id=run.run_id,
            base_artifact_ref=promotion.base_artifact_ref,
            selected_artifact_ref=promotion.selected_artifact_ref,
            publication_artifact_ref=publication,
            apply_request_id=request_id,
            apply_outcome=apply_outcome,
            changed_paths=list(promotion.changed_files),
            dependency_summary={
                "dependencies": list(
                    promotion.provider_handle.get("dependencies") or []
                ),
                "policy_id": "safe_content_v1",
            },
            evidence_refs=evidence_refs,
            provider_error=provider_error,
            created_at=self.clock.timestamp(),
        )
        return self.state.write_pending(record)

    def _set_thinkthread_publication_recovering(
        self,
        *,
        run_id: str,
        provider_error: dict[str, Any],
    ) -> PromotionRecord:
        with self.state.lock_run(run_id):
            run = self.state.read_run(run_id)
            promotion = run.promotion
            outcome = run.publication_outcome
            if (
                promotion is None
                or outcome is None
                or promotion.apply_request_id is None
            ):
                raise RuntimeError(
                    "ThinkThread publication recovery state is incomplete"
                )
            previous_attempts = outcome.recovery.attempts if outcome.recovery else 0
            now = self.clock.timestamp()
            run.promotion = promotion.model_copy(
                update={
                    "state": "recovering",
                    "error": provider_error["message"],
                }
            )
            run.publication_outcome = outcome.model_copy(
                update={
                    "state": "recovering",
                    "apply_outcome": "needs_recovery",
                    "notification_state": "not_applicable",
                    "recovery": PublicationRecoveryRecord(
                        request_id=promotion.apply_request_id,
                        attempts=previous_attempts + 1,
                        last_error=provider_error["message"],
                        updated_at=now,
                    ),
                    "provider_error": provider_error,
                    "updated_at": now,
                }
            )
            self.persist_run(run)
            return run.promotion

    def _set_thinkthread_publication_pending(
        self,
        *,
        run_id: str,
        apply_outcome: Literal["conflict", "policy_rejected", "failed"],
        provider_error: dict[str, Any],
        timing: WorkspaceOperationTiming,
    ) -> PromotionRecord:
        run = self.state.read_run(run_id)
        promotion = run.promotion
        if promotion is None or run.publication_outcome is None:
            raise RuntimeError("ThinkThread publication state is incomplete")
        pending_path = self._write_pending_integration(
            run=run,
            promotion=promotion,
            apply_outcome=apply_outcome,
            provider_error=provider_error,
        )
        with self.state.lock_run(run_id):
            run = self.state.read_run(run_id)
            promotion = run.promotion
            outcome = run.publication_outcome
            if promotion is None or outcome is None:
                raise RuntimeError("ThinkThread publication state disappeared")
            now = self.clock.timestamp()
            run.promotion = promotion.model_copy(
                update={
                    "state": "awaiting_main_integration",
                    "apply_timing": timing,
                    "apply_result": {},
                    "error": provider_error["message"],
                }
            )
            run.publication_outcome = outcome.model_copy(
                update={
                    "state": "awaiting_main_integration",
                    "apply_outcome": apply_outcome,
                    "pending_artifact_path": pending_path,
                    "notification_state": "not_applicable",
                    "recovery": None,
                    "provider_error": provider_error,
                    "updated_at": now,
                }
            )
            self.persist_run(run)
        self.notifications.queue(run_id)
        return self.promotion_record(run_id)

    def _apply_thinkthread_promotion(self, run_id: str) -> PromotionRecord:
        with self.state.lock_run(run_id):
            run = self.state.read_run(run_id)
            promotion = run.promotion
            outcome = run.publication_outcome
            if promotion is None or outcome is None:
                raise RuntimeError("ThinkThread promotion has no publication outcome")
            if promotion.state in {"applied", "awaiting_main_integration"}:
                return promotion
            if promotion.state not in {"prepared", "applying", "recovering"}:
                raise RuntimeError(
                    f"ThinkThread promotion cannot apply from state {promotion.state!r}"
                )
            publication_revision = outcome.revision
            intent = publication_apply_intent(
                promotion,
                publication_revision,
            )
            request = self.thinkthread.describe_apply(intent)
            expected_request = request.request_id
            persisted_mutation = next(
                (
                    item
                    for item in run.provider_mutations
                    if item.request_id == expected_request
                    and item.operation == "apply"
                    and item.identity == request.identity
                ),
                None,
            )
            if persisted_mutation is None:
                raise RuntimeError("ThinkThread apply intent is not durable")
            if promotion.apply_request_id != expected_request:
                raise RuntimeError(
                    "ThinkThread apply RequestId no longer matches intent"
                )
            now = self.clock.timestamp()
            run.promotion = promotion.model_copy(
                update={"state": "applying", "error": None}
            )
            run.publication_outcome = outcome.model_copy(
                update={
                    "state": "applying",
                    "apply_outcome": "unknown",
                    "notification_state": "not_applicable",
                    "recovery": outcome.recovery,
                    "provider_error": {},
                    "updated_at": now,
                }
            )
            self.persist_run(run)

        apply_started_at = self.clock.timestamp()
        apply_started = self.clock.monotonic()
        try:
            result = self.thinkthread.apply(intent)
            if result.request_id != expected_request:
                raise RuntimeError(
                    "ThinkThread apply RequestId changed during execution"
                )
            if result.state == "applied" and (
                result.base_artifact_ref != intent.base_artifact_ref
                or result.target_artifact_ref != intent.target_artifact_ref
            ):
                raise RuntimeError(
                    "ThinkThread apply returned a mismatched success result: "
                    f"{result}"
                )
        except Exception as exc:
            return self._set_thinkthread_publication_recovering(
                run_id=run_id,
                provider_error={"type": type(exc).__name__, "message": str(exc)[:2000]},
            )

        if result.state != "applied":
            timing = WorkspaceOperationTiming(
                started_at=apply_started_at,
                completed_at=self.clock.timestamp(),
                duration_seconds=max(0.0, self.clock.monotonic() - apply_started),
                outcome="failed",
            )
            provider_error = dict(result.error or {"message": "publication apply needs recovery"})
            if result.state in {"conflict", "policy_rejected", "failed"}:
                return self._set_thinkthread_publication_pending(
                    run_id=run_id,
                    apply_outcome=result.state,
                    provider_error=provider_error,
                    timing=timing,
                )
            return self._set_thinkthread_publication_recovering(
                run_id=run_id,
                provider_error=provider_error,
            )

        timing = WorkspaceOperationTiming(
            started_at=apply_started_at,
            completed_at=self.clock.timestamp(),
            duration_seconds=max(0.0, self.clock.monotonic() - apply_started),
            outcome="succeeded",
        )
        with self.state.lock_run(run_id):
            run = self.state.read_run(run_id)
            promotion = run.promotion
            outcome = run.publication_outcome
            if promotion is None or outcome is None:
                raise RuntimeError("ThinkThread publication state disappeared")
            run.promotion = promotion.model_copy(
                update={
                    "state": "applied",
                    "apply_result": dict(result.details or {}),
                    "apply_timing": timing,
                    "error": None,
                    "applied_at": timing.completed_at,
                }
            )
            run.publication_outcome = outcome.model_copy(
                update={
                    "state": "applied",
                    "apply_outcome": "applied",
                    "notification_state": "not_applicable",
                    "recovery": None,
                    "provider_error": {},
                    "updated_at": timing.completed_at,
                }
            )
            self.persist_run(run)
        self.notifications.queue(run_id)
        return self.promotion_record(run_id)

    def apply_promotion(self, run_id: str) -> PromotionRecord:
        self.authorization.require_search_execution(run_id)
        """Apply one prepared provider-native publication to its source root."""

        initial = self.state.read_run(run_id)
        if (
            initial.promotion is not None
            and initial.promotion.provider == "thinkthread"
        ):
            if initial.state != RunState.PROMOTED:
                raise RuntimeError(
                    "search_promote must prepare a promotion before apply"
                )
            if initial.promotion.promotion_mode == "artifact_only":
                raise RuntimeError(
                    "promotion is artifact-only; freeze the SearchSpec with "
                    "workspace.promotion_mode='apply' to authorize source write-back"
                )
            return self._apply_thinkthread_promotion(run_id)

        with self.state.lock_run(run_id):
            run = self.state.read_run(run_id)
            if run.state != RunState.PROMOTED or run.promotion is None:
                raise RuntimeError(
                    "search_promote must prepare a promotion before apply"
                )
            promotion = run.promotion
            if promotion.state == "applied":
                return promotion
            if promotion.promotion_mode == "artifact_only":
                raise RuntimeError(
                    "promotion is artifact-only; freeze the SearchSpec with "
                    "workspace.promotion_mode='apply' to authorize source write-back"
                )
            if promotion.strategy == "legacy_patch":
                raise RuntimeError(
                    "legacy patch promotions remain export-only; apply the saved patch "
                    "with its returned coordinates"
                )
            prepared = PreparedPromotion(
                provider=promotion.provider,
                strategy=promotion.strategy,
                base_artifact_ref=promotion.base_artifact_ref,
                selected_artifact_ref=promotion.selected_artifact_ref,
                publication_artifact_ref=promotion.publication_artifact_ref,
                artifact_path=promotion.artifact_path,
                source_repository=promotion.source_repository,
                source_subdir=promotion.source_subdir,
                changed_files=tuple(promotion.changed_files),
                provider_handle=promotion.provider_handle,
            )
            apply_started_at = self.clock.timestamp()
            apply_started = self.clock.monotonic()
            try:
                result = self.io.apply_local(prepared)
            except Exception as exc:
                apply_timing = WorkspaceOperationTiming(
                    started_at=apply_started_at,
                    completed_at=self.clock.timestamp(),
                    duration_seconds=max(0.0, self.clock.monotonic() - apply_started),
                    outcome="failed",
                )
                run.promotion = promotion.model_copy(
                    update={
                        "state": "apply_failed",
                        "apply_timing": apply_timing,
                        "error": f"{type(exc).__name__}: {exc}"[:2000],
                    }
                )
                self.persist_run(run)
                raise
            apply_timing = WorkspaceOperationTiming(
                started_at=apply_started_at,
                completed_at=self.clock.timestamp(),
                duration_seconds=max(0.0, self.clock.monotonic() - apply_started),
                outcome="succeeded",
            )
            run.promotion = promotion.model_copy(
                update={
                    "state": "applied",
                    "applied_source_revision": result.source_revision,
                    "apply_result": dict(result.details),
                    "apply_timing": apply_timing,
                    "error": None,
                    "applied_at": apply_timing.completed_at,
                }
            )
            self.persist_run(run)
            return run.promotion

    def promotion_record(self, run_id: str) -> PromotionRecord:
        run = self.state.read_run(run_id)
        if run.promotion is None:
            raise RuntimeError("run has no prepared promotion")
        return run.promotion
