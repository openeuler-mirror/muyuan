"""指标归一化与 objective（主指标）解析。"""
from __future__ import annotations

from typing import Any, Mapping

from harness_optimizer.schema import HarnessSpec

from .errors import ComponentModeError
from .util import _as_float

def _normalise_metrics(
    metrics: Mapping[str, Any],
    duration_s: float,
    spec: HarnessSpec | None = None,
) -> dict[str, Any]:
    result = {str(k): v for k, v in metrics.items()}
    aliases = {
        "total_tokens": "tokens",
        "token_count": "tokens",
        "duration_s": "time_s",
        "time": "time_s",
        "pass_rate": "accuracy",
        "pass_at_1": "accuracy",
        "score": "accuracy",
    }
    for source, target in aliases.items():
        if target not in result and source in result:
            result[target] = result[source]
    if "time_s" not in result:
        result["time_s"] = round(duration_s, 6)
    result.setdefault("accuracy_verified", "accuracy" in result)
    return result


def _objective(spec: HarnessSpec) -> tuple[str, str]:
    """解析主指标，不发明 accuracy 别名。"""
    config = dict(spec.adapter.config)
    raw = config.get("objective", spec.metadata.get("objective"))
    objective = raw if isinstance(raw, Mapping) else {}
    metric = objective.get("metric", config.get("primary_metric"))
    if not isinstance(metric, str) or not metric.strip():
        metric = "accuracy"
    direction = str(objective.get("direction", config.get("metric_direction", "maximize"))).lower()
    if direction not in {"maximize", "minimize"}:
        raise ComponentModeError(f"unsupported metric direction: {direction}")
    return metric.strip(), direction


def _primary_metric(metrics: Mapping[str, Any], spec: HarnessSpec) -> tuple[str, float | None]:
    metric, _ = _objective(spec)
    return metric, _as_float(metrics.get(metric))


def _metrics_summary(metrics: Any, spec: HarnessSpec | None = None) -> str:
    if not isinstance(metrics, Mapping):
        return "n/a"
    metric = _objective(spec)[0] if spec is not None else "accuracy"
    keys = [metric, "tokens", "time_s", "f1", "runner_ok"]
    parts = [f"{k}={metrics.get(k)}" for k in keys if metrics.get(k) is not None]
    return " ".join(parts) or "n/a"

