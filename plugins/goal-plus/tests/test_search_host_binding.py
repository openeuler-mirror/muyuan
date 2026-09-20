"""Search inputs cannot select the native Agent or its execution driver."""

import pytest
from pydantic import ValidationError

from goal_plus.domain.search import SearchSpec
from goal_plus.runtime import FileSearchRuntime
from tests._runtime_helpers import make_project, spec_for, start_host_verified_goal


@pytest.mark.parametrize(
    "strategy",
    [
        {"worker_host": "pi"},
        {"agent": "pi"},
        {"worker_launch": {"agent_profile": "pi"}},
        {"evidence_annotator": {"agent_harness": "codex"}},
    ],
)
def test_search_input_rejects_agent_selection(tmp_path, strategy):
    payload = spec_for(make_project(tmp_path)).model_dump(mode="json")
    payload["strategy"] = strategy
    with pytest.raises(ValidationError) as error:
        SearchSpec.model_validate(payload)
    assert any(item["type"] == "extra_forbidden" for item in error.value.errors())


@pytest.mark.parametrize("agent_harness,driver", [("codex", "codex"), ("pi", "pi")])
def test_freeze_uses_goal_main_host_and_rejects_override(tmp_path, agent_harness, driver):
    project = make_project(tmp_path)
    root = tmp_path / ".gp"
    goal = start_host_verified_goal(root, project, agent_harness=agent_harness)
    runtime = FileSearchRuntime(root)
    spec = spec_for(project)
    frozen = runtime.freeze_spec(spec, [], goal_plus_id=goal.goal_plus_id)
    assert frozen.agent_harness == agent_harness
    assert frozen.agent_harness == driver
    assert "worker_host" not in frozen.spec.strategy.model_dump()
    before = {path: path.read_bytes() for path in root.rglob("*.json")}
    with pytest.raises(ValueError, match="host.*match"):
        runtime.freeze_spec(
            spec, [], goal_plus_id=goal.goal_plus_id,
            agent_harness="pi" if agent_harness == "codex" else "codex",
        )
    assert {path: path.read_bytes() for path in root.rglob("*.json")} == before
    loaded = runtime.search_run.state.read_frozen_spec(frozen.frozen_spec_id)
    assert loaded.agent_harness == agent_harness
    assert loaded.agent_harness == driver


def test_search_spec_rejects_removed_verifier_config(tmp_path):
    payload = spec_for(make_project(tmp_path)).model_dump(mode="json")
    payload["workspace"] = {"provider": "git_worktree"}
    payload["strategy"] = {
        "verifier": {},
    }
    spec = SearchSpec.model_validate(payload)
    assert spec.strategy.verifier is not None


def test_agent_harness_is_part_of_frozen_identity(tmp_path):
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    spec = spec_for(project)
    codex = runtime.freeze_spec(spec, [], agent_harness="codex")
    pi = runtime.freeze_spec(spec, [], agent_harness="pi")
    assert codex.spec == pi.spec
    assert codex.spec_hash != pi.spec_hash
    assert codex.frozen_spec_id != pi.frozen_spec_id


def test_unbound_freeze_requires_trusted_host(tmp_path):
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    with pytest.raises(ValueError, match="native host"):
        runtime.freeze_spec(spec_for(project), [])


def test_codex_goal_freezes_thinkthread_workspace_with_default_inner_agent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    root = tmp_path / ".gp"
    goal = start_host_verified_goal(root, project)
    payload = spec_for(project).model_dump(mode="json")
    payload["workspace"] = {"provider": "thinkthread"}
    frozen = FileSearchRuntime(root).freeze_spec(
        SearchSpec.model_validate(payload), [], goal_plus_id=goal.goal_plus_id
    )
    assert frozen.agent_harness == "codex"
    assert frozen.runtime_provider == "thinkthread"
    assert frozen.spec.strategy.inner_agent == "common"
