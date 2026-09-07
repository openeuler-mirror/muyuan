from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from goal_plus import host_services
from goal_plus import internal_agents
from goal_plus.annotation_host import AnnotationHostService
from goal_plus.application.annotation import AnnotationWorkflow
from goal_plus.application.annotation_admission import AnnotationAdmission
from goal_plus.ports.annotation import AnnotationCompletion
from goal_plus.models import (
    ArtifactRef,
    EvidenceAnnotationQueue,
    EvidenceAnnotationTask,
)


def _annotation_task(*, revision: int = 2) -> EvidenceAnnotationTask:
    return EvidenceAnnotationTask(
        task_id="annotation_settlement-test",
        settlement_id="settlement-test",
        run_id="run_test",
        candidate_id="c001",
        iteration=1,
        attempt_base_artifact_ref=ArtifactRef(
            kind="fs_snapshot",
            id="fsnap-base",
            provider="thinkthread",
        ),
        attempt_artifact_ref=ArtifactRef(
            kind="fs_snapshot",
            id="fsnap-candidate",
            provider="thinkthread",
        ),
        queue_generation=1,
        queue_revision=revision,
        attempts=1,
        created_at="2026-09-03T00:00:00Z",
        updated_at="2026-09-03T00:00:00Z",
    )


def _annotation_queue(*, revision: int) -> EvidenceAnnotationQueue:
    return EvidenceAnnotationQueue(
        run_id="run_test",
        generation=1,
        revision=revision,
        created_at="2026-09-03T00:00:00Z",
        updated_at="2026-09-03T00:00:00Z",
    )


def test_annotation_claim_reloads_queue_and_preserves_task_binding(monkeypatch) -> None:
    task = _annotation_task()
    finished: list[tuple[EvidenceAnnotationTask, Exception | None]] = []
    wakeups: list[str] = []
    monkeypatch.setattr(
        internal_agents, "wake_scheduler_after_annotation",
        lambda root, run_id: wakeups.append(run_id),
    )

    class FakeWorkflow(AnnotationWorkflow):
        def __init__(self) -> None:
            self.queue_loads = 0
            self.tasks = self.evidence = self.clock = None

        def queue(
            self,
            run_id: str,
        ) -> EvidenceAnnotationQueue:
            assert run_id == "run_test"
            self.queue_loads += 1
            return _annotation_queue(revision=min(self.queue_loads, 2))

        def eligible(
            self,
            run_id: str,
        ) -> list[tuple[str, int]]:
            assert run_id == "run_test"
            return [("c001", 1)]

        def task(
            self,
            run_id: str,
            candidate_id: str,
            iteration: int,
        ) -> EvidenceAnnotationTask:
            assert (run_id, candidate_id, iteration) == ("run_test", "c001", 1)
            return task

        def claim(self, run_id: str, candidate_id: str, iteration: int) -> EvidenceAnnotationTask:
            return self.task(run_id, candidate_id, iteration)

        def complete(self, claimed, *, result=None, error=None) -> AnnotationCompletion:
            finished.append((claimed, error))
            return AnnotationCompletion(accepted=True, published=False)

    workflow = FakeWorkflow()
    admission = AnnotationAdmission(workflow=workflow, source=None, context=None)  # type: ignore[arg-type]
    monkeypatch.setattr(admission, "eligible", workflow.eligible)
    monkeypatch.setattr(
        "goal_plus.annotation_host.build_annotation_services",
        lambda root: (workflow, admission, None, None),
    )
    service = AnnotationHostService(Path(".gp"))

    queue, claimed = service.admission.claim_next("run_test")

    assert queue.revision == 2
    assert claimed == task
    assert service.workflow.bound_task(
        run_id="run_test",
        candidate_id="c001",
        iteration=1,
        attempts=1,
        queue_generation=1,
        queue_revision=2,
    ) == task
    error = RuntimeError("turn ended")
    assert service.finish_annotation_task(task, error=error) is False
    assert finished == [(task, error)]
    assert wakeups == [task.run_id]


@pytest.mark.parametrize("accepted,published", [(False, False), (True, False), (True, True)])
def test_annotation_completion_only_wakes_host_after_accepted_transition(
    accepted: bool, published: bool,
    monkeypatch,
) -> None:
    wakeups = []

    def wake(root, run_id: str) -> None:
        wakeups.append(run_id)
        raise RuntimeError("host launch unavailable")

    monkeypatch.setattr(internal_agents, "wake_scheduler_after_annotation", wake)

    workflow = SimpleNamespace(complete=lambda *_args, **_kwargs: AnnotationCompletion(
            accepted=accepted, published=published,
        ))
    monkeypatch.setattr(
        "goal_plus.annotation_host.build_annotation_services",
        lambda root: (workflow, None, None, None),
    )
    service = AnnotationHostService(Path(".gp"))
    assert service.finish_annotation_task(_annotation_task()) is published
    assert wakeups == (["run_test"] if accepted else [])


def test_thinkthread_host_modules_do_not_call_runtime_private_methods() -> None:
    package = Path(host_services.__file__).parent
    for name in ("thinkthread_pool.py", "thinkthread_annotation.py"):
        source = (package / name).read_text(encoding="utf-8")
        assert "runtime._" not in source
        assert "FileSearchRuntime._" not in source
