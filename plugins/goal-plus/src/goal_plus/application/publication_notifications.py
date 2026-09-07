"""Durable Main publication notification creation and acknowledgment."""

from typing import Callable, Literal
import hashlib
from goal_plus.domain.promotion import (
    MainAgentNotification,
)
from goal_plus.ports.state import (
    RunRecord,
)
from goal_plus.ports.publication import PublicationNotificationState
from goal_plus.ports.clock import Clock


def publication_notification_id(
    run_id: str,
    publication_revision: int,
) -> str:
    digest = hashlib.sha256(
        f"goal-plus:publication-notification:{run_id}:{publication_revision}".encode()
    ).hexdigest()[:24]
    return f"notification-{digest}"


class PublicationNotifications:
    def __init__(
        self,
        state: PublicationNotificationState,
        clock: Clock,
        persist_run: Callable[[RunRecord], None],
    ) -> None:
        self.state, self.clock, self.persist_run = state, clock, persist_run

    def queue(self, run_id: str) -> MainAgentNotification:
        with self.state.lock_run(run_id):
            run = self.state.read_run(run_id)
            outcome = run.publication_outcome
            if outcome is None or outcome.state not in {
                "applied",
                "awaiting_main_integration",
            }:
                raise RuntimeError(
                    "publication is not terminal and cannot notify MainAgent"
                )
            existing_record = self.state.read_notification(run_id, outcome.revision)
            kind: Literal["publication_applied", "publication_pending_integration"] = (
                "publication_applied"
                if outcome.state == "applied"
                else "publication_pending_integration"
            )
            existing: MainAgentNotification | None = None
            if existing_record is not None:
                existing = existing_record
                if (
                    existing.run_id != run_id
                    or existing.publication_revision != outcome.revision
                    or existing.kind != kind
                ):
                    raise RuntimeError("publication notification identity mismatch")
            if existing is None:
                report_path = self.state.report_path(run_id)
                if kind == "publication_applied":
                    message = (
                        "Search completed with passing verification and the publication "
                        "was applied to the current workspace."
                    )
                else:
                    message = (
                        "Search completed with passing verification, but the publication "
                        "was not applied. Use the pending integration artifact."
                    )
                existing = MainAgentNotification(
                    notification_id=publication_notification_id(
                        run_id,
                        outcome.revision,
                    ),
                    run_id=run_id,
                    publication_revision=outcome.revision,
                    kind=kind,
                    payload={
                        "message": message,
                        "publication_state": outcome.state,
                        "apply_outcome": outcome.apply_outcome,
                        "changed_paths": list(outcome.changed_paths),
                        "report_path": str(report_path),
                        "pending_artifact_path": (
                            str(outcome.pending_artifact_path)
                            if outcome.pending_artifact_path is not None
                            else None
                        ),
                    },
                    created_at=self.clock.timestamp(),
                )
                self.state.write_notification(existing)
            desired_state = "delivered" if existing.state == "delivered" else "pending"
            if outcome.notification_state != desired_state:
                run.publication_outcome = outcome.model_copy(
                    update={
                        "notification_state": desired_state,
                        "updated_at": self.clock.timestamp(),
                    }
                )
                self.persist_run(run)
            return existing

    def main_agent_notifications(
        self,
        run_id: str,
        *,
        pending_only: bool = True,
    ) -> list[MainAgentNotification]:
        run = self.state.read_run(run_id)
        if run.publication_outcome is not None and run.publication_outcome.state in {
            "applied",
            "awaiting_main_integration",
        }:
            self.queue(run_id)
        notifications = self.state.read_notifications(run_id)
        if pending_only:
            notifications = [item for item in notifications if item.state == "pending"]
        return notifications

    def acknowledge_main_agent_notification(
        self,
        run_id: str,
        notification_id: str,
    ) -> MainAgentNotification:
        with self.state.lock_run(run_id):
            run = self.state.read_run(run_id)
            outcome = run.publication_outcome
            if outcome is None:
                raise RuntimeError("run has no publication notification")
            existing_record = self.state.read_notification(run_id, outcome.revision)
            if existing_record is None:
                raise RuntimeError("publication notification is not durable")
            notification = existing_record
            if notification.notification_id != notification_id:
                raise RuntimeError("publication notification identity mismatch")
            if notification.state != "delivered":
                notification = notification.model_copy(
                    update={
                        "state": "delivered",
                        "delivered_at": self.clock.timestamp(),
                    }
                )
                self.state.write_notification(notification)
            if outcome.notification_state != "delivered":
                run.publication_outcome = outcome.model_copy(
                    update={
                        "notification_state": "delivered",
                        "updated_at": notification.delivered_at
                        or self.clock.timestamp(),
                    }
                )
                self.persist_run(run)
            return notification
