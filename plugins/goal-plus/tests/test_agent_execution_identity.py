"""Harness, runtime and workspace providers are separate protocol dimensions."""

import pytest
from pydantic import TypeAdapter, ValidationError

from goal_plus.domain.agents import AgentHarnessKind, AgentSessionHandle, AgentRuntimeProvider
from goal_plus.domain.search import AgentSessionRecord
from goal_plus.domain.artifacts import WorkspaceSpec
from goal_plus.domain.search import FrozenSpec
from goal_plus.domain.annotation import ResolvedEvidenceAnnotatorProfile
from goal_plus.domain.verifier import VerifierInvocationRecord
from tests._runtime_helpers import spec_for, make_project
from goal_plus.runtime import FileSearchRuntime


@pytest.mark.parametrize("value", ["thinkthread", "pi-rpc", "local"])
def test_backend_and_transport_are_not_agent_harnesses(value):
    with pytest.raises(ValidationError):
        TypeAdapter(AgentHarnessKind).validate_python(value)


@pytest.mark.parametrize("value", ["codex", "pi", "pi-rpc", "local"])
def test_harness_and_transport_are_not_execution_backends(value):
    with pytest.raises(ValidationError):
        TypeAdapter(AgentRuntimeProvider).validate_python(value)


def test_retained_child_keeps_pi_harness_identity(tmp_path):
    session = AgentSessionRecord(
        agent_session_id="session", run_id="run", candidate_id="candidate",
        agent_harness="pi", runtime_provider="thinkthread", execution_scope="thinkthread_private",
        session_handle=AgentSessionHandle(agent_harness="pi", runtime_provider="thinkthread", external_id="child"),
        created_at="now", updated_at="now", workspace=tmp_path,
    )
    restored = AgentSessionRecord.model_validate_json(session.model_dump_json())
    assert restored.agent_harness == restored.session_handle.agent_harness == "pi"
    assert restored.runtime_provider == restored.session_handle.runtime_provider == "thinkthread"


@pytest.mark.parametrize("handle", [
    {"agent_harness": "codex", "runtime_provider": "direct"},
    {"agent_harness": "pi", "runtime_provider": "direct"},
])
def test_session_rejects_handle_from_another_harness_or_backend(tmp_path, handle):
    with pytest.raises(ValidationError):
        AgentSessionRecord(
            agent_session_id="session", run_id="run", candidate_id="candidate",
            agent_harness="pi", runtime_provider="thinkthread", execution_scope="thinkthread_private",
            session_handle=handle, created_at="now", updated_at="now", workspace=tmp_path,
        )


@pytest.mark.parametrize("agent_harness,runtime_provider,workspace_provider", [
    ("codex", "direct", "copy"), ("codex", "direct", "git_worktree"),
    ("pi", "direct", "copy"), ("pi", "direct", "git_worktree"),
    ("pi", "thinkthread", "thinkthread"),
])
def test_frozen_execution_matrix_round_trip(tmp_path, agent_harness, runtime_provider, workspace_provider):
    spec = spec_for(tmp_path).model_copy(update={"workspace": WorkspaceSpec(provider=workspace_provider)})
    frozen = FrozenSpec(
        frozen_spec_id="spec", spec_hash="hash", spec=spec,
        agent_harness=agent_harness, runtime_provider=runtime_provider,
        verifier_hashes={}, frozen_verifier_paths={}, created_at="now",
    )
    restored = FrozenSpec.model_validate_json(frozen.model_dump_json())
    assert (restored.agent_harness, restored.runtime_provider, restored.spec.workspace.provider) == (
        agent_harness, runtime_provider, workspace_provider,
    )
    payload = frozen.model_dump(mode="json")
    payload.pop("runtime_provider")
    with pytest.raises(ValidationError):
        FrozenSpec.model_validate(payload)
    for field in ("native_host", "worker_host"):
        with pytest.raises(ValidationError):
            FrozenSpec.model_validate({**frozen.model_dump(mode="json"), field: agent_harness})


@pytest.mark.parametrize("old_field", ["backend", "workspace_backend"])
def test_workspace_rejects_old_protocol(old_field):
    with pytest.raises(ValidationError):
        WorkspaceSpec.model_validate({old_field: "copy"})


def test_roles_reject_unsupported_runtime_providers():
    with pytest.raises(ValidationError):
        ResolvedEvidenceAnnotatorProfile(agent_harness="codex", runtime_provider="thinkthread", timeout_seconds=30)
    with pytest.raises(ValidationError):
        VerifierInvocationRecord(
            invocation_id="test", agent_harness="pi", runtime_provider="thinkthread",
            model="test", started_at="before", completed_at="after", elapsed_seconds=1,
        )


def test_cross_execution_calls_have_no_domain_side_effects(tmp_path):
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(spec_for(project, max_parallel=1), [project / "evaluator.py"], agent_harness="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    [task] = runtime.start_batch(run_id, plan.plan_id)
    sessions = runtime.candidate_sessions
    session = sessions.start_agent_session(run_id, task.candidate_id)
    before = {str(p): p.read_bytes() for p in runtime.root_dir.rglob("*.json")}
    for harness, provider in (("codex", "direct"), ("pi", "thinkthread")):
        with pytest.raises(ValueError):
            sessions.prepare_invocation(run_id, task.candidate_id, agent_harness=harness, runtime_provider=provider)
        with pytest.raises(ValueError):
            sessions.bind_agent_handle(session.agent_session_id, {"agent_harness": harness, "runtime_provider": provider})
    with pytest.raises(ValidationError):
        sessions.bind_agent_handle(session.agent_session_id, {"host": "pi"})
    assert before == {str(p): p.read_bytes() for p in runtime.root_dir.rglob("*.json")}


def test_protocol_does_not_expose_mixed_agent_identities():
    import goal_plus.domain.agents as agents

    for name in ("AgentHostKind", "NativeAgentKind", "AgentHostHandle", "search_worker_host"):
        assert not hasattr(agents, name)
    assert "worker_host" not in FrozenSpec.__dict__


@pytest.mark.parametrize("harness,runtime,workspace", [
    ("codex", "thinkthread", "thinkthread"),
    ("pi", "direct", "thinkthread"),
    ("pi", "thinkthread", "git_worktree"),
    ("pi", "thinkthread", "copy"),
])
def test_unsupported_execution_matrix_is_rejected(tmp_path, harness, runtime, workspace):
    spec = spec_for(tmp_path).model_copy(update={"workspace": WorkspaceSpec(provider=workspace)})
    with pytest.raises(ValidationError):
        FrozenSpec(
            frozen_spec_id="spec", spec_hash="hash", spec=spec,
            agent_harness=harness, runtime_provider=runtime,
            verifier_hashes={}, frozen_verifier_paths={}, created_at="now",
        )


def test_old_goal_identity_cannot_be_skipped_by_start_admission(tmp_path):
    from tests._runtime_helpers import start_host_verified_goal
    from goal_plus.application.goal_authorization import GoalAuthorization
    from goal_plus.infrastructure.state.goal import FileGoalQueries
    from goal_plus.infrastructure.state.files import write_json

    root = tmp_path / ".gp"
    goal = start_host_verified_goal(root, tmp_path, agent_harness="pi")
    payload = goal.model_dump(mode="json")
    payload["active_session"]["host"] = payload["active_session"].pop("agent_harness")
    path = root / "goal-plus" / goal.goal_plus_id / "goal.json"
    write_json(path, payload)
    before = path.read_bytes()
    authorization = GoalAuthorization(FileGoalQueries(root))
    with pytest.raises(ValidationError):
        authorization.require_start(goal.host_command_invocations[0])
    assert path.read_bytes() == before


@pytest.mark.parametrize("harness,provider,default", [
    ("codex", "direct", "autoresearch"),
    ("pi", "direct", "autoresearch"),
    ("pi", "thinkthread", "common"),
])
def test_inner_agent_defaults_belong_to_execution_combination(harness, provider, default):
    from goal_plus.domain.search import StrategySpec
    from goal_plus.domain.worker_policy import resolved_inner_agent_id

    assert resolved_inner_agent_id(StrategySpec(), harness, provider) == default
    for explicit in ("common", "autoresearch"):
        assert resolved_inner_agent_id(StrategySpec(inner_agent=explicit), harness, provider) == explicit
