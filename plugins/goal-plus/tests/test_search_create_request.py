"""Creation requests resume exact runs and cannot attach across Goal revisions."""

from concurrent.futures import ThreadPoolExecutor
import pytest

from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.runtime import FileSearchRuntime
from goal_plus.tools import SearchTools
from tests._runtime_helpers import make_project, spec_for, start_host_verified_goal


@pytest.fixture
def creation(tmp_path):
    project = make_project(tmp_path)
    root = tmp_path / ".gp"
    goal = start_host_verified_goal(root, project)
    runtime = FileSearchRuntime(root)
    goals = FileGoalPlusRuntime(root)
    tools = SearchTools(runtime, goals)
    frozen = tools.goal_plus_search_freeze_spec(goal.goal_plus_id, spec_for(project, max_parallel=1), ["evaluator.py"])
    return goal, goals, runtime, tools, frozen["frozen_spec_id"]


def test_concurrent_create_and_lost_response_return_one_linked_run(creation):
    goal, goals, runtime, tools, frozen = creation
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: tools.goal_plus_search_create(goal.goal_plus_id, frozen, "one"), range(2)))
    assert results[0] == results[1]
    record = goals.status(goal.goal_plus_id)
    assert len(record.search_tasks) == len(record.search_create_requests) == 1
    assert record.search_create_requests[0].state == "linked"
    assert runtime.search_run.state.read_run(results[0]["run_id"]).bootstrap_complete
    assert goals.status(goal.goal_plus_id).spec_draft is None
    before = record.model_dump()
    with pytest.raises(ValueError, match="SearchCreateConflict"):
        tools.goal_plus_search_create(goal.goal_plus_id, frozen, "one", results[0]["run_id"])
    assert goals.status(goal.goal_plus_id).model_dump() == before


def test_new_goal_search_uses_short_run_id_with_legacy_runs_present(creation):
    goal, goals, runtime, tools, frozen = creation
    legacy = runtime.root_dir / "runs" / "run_cff1b1216dbb3770a05d3d0bc6f8e4e5"
    legacy.mkdir(parents=True)

    result = tools.goal_plus_search_create(goal.goal_plus_id, frozen, "new-task")

    assert result["run_id"] == "run_0001"
    assert not result["run_id"].startswith("run_cff1b1216")


def test_link_failure_retries_exact_created_run(creation, monkeypatch):
    goal, goals, runtime, tools, frozen = creation
    link = goals.goal_search.link_search_run
    monkeypatch.setattr(goals.goal_search, "link_search_run", lambda *a, **kw: (_ for _ in ()).throw(OSError("link unavailable")))
    with pytest.raises(OSError, match="link unavailable"):
        tools.goal_plus_search_create(goal.goal_plus_id, frozen, "retry")
    pending = goals.status(goal.goal_plus_id).search_create_requests[0]
    assert pending.state == "failed" and "link unavailable" in pending.error
    with pytest.raises(RuntimeError, match="SearchLinkRequired"):
        runtime.plan_next(pending.run_id, 1)
    monkeypatch.setattr(goals.goal_search, "link_search_run", link)
    assert tools.goal_plus_search_create(goal.goal_plus_id, frozen, "retry")["run_id"] == pending.run_id
    assert len(goals.status(goal.goal_plus_id).search_tasks) == 1


def test_revision_change_after_bootstrap_never_links_old_request(creation, monkeypatch):
    goal, goals, runtime, tools, frozen = creation
    create = runtime.search_run.create_run

    def change_revision(*args, **kwargs):
        result = create(*args, **kwargs)
        goals.update_goal(goal.goal_plus_id, "Revised target", expected_revision=goal.goal_revision)
        return result

    monkeypatch.setattr(runtime.search_run, "create_run", change_revision)
    with pytest.raises(RuntimeError, match="SearchCreateStale"):
        tools.goal_plus_search_create(goal.goal_plus_id, frozen, "stale")
    record = goals.status(goal.goal_plus_id)
    assert not record.search_tasks
    assert record.search_create_requests[0].state == "failed"
    with pytest.raises(RuntimeError, match="SearchCreateStale"):
        tools.goal_plus_search_create(goal.goal_plus_id, frozen, "stale")


def test_freeze_enforces_explicit_config_without_ready_draft(tmp_path):
    project = make_project(tmp_path)
    root = tmp_path / ".gp"
    goal = start_host_verified_goal(root, project)
    goals = FileGoalPlusRuntime(root)
    record = goals.status(goal.goal_plus_id)
    record.command_config.max_parallel = 2
    record.goal_revisions[-1].command_config = record.command_config
    goals.state.write_goal(record)
    tools = SearchTools(FileSearchRuntime(root), goals)
    before = goals.status(goal.goal_plus_id).model_dump()
    with pytest.raises(ValueError, match="explicit command config max_parallel"):
        tools.goal_plus_search_freeze_spec(goal.goal_plus_id, spec_for(project, max_parallel=1), ["evaluator.py"])
    assert goals.status(goal.goal_plus_id).model_dump() == before
    assert not (root / "specs").exists()
