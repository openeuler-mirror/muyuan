from __future__ import annotations

import json
from pathlib import Path

from auto_pipeline.converter.registry import available_converters
from auto_pipeline.generator.registry import available_generators

from muyuan_cannbench.registration import register_integrations
from muyuan_cannbench.smoke import run_non_npu_smoke
from muyuan_cannbench.generator import GoalPlusGenerator
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
    assert generator.config.max_candidates == 1
    assert generator.config.device_id == 3
    assert generator.config.verify_timeout == 900
