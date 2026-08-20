#!/usr/bin/env python3
"""Summarize comparable Codex/Pi Goal Plus evidence for this Torch CPU target."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
import math
from pathlib import Path
from typing import Any


PASS = "pass"
FAIL = "fail"
NOT_APPLICABLE = "not_applicable"
UNAVAILABLE = "unavailable"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _check(
    condition: bool,
    *,
    observed: Any,
    expected: Any,
    reason: str | None = None,
) -> dict[str, Any]:
    result = {
        "status": PASS if condition else FAIL,
        "observed": observed,
        "expected": expected,
    }
    if reason:
        result["reason"] = reason
    return result


def _informational(status: str, *, observed: Any, reason: str) -> dict[str, Any]:
    return {"status": status, "observed": observed, "reason": reason}


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _candidate_iterations(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    iterations = candidate.get("iterations")
    return iterations if isinstance(iterations, list) else []


def _read_position(record: dict[str, Any]) -> int | None:
    if "candidate_verifier_iteration" in record:
        value = record.get("candidate_verifier_iteration")
        if value is None:
            return 0
        if isinstance(value, int) and value >= 0:
            return value
    for key in (
        "verifier_iteration",
        "verifier_run_count",
        "verifier_runs",
        "run_verifier_iteration_count",
    ):
        value = record.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return None


def _metric_values(iteration: dict[str, Any]) -> list[dict[str, Any]]:
    metrics = iteration.get("metrics")
    if not isinstance(metrics, dict):
        return []
    return [value for value in metrics.values() if isinstance(value, dict)]


def analyze_run(
    root: Path,
    run_id: str,
    *,
    expected_host: str | None = None,
    expected_candidates: int = 2,
    expected_runtime_seconds: int = 600,
) -> dict[str, Any]:
    root = root.resolve()
    run_dir = root / "runs" / run_id
    run_path = run_dir / "run.json"
    if not run_path.is_file():
        raise FileNotFoundError(f"run record not found: {run_path}")
    run = _load_json(run_path)
    plans = [_load_json(path) for path in sorted((run_dir / "plans").glob("*.json"))]
    candidates = [
        _load_json(path)
        for path in sorted((run_dir / "candidates").glob("*/candidate.json"))
    ]
    sessions = [
        _load_json(path)
        for path in sorted((run_dir / "agent_sessions").glob("*.json"))
    ]
    frozen_path = root / "specs" / str(run.get("frozen_spec_id")) / "frozen_spec.json"
    frozen = _load_json(frozen_path) if frozen_path.is_file() else {}
    spec = frozen.get("spec") if isinstance(frozen.get("spec"), dict) else {}

    plan = plans[0] if len(plans) == 1 else {}
    strategy = plan.get("strategy") if isinstance(plan.get("strategy"), dict) else {}
    configured_host = strategy.get("worker_host")
    expected_host = expected_host or (
        configured_host if isinstance(configured_host, str) else None
    )
    candidate_ids = [str(candidate.get("candidate_id")) for candidate in candidates]
    session_ids_by_candidate: dict[str, list[str]] = defaultdict(list)
    for session in sessions:
        session_ids_by_candidate[str(session.get("candidate_id"))].append(
            str(session.get("agent_session_id"))
        )

    all_iterations = [
        iteration
        for candidate in candidates
        for iteration in _candidate_iterations(candidate)
    ]
    passing_by_candidate = {
        str(candidate.get("candidate_id")): sum(
            iteration.get("process_passed") is True
            for iteration in _candidate_iterations(candidate)
        )
        for candidate in candidates
    }
    bound_iteration_sessions = {
        str(candidate.get("candidate_id")): sorted(
            {
                str(iteration.get("agent_session_id"))
                for iteration in _candidate_iterations(candidate)
                if iteration.get("agent_session_id")
            }
        )
        for candidate in candidates
    }
    same_session_per_candidate = all(
        len(session_ids_by_candidate[candidate_id]) == 1
        and bound_iteration_sessions[candidate_id]
        == session_ids_by_candidate[candidate_id]
        for candidate_id in candidate_ids
    ) and bool(candidate_ids)

    annotations = [
        _load_json(path)
        for path in sorted(
            (run_dir / "candidates").glob("*/evidence-annotations/*.json")
        )
    ]
    annotation_states = Counter(str(item.get("state", "missing")) for item in annotations)
    annotation_keys = {
        (str(item.get("candidate_id")), item.get("iteration")) for item in annotations
    }
    iteration_keys = {
        (str(candidate.get("candidate_id")), iteration.get("iteration"))
        for candidate in candidates
        for iteration in _candidate_iterations(candidate)
    }
    completed_annotations = sum(
        item.get("state") == "completed" and isinstance(item.get("view"), dict)
        for item in annotations
    )

    reads = [
        (session, record)
        for session in sessions
        for record in (
            session.get("global_evidence_reads")
            if isinstance(session.get("global_evidence_reads"), list)
            else []
        )
        if isinstance(record, dict)
    ]
    read_sources = Counter(str(record.get("source", "missing")) for _, record in reads)
    missing_read_positions = sum(_read_position(record) is None for _, record in reads)
    sessions_without_reads = [
        str(session.get("agent_session_id"))
        for session in sessions
        if not session.get("global_evidence_reads")
    ]
    refresh_gaps: list[int] = []
    for session in sessions:
        verifier_runs = int((session.get("counters") or {}).get("verifier_runs", 0))
        positions = sorted(
            {
                position
                for record in session.get("global_evidence_reads", [])
                if isinstance(record, dict)
                if (position := _read_position(record)) is not None
            }
        )
        if positions:
            boundaries = [0, *positions, verifier_runs]
            refresh_gaps.extend(
                right - left for left, right in zip(boundaries, boundaries[1:])
            )
    detail_read_count = sum(
        len(session.get("evidence_detail_reads") or []) for session in sessions
    )

    shared_config = spec.get("shared_dir") if isinstance(spec.get("shared_dir"), dict) else {}
    shared_enabled = shared_config.get("enabled") is True
    staged_entries = sum(
        len(iteration.get("shared_tool_staged_entries") or [])
        for iteration in all_iterations
    )
    published_tools = sum(
        len(iteration.get("shared_tools") or []) for iteration in all_iterations
    )
    consumed_entries = sum(
        len(iteration.get("shared_tool_consumed_entries") or [])
        for iteration in all_iterations
    )
    tool_views = sum(
        len((item.get("view") or {}).get("tool_views") or []) for item in annotations
    )
    shared_fields_observed = all(
        "shared_tool_publish_status" in iteration for iteration in all_iterations
    ) if all_iterations else False

    selected_candidate_id = run.get("selected_candidate_id")
    selected = next(
        (
            candidate
            for candidate in candidates
            if candidate.get("candidate_id") == selected_candidate_id
        ),
        None,
    )
    promotion_evidence = (
        selected.get("promotion_evidence")
        if isinstance(selected, dict)
        and isinstance(selected.get("promotion_evidence"), dict)
        else {}
    )
    selected_score = run.get("selected_score")
    selection_valid = (
        selected is not None
        and isinstance(selected_score, (int, float))
        and math.isfinite(float(selected_score))
        and isinstance(run.get("selected_iteration"), int)
    )

    thread_counts = [
        metric.get("torch_num_threads")
        for iteration in all_iterations
        for metric in _metric_values(iteration)
        if "torch_num_threads" in metric
    ]
    promotion_results = (
        (selected or {}).get("promotion_report", {}).get("verifier_results", [])
        if isinstance((selected or {}).get("promotion_report"), dict)
        else []
    )
    promotion_thread_counts = [
        result.get("metrics", {}).get("torch_num_threads")
        for result in promotion_results
        if isinstance(result, dict) and isinstance(result.get("metrics"), dict)
        if "torch_num_threads" in result.get("metrics", {})
    ]
    thread_counts.extend(promotion_thread_counts)

    session_hosts = sorted({str(session.get("host")) for session in sessions})
    iteration_model_missing = sum(
        not isinstance(iteration.get("model_provenance"), dict)
        or not iteration.get("exact_model_ref")
        for iteration in all_iterations
    )
    provenance_valid = (
        bool(expected_host)
        and configured_host == expected_host
        and session_hosts == [expected_host]
        and all(isinstance(session.get("model_provenance"), dict) for session in sessions)
        and iteration_model_missing == 0
    )

    timestamps = [
        timestamp
        for timestamp in [
            _timestamp(run.get("created_at")),
            *[_timestamp(session.get("updated_at")) for session in sessions],
            *[_timestamp(item.get("updated_at")) for item in annotations],
            _timestamp(promotion_evidence.get("created_at")),
        ]
        if timestamp is not None
    ]
    duration_seconds = (
        round((max(timestamps) - min(timestamps)).total_seconds(), 3)
        if len(timestamps) >= 2
        else None
    )

    worker_budget = strategy.get("worker_budget")
    configured_runtime = (
        worker_budget.get("max_runtime_seconds")
        if isinstance(worker_budget, dict)
        else None
    )
    metric_direction = spec.get("metric_direction", "maximize")
    best_by_candidate: dict[str, float | None] = {}
    for candidate in candidates:
        scores = [
            float(iteration["score"])
            for iteration in _candidate_iterations(candidate)
            if iteration.get("process_passed") is True
            and isinstance(iteration.get("score"), (int, float))
            and math.isfinite(float(iteration["score"]))
        ]
        best_by_candidate[str(candidate.get("candidate_id"))] = (
            (min(scores) if metric_direction == "minimize" else max(scores))
            if scores
            else None
        )
    checks: dict[str, dict[str, Any]] = {
        "one_initial_plan": _check(
            len(plans) == 1,
            observed=len(plans),
            expected=1,
        ),
        "parallel_loops": _check(
            strategy.get("orchestration_mode") == "parallel_loops",
            observed=strategy.get("orchestration_mode"),
            expected="parallel_loops",
        ),
        "candidate_count": _check(
            len(candidates) == expected_candidates,
            observed=len(candidates),
            expected=expected_candidates,
        ),
        "agent_session_count": _check(
            len(sessions) == expected_candidates,
            observed=len(sessions),
            expected=expected_candidates,
        ),
        "same_candidate_session": _check(
            same_session_per_candidate,
            observed={
                candidate_id: {
                    "sessions": session_ids_by_candidate[candidate_id],
                    "iteration_sessions": bound_iteration_sessions[candidate_id],
                }
                for candidate_id in candidate_ids
            },
            expected="one durable agent_session_id per candidate across all iterations",
        ),
        "worker_runtime_budget": _check(
            configured_runtime == expected_runtime_seconds,
            observed=configured_runtime,
            expected=expected_runtime_seconds,
        ),
        "verifier_evidence": _check(
            bool(all_iterations)
            and all(passing_by_candidate.get(candidate_id, 0) > 0 for candidate_id in candidate_ids),
            observed={
                "iterations": len(all_iterations),
                "passing_by_candidate": passing_by_candidate,
            },
            expected="at least one passing process verifier iteration per candidate",
        ),
        "annotations_complete": _check(
            annotation_keys == iteration_keys
            and completed_annotations == len(all_iterations),
            observed={
                "iterations": len(all_iterations),
                "annotations": len(annotations),
                "completed": completed_annotations,
                "states": dict(sorted(annotation_states.items())),
            },
            expected="one completed evidence annotation with a view per iteration",
        ),
        "evidence_read_audit": _check(
            bool(reads)
            and not sessions_without_reads
            and read_sources.get("missing", 0) == 0
            and set(read_sources).issubset({"explicit", "verifier_injected"})
            and missing_read_positions == 0
            and bool(refresh_gaps)
            and max(refresh_gaps) <= 3,
            observed={
                "total": len(reads),
                "sources": dict(sorted(read_sources.items())),
                "sessions_without_reads": sessions_without_reads,
                "missing_verifier_position": missing_read_positions,
                "detail_reads": detail_read_count,
                "max_refresh_gap": max(refresh_gaps) if refresh_gaps else None,
            },
            expected=(
                "each session has durable source-tagged reads with verifier positions; "
                "refresh gaps are at most three verifier iterations; supplemental "
                "detail reads remain on demand"
            ),
        ),
        "final_selection": _check(
            selection_valid,
            observed={
                "candidate_id": selected_candidate_id,
                "iteration": run.get("selected_iteration"),
                "score": selected_score,
            },
            expected="finite verifier-backed selected candidate/iteration/score",
        ),
        "promotion": _check(
            run.get("state") == "promoted" and promotion_evidence.get("passed") is True,
            observed={
                "run_state": run.get("state"),
                "promotion_passed": promotion_evidence.get("passed"),
            },
            expected={"run_state": "promoted", "promotion_passed": True},
        ),
        "host_provenance": _check(
            provenance_valid,
            observed={
                "configured_host": configured_host,
                "session_hosts": session_hosts,
                "iterations_missing_model_provenance": iteration_model_missing,
            },
            expected=expected_host,
        ),
        "single_cpu_thread": _check(
            bool(thread_counts) and all(value == 1 for value in thread_counts),
            observed=sorted(set(thread_counts), key=lambda value: str(value)),
            expected=[1],
        ),
    }
    if shared_enabled:
        checks["shared_dir"] = _check(
            shared_fields_observed,
            observed={
                "enabled": True,
                "staged_entries": staged_entries,
                "published_tools": published_tools,
                "tool_views": tool_views,
                "consumed_entries": consumed_entries,
            },
            expected="settlement fields on every verifier iteration; activity may be zero",
        )
    else:
        checks["shared_dir"] = _informational(
            NOT_APPLICABLE,
            observed={"enabled": False},
            reason="the frozen spec disabled shared_dir; no sharing activity is inferred",
        )

    mandatory = [item for item in checks.values() if item["status"] != NOT_APPLICABLE]
    overall_status = PASS if mandatory and all(item["status"] == PASS for item in mandatory) else FAIL
    return {
        "schema_version": 1,
        "run_id": run_id,
        "overall_status": overall_status,
        "host": expected_host,
        "duration_seconds": duration_seconds,
        "counts": {
            "plans": len(plans),
            "candidates": len(candidates),
            "agent_sessions": len(sessions),
            "verifier_iterations": len(all_iterations),
            "annotations": len(annotations),
            "global_evidence_reads": len(reads),
            "evidence_detail_reads": detail_read_count,
        },
        "models": [
            {
                "candidate_id": session.get("candidate_id"),
                "model": (session.get("selected_model") or {}).get("model"),
                "provider": (session.get("model_provenance") or {}).get("provider"),
                "resume_dispatches": (session.get("counters") or {}).get(
                    "resume_dispatches", 0
                ),
            }
            for session in sessions
        ],
        "score": {
            "selected_candidate_id": selected_candidate_id,
            "selected_iteration": run.get("selected_iteration"),
            "selected_score": selected_score,
            "best_by_candidate": best_by_candidate,
        },
        "checks": checks,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(".gp"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-host", choices=("codex", "pi-rpc"))
    parser.add_argument("--expected-candidates", type=int, default=2)
    parser.add_argument("--expected-runtime-seconds", type=int, default=600)
    parser.add_argument(
        "--require-pass",
        action="store_true",
        help="exit 1 after printing JSON when any applicable check fails",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    result = analyze_run(
        args.root,
        args.run_id,
        expected_host=args.expected_host,
        expected_candidates=args.expected_candidates,
        expected_runtime_seconds=args.expected_runtime_seconds,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 1 if args.require_pass and result["overall_status"] != PASS else 0


if __name__ == "__main__":
    raise SystemExit(main())
