"""Verifier-owned FIFO, bounded retries, immutable terminal output and delivery."""

from typing import Any
from goal_plus.domain.serialization import canonical_json, sha256_text
from goal_plus.domain.time import timestamp_epoch
from goal_plus.domain.verifier import (
    VerifierInput, VerifierInvocationRecord, VerifierLease, VerifierRecord,
    VerifierResult, VerifierScoreContract, VerifierSubject, VerifierTask,
    VerifierTaskState, validate_scores, validate_diagnostics, diagnostic_references,
    validate_knowledge_updates, verifier_knowledge_contract,
)

def _task_knowledge_contract(task: VerifierTask):
    return verifier_knowledge_contract(task.input.knowledge_snapshot)
from goal_plus.ports.clock import Clock
from goal_plus.ports.verifier import VerifierState


class VerifierWorkflow:
    def __init__(self, state: VerifierState, clock: Clock) -> None:
        self.state, self.clock = state, clock

    def ensure_assessment(self, subject: VerifierSubject,
                          score_contract: VerifierScoreContract,
                          submission: VerifierInput) -> VerifierTask:
        return self.ensure_assessments(
            [(subject, score_contract, submission)]
        )[0]

    def ensure_assessments(
        self,
        submissions: list[
            tuple[VerifierSubject, VerifierScoreContract, VerifierInput]
        ],
    ) -> list[VerifierTask]:
        """Append a committed, already ordered submission batch atomically."""

        if not submissions:
            return []
        run_id = submissions[0][0].run_id
        if any(subject.run_id != run_id for subject, _contract, _input in submissions):
            raise ValueError("verifier assessment batch must belong to one run")
        with self.state.lock_run(run_id):
            tasks = self.list_tasks(run_id)
            by_id = {task.task_id: task for task in tasks}
            sequence = max((task.queue_sequence for task in tasks), default=0)
            ensured: list[VerifierTask] = []
            for subject, score_contract, submission in submissions:
                identity = {
                    "run_id": subject.run_id,
                    "settlement_id": subject.settlement_id,
                    "score_contract_hash": score_contract.contract_hash,
                }
                task_id = f"verifier_{sha256_text(canonical_json(identity))[:24]}"
                existing = by_id.get(task_id)
                if existing is not None:
                    if (
                        existing.subject,
                        existing.score_contract,
                        existing.input,
                    ) != (subject, score_contract, submission):
                        raise RuntimeError("verifier task identity collision")
                    ensured.append(existing)
                    continue
                sequence += 1
                now = self.clock.timestamp()
                task = VerifierTask(
                    task_id=task_id,
                    subject=subject,
                    score_contract=score_contract,
                    input=submission,
                    queue_sequence=sequence,
                    created_at=now,
                    updated_at=now,
                )
                self.state.write_verifier_task(task)
                by_id[task_id] = task
                ensured.append(task)
            return ensured

    def get_task(self, task_id: str) -> VerifierTask:
        return self.state.load_verifier_task(task_id)

    def list_tasks(self, run_id: str | None = None, *, candidate_id: str | None = None,
                   state: VerifierTaskState | None = None) -> list[VerifierTask]:
        return self.state.load_verifier_tasks(run_id, candidate_id=candidate_id, state=state)

    def list_records(self, run_id: str, *, candidate_id: str | None = None,
                     iteration: int | None = None, settlement_id: str | None = None) -> list[VerifierRecord]:
        return self.state.load_verifier_records(run_id, candidate_id=candidate_id,
                                               iteration=iteration, settlement_id=settlement_id)

    def head(self, run_id: str) -> VerifierTask | None:
        return next((t for t in self.list_tasks(run_id) if not t.delivered), None)

    def claim_task(self, worker_id: str, lease_seconds: int, *, run_id: str) -> VerifierTask | None:
        if not worker_id.strip() or lease_seconds <= 0:
            raise ValueError("verifier claim requires worker identity and positive lease")
        with self.state.lock_run(run_id):
            task = self.head(run_id)
            if task is None or task.state in {"completed", "abandoned"}:
                return None
            now = self.clock.now()
            due = task.lease.expires_at if task.lease else task.next_attempt_at
            if due is not None and ((epoch := timestamp_epoch(due)) is None or epoch > now):
                return None
            if task.attempts >= task.max_attempts:
                self._abandon(task, "verifier attempt limit exhausted after interrupted inference")
                return None
            task = task.model_copy(update={
                "state": "running", "attempts": task.attempts + 1,
                "next_attempt_at": None, "error": None,
                "lease": VerifierLease(worker_id=worker_id, expires_at=self.clock.timestamp_from_epoch(now + lease_seconds)),
                "updated_at": self.clock.timestamp(),
            })
            self.state.write_verifier_task(task)
            return task

    @staticmethod
    def _require_current_claim(task: VerifierTask, *, worker_id: str, attempt: int) -> None:
        if task.state != "running" or task.lease is None:
            raise RuntimeError("only a running verifier task can be settled")
        if task.lease.worker_id != worker_id or task.attempts != attempt:
            raise RuntimeError("verifier task claim is no longer current")

    def _output(self, task: VerifierTask) -> VerifierRecord | None:
        return next((r for r in self.list_records(task.run_id, settlement_id=task.subject.settlement_id)
                     if r.task_id == task.task_id), None)

    def _terminal(self, task: VerifierTask, record: VerifierRecord) -> VerifierTask:
        if record.subject != task.subject or record.score_contract_hash != task.score_contract.contract_hash:
            raise ValueError("verifier output identity mismatch")
        self.state.write_verifier_record(record)
        task = task.model_copy(update={
            "state": "completed" if record.outcome == "completed" else "abandoned",
            "lease": None, "next_attempt_at": None, "output_record_id": record.record_id,
            "usage": record.usage, "invocation": record.invocation, "error": record.error,
            "updated_at": self.clock.timestamp(),
        })
        self.state.write_verifier_task(task)
        return task

    def _record_fields(self, task: VerifierTask) -> dict[str, Any]:
        return dict(record_id=f"record_{sha256_text(task.task_id)[:24]}", task_id=task.task_id,
                    subject=task.subject, queue_sequence=task.queue_sequence,
                    score_contract_hash=task.score_contract.contract_hash,
                    initial_artifact_ref=task.input.initial_artifact_ref, parent_node_id=task.input.parent_node_id,
                    allocation_depth=task.input.allocation_depth, replaces_candidate_id=task.input.replaces_candidate_id,
                    created_at=self.clock.timestamp())

    def complete_task(self, task_id: str, result: VerifierResult | dict[str, Any],
                      invocation: VerifierInvocationRecord | dict[str, Any],
                      usage: dict[str, int | float] | None = None, *, worker_id: str, attempt: int) -> VerifierRecord:
        task = self.get_task(task_id)
        with self.state.lock_run(task.run_id):
            task = self.get_task(task_id)
            existing = self._output(task)
            if existing is not None:
                self._terminal(task, existing)
                return existing
            self._require_current_claim(task, worker_id=worker_id, attempt=attempt)
            parsed = VerifierResult.model_validate(result)
            validate_scores(parsed.scores, task.score_contract.score_rules)
            validate_diagnostics(parsed.steering_findings, parsed.scores, task.score_contract,
                                 diagnostic_references(task.input, task.subject))
            validate_knowledge_updates(parsed.knowledge_updates, _task_knowledge_contract(task))
            record = VerifierRecord(**self._record_fields(task), **parsed.model_dump(),
                                    invocation=VerifierInvocationRecord.model_validate(invocation), usage=dict(usage or {}))
            self._terminal(task, record)
            return record

    def _abandon(self, task: VerifierTask, error: str) -> VerifierTask:
        existing = self._output(task)
        record = existing or VerifierRecord(**self._record_fields(task), outcome="failed", error=error)
        return self._terminal(task, record)

    def fail_task(self, task_id: str, error: str, retryable: bool, *, worker_id: str,
                  attempt: int, retry_delay_seconds: int = 30) -> VerifierTask:
        if not error.strip():
            raise ValueError("verifier task error must be non-empty")
        task = self.get_task(task_id)
        with self.state.lock_run(task.run_id):
            task = self.get_task(task_id)
            if task.state in {"completed", "abandoned"}:
                return task
            self._require_current_claim(task, worker_id=worker_id, attempt=attempt)
            if (output := self._output(task)) is not None:
                return self._terminal(task, output)
            if not retryable or task.attempts >= task.max_attempts:
                return self._abandon(task, error)
            task = task.model_copy(update={"state": "retry_wait", "lease": None, "error": error,
                "next_attempt_at": self.clock.timestamp_from_epoch(self.clock.now() + retry_delay_seconds),
                "updated_at": self.clock.timestamp()})
            self.state.write_verifier_task(task)
            return task

    def terminal_output(self, task: VerifierTask) -> VerifierRecord | None:
        # Recover an output committed immediately before a consumer crash.
        with self.state.lock_run(task.run_id):
            task = self.get_task(task.task_id)
            output = self._output(task)
            if output is not None and task.output_record_id is None:
                self._terminal(task, output)
            return output

    def acknowledge_delivery(self, task_id: str) -> None:
        task = self.get_task(task_id)
        with self.state.lock_run(task.run_id):
            task = self.get_task(task_id)
            if task.state not in {"completed", "abandoned"}:
                raise RuntimeError("cannot acknowledge a nonterminal verifier task")
            self.state.write_verifier_task(task.model_copy(update={"delivered": True}))
