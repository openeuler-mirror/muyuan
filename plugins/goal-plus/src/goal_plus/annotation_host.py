"""Annotation host assembly and post-commit callback, without Search Runtime."""

from pathlib import Path

from goal_plus.bootstrap import build_annotation_services
from goal_plus.domain.annotation import EvidenceAnnotationTask
from goal_plus.internal_agents import annotation_completion_published
from goal_plus.ports.annotation import EvidenceAnnotationResult


class AnnotationHostService:
    def __init__(self, root_dir: Path | str) -> None:
        self.root_dir = Path(root_dir).expanduser().resolve()
        self.workflow, self.admission, _, self.context = build_annotation_services(root_dir)

    def finish_annotation_task(
        self,
        task: EvidenceAnnotationTask,
        *,
        result: EvidenceAnnotationResult | None = None,
        error: Exception | None = None,
    ) -> bool:
        completion = self.workflow.complete(task, result=result, error=error)
        return annotation_completion_published(self.root_dir, task.run_id, completion)
