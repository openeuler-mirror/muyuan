"""Search inputs cannot select the native Agent or its execution driver."""

import pytest
from pydantic import ValidationError

from goal_plus.domain.search import SearchSpec
from goal_plus.runtime import FileSearchRuntime
from tests._runtime_helpers import make_project, spec_for, start_host_verified_goal


@pytest.mark.parametrize(
    "strategy",
    [
        {"worker_host": "pi-rpc"},
        {"agent": "pi"},
        {"worker_launch": {"agent_profile": "pi"}},
        {"evidence_annotator": {"host": "codex"}},
    ],
)
def test_search_input_rejects_agent_selection(tmp_path, strategy):
    payload = spec_for(make_project(tmp_path)).model_dump(mode="json")
    payload["strategy"] = strategy
    with pytest.raises(ValidationError) as error:
        SearchSpec.model_validate(payload)
    assert any(item["type"] == "extra_forbidden" for item in error.value.errors())


@pytest.mark.parametrize("native_host,driver", [("codex", "codex"), ("pi", "pi-rpc")])
def test_freeze_uses_goal_main_host_and_rejects_override(tmp_path, native_host, driver):
    project = make_project(tmp_path)
    root = tmp_path / ".gp"
    goal = start_host_verified_goal(root, project, native_host=native_host)
    runtime = FileSearchRuntime(root)
    spec = spec_for(project)
    frozen = runtime.freeze_spec(spec, [], goal_plus_id=goal.goal_plus_id)
    assert frozen.native_host == native_host
    assert frozen.worker_host == driver
    assert "worker_host" not in frozen.spec.strategy.model_dump()
    before = {path: path.read_bytes() for path in root.rglob("*.json")}
    with pytest.raises(ValueError, match="host.*match"):
        runtime.freeze_spec(
            spec, [], goal_plus_id=goal.goal_plus_id,
            native_host="pi" if native_host == "codex" else "codex",
        )
    assert {path: path.read_bytes() for path in root.rglob("*.json")} == before
    loaded = runtime.search_run.state.read_frozen_spec(frozen.frozen_spec_id)
    assert loaded.native_host == native_host
    assert loaded.worker_host == driver


def test_scheduler_cannot_override_main_agent(tmp_path):
    payload = spec_for(make_project(tmp_path)).model_dump(mode="json")
    payload["workspace"] = {"backend": "git_worktree"}
    payload["strategy"] = {
        "orchestration_mode": "adaptive_search",
        "search_scheduler": {
            "host": "pi-rpc", "model": "test-model", "reasoning_effort": "low",
            "timeout_seconds": 60, "reward": "evidence_llm_value/v3",
            "allocation": "value_guided_replace/v2",
        },
    }
    with pytest.raises(ValidationError) as error:
        SearchSpec.model_validate(payload)
    assert error.value.errors()[0]["loc"] == ("strategy", "search_scheduler", "host")


def test_native_host_is_part_of_frozen_identity(tmp_path):
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    spec = spec_for(project)
    codex = runtime.freeze_spec(spec, [], native_host="codex")
    pi = runtime.freeze_spec(spec, [], native_host="pi")
    assert codex.spec == pi.spec
    assert codex.spec_hash != pi.spec_hash
    assert codex.frozen_spec_id != pi.frozen_spec_id


def test_unbound_freeze_requires_trusted_host(tmp_path):
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    with pytest.raises(ValueError, match="native host"):
        runtime.freeze_spec(spec_for(project), [])


def test_codex_cannot_freeze_thinkthread_workspace(tmp_path):
    project = make_project(tmp_path)
    root = tmp_path / ".gp"
    goal = start_host_verified_goal(root, project)
    payload = spec_for(project).model_dump(mode="json")
    payload["workspace"] = {"backend": "thinkthread"}
    with pytest.raises(ValueError, match="Codex.*ThinkThread"):
        FileSearchRuntime(root).freeze_spec(
            SearchSpec.model_validate(payload), [], goal_plus_id=goal.goal_plus_id
        )
