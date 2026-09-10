"""LLM Verifier task and record persistence boundaries."""

from contextlib import AbstractContextManager
from typing import Protocol

from goal_plus.domain.verifier import (
    VerifierRecord,
    VerifierTask,
    VerifierTaskState,
)


class VerifierTaskQuery(Protocol):
    def load_verifier_task(self, task_id: str) -> VerifierTask: ...
    def load_verifier_tasks(
        self,
        run_id: str | None = None,
        *,
        candidate_id: str | None = None,
        state: VerifierTaskState | None = None,
    ) -> list[VerifierTask]: ...
    def load_verifier_records(
        self,
        run_id: str,
        *,
        candidate_id: str | None = None,
        iteration: int | None = None,
        settlement_id: str | None = None,
    ) -> list[VerifierRecord]: ...


class VerifierState(VerifierTaskQuery, Protocol):
    def lock_verifier_queue(self) -> AbstractContextManager[None]: ...
    def lock_run(self, run_id: str) -> AbstractContextManager[None]: ...
    def write_verifier_task(self, task: VerifierTask) -> None: ...
    def write_verifier_record(self, record: VerifierRecord) -> None: ...


class VerifierRecordConsumer(Protocol):
    def next_pending_record(
        self,
        run_id: str,
        *,
        excluded_record_ids: set[str] | None = None,
    ) -> VerifierRecord | None: ...
    def has_pending_record(
        self,
        run_id: str,
        *,
        excluded_record_ids: set[str] | None = None,
    ) -> bool: ...
    def on_verifier_record_completed(self, record: VerifierRecord) -> object: ...


__all__ = ["VerifierRecordConsumer", "VerifierState", "VerifierTaskQuery"]
