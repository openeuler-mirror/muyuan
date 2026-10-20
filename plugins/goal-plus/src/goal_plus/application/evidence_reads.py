from __future__ import annotations
from typing import Any, Literal
from goal_plus.application.artifact_availability import artifact_availability
from goal_plus.domain.worker_policy import (
    global_evidence_mode,
)
from goal_plus.domain.annotation import (
    EvidenceDetailReadRecord,
    EvidenceViewRecord,
    GlobalEvidenceReadRecord,
    GlobalEvidenceViewReference,
)
from goal_plus.domain.artifacts import (
    ArtifactRef,
    WorkspaceProvider,
)
from goal_plus.domain.evidence import (
    IterationRecord,
    public_metric_projection,
)
from goal_plus.ports.state import (
    ProviderResourceCleanupRecord,
)

from goal_plus.ports.search_inspection import EvidenceReadState, EvidenceAttachments
from goal_plus.ports.clock import Clock
from goal_plus.application.candidate_sessions import CandidateSessions
from goal_plus.application.annotation import AnnotationWorkflow
from goal_plus.application.annotation_admission import AnnotationAdmission
from goal_plus.application.candidate_trace import CandidateTrace
from goal_plus.application.candidate_spec import iteration_spec_id


def global_evidence_entry(
    candidate_id: str,
    iteration: IterationRecord,
    view: EvidenceViewRecord | None,
    discoverable_tool_ids: set[str] | None = None,
    workspace_provider: WorkspaceProvider = "git_worktree",
    provider_cleanup: list[ProviderResourceCleanupRecord] | None = None,
    frozen_spec_id: str | None = None,
    *, spec_hash: str | None = None,
    metric_name: str | None = None,
    metric_direction: str | None = None,
) -> dict[str, Any]:
    tool_views = {
        item.tool_id: item for item in (view.tool_views if view is not None else [])
    }
    artifact_ref = iteration.artifact_ref
    if artifact_ref is None and iteration.git_head is not None:
        artifact_ref = ArtifactRef(
            kind="git_commit",
            id=iteration.git_head,
            provider=workspace_provider,
        )
    availability = artifact_availability(
        artifact_ref,
        provider_cleanup or [],
    )
    public_metrics, metrics_omitted = public_metric_projection(iteration.metrics)
    entry = {
        "candidate_id": candidate_id,
        "iteration": iteration.iteration,
        "commit": iteration.git_head,
        "artifact_ref": (
            artifact_ref.model_dump(mode="json") if artifact_ref is not None else None
        ),
        "reference_available": availability == "available" and artifact_ref is not None and artifact_ref.provider != "thinkthread",
        "artifact_availability": availability,
        "annotation_result_available": view is not None,
        "score": iteration.score,
        "disposition": iteration.disposition,
        "view": view.description if view is not None else None,
        "view_created_at": view.created_at if view is not None else None,
        "metrics": public_metrics,
        "shared_tools": [
            {
                **tool.model_dump(mode="json", exclude={"read_only_path"}),
                "tool_view": tool_views[tool.tool_id].model_dump(mode="json"),
            }
            for tool in iteration.shared_tools
            if tool.tool_id in tool_views
            and (discoverable_tool_ids is None or tool.tool_id in discoverable_tool_ids)
        ],
    }
    if frozen_spec_id is not None:
        entry["frozen_spec_id"] = frozen_spec_id
    if spec_hash is not None:
        entry["spec_hash"] = spec_hash
    if metric_name is not None:
        entry["metric_name"] = metric_name
    if metric_direction is not None:
        entry["metric_direction"] = metric_direction
    if metrics_omitted:
        entry["metrics_omitted"] = True
    if view is not None and view.supplemental_evaluation is not None:
        entry["supplemental_available"] = True
    return entry


class EvidenceReads:
    def __init__(
        self,
        *,
        state: EvidenceReadState,
        sessions: CandidateSessions,
        annotation: AnnotationWorkflow,
        admission: AnnotationAdmission,
        attachments: EvidenceAttachments,
        clock: Clock,
        trace: CandidateTrace,
    ) -> None:
        self.state = state
        self.sessions = sessions
        self.annotation = annotation
        self.annotation_admission = admission
        self.attachments = attachments
        self.clock = clock
        self.trace = trace

    def get_global_evidence(
        self,
        agent_session_id: str,
        *,
        source: Literal["explicit", "verifier_injected"] = "explicit",
        verifier_attempt_id: str | None = None,
        known_snapshot_id: str | None = None,
        compact: bool = False,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Return settled worker evidence and any completed objective views."""
        session = self.sessions.session(agent_session_id)
        # Automatic injection normally follows a prior verifier response in
        # the same session.  Derive its base from the durable read receipt so
        # process restarts and retained ThinkThread wakes behave identically.
        if compact and known_snapshot_id is None and session.global_evidence_reads:
            known_snapshot_id = session.global_evidence_reads[-1].snapshot_id
        operation_id = self.trace.new_operation_id("read")
        try:
            with self.state.lock_run(session.run_id):
                self.annotation.reconcile_tool_heads(session.run_id)
                session = self.sessions.session(
                    agent_session_id,
                    run_id=session.run_id,
                )
                run = self.state.read_run(session.run_id)
                view = self.view(session.run_id)
                self.attachments.attach_external_evaluations(session.run_id, view)
                frozen = self.state.read_frozen_spec(run.frozen_spec_id)
                mode = global_evidence_mode(frozen.spec.strategy.config)
                if compact and known_snapshot_id is not None:
                    base_receipt = next(
                        (
                            item
                            for item in reversed(session.global_evidence_reads)
                            if item.snapshot_id == known_snapshot_id
                        ),
                        None,
                    )
                    # A dynamic FrozenSpec/mode revision changes the visible
                    # contract. Old receipts without these fields are also
                    # intentionally treated as an unusable cursor.
                    if (
                        base_receipt is None
                        or base_receipt.frozen_spec_id != run.frozen_spec_id
                        or base_receipt.mode != mode
                    ):
                        known_snapshot_id = None
                if mode == "independent":
                    view = [
                        entry
                        for entry in view
                        if entry["candidate_id"] == session.candidate_id
                    ]
                completed_views = [
                    GlobalEvidenceViewReference(
                        candidate_id=str(entry["candidate_id"]),
                        iteration=int(entry["iteration"]),
                        commit=(
                            str(entry["commit"])
                            if entry.get("commit") is not None
                            else None
                        ),
                        artifact_ref=(
                            ArtifactRef.model_validate(entry["artifact_ref"])
                            if entry.get("artifact_ref") is not None
                            else None
                        ),
                        view_created_at=str(entry["view_created_at"]),
                        supplemental_evaluation_present=(
                            bool(entry.get("supplemental_available"))
                        ),
                    )
                    for entry in view
                    if entry["view"] is not None
                    and (
                        entry.get("commit") is not None
                        or entry.get("artifact_ref") is not None
                    )
                    and entry["view_created_at"] is not None
                ]
                candidate_records = self.state.read_candidates(session.run_id)
                caller_record = next(
                    (
                        record
                        for record in candidate_records
                        if record.candidate_id == session.candidate_id
                    ),
                    None,
                )
                caller_iterations = caller_record.iterations if caller_record else []
                if source == "verifier_injected" and verifier_attempt_id is None:
                    latest_attempt = (
                        caller_iterations[-1].metrics.get("_candidate_attempt", {})
                        if caller_iterations
                        else {}
                    )
                    verifier_attempt_id = (
                        str(latest_attempt["id"])
                        if isinstance(latest_attempt, dict)
                        and latest_attempt.get("id")
                        else None
                    )
                snapshot = self.trace.snapshot(session, view)
                read_at = self.clock.timestamp()
                self.trace.record(
                    session,
                    kind="evidence.global_returned",
                    operation_id=operation_id,
                    occurred_at=read_at,
                    payload={
                        "source": source,
                        "snapshot_id": snapshot.snapshot_id,
                        "entry_count": len(view),
                        "mode": mode,
                        "candidate_iteration": (
                            caller_iterations[-1].iteration
                            if caller_iterations
                            else None
                        ),
                        "run_iteration_count": sum(
                            len(record.iterations) for record in candidate_records
                        ),
                        "verifier_attempt_id": verifier_attempt_id,
                    },
                )
                read_record = GlobalEvidenceReadRecord(
                    read_at=read_at,
                    operation_id=operation_id,
                    snapshot_id=snapshot.snapshot_id,
                    source=source,
                    frozen_spec_id=run.frozen_spec_id,
                    mode=mode,
                    candidate_verifier_iteration=(
                        caller_iterations[-1].iteration if caller_iterations else None
                    ),
                    run_verifier_iteration_count=sum(
                        len(record.iterations) for record in candidate_records
                    ),
                    evidence_count=len(view),
                    completed_view_count=len(completed_views),
                    completed_supplemental_evaluation_count=sum(
                        item.supplemental_evaluation_present for item in completed_views
                    ),
                    completed_views=completed_views,
                )
                self.state.write_agent_session(
                    session.model_copy(
                        update={
                            "updated_at": read_record.read_at,
                            "global_evidence_reads": [
                                *session.global_evidence_reads,
                                read_record,
                            ],
                        }
                    )
                )
        except Exception as exc:
            self.trace.failed(
                session,
                operation="global_evidence",
                operation_id=operation_id,
                error=exc,
                verified_target={"agent_session_id": agent_session_id},
            )
            raise
        self.register_annotations(session.run_id)
        if not compact:
            return view
        return self._compact_global_evidence(
            session,
            snapshot_id=snapshot.snapshot_id,
            entries=view,
            known_snapshot_id=known_snapshot_id,
        )

    def _compact_global_evidence(
        self,
        session: AgentSessionRecord,
        *,
        snapshot_id: str,
        entries: list[dict[str, Any]],
        known_snapshot_id: str | None,
    ) -> dict[str, Any]:
        """Build a full, delta, or unchanged Evidence delivery.

        The durable snapshot remains the complete list.  Only the model-facing
        response is compacted; if the base cannot be read or is not a list we
        deliberately fall back to a full delivery rather than guessing a
        removal or cross-mode change.
        """
        base_entries: list[dict[str, Any]] | None = None
        if known_snapshot_id:
            try:
                previous = self.trace.read_snapshot(session, known_snapshot_id)
                if isinstance(previous.content, list) and all(
                    isinstance(item, dict) for item in previous.content
                ):
                    base_entries = previous.content
            except (FileNotFoundError, OSError, ValueError, TypeError):
                base_entries = None

        def identity(item: dict[str, Any]) -> tuple[str, int]:
            return (str(item.get("candidate_id", "")), int(item.get("iteration", 0)))

        if base_entries is None:
            return {
                "snapshot_id": snapshot_id,
                "base_snapshot_id": None,
                "delivery": "full",
                "unchanged": False,
                "entry_count": len(entries),
                "entries": entries,
                "added": [],
                "changed": [],
                "removed": [],
            }
        if known_snapshot_id == snapshot_id:
            return {
                "snapshot_id": snapshot_id,
                "base_snapshot_id": known_snapshot_id,
                "delivery": "unchanged",
                "unchanged": True,
                "entry_count": len(entries),
                "entries": [],
                "added": [],
                "changed": [],
                "removed": [],
            }

        old = {identity(item): item for item in base_entries}
        current = {identity(item): item for item in entries}
        added = [item for key, item in current.items() if key not in old]
        changed = [
            item for key, item in current.items()
            if key in old and old[key] != item
        ]
        removed = [
            {"candidate_id": key[0], "iteration": key[1]}
            for key in old
            if key not in current
        ]
        return {
            "snapshot_id": snapshot_id,
            "base_snapshot_id": known_snapshot_id,
            "delivery": "delta",
            "unchanged": False,
            "entry_count": len(entries),
            "entries": [],
            "added": added,
            "changed": changed,
            "removed": removed,
        }

    def get_evidence_detail(
        self,
        agent_session_id: str,
        candidate_id: str,
        iteration: int,
    ) -> dict[str, Any]:
        """Return one immutable supplemental evaluation visible to a worker."""
        session = self.sessions.session(agent_session_id)
        operation_id = self.trace.new_operation_id("read")
        try:
            with self.state.lock_run(session.run_id):
                session = self.sessions.session(
                    agent_session_id,
                    run_id=session.run_id,
                )
                run = self.state.read_run(session.run_id)
                frozen = self.state.read_frozen_spec(run.frozen_spec_id)
                if not frozen.supplemental_evaluation_enabled:
                    raise RuntimeError("supplemental evaluation is disabled for this run")
                if (
                    global_evidence_mode(frozen.spec.strategy.config) == "independent"
                    and candidate_id != session.candidate_id
                ):
                    raise PermissionError(
                        "independent Global Evidence only exposes the caller's candidate"
                    )

                entry = next(
                    (
                        item
                        for item in self.view(session.run_id)
                        if item["candidate_id"] == candidate_id
                        and item["iteration"] == iteration
                    ),
                    None,
                )
                if entry is None:
                    raise ValueError("settled worker Evidence iteration not found")

                task = self.annotation.task(session.run_id, candidate_id, iteration)
                view = task.view if task is not None and task.state == "completed" else None
                if (
                    task is None
                    or view is None
                    or task.run_id != session.run_id
                    or task.candidate_id != candidate_id
                    or task.iteration != iteration
                    or task.attempt_commit != entry["commit"]
                    or task.attempt_artifact_ref
                    != (
                        ArtifactRef.model_validate(entry["artifact_ref"])
                        if entry.get("artifact_ref") is not None
                        else None
                    )
                ):
                    raise RuntimeError(
                        "supplemental Evidence identity does not match iteration"
                    )
                if view.supplemental_evaluation is None:
                    raise RuntimeError("supplemental evaluation is not available")

                if entry.get("artifact_ref") is None:
                    raise RuntimeError(
                        "supplemental Evidence has no exact artifact reference"
                    )
                artifact_ref = ArtifactRef.model_validate(entry["artifact_ref"])
                result = {
                    "candidate_id": candidate_id,
                    "iteration": iteration,
                    "commit": entry["commit"],
                    "frozen_spec_id": entry["frozen_spec_id"],
                    "spec_hash": entry["spec_hash"],
                    "metric_name": entry["metric_name"],
                    "metric_direction": entry["metric_direction"],
                    "supplemental_evaluation": view.supplemental_evaluation.model_dump(
                        mode="json"
                    ),
                }
                snapshot = self.trace.snapshot(session, result)
                read_at = self.clock.timestamp()
                self.trace.record(
                    session,
                    kind="evidence.detail_returned",
                    operation_id=operation_id,
                    occurred_at=read_at,
                    payload={
                        "target_candidate_id": candidate_id,
                        "target_iteration": iteration,
                        "artifact_ref": artifact_ref,
                        "snapshot_id": snapshot.snapshot_id,
                    },
                )
                read_record = EvidenceDetailReadRecord(
                    read_at=read_at,
                    operation_id=operation_id,
                    snapshot_id=snapshot.snapshot_id,
                    candidate_id=candidate_id,
                    iteration=iteration,
                    commit=(
                        str(entry["commit"])
                        if entry.get("commit") is not None
                        else None
                    ),
                    artifact_ref=artifact_ref,
                )
                self.state.write_agent_session(
                    session.model_copy(
                        update={
                            "updated_at": read_record.read_at,
                            "evidence_detail_reads": [
                                *session.evidence_detail_reads,
                                read_record,
                            ],
                        }
                    )
                )
                return result
        except Exception as exc:
            self.trace.failed(
                session,
                operation="evidence_detail",
                operation_id=operation_id,
                error=exc,
                verified_target={
                    "candidate_id": candidate_id,
                    "iteration": iteration,
                },
            )
            raise

    def evidence_annotation_usage(self, run_id: str) -> dict[str, Any]:
        """Return persisted annotator usage without exposing annotation tasks."""
        self.state.read_run(run_id)
        tasks = self.annotation.list_tasks(run_id)
        usage: dict[str, int | float] = {}
        for task in tasks:
            for key, value in task.usage.items():
                usage[key] = usage.get(key, 0) + value
        states: dict[str, int] = {}
        for task in tasks:
            states[task.state] = states.get(task.state, 0) + 1
        return {
            **usage,
            "tasks": len(tasks),
            "attempts": sum(task.attempts for task in tasks),
            "states": states,
            "coverage": "persisted host-native Evidence annotator turn usage",
        }

    def view(self, run_id: str) -> list[dict[str, Any]]:
        run = self.state.read_run(run_id)
        frozen = self.state.read_frozen_spec(run.frozen_spec_id)
        specs = {frozen.frozen_spec_id: frozen}
        discoverable_tool_ids = (
            self.attachments.discoverable_tool_ids(run_id)
            if frozen.spec.shared_dir.enabled
            else set()
        )
        evidence = [
            (
                iteration.created_at,
                record.candidate_id,
                iteration,
                record.task.workspace_provider,
            )
            for record in self.state.read_candidates(run_id)
            for iteration in record.iterations
            if iteration.agent_session_id is not None
        ]
        evidence.sort(
            key=lambda item: (
                item[0],
                item[1],
                item[2].iteration,
            )
        )
        result = []
        for _, candidate_id, iteration, workspace_provider in evidence:
            spec_id = iteration_spec_id(run, iteration)
            if spec_id not in specs:
                specs[spec_id] = self.state.read_frozen_spec(spec_id)
            iteration_frozen = specs[spec_id]
            task = self.annotation.task(run_id, candidate_id, iteration.iteration)
            view = task.view if task is not None and task.state == "completed" else None
            if view is not None and (
                view.run_id != run_id
                or view.candidate_id != candidate_id
                or view.iteration != iteration.iteration
                or view.attempt_commit != iteration.git_head
                or (
                    view.attempt_artifact_ref is not None
                    and view.attempt_artifact_ref != iteration.artifact_ref
                )
                or (view.attempt_commit is None and view.attempt_artifact_ref is None)
            ):
                raise RuntimeError("evidence view does not match iteration")
            result.append(
                global_evidence_entry(
                    candidate_id,
                    iteration,
                    view,
                    discoverable_tool_ids,
                    workspace_provider,
                    run.provider_cleanup,
                    spec_id,
                    spec_hash=iteration_frozen.spec_hash,
                    metric_name=iteration_frozen.spec.metric_name,
                    metric_direction=iteration_frozen.spec.metric_direction,
                )
            )
        return result

    def register_annotations(self, run_id: str) -> None:
        try:
            self.annotation_admission.ensure(run_id)
        except Exception:
            # Annotation registration never changes settled verifier Evidence.
            pass
