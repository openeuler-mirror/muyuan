from __future__ import annotations

import ast
from importlib.util import find_spec
import inspect
from pathlib import Path

import goal_plus.models as core_models
import goal_plus.runtime as runtime_module
import goal_plus.adaptive_search.adaptive_search as adaptive_search_module
import goal_plus.adaptive_search.allocation as allocation
import goal_plus.adaptive_search.policies as policies
import goal_plus.adaptive_search.scheduler as scheduler
from goal_plus.runtime import FileSearchRuntime
from goal_plus.adaptive_search import AdaptiveSearch


def test_adaptive_search_has_four_business_modules() -> None:
    package_dir = Path(scheduler.__file__).parent
    assert {path.name for path in package_dir.glob("*.py")} == {
        "__init__.py",
        "allocation.py",
        "policies.py",
        "adaptive_search.py",
        "scheduler.py",
    }
    for name in (
        "allocation_service",
        "backup",
        "component_hash",
        "graph",
        "observability",
        "reward",
        "runtime_extension",
        "schedule_tasks",
        "scheduler_agent",
        "scheduler_executor",
        "schema_binding",
        "settlement",
        "spec",
        "store",
        "value_agent",
        "value_executor",
        "value_tasks",
        "value_tree",
    ):
        assert find_spec(f"goal_plus.adaptive_search.{name}") is None
    assert find_spec("goal_plus.search_scheduler") is None


def test_business_interfaces_have_single_owners(tmp_path: Path) -> None:
    assert scheduler.ScheduleTask.__module__ == scheduler.__name__
    assert scheduler.SchedulerManager.__module__ == scheduler.__name__
    assert scheduler.SearchSchedulerSpec.__module__ == scheduler.__name__
    assert allocation.AllocationDecision.__module__ == allocation.__name__
    assert allocation.AllocationExecutor.__module__ == allocation.__name__
    assert allocation.ExpansionSource.__module__ == allocation.__name__
    assert policies.ResolvedSearchSchedulerPolicies.__module__ == policies.__name__
    assert policies.SchedulerPolicyContextInput.__module__ == policies.__name__
    assert adaptive_search_module.SearchSchedulerViews.__module__ == (
        adaptive_search_module.__name__
    )
    runtime = FileSearchRuntime(tmp_path / ".gp")
    assert isinstance(runtime._adaptive_search, AdaptiveSearch)


def test_scheduler_and_allocation_own_only_their_state() -> None:
    scheduler_source = inspect.getsource(scheduler)
    allocation_source = inspect.getsource(allocation)
    policy_source = inspect.getsource(policies)
    assert '"schedule-tasks"' in scheduler_source
    assert '"allocation-decisions"' not in scheduler_source
    assert '"allocation-decisions"' in allocation_source
    assert '"schedule-tasks"' not in allocation_source
    assert "ScheduleTask" not in allocation_source
    assert "write_allocation_decision" not in scheduler_source
    assert "candidate_history" not in scheduler_source
    assert "peer_frontiers" not in scheduler_source
    assert "build_scheduler_policy_contexts" in scheduler_source
    assert "def build_scheduler_policy_contexts" in policy_source


def test_runtime_is_the_only_cross_module_coordinator() -> None:
    source = inspect.getsource(adaptive_search_module)
    assert "SchedulerManager" in source
    assert "AllocationExecutor" in source
    assert "AllocationTrigger" in source
    assert "RewardedSource" in source
    assert "._allocation.record(" in source
    assert "._scheduler.bind_decision(" in source


def test_settlement_hook_only_registers_schedule_tasks() -> None:
    signature = inspect.signature(AdaptiveSearch.settle_iteration)
    assert list(signature.parameters) == [
        "self",
        "run",
        "frozen",
        "record",
        "iteration",
    ]
    assert signature.return_annotation in {None, "None"}


def test_scheduler_has_no_persisted_wait_mode_or_legacy_value_shims() -> None:
    scheduler_source = inspect.getsource(scheduler)
    model_source = inspect.getsource(core_models)
    assert "await_result" not in scheduler_source
    assert "migrate_legacy_allocation_flag" not in scheduler_source
    for legacy_name in (
        "drop_legacy_reward_evaluation",
        "drop_legacy_node_value_cache",
        "value_task_id",
        "value_backup_event_id",
    ):
        assert legacy_name not in model_source


def test_core_models_keep_one_way_scheduler_schema_binding() -> None:
    tree = ast.parse(inspect.getsource(core_models))
    assert not any(
        isinstance(node, ast.ImportFrom)
        and (node.module or "").startswith("goal_plus.adaptive_search")
        for node in ast.walk(tree)
    )
    for model in (
        core_models.StrategySpec,
        core_models.SearchSpec,
        core_models.FrozenSpec,
        core_models.CandidateTask,
        core_models.ScoreReport,
        core_models.IterationRecord,
        core_models.CandidateRecord,
    ):
        assert model.__pydantic_complete__


def test_external_modules_use_scheduler_facade_and_runtime_hooks() -> None:
    source_root = Path(runtime_module.__file__).parent
    scheduler_root = source_root / "adaptive_search"
    direct_internal_imports: list[str] = []
    private_facade_access: list[str] = []
    for path in source_root.rglob("*.py"):
        if scheduler_root in path.parents:
            continue
        source = path.read_text(encoding="utf-8")
        if path.name != "__init__.py" and "goal_plus.adaptive_search." in source:
            direct_internal_imports.append(str(path.relative_to(source_root)))
        if path.name != "runtime.py" and "._adaptive_search" in source:
            private_facade_access.append(str(path.relative_to(source_root)))
    assert direct_internal_imports == []
    assert private_facade_access == []
