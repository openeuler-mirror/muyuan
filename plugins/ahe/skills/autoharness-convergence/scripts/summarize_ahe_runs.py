#!/usr/bin/env python3
"""Summarize and classify exported AHE run directories.

The script is intentionally dependency-free. It consumes evidence.jsonl first,
then falls back to trace.jsonl evaluation events. It never changes a run.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any, Iterable


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    records.append(value)
    except OSError:
        pass
    return records


def _run_dirs(root: Path) -> list[Path]:
    if root.is_file():
        return [root.parent]
    if (root / "evidence.jsonl").exists() or (root / "trace.jsonl").exists():
        return [root]
    found = {p.parent for p in root.rglob("evidence.jsonl")}
    found.update(p.parent for p in root.rglob("trace.jsonl"))
    return sorted(found)


def _evaluations(run: Path) -> list[dict[str, Any]]:
    records = _read_jsonl(run / "evidence.jsonl")
    values = [r for r in records if r.get("kind") == "evaluation" and isinstance(r.get("metrics"), dict)]
    if not values:
        records = _read_jsonl(run / "trace.jsonl")
        for record in records:
            if record.get("event") != "evaluation":
                continue
            metrics = (record.get("payload") or {}).get("metrics")
            if isinstance(metrics, dict):
                values.append(
                    {
                        "candidate_id": record.get("candidate_id"),
                        "metrics": metrics,
                        "status": "passed" if metrics.get("runner_ok", True) else "failed",
                        "run_id": record.get("run_id"),
                        "timestamp": record.get("timestamp"),
                    }
                )

    unique: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for record in values:
        metrics = record["metrics"]
        key = (
            record.get("candidate_id"),
            record.get("run_id"),
            metrics.get("score", metrics.get("objective")),
            metrics.get("pass_rate"),
            metrics.get("duration_s", metrics.get("elapsed_s")),
            metrics.get("time_s"),
        )
        if key not in seen:
            seen.add(key)
            unique.append(record)
    return unique


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and math.isfinite(float(value)) else None


def _score(metrics: dict[str, Any]) -> float | None:
    for key in ("score", "objective", "primary_score", "native_score"):
        value = _number(metrics.get(key))
        if value is not None:
            return value
    return None


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    index = min(len(values) - 1, max(0, round((len(values) - 1) * percentile)))
    return values[index]


def summarize(run: Path, epsilon: float) -> dict[str, Any]:
    records = _evaluations(run)
    samples: list[dict[str, Any]] = []
    for record in records:
        metrics = record["metrics"]
        samples.append(
            {
                "candidate_id": record.get("candidate_id"),
                "status": record.get("status"),
                "score": _score(metrics),
                "pass_rate": _number(metrics.get("pass_rate")),
                "duration_s": _number(metrics.get("duration_s", metrics.get("elapsed_s"))),
                "tokens": _number(metrics.get("tokens")),
                "runner_ok": metrics.get("runner_ok"),
            }
        )

    valid = [sample for sample in samples if sample["score"] is not None]
    scores = [sample["score"] for sample in valid]
    if not scores:
        return {
            "run": run.name,
            "path": str(run),
            "classification": "no_progress",
            "evaluation_count": len(samples),
            "valid_evaluation_count": 0,
            "reason": "no numeric primary score",
            "samples": samples,
        }

    baseline = scores[0]
    best = scores[0]
    improvements: list[dict[str, Any]] = []
    last_best_index = 1
    for index, score in enumerate(scores, start=1):
        if score > best + epsilon:
            best = score
            last_best_index = index
            improvements.append({"index": index, "score": score})

    zero_rate = sum(score <= epsilon for score in scores) / len(scores)
    repeat_rate = sum(abs(score - baseline) <= epsilon for score in scores) / len(scores)
    failures = sum(
        sample["status"] == "failed"
        or sample["runner_ok"] is False
        for sample in samples
    )
    failure_rate = failures / len(samples) if samples else 0.0
    first_improvement = improvements[0]["index"] if improvements else None
    plateau_length = len(scores) - last_best_index + 1

    silent_success = sum(
        sample["status"] == "passed"
        and sample["runner_ok"] is False
        for sample in valid
    )
    if silent_success or (failure_rate > 0 and any(sample["runner_ok"] is False for sample in valid)):
        classification = "accounting_anomaly"
    elif len(scores) < 2:
        classification = "insufficient_data"
    elif best <= baseline + epsilon and (zero_rate >= 0.5 or repeat_rate >= 0.5 or failure_rate >= 0.5):
        classification = "no_progress"
    elif first_improvement is not None and first_improvement <= max(3, math.ceil(len(scores) * 0.35)) and zero_rate < 0.35 and failure_rate < 0.25:
        if repeat_rate >= 0.4 or plateau_length >= max(3, math.ceil(len(scores) * 0.4)):
            classification = "productive_but_wasteful"
        else:
            classification = "fast_converge"
    elif failure_rate >= 0.25 or zero_rate >= 0.35:
        classification = "unstable"
    elif best > baseline + epsilon:
        classification = "productive_but_wasteful"
    else:
        classification = "no_progress"

    durations = [sample["duration_s"] for sample in valid if sample["duration_s"] is not None]
    tokens = [sample["tokens"] for sample in valid if sample["tokens"] is not None]
    return {
        "run": run.name,
        "path": str(run),
        "classification": classification,
        "evaluation_count": len(samples),
        "valid_evaluation_count": len(valid),
        "baseline_score": baseline,
        "best_score": best,
        "first_improvement_index": first_improvement,
        "improvements": improvements,
        "last_best_index": last_best_index,
        "plateau_length": plateau_length,
        "zero_rate": round(zero_rate, 4),
        "baseline_repeat_rate": round(repeat_rate, 4),
        "failure_rate": round(failure_rate, 4),
        "median_duration_s": statistics.median(durations) if durations else None,
        "median_tokens": statistics.median(tokens) if tokens else None,
        "p90_duration_s": _percentile(durations, 0.9),
        "samples": samples,
    }


def _text(rows: Iterable[dict[str, Any]]) -> str:
    header = "classification\trun\tn\tbaseline\tbest\tfirst_improvement\tzero_rate\trepeat_rate\tfailure_rate\tplateau"
    lines = [header]
    for row in rows:
        lines.append(
            "\t".join(
                str(row.get(key, ""))
                for key in (
                    "classification",
                    "run",
                    "valid_evaluation_count",
                    "baseline_score",
                    "best_score",
                    "first_improvement_index",
                    "zero_rate",
                    "baseline_repeat_rate",
                    "failure_rate",
                    "plateau_length",
                )
            )
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="A run directory or a directory containing exported runs")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--output", type=Path, help="Optional output file; stdout is used when omitted")
    parser.add_argument("--epsilon", type=float, default=1e-9, help="Score equality tolerance")
    args = parser.parse_args()
    rows = [summarize(run, args.epsilon) for run in _run_dirs(args.root)]
    rendered = json.dumps(rows, indent=2, ensure_ascii=False) if args.format == "json" else _text(rows)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
