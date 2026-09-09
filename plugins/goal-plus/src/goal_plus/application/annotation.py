"""Annotation claims and publication, independent of model execution."""

from __future__ import annotations

import hashlib

from goal_plus.domain.annotation import (
    MAX_ANNOTATION_ATTEMPTS,
    ANNOTATION_RETRY_BACKOFF_SECONDS,
    AnnotationError,
    AnnotationOutputError,
    PermanentAnnotationError,
    EvidenceAnnotationOutput,
    EvidenceAnnotationQueue,
    EvidenceAnnotationTask,
    EvidenceViewRecord,
    ToolViewOutput,
    validate_supplemental_output,
)
from goal_plus.domain.search import RunState
from goal_plus.domain.shared_tools import SharedToolRecord, ToolViewRecord
from goal_plus.domain.time import outer_deadline_epoch
from goal_plus.ports.annotation import (
    AnnotationCompletion,
    AnnotationEvidenceSource,
    AnnotationTaskState,
    AnnotationToolFamilies,
    EvidenceAnnotationResult,
)
from goal_plus.ports.clock import Clock

EVIDENCE_ANNOTATION_RUN_STATES = frozenset(
    {
        RunState.RUNNING,
        RunState.WAITING_FOR_WORKERS,
        RunState.SELECTING,
        RunState.SELECTION_BLOCKED,
    }
)


class AnnotationWorkflow:
    def __init__(
        self,
        *,
        tasks: AnnotationTaskState,
        evidence: AnnotationEvidenceSource,
        tools: AnnotationToolFamilies,
        clock: Clock,
    ) -> None:
        self.tasks = tasks
        self.evidence = evidence
        self.tools = tools
        self.clock = clock

    def task(
        self,
        run_id: str,
        candidate_id: str,
        iteration: int,
    ) -> EvidenceAnnotationTask | None:
        return self.tasks.read_task(run_id, candidate_id, iteration)

    def bound_task(
        self,
        *,
        run_id: str,
        candidate_id: str,
        iteration: int,
        attempts: int,
        queue_generation: int,
        queue_revision: int,
    ) -> EvidenceAnnotationTask:
        task = self.task(run_id, candidate_id, iteration)
        if (
            task is None
            or task.attempts != attempts
            or task.queue_generation != queue_generation
            or task.queue_revision != queue_revision
        ):
            raise PermissionError("ThinkThread Annotation Child task binding is stale")
        return task

    def pending(self, run_id: str) -> list[tuple[str, int]]:
        settled = sorted(
            (iteration.created_at, candidate_id, iteration.iteration, iteration)
            for candidate_id, iterations in self.evidence.read_iterations(
                run_id
            ).items()
            for iteration in iterations
        )
        pending = []
        for _, candidate_id, number, iteration in settled:
            task = self.tasks.read_task(run_id, candidate_id, number)
            if iteration.agent_session_id is None and task is None:
                continue
            view = task.view if task is not None and task.state == "completed" else None
            if view is not None and (
                view.run_id != run_id
                or view.candidate_id != candidate_id
                or view.iteration != number
                or view.attempt_commit != iteration.git_head
                or (
                    view.attempt_artifact_ref is not None
                    and view.attempt_artifact_ref != iteration.artifact_ref
                )
                or (view.attempt_commit is None and view.attempt_artifact_ref is None)
            ):
                raise RuntimeError("evidence view does not match iteration")
            if view is None:
                pending.append((candidate_id, number))
        return pending

    def list_tasks(self, run_id: str) -> list[EvidenceAnnotationTask]:
        return self.tasks.list_tasks(run_id)

    def queue(
        self,
        run_id: str,
    ) -> EvidenceAnnotationQueue:
        queue = self.tasks.read_queue(run_id)
        if queue is not None:
            return queue
        run = self.evidence.read_run(run_id)
        active = (
            run.invalidated_at is None and run.state in EVIDENCE_ANNOTATION_RUN_STATES
        )
        return EvidenceAnnotationQueue(
            run_id=run_id,
            state="open" if active else "closed",
            created_at=run.created_at,
            updated_at=run.created_at,
            closed_at=None if active else self.clock.timestamp(),
            closed_reason=None if active else f"run_state:{run.state}",
        )

    def is_active(self, run_id: str) -> bool:
        run = self.evidence.read_run(run_id)
        queue = self.queue(run_id)
        return (
            run.invalidated_at is None
            and run.state in EVIDENCE_ANNOTATION_RUN_STATES
            and queue.state == "open"
        )

    def close_queue(
        self,
        run_id: str,
        *,
        reason: str,
    ) -> EvidenceAnnotationQueue:
        """Close admission and invalidate unpublished annotation attempts.

        The caller owns the run transaction. The generation bump is the
        publication fence: an inference result from an older generation can no
        longer become a View even if its host process exits after Search ends.
        """
        with self.tasks.lock_tasks(run_id):
            queue = self.queue(run_id)
            if queue.state == "closed":
                return queue
            now = self.clock.timestamp()
            closed = queue.model_copy(
                update={
                    "state": "closed",
                    "generation": queue.generation + 1,
                    "revision": queue.revision + 1,
                    "updated_at": now,
                    "closed_at": now,
                    "closed_reason": reason,
                }
            )
            # Write the fence first. A concurrently finishing host result then
            # observes closed even before individual task files are cancelled.
            self.tasks.write_queue(closed)
            for task in self.tasks.list_tasks(run_id):
                if task.state not in {"pending", "retry_wait"}:
                    continue
                self.tasks.write_task(
                    task.model_copy(
                        update={
                            "state": "cancelled",
                            "next_attempt_at": None,
                            "last_error": f"annotation queue closed: {reason}",
                            "updated_at": now,
                        }
                    )
                )
            return closed

    def claim(
        self,
        run_id: str,
        candidate_id: str,
        iteration: int,
    ) -> EvidenceAnnotationTask | None:
        with self.tasks.lock_tasks(run_id):
            if not self.is_active(run_id):
                return None
            task = self.tasks.read_task(run_id, candidate_id, iteration)
            if task is None or task.state not in {"pending", "retry_wait"}:
                return None
            queue = self.queue(run_id)
            if (
                queue.state != "open"
                or task.queue_generation != queue.generation
                or task.queue_revision > queue.revision
            ):
                return None
            if task.attempts >= MAX_ANNOTATION_ATTEMPTS:
                error = "annotation attempt limit reached"
                self.tasks.write_task(
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
            now_epoch = self.clock.now()
            deadline = outer_deadline_epoch(task.outer_deadline_at)
            if deadline is not None and deadline <= now_epoch:
                error = "annotation outer deadline expired"
                task = task.model_copy(
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
                self.tasks.write_task(task)
                return None
            retry_at = outer_deadline_epoch(task.next_attempt_at)
            if retry_at is not None and retry_at > now_epoch:
                return None
            attempt_number = task.attempts + 1
            backoff = ANNOTATION_RETRY_BACKOFF_SECONDS[
                min(attempt_number - 1, len(ANNOTATION_RETRY_BACKOFF_SECONDS) - 1)
            ]
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
                    "state": "retry_wait",
                    "attempts": attempt_number,
                    "next_attempt_at": self.clock.timestamp_from_epoch(
                        now_epoch + backoff
                    ),
                    "attempt_history": history,
                    "updated_at": self.clock.timestamp(),
                }
            )
            self.tasks.write_task(claimed)
            return claimed

    def _bind_tool_views(
        self,
        task: EvidenceAnnotationTask,
        outputs: list[ToolViewOutput],
    ) -> list[ToolViewRecord]:
        iterations = self.evidence.read_candidate_iterations(
            task.run_id, task.candidate_id
        )
        iteration = next(
            (item for item in iterations if item.iteration == task.iteration),
            None,
        )
        if iteration is None:
            raise AnnotationOutputError("annotation iteration no longer exists")
        pending_tool_ids = self.tools.pending_tool_ids(task.run_id)
        expected = {
            tool.tool_id: tool
            for tool in iteration.shared_tools
            if tool.tool_id in pending_tool_ids
        }
        returned = {item.tool_id: item for item in outputs}
        iteration_tool_ids = {tool.tool_id for tool in iteration.shared_tools}
        if not set(returned).issubset(iteration_tool_ids):
            raise AnnotationOutputError(
                "Tool View identities do not match published tools"
            )
        returned = {
            tool_id: output
            for tool_id, output in returned.items()
            if tool_id in expected
        }
        if set(returned) != set(expected):
            raise AnnotationOutputError(
                "Tool View identities do not match current family heads"
            )
        return [
            ToolViewRecord(
                tool_id=tool.tool_id,
                snapshot_hash=tool.snapshot_hash,
                source_commit=tool.source_commit or task.attempt_commit,
                summary=returned[tool.tool_id].summary,
                capabilities=returned[tool.tool_id].capabilities,
                when_to_use=returned[tool.tool_id].when_to_use,
                entrypoint=tool.entrypoint or returned[tool.tool_id].entrypoint,
                inputs=returned[tool.tool_id].inputs,
                outputs=returned[tool.tool_id].outputs,
                dependencies=returned[tool.tool_id].dependencies,
                adoption_steps=returned[tool.tool_id].adoption_steps,
                limitations=returned[tool.tool_id].limitations,
                evidence_scope="来自通过 process verifier 的 iteration，但不代表工具已被独立验证。",
            )
            for tool in expected.values()
        ]

    def complete(
        self,
        task: EvidenceAnnotationTask,
        *,
        result: EvidenceAnnotationResult | None = None,
        error: Exception | None = None,
    ) -> AnnotationCompletion:
        if result is not None:
            output = EvidenceAnnotationOutput(
                description=result.description,
                supplemental_evaluation=result.supplemental_evaluation,
                tool_views=result.tool_views,
            )
            try:
                validate_supplemental_output(
                    output,
                    enabled=task.supplemental_evaluation_enabled,
                    comparison_basis=[
                        item.model_dump(mode="json") for item in task.comparison_basis
                    ],
                )
                if list(result.comparison_basis or []) != list(task.comparison_basis):
                    raise AnnotationOutputError(
                        "annotation result comparison basis does not match its immutable task"
                    )
            except AnnotationOutputError as exc:
                exc.usage = dict(result.usage)
                raise
        transaction = self.tasks.lock_run(task.run_id)
        with transaction, self.tasks.lock_tasks(task.run_id):
            current = self.tasks.read_task(
                task.run_id, task.candidate_id, task.iteration
            )
            queue = self.queue(task.run_id)
            if (
                current is None
                or current.attempts != task.attempts
                or current.state not in {"pending", "retry_wait"}
                or queue.state != "open"
                or current.queue_generation != queue.generation
                or current.queue_revision > queue.revision
            ):
                return AnnotationCompletion(accepted=False, published=False)
            if result is not None:
                deadline = outer_deadline_epoch(current.outer_deadline_at)
                if not self.is_active(task.run_id):
                    error = PermanentAnnotationError(
                        "annotation run closed before View publication",
                        usage=result.usage,
                    )
                    result = None
                elif deadline is not None and deadline <= self.clock.now():
                    error = PermanentAnnotationError(
                        "annotation outer deadline expired before publication",
                        usage=result.usage,
                    )
                    result = None
            history = list(current.attempt_history)
            if history:
                latest = dict(history[-1])
                latest["finished_at"] = self.clock.timestamp()
                if result is not None:
                    latest["usage"] = dict(result.usage)
                if error is not None:
                    latest["error"] = f"{type(error).__name__}: {error}"[:2000]
                    error_usage = getattr(error, "usage", {})
                    if error_usage:
                        latest["usage"] = dict(error_usage)
                history[-1] = latest

            usage = dict(current.usage)
            observed_usage: dict[str, int | float] = {}
            if result is not None:
                observed_usage = result.usage
            elif error is not None:
                observed_usage = getattr(error, "usage", {})
            for key, value in observed_usage.items():
                usage[key] = usage.get(key, 0) + value
            if result is not None:
                try:
                    tool_views = self._bind_tool_views(current, result.tool_views)
                except AnnotationError as exc:
                    exc.usage.update(result.usage)
                    raise
                update = {
                    "state": "completed",
                    "next_attempt_at": None,
                    "last_error": None,
                    "error_fingerprint": None,
                    "view": EvidenceViewRecord(
                        run_id=current.run_id,
                        candidate_id=current.candidate_id,
                        iteration=current.iteration,
                        attempt_commit=current.attempt_commit,
                        attempt_artifact_ref=current.attempt_artifact_ref,
                        description=result.description,
                        supplemental_evaluation=result.supplemental_evaluation,
                        comparison_basis=list(current.comparison_basis),
                        tool_views=tool_views,
                        created_at=self.clock.timestamp(),
                    ),
                }
            else:
                assert error is not None
                error_text = f"{type(error).__name__}: {error}"[:2000]
                terminal = (
                    isinstance(error, PermanentAnnotationError)
                    or current.attempts >= MAX_ANNOTATION_ATTEMPTS
                    or not self.is_active(task.run_id)
                )
                update = {
                    "state": "terminal_error" if terminal else "retry_wait",
                    "next_attempt_at": None if terminal else current.next_attempt_at,
                    "last_error": error_text,
                    "error_fingerprint": hashlib.sha256(
                        error_text.encode("utf-8")
                    ).hexdigest(),
                }
            self.tasks.write_task(
                current.model_copy(
                    update={
                        **update,
                        "attempt_history": history,
                        "usage": usage,
                        "updated_at": self.clock.timestamp(),
                    }
                )
            )
            # Persist the exact Tool View before exposing its immutable snapshot.
            # All runtime readers take the run lock, so they see either the old head
            # or the completed View and its new head together.
            self.reconcile_tool_heads(current.run_id)
            published = result is not None
        return AnnotationCompletion(accepted=True, published=published)

    def retry_delay(
        self,
        run_id: str,
    ) -> float | None:
        """Return the next retry delay and settle tasks that can no longer run."""
        if not self.is_active(run_id):
            return None
        now_epoch = self.clock.now()
        delays: list[float] = []
        for candidate_id, iteration in self.pending(run_id):
            task = self.tasks.read_task(run_id, candidate_id, iteration)
            if task is None or task.state not in {"pending", "retry_wait"}:
                continue
            deadline = outer_deadline_epoch(task.outer_deadline_at)
            if task.attempts >= MAX_ANNOTATION_ATTEMPTS or (
                deadline is not None and deadline <= now_epoch
            ):
                self.claim(run_id, candidate_id, iteration)
                continue
            retry_at = outer_deadline_epoch(task.next_attempt_at)
            delays.append(max(0.0, (retry_at or now_epoch) - now_epoch))
        return min(delays) if delays else None

    def reconcile_tool_heads(self, run_id: str) -> None:
        """Repair publication from durable Views while the caller holds the run lock."""
        pending_ids = self.tools.pending_tool_ids(run_id)
        if not pending_ids:
            return
        tools = {
            tool.tool_id: tool
            for iterations in self.evidence.read_iterations(run_id).values()
            for iteration in iterations
            for tool in iteration.shared_tools
            if tool.tool_id in pending_ids
        }
        completed: list[SharedToolRecord] = []
        rejected: list[SharedToolRecord] = []
        for tool in tools.values():
            task = self.tasks.read_task(run_id, tool.candidate_id, tool.iteration)
            if task is None:
                continue
            if (
                task.state == "completed"
                and task.view is not None
                and any(
                    view.tool_id == tool.tool_id
                    and view.snapshot_hash == tool.snapshot_hash
                    for view in task.view.tool_views
                )
            ):
                completed.append(tool)
            elif task.state == "terminal_error":
                rejected.append(tool)
        if completed:
            self.tools.advance_heads(run_id, completed)
        if rejected:
            self.tools.reject_heads(run_id, rejected)
