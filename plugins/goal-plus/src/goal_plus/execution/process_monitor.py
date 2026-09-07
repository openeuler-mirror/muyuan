"""Best-effort process diagnostics shared by existing inference roles."""

import json
import time
from collections.abc import Callable

from goal_plus.ports.clock import Clock
from goal_plus.ports.process_monitor import (
    ProcessEventSnapshot,
    ProcessEventSummary,
    ProcessMonitorIdentity,
    ProcessMonitorSink,
    ProcessMonitorSnapshot,
    ProcessMonitorState,
    ProcessObservation,
)

# Existing Annotation and Scheduler diagnostic limits, not model output limits.
MONITOR_UPDATE_SECONDS = 5.0
MONITOR_TAIL_CHARS = 2_000
MONITOR_LAST_EVENTS = 12


def event_snapshot(stdout: str, *, include_timestamps: bool = False) -> ProcessEventSnapshot:
    snapshot = ProcessEventSnapshot()
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            snapshot.non_json_lines += 1
            continue
        if not isinstance(event, dict):
            snapshot.non_json_lines += 1
            continue
        snapshot.json_lines += 1
        event_type = str(event.get("type") or "unknown")
        counts = snapshot.event_type_counts
        counts[event_type] = counts.get(event_type, 0) + 1
        summary = ProcessEventSummary(type=event_type)
        timestamp = event.get("timestamp")
        if include_timestamps and isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
            summary.timestamp = timestamp
        assistant = event.get("assistantMessageEvent")
        if isinstance(assistant, dict):
            assistant_type = str(assistant.get("type") or "unknown")
            counts = snapshot.assistant_event_type_counts
            counts[assistant_type] = counts.get(assistant_type, 0) + 1
            summary.assistant_event_type = assistant_type
            index = assistant.get("contentIndex")
            if isinstance(index, int) and not isinstance(index, bool):
                summary.content_index = index
            delta = assistant.get("delta")
            if isinstance(delta, str):
                summary.delta_bytes = len(delta.encode("utf-8"))
        message = event.get("message")
        if isinstance(message, dict):
            for source, target in (
                ("role", "message_role"), ("stopReason", "stop_reason"), ("responseId", "response_id"),
            ):
                value = message.get(source)
                if isinstance(value, str) and value:
                    setattr(summary, target, value)
        snapshot.last_events.append(summary)
        if len(snapshot.last_events) > MONITOR_LAST_EVENTS:
            snapshot.last_events.pop(0)
    return snapshot


class ProcessMonitor:
    def __init__(
        self,
        identity: ProcessMonitorIdentity,
        *,
        sink: ProcessMonitorSink | None,
        clock: Clock,
        monotonic: Callable[[], float] = time.monotonic,
        include_event_timestamps: bool = False,
    ) -> None:
        self.sink = sink
        self.clock = clock
        self.monotonic = monotonic
        self.include_event_timestamps = include_event_timestamps
        self.started = monotonic()
        self.last_write = 0.0
        self.snapshot = ProcessMonitorSnapshot(
            **identity.model_dump(), started_at=clock.timestamp(), updated_at=clock.timestamp(),
        )

    def observe(
        self,
        state: ProcessMonitorState,
        process: ProcessObservation,
        *,
        stdout: str = "",
        stderr: str = "",
        detail: str | None = None,
        force: bool = False,
    ) -> None:
        if self.sink is None:
            return
        now = self.monotonic()
        if not force and self.last_write and now - self.last_write < MONITOR_UPDATE_SECONDS:
            return
        events = event_snapshot(stdout, include_timestamps=self.include_event_timestamps)
        self.snapshot = ProcessMonitorSnapshot.model_validate({
            **self.snapshot.model_dump(), **events.model_dump(),
            "state": state,
            "updated_at": self.clock.timestamp(),
            "elapsed_seconds": round(now - self.started, 3),
            "pid": getattr(process, "pid", None),
            "process_returncode": getattr(process, "returncode", None),
            "stdout_bytes": len(stdout.encode("utf-8")),
            "stderr_bytes": len(stderr.encode("utf-8")),
            "stdout_tail": stdout[-MONITOR_TAIL_CHARS:],
            "stderr_tail": stderr[-MONITOR_TAIL_CHARS:],
            "detail": detail[:MONITOR_TAIL_CHARS] if detail else None,
        })
        try:
            self.sink(self.snapshot)
            self.last_write = now
        except OSError:
            # Diagnostics cannot change the inference/task result.
            return
