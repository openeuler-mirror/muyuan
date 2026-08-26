from __future__ import annotations

import json
from pathlib import Path

import pytest

from goal_plus.models import SearchSpec


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "relative_path",
    (
        "tests/fixtures/k_module_problem/search-spec.json",
        "tests/fixtures/signal_processing/search-spec.json",
        "tests/fixtures/signal_processing/multi-model-search-spec.json",
        "tests/fixtures/swe_bench_20212/search-spec.json",
    ),
)
def test_search_spec_fixture_is_loadable(relative_path: str) -> None:
    payload = json.loads((ROOT / relative_path).read_text(encoding="utf-8"))
    spec = SearchSpec.model_validate(payload)

    assert not Path(spec.source_path).is_absolute()
    assert (ROOT / spec.source_path).is_dir()
    assert spec.strategy.orchestration_mode == "parallel_loops"
    assert spec.strategy.worker_budget is not None
