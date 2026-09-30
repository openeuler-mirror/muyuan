"""Requester-scoped access to one immutable settled reference workspace."""

from goal_plus.domain.worker_policy import (
    global_evidence_mode,
)
from goal_plus.domain.artifacts import (
    ArtifactRef,
    ReferenceWorkspaceAccess,
    ReferenceWorkspaceRecord,
)
from goal_plus.domain.serialization import canonical_json, sha256_text
from goal_plus.application.candidate_sessions import CandidateSessions
from goal_plus.application.candidate_trace import CandidateTrace
from goal_plus.ports.references import ReferenceState, ReferenceWorkspace
from goal_plus.ports.clock import Clock


class ReferenceWorkspaces:
    def __init__(
        self,
        state: ReferenceState,
        workspace: ReferenceWorkspace,
        sessions: CandidateSessions,
        clock: Clock,
        trace: CandidateTrace,
    ) -> None:
        self.state, self.workspace, self.sessions, self.clock = (
            state,
            workspace,
            sessions,
            clock,
        )
        self.trace = trace

    def open(
        self,
        agent_session_id: str,
        source_candidate_id: str,
        source_iteration: int,
    ) -> ReferenceWorkspaceRecord:
        """Check out one exact settled artifact in the worker's ref-workspace."""
        session = self.sessions.session(agent_session_id)
        operation_id = self.trace.new_operation_id("reference")
        try:
            return self._open(
                session,
                source_candidate_id,
                source_iteration,
                operation_id,
            )
        except Exception as exc:
            self.trace.failed(
                session,
                operation="reference_open",
                operation_id=operation_id,
                error=exc,
                verified_target={
                    "source_candidate_id": source_candidate_id,
                    "source_iteration": source_iteration,
                },
            )
            raise

    def _open(
        self,
        session,
        source_candidate_id: str,
        source_iteration: int,
        operation_id: str,
    ) -> ReferenceWorkspaceRecord:
        candidate_id = source_candidate_id
        iteration = source_iteration
        with self.state.lock_run(session.run_id):
            session = self.sessions.session(
                session.agent_session_id,
                run_id=session.run_id,
            )
            run = self.state.read_run(session.run_id)
            frozen = self.state.read_frozen_spec(run.frozen_spec_id)
            if (
                global_evidence_mode(frozen.spec.strategy.config) == "independent"
                and candidate_id != session.candidate_id
            ):
                raise PermissionError(
                    "independent Global Evidence only exposes the caller's candidate"
                )
            source = self.state.read_candidate(session.run_id, candidate_id)
            evidence = next(
                (
                    item
                    for item in source.iterations
                    if item.iteration == iteration and item.agent_session_id is not None
                ),
                None,
            )
            if evidence is None:
                raise ValueError("settled worker Evidence iteration not found")
            artifact_ref = evidence.artifact_ref
            if artifact_ref is None and evidence.git_head is not None:
                artifact_ref = ArtifactRef(
                    kind="git_commit",
                    id=evidence.git_head,
                    provider=source.task.workspace_provider,
                )
            if artifact_ref is None:
                raise RuntimeError("Evidence iteration has no immutable artifact")

            identity = canonical_json({"requester": session.agent_session_id})
            reference_id = f"ref_{sha256_text(identity)[:16]}"
            display_name = "ref-workspace"
            destination = self.state.destination(
                session.run_id, session.agent_session_id
            )
            for previous in self.list(
                session.run_id,
                requester_agent_session_id=session.agent_session_id,
                open_only=True,
            ):
                previous_source = self.state.read_candidate(
                    session.run_id,
                    previous.source_candidate_id,
                )
                available, clean = self.workspace.remove(previous_source, previous)
                if previous.reference_id != reference_id:
                    self.state.write(
                        previous.model_copy(
                            update={
                                "state": "closed",
                                "integrity_status": (
                                    "unavailable"
                                    if not available
                                    else "clean"
                                    if clean
                                    else "contaminated"
                                ),
                                "closed_at": self.clock.timestamp(),
                            }
                        )
                    )
            reference_root = self.workspace.materialize(
                source,
                artifact_ref,
                destination,
            )
            record = ReferenceWorkspaceRecord(
                reference_id=reference_id,
                run_id=session.run_id,
                requester_agent_session_id=session.agent_session_id,
                source_candidate_id=candidate_id,
                source_iteration=iteration,
                artifact_ref=artifact_ref,
                display_name=display_name,
                workspace_provider=source.task.workspace_provider,
                access=ReferenceWorkspaceAccess(
                    kind="filesystem_path",
                    root=reference_root,
                    readonly_contract="soft_detected",
                ),
                state="open",
                integrity_status="clean",
                created_at=self.clock.timestamp(),
                trace_operation_id=operation_id,
            )
            self.state.write(record)
            try:
                self.trace.record(
                    session,
                    kind="reference.opened",
                    operation_id=operation_id,
                    occurred_at=record.created_at,
                    payload={
                        "source_candidate_id": candidate_id,
                        "source_iteration": iteration,
                        "artifact_ref": artifact_ref,
                        "reference_id": reference_id,
                        "workspace_provider": source.task.workspace_provider,
                    },
                )
            except Exception as exc:
                failed = record.model_copy(update={"trace_capture": "failed"})
                self.state.write(failed)
                self.trace.failed(
                    session,
                    operation="reference_open",
                    operation_id=operation_id,
                    error=exc,
                    verified_target={
                        "source_candidate_id": candidate_id,
                        "source_iteration": iteration,
                        "reference_id": reference_id,
                    },
                )
                return failed
            captured = record.model_copy(update={"trace_capture": "captured"})
            self.state.write(captured)
            return captured

    def list(
        self,
        run_id: str,
        *,
        requester_agent_session_id: str | None = None,
        open_only: bool = False,
    ) -> list[ReferenceWorkspaceRecord]:
        records = self.state.read(run_id)
        if requester_agent_session_id is not None:
            records = [
                record
                for record in records
                if record.requester_agent_session_id == requester_agent_session_id
            ]
        if open_only:
            records = [record for record in records if record.state == "open"]
        return records
