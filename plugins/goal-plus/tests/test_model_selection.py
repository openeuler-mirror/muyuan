from __future__ import annotations

from pathlib import Path

import pytest

from goal_plus.runtime import FileSearchRuntime
from goal_plus.domain.agents import WorkerLaunchDefaults
from goal_plus.domain.search_input import SearchSpecInput
from tests._runtime_helpers import make_project, spec_with_strategy, start_host_verified_goal


def _available_models() -> list[dict[str, object]]:
    return [
        {
            "model": "deepseek/deepseek-v3",
            "model_id": "deepseek-v3",
            "provider": "deepseek",
            "display_name": "DeepSeek V3",
        },
        {
            "model": "openai/gpt-5.6",
            "model_id": "gpt-5.6",
            "provider": "openai",
            "display_name": "GPT-5.6",
        },
    ]


def _stub_model_discovery(runtime: FileSearchRuntime) -> None:
    runtime.models.list_available_models = lambda agent_harness, query=None: {  # type: ignore[method-assign]
        "agent_harness": agent_harness,
        "adapter_version": f"{agent_harness}-adapter-v1",
        "models": _available_models(),
    }


def _models_spec(project: Path, *, counts: bool = True, max_parallel: int = 4):
    models: list[dict[str, object]] = [
        {"model": "deepseek-v3"},
        {"model": "gpt-5.6"},
    ]
    if counts:
        models[0]["count"] = 1
        models[1]["count"] = max_parallel - 1
    return spec_with_strategy(
        project,
        {
            "name": "random",
            "models": models,
        },
        max_parallel=max_parallel,
    )


def test_explicit_a1b3_models_bind_each_candidate_and_session(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    _stub_model_discovery(runtime)
    spec = SearchSpecInput.model_validate(_models_spec(project).model_dump(mode="json", exclude_defaults=True))
    spec.strategy.worker_launch = WorkerLaunchDefaults(reasoning_effort="high", service_tier="priority")
    spec.strategy.models[0].reasoning_effort = "low"
    frozen = runtime.freeze_spec(spec.to_runtime_spec(), [project / "evaluator.py"], agent_harness="codex")
    assert frozen.spec.strategy.models[0].provider == "deepseek"
    assert frozen.spec.strategy.models[0].adapter_version == "codex-adapter-v1"

    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=4)
    tasks = runtime.start_batch(run_id, plan.plan_id)

    expected = [
        "deepseek/deepseek-v3",
        "openai/gpt-5.6",
        "openai/gpt-5.6",
        "openai/gpt-5.6",
    ]
    assert [model.model for model in plan.selected_models] == expected
    assert [task.selected_model.model for task in tasks if task.selected_model] == expected
    assert tasks[0].model_provenance["exact_model_ref"] == expected[0]

    session = runtime.start_agent_session(run_id, tasks[0].candidate_id)
    assert session.selected_model is not None
    assert session.selected_model.model == expected[0]
    assert session.launch["model"] == expected[0]
    assert session.launch["reasoning_effort"] == "low"
    assert session.launch["service_tier"] == "priority"
    other = runtime.start_agent_session(run_id, tasks[1].candidate_id)
    assert other.launch["model"] == expected[1]
    assert other.launch["reasoning_effort"] == "high"
    assert other.launch["service_tier"] == "priority"

    runtime.bind_agent_handle(session.agent_session_id, {"external_id": "native-worker"})
    continued = runtime.continue_agent_session(session.agent_session_id)
    assert continued.selected_model == session.selected_model
    assert "model" not in continued.launch


def test_models_without_counts_balance_across_max_parallel(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    _stub_model_discovery(runtime)
    frozen = runtime.freeze_spec(
        _models_spec(project, counts=False), [project / "evaluator.py"], agent_harness="codex"
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=4)

    assert [model.model for model in plan.selected_models] == [
        "deepseek/deepseek-v3",
        "openai/gpt-5.6",
        "deepseek/deepseek-v3",
        "openai/gpt-5.6",
    ]


@pytest.mark.parametrize("agent_harness", ["codex", "pi"])
@pytest.mark.parametrize("counts,error", [
    ([None, None], None),
    ([1, 3], None),
    ([1, 2], "counts must sum"),
    ([1, None], "count for every model or for none"),
])
def test_uncounted_command_accepts_valid_spec_allocation(tmp_path, agent_harness, counts, error):
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    _stub_model_discovery(runtime)
    goal = start_host_verified_goal(
        runtime.root_dir, project, agent_harness=agent_harness,
        raw_goal="max_parallel=4 workers=deepseek-v3,gpt-5.6 Optimize score",
    )
    spec = _models_spec(project)
    for model, count in zip(spec.strategy.models, counts, strict=True):
        model.count = count
    if error:
        goal_path = runtime.root_dir / "goal-plus" / goal.goal_plus_id / "goal.json"
        before = goal_path.read_bytes()
        with pytest.raises(ValueError, match=error):
            runtime.freeze_spec(spec, [project / "evaluator.py"], goal_plus_id=goal.goal_plus_id)
        assert goal_path.read_bytes() == before
        assert not list((runtime.root_dir / "specs").glob("*/frozen_spec.json"))
    else:
        frozen = runtime.freeze_spec(spec, [project / "evaluator.py"], goal_plus_id=goal.goal_plus_id)
        assert [model.count for model in frozen.spec.strategy.models] == counts


def test_selected_models_require_full_initial_deployment(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    _stub_model_discovery(runtime)
    frozen = runtime.freeze_spec(_models_spec(project), [project / "evaluator.py"], agent_harness="codex")
    run_id = runtime.create_run(frozen.frozen_spec_id)

    with pytest.raises(ValueError, match="requested_k must equal"):
        runtime.plan_next(run_id, requested_k=1)


def test_models_fail_fast_when_requested_model_is_missing(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    _stub_model_discovery(runtime)
    spec = spec_with_strategy(
        project,
        {
            "name": "random",
            "models": [{"model": "missing-model", "count": 1}],
        },
        max_parallel=1,
    )

    with pytest.raises(ValueError, match="requested model is not available"):
        runtime.freeze_spec(spec, [project / "evaluator.py"], agent_harness="codex")


def test_explicit_counts_must_equal_max_parallel(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    _stub_model_discovery(runtime)
    spec = _models_spec(project, max_parallel=5)

    spec.strategy.models[1].count = 3
    with pytest.raises(ValueError, match="counts must sum"):
        runtime.freeze_spec(spec, [project / "evaluator.py"], agent_harness="codex")


def test_without_models_candidates_keep_the_strategy_default_model(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    strategy = _models_spec(project).strategy.model_dump(mode="json")
    strategy["models"] = []
    strategy["worker_launch"] = {"model": "gpt-5.6-sol"}
    spec = spec_with_strategy(project, strategy, max_parallel=4)
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"], agent_harness="codex")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=4)
    tasks = runtime.start_batch(run_id, plan.plan_id)
    sessions = [runtime.start_agent_session(run_id, task.candidate_id) for task in tasks]

    assert plan.selected_models == []
    assert [task.selected_model for task in tasks] == [None] * 4
    assert [session.launch["model"] for session in sessions] == ["gpt-5.6-sol"] * 4


def test_selected_models_share_the_run_global_evidence(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    _stub_model_discovery(runtime)
    frozen = runtime.freeze_spec(
        _models_spec(project, max_parallel=2), [project / "evaluator.py"], agent_harness="codex"
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    first, second = runtime.start_batch(run_id, plan.plan_id)
    first_session = runtime.start_agent_session(run_id, first.candidate_id)
    second_session = runtime.start_agent_session(run_id, second.candidate_id)

    (first.workspace / "initial_program.py").write_text("VALUE = 1\n", encoding="utf-8")
    runtime.run_verifier(
        run_id,
        first.candidate_id,
        agent_session_id=first_session.agent_session_id,
        hypothesis="test causal filter",
    )
    shared = runtime.get_global_evidence(second_session.agent_session_id)

    assert [
        (entry["candidate_id"], entry["score"], entry["view"])
        for entry in shared
    ] == [(first.candidate_id, 0.0, None)]
