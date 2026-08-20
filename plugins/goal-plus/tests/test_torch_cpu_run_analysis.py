from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]
ANALYZER_PATH = (
    ROOT
    / "examples"
    / "model-optimize"
    / "torch-cpu-target"
    / "analyze_run.py"
)
pytestmark = [pytest.mark.codex, pytest.mark.pi]


def _analyzer() -> ModuleType:
    spec = importlib.util.spec_from_file_location("torch_cpu_run_analyzer", ANALYZER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_complete_run(root: Path, host: str, *, shared_dir: bool = False) -> str:
    run_id = f"run_{host.replace('-', '_')}"
    run_dir = root / "runs" / run_id
    _write_json(
        root / "specs" / "spec_test" / "frozen_spec.json",
        {
            "frozen_spec_id": "spec_test",
            "spec": {
                "metric_name": "tokens_per_second",
                "shared_dir": {"enabled": shared_dir},
            },
        },
    )
    _write_json(
        run_dir / "run.json",
        {
            "run_id": run_id,
            "frozen_spec_id": "spec_test",
            "created_at": "2026-08-20T00:00:00Z",
            "state": "promoted",
            "selected_candidate_id": "c001",
            "selected_iteration": 1,
            "selected_score": 12.0,
        },
    )
    _write_json(
        run_dir / "plans" / "plan_001.json",
        {
            "plan_id": "plan_001",
            "requested_k": 2,
            "planned_k": 2,
            "strategy": {
                "orchestration_mode": "parallel_loops",
                "worker_host": host,
                "worker_budget": {"max_runtime_seconds": 600},
            },
        },
    )
    for index in (1, 2):
        candidate_id = f"c{index:03d}"
        agent_session_id = f"agent_{index:03d}"
        iteration = {
            "iteration": 1,
            "agent_session_id": agent_session_id,
            "process_passed": True,
            "score": float(13 - index),
            "exact_model_ref": f"model-{index}",
            "model_provenance": {
                "provider": "test",
                "selected_model": f"model-{index}",
            },
            "metrics": {
                "torch_cpu_tokens_per_second": {
                    "tokens_per_second": float(13 - index),
                    "torch_num_threads": 1,
                    "valid": True,
                }
            },
            "shared_tool_publish_status": "not_staged",
            "shared_tool_staged_entries": [],
            "shared_tools": [],
            "shared_tool_consumed_entries": [],
        }
        candidate = {
            "candidate_id": candidate_id,
            "iterations": [iteration],
            "score_report": {"aggregate_score": float(13 - index)},
        }
        if index == 1:
            candidate.update(
                {
                    "promotion_evidence": {
                        "passed": True,
                        "created_at": "2026-08-20T00:10:00Z",
                    },
                    "promotion_report": {
                        "verifier_results": [
                            {"metrics": {"torch_num_threads": 1, "valid": True}}
                        ]
                    },
                }
            )
        _write_json(
            run_dir / "candidates" / candidate_id / "candidate.json",
            candidate,
        )
        _write_json(
            run_dir / "candidates" / candidate_id / "evidence-annotations" / "iteration-0001.json",
            {
                "candidate_id": candidate_id,
                "iteration": 1,
                "state": "completed",
                "updated_at": "2026-08-20T00:09:00Z",
                "view": {"description": "objective diff", "tool_views": []},
            },
        )
        _write_json(
            run_dir / "agent_sessions" / f"{agent_session_id}.json",
            {
                "agent_session_id": agent_session_id,
                "candidate_id": candidate_id,
                "host": host,
                "updated_at": "2026-08-20T00:09:30Z",
                "selected_model": {"model": f"model-{index}"},
                "model_provenance": {
                    "provider": "test",
                    "selected_model": f"model-{index}",
                },
                "counters": {"verifier_runs": 1, "resume_dispatches": 1},
                "global_evidence_reads": [
                    {
                        "source": "explicit",
                        "candidate_verifier_iteration": None,
                        "run_verifier_iteration_count": 0,
                        "evidence_count": 0,
                    },
                    {
                        "source": "verifier_injected",
                        "candidate_verifier_iteration": 1,
                        "run_verifier_iteration_count": 2,
                        "evidence_count": 2,
                    },
                ],
                "evidence_detail_reads": [],
            },
        )
    return run_id


@pytest.mark.parametrize("host", ["codex", "pi-rpc"])
def test_analyzer_accepts_portable_codex_and_pi_evidence(
    tmp_path: Path,
    host: str,
) -> None:
    root = tmp_path / ".gp"
    run_id = _write_complete_run(root, host)

    result = _analyzer().analyze_run(root, run_id, expected_host=host)

    assert result["overall_status"] == "pass"
    assert result["counts"] == {
        "plans": 1,
        "candidates": 2,
        "agent_sessions": 2,
        "verifier_iterations": 2,
        "annotations": 2,
        "global_evidence_reads": 4,
        "evidence_detail_reads": 0,
    }
    assert result["checks"]["same_candidate_session"]["status"] == "pass"
    assert result["checks"]["evidence_read_audit"]["status"] == "pass"
    assert result["checks"]["shared_dir"]["status"] == "not_applicable"


def test_analyzer_reports_enabled_shared_dir_without_inventing_activity(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".gp"
    run_id = _write_complete_run(root, "codex", shared_dir=True)

    result = _analyzer().analyze_run(root, run_id, expected_host="codex")

    assert result["overall_status"] == "pass"
    assert result["checks"]["shared_dir"] == {
        "status": "pass",
        "observed": {
            "enabled": True,
            "staged_entries": 0,
            "published_tools": 0,
            "tool_views": 0,
            "consumed_entries": 0,
        },
        "expected": "settlement fields on every verifier iteration; activity may be zero",
    }


def test_analyzer_fails_missing_read_audit_and_promotion(tmp_path: Path) -> None:
    root = tmp_path / ".gp"
    run_id = _write_complete_run(root, "pi-rpc")
    run_dir = root / "runs" / run_id
    session_path = run_dir / "agent_sessions" / "agent_001.json"
    session = json.loads(session_path.read_text(encoding="utf-8"))
    session["global_evidence_reads"] = []
    _write_json(session_path, session)
    candidate_path = run_dir / "candidates" / "c001" / "candidate.json"
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidate["promotion_evidence"]["passed"] = False
    _write_json(candidate_path, candidate)

    result = _analyzer().analyze_run(root, run_id, expected_host="pi-rpc")

    assert result["overall_status"] == "fail"
    assert result["checks"]["evidence_read_audit"]["status"] == "fail"
    assert result["checks"]["promotion"]["status"] == "fail"
