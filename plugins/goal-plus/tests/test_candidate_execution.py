"""Binding checks are pure and shared by direct and cached session paths."""

from pathlib import Path

import pytest

from goal_plus.application.candidate_execution import (
    require_candidate_not_recovering,
    require_current_session,
)
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.search import (
    AgentSessionRecord,
    CandidateRecovery,
    CandidateRecoveryPoint,
    CandidateTask,
)
from goal_plus.ports.state import CandidateRecord


@pytest.fixture
def binding():
    record = CandidateRecord(
        candidate_id="c001",
        status="created",
        execution_generation=1,
        task=CandidateTask(
            candidate_id="c001",
            run_id="run_test",
            workspace=Path("/unmaterialized"),
            workspace_provider="git_worktree",
            hypothesis="visible task",
            allowed_files=["main.py"],
            denied_files=["verify.py"],
        ),
    )
    session = AgentSessionRecord(
        agent_session_id="agent_test_001",
        run_id="run_test",
        candidate_id="c001",
        execution_generation=1,
        workspace=record.task.workspace,
        created_at="2026-09-05T00:00:00Z",
        updated_at="2026-09-05T00:00:00Z",
    )
    return session, record


@pytest.mark.parametrize("state", [None, "prepared", "restoring", "restored"])
@pytest.mark.parametrize("generation", [0, 1, 2])
def test_reuse_requires_current_generation_and_completed_recovery(
    binding, state, generation
):
    session, record = binding
    session.execution_generation = generation
    if state is not None:
        artifact = ArtifactRef(kind="git_commit", provider="git_worktree", id="head")
        record.recovery = CandidateRecovery(
            recovery_id="recovery_" + "1" * 32,
            generation=record.execution_generation,
            state=state,
            point=CandidateRecoveryPoint(
                artifact_ref=artifact, iteration=0, result_count=0
            ),
            fenced_session_ids=[session.agent_session_id],
            created_at=session.created_at,
            restored_artifact_ref=artifact if state == "restored" else None,
            restored_at=session.updated_at if state == "restored" else None,
        )
    before = session.model_dump(), record.model_dump()
    if generation != record.execution_generation:
        with pytest.raises(RuntimeError, match="StaleAgentSession"):
            require_current_session(session, record)
    elif state in {"prepared", "restoring"}:
        with pytest.raises(RuntimeError, match="CandidateRecoveryRequired"):
            require_current_session(session, record)
    else:
        assert require_current_session(session, record) is session
        require_candidate_not_recovering(record)
    assert (session.model_dump(), record.model_dump()) == before


@pytest.mark.parametrize("field", ["run_id", "candidate_id"])
def test_reuse_rejects_another_candidate_binding(binding, field):
    session, record = binding
    setattr(session, field, "another")
    before = session.model_dump(), record.model_dump()
    with pytest.raises(RuntimeError, match="AgentSessionOwnershipMismatch"):
        require_current_session(session, record)
    assert (session.model_dump(), record.model_dump()) == before
