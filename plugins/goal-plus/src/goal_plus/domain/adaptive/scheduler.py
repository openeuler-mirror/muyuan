"""Scheduler identity and failure categories, independent of inference."""

from goal_plus.domain.adaptive.models import ScheduleTask, requests_allocation_confirmation


def confirmation_execution_barrier(previous: ScheduleTask | None, current: ScheduleTask | None) -> str | None:
    if not requests_allocation_confirmation(previous):
        return None
    if current is None:
        return "adaptive allocation confirmation is waiting for its AnnotationTask and ScheduleTask"
    if current.state not in {"completed", "terminal_error"}:
        return f"adaptive allocation confirmation ScheduleTask is {current.state}"
    if current.state == "completed" and current.output is not None and current.output.allocation_flag and current.decision_id is None:
        return "adaptive allocation confirmation Decision is not reconciled"
    return None


def scheduler_node_id(candidate_id: str, iteration: int) -> str:
    return f"{candidate_id}/iteration-{iteration:04d}"


class SchedulerError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        usage: dict[str, int | float] | None = None,
    ) -> None:
        super().__init__(message)
        self.usage = dict(usage or {})


class PermanentSchedulerError(SchedulerError):
    pass


class TransientSchedulerError(SchedulerError):
    pass
