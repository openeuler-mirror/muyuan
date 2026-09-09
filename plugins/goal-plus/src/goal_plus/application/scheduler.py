"""Scheduler claim/completion policy, separate from Annotation and host lifecycle."""

import hashlib
from typing import Any

from goal_plus.domain.adaptive.models import (
    ScheduleTask,
    requests_allocation_confirmation,
)
from goal_plus.domain.adaptive.scheduler import (
    PermanentSchedulerError,
    SchedulerError,
    TransientSchedulerError,
)
from goal_plus.domain.search import RunState
from goal_plus.domain.time import outer_deadline_epoch
from goal_plus.ports.clock import Clock
from goal_plus.ports.scheduler import (
    SchedulerResult,
    SchedulerRunSource,
    SchedulerTaskState,
)

DEFAULT_SCHEDULER_MAX_ATTEMPTS = 3
SCHEDULER_RETRY_BACKOFF_SECONDS = (1, 5, 15)
# Existing fallback when the frozen task has no runnable profile.
DEFAULT_SCHEDULER_LEASE_SECONDS = 180
SCHEDULER_RUN_STATES = frozenset(
    {
        RunState.RUNNING,
        RunState.WAITING_FOR_WORKERS,
        RunState.SELECTING,
        RunState.SELECTION_BLOCKED,
    }
)


class SchedulerWorkflow:
    def __init__(
        self, *, tasks: SchedulerTaskState, runs: SchedulerRunSource, clock: Clock
    ) -> None:
        self.tasks = tasks
        self.runs = runs
        self.clock = clock

    def run_active(self, run_id: str) -> bool:
        run = self.runs.read_run(run_id)
        return run.invalidated_at is None and run.state in SCHEDULER_RUN_STATES

    def claim(
        self,
        run_id: str,
        candidate_id: str,
        iteration: int,
    ) -> ScheduleTask | None:
        with self.tasks.lock_run(run_id):
            if not self.run_active(run_id):
                return None
            task = self.tasks.load_schedule_task(
                run_id,
                candidate_id,
                iteration,
            )
            if task is None or task.state not in {"pending", "running", "retry_wait"}:
                return None
            now_epoch = self.clock.now()
            deadline = outer_deadline_epoch(task.outer_deadline_at)
            maximum = DEFAULT_SCHEDULER_MAX_ATTEMPTS
            if task.attempts >= maximum or (
                deadline is not None and deadline <= now_epoch
            ):
                error = (
                    "scheduler outer deadline expired"
                    if deadline is not None and deadline <= now_epoch
                    else "scheduler attempt limit reached"
                )
                self.tasks.write_schedule_task(
                    task.model_copy(
                        update={
                            "state": "terminal_error",
                            "next_attempt_at": None,
                            "last_error": error,
                            "error_fingerprint": hashlib.sha256(
                                error.encode("utf-8")
                            ).hexdigest(),
                            "updated_at": self.clock.timestamp(),
                        }
                    )
                )
                return None
            available_at = outer_deadline_epoch(task.next_attempt_at)
            if available_at is not None and available_at > now_epoch:
                return None
            attempt_number = task.attempts + 1
            lease_seconds = (
                task.profile.timeout_seconds
                if task.profile is not None
                else DEFAULT_SCHEDULER_LEASE_SECONDS
            )
            history = [
                *task.attempt_history,
                {
                    "attempt": attempt_number,
                    "started_at": self.clock.timestamp(),
                    "monitor_path": self.tasks.attempt_monitor_reference(
                        run_id,
                        candidate_id,
                        iteration,
                        attempt_number,
                    ),
                },
            ]
            claimed = task.model_copy(
                update={
                    "state": "running",
                    "attempts": attempt_number,
                    "next_attempt_at": self.clock.timestamp_from_epoch(
                        now_epoch + lease_seconds
                    ),
                    "attempt_history": history,
                    "updated_at": self.clock.timestamp(),
                }
            )
            self.tasks.write_schedule_task(claimed)
            return claimed

    def complete(
        self,
        claimed: ScheduleTask,
        *,
        result: SchedulerResult | None = None,
        error: SchedulerError | None = None,
    ) -> bool:
        with self.tasks.lock_run(claimed.run_id):
            current = self.tasks.load_schedule_task(
                claimed.run_id,
                claimed.candidate_id,
                claimed.iteration,
            )
            if (
                current is None
                or current.attempts != claimed.attempts
                or current.state != "running"
            ):
                return False
            if result is not None:
                previous = self.tasks.load_schedule_task(
                    current.run_id, current.candidate_id, current.iteration - 1
                )
                confirmation = requests_allocation_confirmation(previous)
                if not confirmation and result.output.decision is not None:
                    raise TransientSchedulerError(
                        "non-blocking scheduler output cannot include a Decision"
                    )
                if (
                    confirmation
                    and result.output.allocation_flag
                    and result.output.decision is None
                ):
                    raise TransientSchedulerError(
                        "blocking reschedule output requires a Decision"
                    )
            history = list(current.attempt_history)
            latest = dict(history[-1]) if history else {}
            latest["finished_at"] = self.clock.timestamp()
            usage = dict(current.usage)
            observed_usage = (
                result.usage if result is not None else error.usage if error else {}
            )
            for key, value in observed_usage.items():
                usage[key] = usage.get(key, 0) + value
            if observed_usage:
                latest["usage"] = dict(observed_usage)
            if result is not None:
                update: dict[str, Any] = {
                    "state": "completed",
                    "next_attempt_at": None,
                    "last_error": None,
                    "error_fingerprint": None,
                    "output": result.output,
                    "invocation": result.invocation,
                }
            else:
                assert error is not None
                error_text = f"{type(error).__name__}: {error}"[:2000]
                latest["error"] = error_text
                terminal = (
                    isinstance(error, PermanentSchedulerError)
                    or current.attempts >= DEFAULT_SCHEDULER_MAX_ATTEMPTS
                    or not self.run_active(current.run_id)
                )
                backoff = SCHEDULER_RETRY_BACKOFF_SECONDS[
                    min(current.attempts - 1, len(SCHEDULER_RETRY_BACKOFF_SECONDS) - 1)
                ]
                update = {
                    "state": "terminal_error" if terminal else "retry_wait",
                    "next_attempt_at": (
                        None
                        if terminal
                        else self.clock.timestamp_from_epoch(self.clock.now() + backoff)
                    ),
                    "last_error": error_text,
                    "error_fingerprint": hashlib.sha256(
                        error_text.encode("utf-8")
                    ).hexdigest(),
                }
            if history:
                history[-1] = latest
            self.tasks.write_schedule_task(
                current.model_copy(
                    update={
                        **update,
                        "attempt_history": history,
                        "usage": usage,
                        "updated_at": self.clock.timestamp(),
                    }
                )
            )
            return result is not None
