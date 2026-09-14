"""Existing Annotation file layout; task transitions belong to Application."""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path

from goal_plus.domain.annotation import EvidenceAnnotationQueue, EvidenceAnnotationTask
from goal_plus.domain.evidence import IterationRecord
from goal_plus.domain.search import FrozenSpec
from goal_plus.domain.shared_tools import SharedToolRecord
from goal_plus.infrastructure.state.frozen_spec import load_frozen_spec
from goal_plus.infrastructure.state.files import (
    exclusive_file_lock,
    load_json,
    write_json,
)
from goal_plus.ports.state import CandidateRecord, RunRecord
from goal_plus.ports.annotation import AnnotationCandidateEvidence
from goal_plus.ports.annotation_context import AnnotationCandidateContext
from goal_plus.shared_dir import SharedDirManager


class FileAnnotationState:
    def __init__(self, root_dir: Path | str) -> None:
        self.root_dir = Path(root_dir).resolve()

    def run_dir(self, run_id: str) -> Path:
        return self.root_dir / "runs" / run_id

    def lock_run(self, run_id: str) -> AbstractContextManager[None]:
        return exclusive_file_lock(self.run_dir(run_id) / "run.lock")

    def lock_tasks(self, run_id: str) -> AbstractContextManager[None]:
        return exclusive_file_lock(
            self.run_dir(run_id) / "evidence-annotator" / "tasks.lock"
        )

    def read_run(self, run_id: str) -> RunRecord:
        return RunRecord.model_validate(load_json(self.run_dir(run_id) / "run.json"))

    def read_queue(self, run_id: str) -> EvidenceAnnotationQueue | None:
        path = self.run_dir(run_id) / "evidence-annotator" / "queue.json"
        return (
            EvidenceAnnotationQueue.model_validate(load_json(path))
            if path.exists()
            else None
        )

    def write_queue(self, queue: EvidenceAnnotationQueue) -> None:
        write_json(
            self.run_dir(queue.run_id) / "evidence-annotator" / "queue.json",
            queue.model_dump(mode="json"),
        )

    def task_path(self, run_id: str, candidate_id: str, iteration: int) -> Path:
        return (
            self.run_dir(run_id)
            / "candidates"
            / candidate_id
            / "evidence-annotations"
            / f"iteration-{iteration:04d}.json"
        )

    def read_task(
        self,
        run_id: str,
        candidate_id: str,
        iteration: int,
    ) -> EvidenceAnnotationTask | None:
        path = self.task_path(run_id, candidate_id, iteration)
        return (
            EvidenceAnnotationTask.model_validate(load_json(path))
            if path.exists()
            else None
        )

    def write_task(self, task: EvidenceAnnotationTask) -> None:
        write_json(
            self.task_path(task.run_id, task.candidate_id, task.iteration),
            task.model_dump(mode="json"),
        )

    def list_tasks(self, run_id: str) -> list[EvidenceAnnotationTask]:
        return [
            EvidenceAnnotationTask.model_validate(load_json(path))
            for path in sorted(
                self.run_dir(run_id).glob(
                    "candidates/*/evidence-annotations/iteration-*.json"
                )
            )
        ]

    def read_iterations(self, run_id: str) -> dict[str, list[IterationRecord]]:
        return {
            record.candidate_id: record.iterations
            for record in self.read_candidate_evidence(run_id)
        }

    def read_candidate_evidence(self, run_id: str) -> list[AnnotationCandidateEvidence]:
        records = [
            CandidateRecord.model_validate(load_json(path))
            for path in sorted(self.run_dir(run_id).glob("candidates/*/candidate.json"))
        ]
        return [
            AnnotationCandidateEvidence(
                candidate_id=record.candidate_id,
                workspace_provider=record.task.workspace_provider,
                iterations=record.iterations,
            )
            for record in records
        ]

    def read_frozen_spec(self, frozen_spec_id: str) -> FrozenSpec:
        return load_frozen_spec(
            self.root_dir / "specs" / frozen_spec_id / "frozen_spec.json"
        )

    def read_candidate_iterations(
        self,
        run_id: str,
        candidate_id: str,
    ) -> list[IterationRecord]:
        return CandidateRecord.model_validate(
            load_json(
                self.run_dir(run_id) / "candidates" / candidate_id / "candidate.json"
            )
        ).iterations

    def attempt_monitor_reference(
        self,
        run_id: str,
        candidate_id: str,
        iteration: int,
        attempt: int,
    ) -> str:
        safe_candidate = "".join(
            character if character.isalnum() or character in {"-", "_", "."} else "_"
            for character in candidate_id
        )
        return str(
            (
                self.run_dir(run_id)
                / "evidence-annotator"
                / "attempts"
                / f"{safe_candidate}-iteration-{iteration:04d}-attempt-{attempt:02d}.json"
            ).relative_to(self.root_dir)
        )

    def pending_tool_ids(self, run_id: str) -> set[str]:
        return SharedDirManager(self.run_dir(run_id)).pending_tool_ids()

    def read_context_candidate(
        self,
        run_id: str,
        candidate_id: str,
    ) -> AnnotationCandidateContext:
        record = CandidateRecord.model_validate(
            load_json(
                self.run_dir(run_id) / "candidates" / candidate_id / "candidate.json"
            )
        )
        return self._context_record(record)

    def read_context_candidates(self, run_id: str) -> list[AnnotationCandidateContext]:
        return [
            self._context_record(CandidateRecord.model_validate(load_json(path)))
            for path in sorted(self.run_dir(run_id).glob("candidates/*/candidate.json"))
        ]

    @staticmethod
    def _context_record(record: CandidateRecord) -> AnnotationCandidateContext:
        return AnnotationCandidateContext(
            candidate_id=record.candidate_id,
            workspace_provider=record.task.workspace_provider,
            iterations=record.iterations,
            workspace=record.task.workspace,
            workspace_base_revision=record.task.workspace_base_revision,
            base_artifact_ref=record.task.base_artifact_ref,
        )

    def tool_view_input(
        self,
        run_id: str,
        tool: SharedToolRecord,
        *,
        max_content_bytes: int,
    ) -> tuple[dict, int]:
        return SharedDirManager(self.run_dir(run_id)).tool_view_input(
            tool,
            max_content_bytes=max_content_bytes,
        )

    def advance_heads(self, run_id: str, tools: list[SharedToolRecord]) -> None:
        SharedDirManager(self.run_dir(run_id)).advance_discoverable_heads(tools)

    def reject_heads(self, run_id: str, tools: list[SharedToolRecord]) -> None:
        SharedDirManager(self.run_dir(run_id)).reject_pending_heads(tools)
