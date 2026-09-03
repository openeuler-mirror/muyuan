from __future__ import annotations

from pathlib import Path

import goal_plus.models as core_models
from goal_plus.adaptive_search import AdaptiveSearch
from goal_plus.adaptive_search.allocation import AllocationDecision, ExpansionSource
from goal_plus.adaptive_search.policies import ResolvedSearchSchedulerPolicies
from goal_plus.adaptive_search.scheduler import ScheduleTask, SearchSchedulerSpec
from goal_plus.runtime import FileSearchRuntime


def test_runtime_exposes_the_adaptive_search_facade(tmp_path: Path) -> None:
    runtime = FileSearchRuntime(tmp_path / ".gp")

    assert isinstance(runtime._adaptive_search, AdaptiveSearch)


def test_adaptive_models_are_bound_into_public_search_schemas() -> None:
    for model in (
        core_models.StrategySpec,
        core_models.SearchSpec,
        core_models.FrozenSpec,
        core_models.CandidateTask,
        core_models.ScoreReport,
        core_models.IterationRecord,
        core_models.CandidateRecord,
        SearchSchedulerSpec,
        ScheduleTask,
        AllocationDecision,
        ExpansionSource,
        ResolvedSearchSchedulerPolicies,
    ):
        assert model.__pydantic_complete__
