"""Persist the existing monitor document without owning task transitions."""

from pathlib import Path

from goal_plus.infrastructure.state.files import write_json
from goal_plus.ports.process_monitor import ProcessMonitorSnapshot


class FileProcessMonitorSink:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __call__(self, snapshot: ProcessMonitorSnapshot) -> None:
        payload = snapshot.model_dump(mode="json", exclude={"last_events"})
        payload["last_events"] = [
            event.model_dump(mode="json", exclude_none=True) for event in snapshot.last_events
        ]
        if snapshot.task_kind is None:
            payload.pop("task_kind")
        write_json(self.path, payload)
