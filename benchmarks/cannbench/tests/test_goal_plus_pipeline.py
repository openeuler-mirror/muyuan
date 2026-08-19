from __future__ import annotations

import json
from pathlib import Path

import pytest

from auto_pipeline.converter.registry import available_converters
from auto_pipeline.generator.registry import available_generators

import muyuan_cannbench.generator as generator_module
from muyuan_cannbench.generator import GoalPlusGenerator
from muyuan_cannbench.registration import register_integrations
from muyuan_cannbench.smoke import run_non_npu_smoke
from muyuan_cannbench.paths import MUYUAN_ROOT


def test_registration_exposes_goal_plus_boundaries() -> None:
    register_integrations()
    assert "goal-plus" in available_generators()
    assert "goal-plus -> cann" in available_converters()


def test_non_npu_pipeline_runs_goal_plus_selection_and_conversion(tmp_path: Path) -> None:
    output = tmp_path / "run"
    summary = run_non_npu_smoke(output_root=output)

    assert summary["status"] == "success"
    assert summary["eval_ok"] is True
    assert summary["npu_executed"] is False
    assert summary["goal_plus"]["candidate_count"] == 2
    assert summary["goal_plus"]["selected_candidate_id"] == "c002"
    assert summary["goal_plus"]["selected_score"] == 2.0
    assert (output / "submission" / "build.sh").is_file()
    assert (output / "submission" / "setup.py").is_file()
    assert (output / "submission" / "cann_bench" / "mish.py").is_file()
    report = json.loads((output / "reports" / "non_npu_smoke.json").read_text(encoding="utf-8"))
    assert report["kernel_eval_executed"] is False


def test_npu_config_keeps_real_verifier_and_device_override() -> None:
    generator = GoalPlusGenerator(
        {
            "config_path": str(
                MUYUAN_ROOT
                / "benchmarks"
                / "cannbench"
                / "configs"
                / "goal-plus-triton-ascend.npu.yaml"
            ),
            "codegen_target": "triton_ascend",
            "device_id": 3,
            "verify_timeout": 900,
        }
    )

    assert generator.config.driver == "pi-rpc"
    assert generator.config.verifier == "cann"
    assert generator.config.max_parallel == 4
    assert generator.config.max_candidates == 4
    assert generator.config.device_id == 3
    assert generator.config.verify_timeout == 900


def test_pi_driver_uses_one_goal_plus_pool_for_k4(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generator = GoalPlusGenerator(
        {
            "config_path": str(
                MUYUAN_ROOT
                / "benchmarks"
                / "cannbench"
                / "configs"
                / "goal-plus-triton-ascend.npu.yaml"
            ),
            "codegen_target": "triton_ascend",
        }
    )
    tasks = [{"candidate_id": f"c{index:03d}"} for index in range(1, 5)]
    calls: dict[str, object] = {}
    wait_calls: list[dict[str, object]] = []
    waits = iter(
        [
            {
                "events": [
                    {"candidate_id": "c002", "kind": "candidate_ready"},
                    {"candidate_id": "c004", "kind": "candidate_ready"},
                ],
                "active_count": 2,
            },
            {
                "events": [
                    {"candidate_id": "c001", "kind": "candidate_ready"},
                    {"candidate_id": "c003", "kind": "candidate_ready"},
                ],
                "active_count": 0,
            },
        ]
    )

    def fake_open(**kwargs: object) -> dict[str, object]:
        calls["open"] = kwargs
        return {"pool_id": "pool_test"}

    def fake_wait(**kwargs: object) -> dict[str, object]:
        wait_calls.append(kwargs)
        return next(waits)

    def fake_close(**kwargs: object) -> dict[str, object]:
        calls["close"] = kwargs
        return {"state": "closed", "active_count": 0}

    monkeypatch.setattr(generator_module, "open_pi_search_pool", fake_open)
    monkeypatch.setattr(generator_module, "wait_any_pi_search_pool", fake_wait)
    monkeypatch.setattr(generator_module, "close_pi_search_pool", fake_close)

    summary = generator._drive_pi("run_test", tasks, tmp_path)

    opened = calls["open"]
    assert isinstance(opened, dict)
    assert opened["candidate_ids"] == ["c001", "c002", "c003", "c004"]
    assert opened["max_parallel"] == 4
    assert len(wait_calls) == 2
    assert opened["worker_budgets"] == {
        candidate_id: generator.config.worker_budget
        for candidate_id in ("c001", "c002", "c003", "c004")
    }
    assert calls["close"] == {
        "root_dir": tmp_path,
        "pool_id": "pool_test",
        "mode": "drain",
        "timeout_seconds": 30,
    }
    assert summary == {
        "pool_id": "pool_test",
        "state": "closed",
        "max_parallel": 4,
        "candidate_ready": ["c001", "c002", "c003", "c004"],
    }
