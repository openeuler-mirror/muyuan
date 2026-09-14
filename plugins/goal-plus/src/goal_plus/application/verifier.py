"""Idempotent LLM Verifier admission and reliable task state transitions."""

from __future__ import annotations

from typing import Any

from goal_plus.domain.evidence import IterationRecord
from goal_plus.domain.search import FrozenSpec
from goal_plus.domain.serialization import canonical_json, sha256_text
from goal_plus.domain.time import timestamp_epoch
from goal_plus.domain.verifier import (
    VerifierInvocationRecord,
    VerifierLease,
    VerifierRecord,
    VerifierResult,
    VerifierSubject,
    VerifierTask,
    VerifierTaskState,
)
from goal_plus.ports.clock import Clock
from goal_plus.ports.state import CandidateRecord
from goal_plus.ports.verifier import VerifierState


def verifier_subject(
    frozen: FrozenSpec,
    record: CandidateRecord,
    iteration: IterationRecord,
) -> VerifierSubject:
    receipt = iteration.settlement_receipt
    if receipt is None:
        raise ValueError("verifier assessment requires an exact settlement receipt")
    if (
        receipt.run_id != record.task.run_id
        or receipt.candidate_id != record.candidate_id
        or receipt.evidence.metric_name != frozen.spec.metric_name
        or receipt.attempt_artifact_ref.provider != record.task.workspace_backend
    ):
        raise ValueError("settlement receipt does not match the candidate contract")
    return VerifierSubject(
        frozen_spec_id=frozen.frozen_spec_id,
        settlement=receipt,
    )


class VerifierWorkflow:
    def __init__(self, state: VerifierState, clock: Clock) -> None:
        self.state, self.clock = state, clock

    @staticmethod
    def _task_id(
        subject: VerifierSubject,
        allocation_policy_id: str | None,
        allocation_score_names: list[str],
    ) -> str:
        identity = {
            "run_id": subject.run_id,
            "settlement_id": subject.settlement_id,
            "allocation_policy_id": allocation_policy_id,
            "allocation_score_names": allocation_score_names,
        }
        return f"verifier_{sha256_text(canonical_json(identity))[:24]}"

    def ensure_assessment(
        self,
        subject: VerifierSubject,
        allocation_policy_id: str | None = None,
        allocation_score_names: list[str] | None = None,
    ) -> VerifierTask:
        score_names = list(allocation_score_names or [])
        task_id = self._task_id(subject, allocation_policy_id, score_names)
        with self.state.lock_run(subject.run_id):
            tasks = self.state.load_verifier_tasks(subject.run_id)
            existing = next(
                (item for item in tasks if item.task_id == task_id),
                None,
            )
            if existing is not None:
                if (
                    existing.subject != subject
                    or existing.allocation_policy_id != allocation_policy_id
                    or existing.allocation_score_names != score_names
                ):
                    raise RuntimeError("verifier task identity collision")
                return existing
            now = self.clock.timestamp()
            task = VerifierTask(
                task_id=task_id,
                queue_sequence=max(
                    (item.queue_sequence for item in tasks), default=0
                )
                + 1,
                subject=subject,
                allocation_policy_id=allocation_policy_id,
                allocation_score_names=score_names,
                created_at=now,
                updated_at=now,
            )
            self.state.write_verifier_task(task)
            return task

    def get_task(self, task_id: str) -> VerifierTask:
        return self.state.load_verifier_task(task_id)

    def list_tasks(
        self,
        run_id: str | None = None,
        *,
        candidate_id: str | None = None,
        state: VerifierTaskState | None = None,
    ) -> list[VerifierTask]:
        return self.state.load_verifier_tasks(
            run_id,
            candidate_id=candidate_id,
            state=state,
        )

    def list_records(
        self,
        run_id: str,
        *,
        candidate_id: str | None = None,
        iteration: int | None = None,
        settlement_id: str | None = None,
    ) -> list[VerifierRecord]:
        return self.state.load_verifier_records(
            run_id,
            candidate_id=candidate_id,
            iteration=iteration,
            settlement_id=settlement_id,
        )

    def claim_task(
        self, worker_id: str, lease_seconds: int, *, run_id: str | None = None
    ) -> VerifierTask | None:
        if not worker_id.strip():
            raise ValueError("verifier worker_id must be non-empty")
        if lease_seconds <= 0:
            raise ValueError("verifier lease_seconds must be positive")
        with self.state.lock_verifier_queue():
            now_epoch = self.clock.now()
            discovered_tasks = self.state.load_verifier_tasks()
            active_runs = {
                task.run_id
                for task in discovered_tasks
                if task.state == "running"
                and task.lease is not None
                and (
                    (lease_epoch := timestamp_epoch(task.lease.expires_at)) is None
                    or lease_epoch > now_epoch
                )
            }
            for discovered in discovered_tasks:
                if run_id is not None and discovered.run_id != run_id:
                    continue
                if discovered.run_id in active_runs:
                    continue
                with self.state.lock_run(discovered.run_id):
                    task = self.state.load_verifier_task(discovered.task_id)
                    eligible = task.state == "pending"
                    if task.state == "retry_wait" and task.next_attempt_at is not None:
                        retry_epoch = timestamp_epoch(task.next_attempt_at)
                        eligible = retry_epoch is not None and retry_epoch <= now_epoch
                    if task.state == "running" and task.lease is not None:
                        lease_epoch = timestamp_epoch(task.lease.expires_at)
                        eligible = lease_epoch is not None and lease_epoch <= now_epoch
                    if not eligible:
                        continue
                    claimed = task.model_copy(
                        update={
                            "state": "running",
                            "attempts": task.attempts + 1,
                            "next_attempt_at": None,
                            "lease": VerifierLease(
                                worker_id=worker_id.strip(),
                                expires_at=self.clock.timestamp_from_epoch(
                                    now_epoch + lease_seconds
                                ),
                            ),
                            "error": None,
                            "updated_at": self.clock.timestamp(),
                        }
                    )
                    self.state.write_verifier_task(claimed)
                    return claimed
        return None

    def complete_task(
        self,
        task_id: str,
        result: VerifierResult | dict[str, Any],
        invocation: VerifierInvocationRecord | dict[str, Any],
        usage: dict[str, int | float] | None = None,
    ) -> VerifierRecord:
        task = self.state.load_verifier_task(task_id)
        with self.state.lock_run(task.run_id):
            task = self.state.load_verifier_task(task_id)
            if task.state == "completed" and task.output_record_id is not None:
                return self._load_output(task)
            if task.state != "running":
                raise RuntimeError("only a running verifier task can be completed")
            now = self.clock.timestamp()
            record_id = f"record_{sha256_text(task.task_id)[:24]}"
            existing = self._existing_output(task, record_id)
            if existing is not None:
                completed = task.model_copy(
                    update={
                        "state": "completed",
                        "lease": None,
                        "output_record_id": record_id,
                        "usage": dict(existing.usage),
                        "invocation": existing.invocation,
                        "error": None,
                        "updated_at": now,
                    }
                )
                self.state.write_verifier_task(completed)
                return existing
            invocation_record = VerifierInvocationRecord.model_validate(invocation)
            parsed = VerifierResult.model_validate(result)
            actual_names = (
                sorted(parsed.allocation_scores)
                if parsed.allocation_scores is not None
                else []
            )
            if actual_names != sorted(task.allocation_score_names):
                raise ValueError(
                    "verifier allocation_scores must exactly match the task registration"
                )
            output = VerifierRecord(
                record_id=record_id,
                task_id=task.task_id,
                subject=task.subject,
                allocation_policy_id=task.allocation_policy_id,
                invocation=invocation_record,
                usage=dict(usage or {}),
                created_at=now,
                **parsed.model_dump(),
            )
            self.state.write_verifier_record(output)
            completed = task.model_copy(
                update={
                    "state": "completed",
                    "lease": None,
                    "output_record_id": record_id,
                    "usage": dict(usage or {}),
                    "invocation": invocation_record,
                    "error": None,
                    "updated_at": now,
                }
            )
            self.state.write_verifier_task(completed)
            return output

    def _existing_output(
        self, task: VerifierTask, record_id: str
    ) -> VerifierRecord | None:
        return next(
            (
                item
                for item in self.state.load_verifier_records(
                    task.run_id,
                    settlement_id=task.subject.settlement_id,
                )
                if item.record_id == record_id
                and item.task_id == task.task_id
                and item.subject == task.subject
            ),
            None,
        )

    def _load_output(self, task: VerifierTask) -> VerifierRecord:
        records = self.state.load_verifier_records(
            task.run_id,
            settlement_id=task.subject.settlement_id,
        )
        record = next(
            (item for item in records if item.record_id == task.output_record_id),
            None,
        )
        if record is None:
            raise RuntimeError("completed verifier task has no immutable output record")
        return record

    def fail_task(
        self,
        task_id: str,
        error: str,
        retryable: bool,
        *,
        retry_delay_seconds: int = 30,
    ) -> VerifierTask:
        if not error.strip():
            raise ValueError("verifier task error must be non-empty")
        task = self.state.load_verifier_task(task_id)
        with self.state.lock_run(task.run_id):
            task = self.state.load_verifier_task(task_id)
            if task.state == "completed":
                return task
            if task.state != "running":
                raise RuntimeError("only a running verifier task can fail")
            can_retry = retryable and task.attempts < task.max_attempts
            now_epoch = self.clock.now()
            failed = task.model_copy(
                update={
                    "state": "retry_wait" if can_retry else "terminal_error",
                    "lease": None,
                    "next_attempt_at": (
                        self.clock.timestamp_from_epoch(now_epoch + retry_delay_seconds)
                        if can_retry
                        else None
                    ),
                    "error": error.strip(),
                    "updated_at": self.clock.timestamp(),
                }
            )
            self.state.write_verifier_task(failed)
            return failed


__all__ = ["VerifierWorkflow", "verifier_subject"]
