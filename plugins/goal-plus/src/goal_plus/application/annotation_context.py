"""Build exact Annotation inputs without stores, providers or model execution."""

from pathlib import Path
from typing import Any

from goal_plus.application.annotation import AnnotationWorkflow
from goal_plus.application.artifact_availability import artifact_availability
from goal_plus.application.search_context import SearchContext
from goal_plus.domain.annotation import (
    EvidenceAnnotationTask,
    EvidenceComparisonReference,
)
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.evidence_policy import (
    iteration_artifact_ref,
    iteration_matches_evidence_reference,
)
from goal_plus.domain.serialization import sha256_text
from goal_plus.ports.annotation import AnnotationAdmissionSource
from goal_plus.ports.annotation_context import (
    MAX_EVIDENCE_ANNOTATION_DIFF_BYTES,
    MAX_EVIDENCE_PEER_DIFF_BYTES,
    TOOL_VIEW_MAX_CONTENT_BYTES,
    AnnotationContext,
    AnnotationContextSource,
)
from goal_plus.ports.artifacts import (
    ArtifactComparison,
    ArtifactReader,
    ArtifactReadError,
)


class AnnotationContextBuilder:
    def __init__(
        self,
        *,
        workflow: AnnotationWorkflow,
        source: AnnotationContextSource,
        admission_source: AnnotationAdmissionSource,
        context: SearchContext,
        artifacts: ArtifactReader,
        root_dir: Path,
    ) -> None:
        self.workflow = workflow
        self.source = source
        self.admission_source = admission_source
        self.context = context
        self.artifacts = artifacts
        self.root_dir = root_dir

    def _target_diff(
        self, comparison: ArtifactComparison, paths: list[str]
    ) -> tuple[str, str | None]:
        try:
            result = self.artifacts.diff(
                comparison,
                paths=paths,
                max_bytes=MAX_EVIDENCE_ANNOTATION_DIFF_BYTES,
                function_context=True,
            )
            if result.truncated:
                return (
                    "",
                    "bounded snapshot diff truncated; inspect the full target Artifact View",
                )
            return result.text, None
        except ArtifactReadError as exc:
            return "", str(exc)[:500]

    def open_views(
        self,
        task: EvidenceAnnotationTask,
    ) -> list[dict[str, Any]]:
        opened: list[dict[str, Any]] = []
        run = self.workflow.evidence.read_run(task.run_id)
        task_root = (
            self.root_dir
            / "runs"
            / task.run_id
            / "evidence-annotator"
            / "artifact-views"
            / f"{task.candidate_id}-iteration-{task.iteration:04d}"
        )
        for reference in task.artifact_views:
            record = self.source.read_context_candidate(
                task.run_id,
                reference.candidate_id,
            )
            evidence = next(
                (
                    item
                    for item in record.iterations
                    if item.iteration == reference.iteration
                    and (
                        item.artifact_ref == reference.artifact_ref
                        or (
                            item.artifact_ref is None
                            and item.git_head == reference.artifact_ref.id
                        )
                    )
                ),
                None,
            )
            if evidence is None:
                raise RuntimeError("annotation Artifact View Evidence is unavailable")
            availability = artifact_availability(
                reference.artifact_ref,
                run.provider_cleanup,
            )
            if availability == "removed":
                raise RuntimeError(
                    "annotation Artifact View artifact was removed after terminal cleanup"
                )
            if availability != "available":
                raise RuntimeError(
                    f"annotation Artifact View artifact availability is {availability}"
                )
            destination = task_root / reference.view_id / "workspace"
            access = self.artifacts.open_view(
                record.workspace,
                reference.artifact_ref,
                destination,
                backend=reference.workspace_backend,
            )
            opened.append(
                {
                    **reference.model_dump(mode="json"),
                    "access": access.model_dump(mode="json"),
                }
            )
        return opened

    def build(
        self,
        run_id: str,
        candidate_id: str,
        iteration_number: int,
    ) -> AnnotationContext:
        run = self.workflow.evidence.read_run(run_id)
        frozen = self.admission_source.read_frozen_spec(run.frozen_spec_id)
        record = self.source.read_context_candidate(run_id, candidate_id)
        iteration = next(
            (
                item
                for item in record.iterations
                if item.iteration == iteration_number
                and (
                    item.agent_session_id is not None
                    or frozen.spec.strategy.orchestration_mode == "adaptive_search"
                )
            ),
            None,
        )
        if (
            iteration is None
            or iteration_artifact_ref(iteration, record.workspace_backend) is None
        ):
            raise RuntimeError("annotation requires immutable worker Evidence")
        task = self.workflow.task(run_id, candidate_id, iteration_number)
        if task is None or task.profile is None:
            raise RuntimeError("annotation requires a runnable immutable task")
        commit = iteration.git_head
        if (
            task.attempt_commit != commit
            or task.attempt_base_commit != iteration.attempt_base_git_head
            or task.attempt_artifact_ref != iteration.artifact_ref
            or task.attempt_base_artifact_ref != iteration.attempt_base_artifact_ref
            or task.attempt_changed_files != iteration.attempt_changed_files
        ):
            raise RuntimeError("annotation task does not match settled Evidence")
        attempt_artifact_ref = iteration_artifact_ref(
            iteration, record.workspace_backend
        )
        assert attempt_artifact_ref is not None
        diff_head = attempt_artifact_ref
        candidate_base_artifact_ref = record.base_artifact_ref
        if record.workspace_backend == "thinkthread":
            attempt_base = task.attempt_base_artifact_ref
            candidate_base = candidate_base_artifact_ref
            if (
                attempt_base is None
                or candidate_base is None
                or attempt_artifact_ref.kind != "fs_snapshot"
            ):
                raise RuntimeError(
                    "ThinkThread annotation snapshot anchors are missing"
                )
            candidate_base_commit = None
        else:
            if commit is None or task.attempt_base_commit is None:
                raise RuntimeError("Git annotation commit anchors are missing")
            attempt_base = ArtifactRef(
                kind="git_commit",
                id=task.attempt_base_commit,
                provider=record.workspace_backend,
            )
            head = ArtifactRef(
                kind="git_commit", id=commit, provider=record.workspace_backend
            )
            if not self.artifacts.commit_available(
                record.workspace, task.attempt_base_commit
            ) or not self.artifacts.commit_available(record.workspace, commit):
                raise RuntimeError("annotation Evidence commit is unavailable")
            diff_head = head
            candidate_base_commit = (
                record.workspace_base_revision or task.attempt_base_commit
            )
            candidate_base = ArtifactRef(
                kind="git_commit",
                id=candidate_base_commit,
                provider=record.workspace_backend,
            )
        source_path = str(run.source_provider_handle.get("source_relative_path") or ".")
        attempt = ArtifactComparison(
            record.workspace, attempt_base, diff_head, source_path
        )
        candidate = ArtifactComparison(
            record.workspace, candidate_base, diff_head, source_path
        )
        diff, actual_diff_omitted = self._target_diff(
            attempt, task.attempt_changed_files
        )
        # Unlike peer reads, a missing target baseline/index makes this input unusable.
        candidate_changed_files = self.artifacts.changed_files(candidate)
        candidate_diff, candidate_diff_omitted = self._target_diff(
            candidate, candidate_changed_files
        )
        peer_evidence = self.comparison_peers(
            run_id,
            comparison_basis=task.comparison_basis,
        )
        task_context = frozen.spec.objective
        task_context_source = "frozen_objective"
        if task.task_context_source is not None:
            resolved = self.context.task_context(run_id, fallback=frozen.spec.objective)
            resolved_context, resolved_source, resolved_ref = (
                resolved.text,
                resolved.source,
                resolved.reference,
            )
            if (
                resolved_source != task.task_context_source
                or resolved_ref != task.task_context_ref
                or sha256_text(resolved_context) != task.task_context_sha256
            ):
                raise RuntimeError(
                    "annotation task context no longer matches its snapshot"
                )
            task_context = resolved_context
            task_context_source = resolved_source
        published_tools = []
        remaining_tool_bytes = TOOL_VIEW_MAX_CONTENT_BYTES
        pending_tool_ids = self.source.pending_tool_ids(run_id)
        pending_tools = [
            tool for tool in iteration.shared_tools if tool.tool_id in pending_tool_ids
        ]
        if pending_tools:
            prior = next(
                (
                    item
                    for item in reversed(record.iterations)
                    if item.iteration < iteration.iteration and item.score is not None
                ),
                None,
            )
            for tool in pending_tools:
                tool_input, used = self.source.tool_view_input(
                    run_id, tool, max_content_bytes=remaining_tool_bytes
                )
                tool_input["goal_evidence"] = {
                    "score": iteration.score,
                    "baseline_score": prior.score if prior is not None else None,
                    "goal_delta": (
                        iteration.score - prior.score
                        if iteration.score is not None and prior is not None
                        else None
                    ),
                    "goal_effect": (
                        "unknown"
                        if prior is None
                        else "improved"
                        if iteration.disposition == "keep"
                        else "unchanged"
                        if iteration.disposition == "retain"
                        else "degraded"
                        if iteration.disposition == "discard"
                        else "failed"
                    ),
                    "disposition": iteration.disposition,
                }
                published_tools.append(tool_input)
                remaining_tool_bytes = max(0, remaining_tool_bytes - used)
        return {
            "run_id": run_id,
            "candidate_id": candidate_id,
            "iteration": iteration.iteration,
            "agent_summary": iteration.hypothesis,
            "exact_attempt_commit": commit,
            "exact_attempt_artifact_ref": attempt_artifact_ref.model_dump(mode="json"),
            "changed_files": list(task.attempt_changed_files),
            "actual_diff": diff,
            "actual_diff_omitted": actual_diff_omitted,
            "candidate_base_commit": candidate_base_commit,
            "candidate_base_artifact_ref": (
                candidate_base_artifact_ref.model_dump(mode="json")
                if candidate_base_artifact_ref is not None
                else None
            ),
            "candidate_changed_files": candidate_changed_files,
            "candidate_diff": candidate_diff,
            "candidate_diff_omitted": candidate_diff_omitted,
            "diff_context_policy": (
                "provider-native bounded diff index; Git uses function context with "
                "at least 10 unchanged lines around hunks; "
                "output remains byte-bounded and may be omitted; immutable Artifact "
                "Views are the full-workspace source of truth"
            ),
            "verifier_result": {
                "score": iteration.score,
                "process_passed": iteration.process_passed,
                "disposition": iteration.disposition,
                "failure_class": iteration.failure_class,
            },
            "relevant_metrics": iteration.metrics,
            "verifier_contract": [
                {
                    "name": command.name,
                    "role": str(command.role),
                    "command": list(command.command),
                    "cwd": command.cwd,
                    "timeout_seconds": command.timeout_seconds,
                }
                for command in frozen.spec.process_verifiers
            ],
            "objective": frozen.spec.objective,
            "task_context": task_context,
            "task_context_source": task_context_source,
            "supplemental_evaluation_enabled": (task.supplemental_evaluation_enabled),
            "peer_evidence": peer_evidence,
            "artifact_views": self.open_views(task),
            "comparison_basis": [
                item.model_dump(mode="json") for item in task.comparison_basis
            ],
            "published_tools": published_tools,
            "tool_adoptions": [
                {
                    **item.model_dump(mode="json"),
                    "disposition": iteration.disposition,
                    "confounded": iteration.adoption_confounded,
                }
                for item in iteration.adopted_tools
            ],
            "annotator": task.profile.model_dump(mode="json"),
            "outer_deadline_at": task.outer_deadline_at,
            "runtime_root": str(self.root_dir),
        }

    def comparison_peers(
        self,
        run_id: str,
        *,
        comparison_basis: list[EvidenceComparisonReference],
    ) -> list[dict[str, Any]]:
        peers: list[dict[str, Any]] = []
        records = {
            record.candidate_id: record
            for record in self.source.read_context_candidates(run_id)
        }
        for reference in comparison_basis:
            record = records.get(reference.candidate_id)
            if record is None:
                raise RuntimeError("annotation comparison candidate is unavailable")
            iteration = next(
                (
                    item
                    for item in record.iterations
                    if iteration_matches_evidence_reference(
                        item, reference, record.workspace_backend
                    )
                ),
                None,
            )
            if iteration is None:
                raise RuntimeError("annotation comparison Evidence is unavailable")
            changed_files: list[str] = []
            peer_diff: str | None = None
            diff_omitted: str | None = None
            artifact_ref = iteration_artifact_ref(iteration, record.workspace_backend)
            if artifact_ref is None:
                raise RuntimeError("annotation comparison artifact is unavailable")
            diff_head = artifact_ref
            if record.workspace_backend == "thinkthread":
                base = record.base_artifact_ref
                if base is None:
                    raise RuntimeError(
                        "annotation comparison base snapshot is unavailable"
                    )
            else:
                if iteration.git_head is None:
                    raise RuntimeError("annotation comparison commit is unavailable")
                diff_head = ArtifactRef(
                    kind="git_commit",
                    id=iteration.git_head,
                    provider=record.workspace_backend,
                )
                base_commit = (
                    record.workspace_base_revision or iteration.attempt_base_git_head
                )
                base = (
                    ArtifactRef(
                        kind="git_commit",
                        id=base_commit,
                        provider=record.workspace_backend,
                    )
                    if base_commit
                    else None
                )
            if base is not None:
                run = self.workflow.evidence.read_run(run_id)
                comparison = ArtifactComparison(
                    record.workspace,
                    base,
                    diff_head,
                    str(run.source_provider_handle.get("source_relative_path") or "."),
                )
                try:
                    changed_files = self.artifacts.changed_files(comparison)
                    if changed_files:
                        result = self.artifacts.diff(
                            comparison,
                            paths=changed_files,
                            max_bytes=MAX_EVIDENCE_PEER_DIFF_BYTES,
                            function_context=False,
                        )
                        peer_diff = result.text
                        if result.truncated:
                            diff_omitted = "bounded snapshot diff truncated; inspect the full peer Artifact View"
                            peer_diff = None
                except ArtifactReadError as exc:
                    diff_omitted = str(exc)[:500]
                    peer_diff = None
                except ValueError as exc:
                    diff_omitted = f"{type(exc).__name__}: {exc}"[:500]
                    peer_diff = None
            peers.append(
                {
                    "candidate_id": record.candidate_id,
                    "iteration": iteration.iteration,
                    "commit": iteration.git_head,
                    "artifact_ref": artifact_ref.model_dump(mode="json"),
                    "score": iteration.score,
                    "process_passed": iteration.process_passed,
                    "disposition": iteration.disposition,
                    "agent_summary": iteration.hypothesis,
                    "changed_files": changed_files,
                    "candidate_diff": peer_diff,
                    "diff_omitted": diff_omitted,
                }
            )
        return peers
