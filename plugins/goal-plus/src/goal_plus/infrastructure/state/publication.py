"""Publication artifacts and notifications in the existing run layout."""

from pathlib import Path
from goal_plus.domain.promotion import MainAgentNotification, PendingIntegrationRecord
from goal_plus.infrastructure.state.candidate_creation import FileCandidateCreationState
from goal_plus.infrastructure.state.files import load_json, write_json, exclusive_file_lock


class FilePublicationState(FileCandidateCreationState):
    def lock_run(self, run_id: str):
        return exclusive_file_lock(self.run_dir(run_id) / "run.lock")

    def notification_path(self, run_id: str, revision: int) -> Path:
        return (
            self.run_dir(run_id) / "notifications" / f"publication-{revision:04d}.json"
        )

    def read_notification(
        self, run_id: str, revision: int
    ) -> MainAgentNotification | None:
        path = self.notification_path(run_id, revision)
        return (
            MainAgentNotification.model_validate(load_json(path))
            if path.is_file()
            else None
        )

    def read_notifications(self, run_id: str) -> list[MainAgentNotification]:
        return [
            MainAgentNotification.model_validate(load_json(path))
            for path in sorted((self.run_dir(run_id) / "notifications").glob("*.json"))
        ]

    def write_notification(self, notification: MainAgentNotification) -> None:
        write_json(
            self.notification_path(
                notification.run_id, notification.publication_revision
            ),
            notification.model_dump(mode="json"),
        )

    def report_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "report.md"

    def publication_path(self, run_id: str, candidate_id: str) -> Path:
        return self.run_dir(run_id) / "promotion" / f"{candidate_id}.json"

    def write_pending(self, record: PendingIntegrationRecord) -> Path:
        path = self.run_dir(record.run_id) / "artifacts" / "pending-integration.json"
        write_json(path, record.model_dump(mode="json"))
        return path
