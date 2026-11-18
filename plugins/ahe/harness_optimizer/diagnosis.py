"""将 debugger 输出归一化为稳定、有界的诊断合同。"""
from __future__ import annotations

import math
from typing import Any, Mapping


DIAGNOSIS_SCHEMA_VERSION = 1
DIAGNOSIS_TYPE = "ahe.diagnosis"
_MAX_TEXT = 4000
_MAX_LIST_ITEMS = 32
_MAX_EVIDENCE_ITEMS = 32


def _text(value: Any, *, limit: int = _MAX_TEXT) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
    else:
        text = str(value).strip()
    return text[:limit]


def _first_text(*values: Any) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def _string_list(value: Any, *, limit: int = _MAX_LIST_ITEMS) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple, set)) else [value]
    result: list[str] = []
    for item in values:
        text = _text(item, limit=1000)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _bounded_value(value: Any, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= 2:
        return _text(value, limit=1000)
    if isinstance(value, Mapping):
        return {
            _text(key, limit=200): _bounded_value(item, depth + 1)
            for key, item in list(value.items())[:_MAX_LIST_ITEMS]
        }
    if isinstance(value, (list, tuple, set)):
        items = value if not isinstance(value, set) else sorted(value, key=str)
        return [_bounded_value(item, depth + 1) for item in list(items)[:_MAX_LIST_ITEMS]]
    return _text(value, limit=1000)


def _confidence(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return max(0.0, min(1.0, number))


def _evidence_items(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple)) else [value]
    result: list[dict[str, Any]] = []
    for index, item in enumerate(values):
        if isinstance(item, Mapping):
            evidence_id = _first_text(item.get("id"), item.get("evidence_id"), f"evidence-{index + 1}")
            kind = _first_text(item.get("kind"), item.get("type"), "other")
            detail = _first_text(item.get("detail"), item.get("description"), item.get("text"), item.get("evidence"))
            source = _first_text(item.get("source"), item.get("path"), item.get("location"))
            normalized = {"id": evidence_id, "kind": kind, "detail": detail}
            if source:
                normalized["source"] = source
        else:
            detail = _text(item, limit=2000)
            if not detail:
                continue
            normalized = {"id": f"evidence-{index + 1}", "kind": "other", "detail": detail}
        result.append(normalized)
        if len(result) >= _MAX_EVIDENCE_ITEMS:
            break
    return result


def _status(metrics: Mapping[str, Any], stage: str, trigger: str) -> str:
    status = _text(metrics.get("status")).lower()
    if metrics.get("timed_out") is True or status in {"timeout", "timed_out"}:
        return "timeout"
    if metrics.get("runner_ok") is False or metrics.get("failed") is True or status in {"failed", "error"}:
        return "failed"
    if _text(trigger).lower().startswith("candidate"):
        return "regressed"
    if _text(stage).lower().startswith("debug"):
        return "diagnostic"
    return "observed"


def normalize_diagnosis(
    analysis: Mapping[str, Any] | None,
    *,
    task: str,
    stage: str,
    candidate_id: str,
    metrics: Mapping[str, Any],
    source: str,
    trigger: str = "evaluation",
) -> dict[str, Any]:
    """返回带稳定字段的有界诊断，供变异 agent 使用。

    debugger 实现刻意可插拔，其原始 JSON 不被视为 schema。
    本函数保留有用的公共字段，同时把原始应答以 ``analysis`` 保留在
    debugger 结果中，以兼容既有消费方。
    """
    outer = _mapping(analysis)
    nested = _mapping(outer.get("diagnosis"))
    raw = dict(outer)
    raw.update(nested)
    raw_impact = _mapping(raw.get("impact"))
    root_cause = _first_text(
        raw.get("root_cause"), raw.get("cause"), raw.get("failure_reason"), raw.get("reason")
    )
    failure_modes = _string_list(raw.get("failure_modes", raw.get("failure_mode", raw.get("issues"))))
    general_mechanism = _first_text(
        raw.get("general_mechanism"), raw.get("mechanism"), raw.get("reusable_pattern"),
        raw.get("structural_fix"),
    )
    recommended_focus = _string_list(
        raw.get("recommended_focus", raw.get("recommendation", raw.get("next_steps", raw.get("actions"))))
    )
    if not recommended_focus and general_mechanism:
        recommended_focus = [general_mechanism]
    summary = _first_text(raw.get("summary"), root_cause, general_mechanism, "Debugger returned no diagnosis.")
    evidence = _evidence_items(raw.get("evidence"))
    evidence_ids = _string_list(raw.get("evidence_ids"))
    for item in evidence:
        evidence_id = item["id"]
        if evidence_id not in evidence_ids:
            evidence_ids.append(evidence_id)
    impact = {
        "performance": _first_text(raw.get("performance_impact"), raw_impact.get("performance")),
        "cost": _first_text(raw.get("cost_impact"), raw_impact.get("cost")),
        "latency": _first_text(raw.get("latency_impact"), raw_impact.get("latency")),
    }
    confidence = _confidence(raw.get("confidence"))
    risks = _string_list(raw.get("risks", raw.get("risk")))
    return {
        "type": DIAGNOSIS_TYPE,
        "schema_version": DIAGNOSIS_SCHEMA_VERSION,
        "task": _text(task, limit=2000),
        "stage": _text(stage, limit=200),
        "candidate_id": _text(candidate_id, limit=200),
        "source": _text(source, limit=200),
        "trigger": _text(trigger, limit=200),
        "status": _status(metrics, stage, trigger),
        "summary": summary,
        "root_cause": root_cause or summary,
        "failure_modes": failure_modes,
        "impact": impact,
        "metrics": _bounded_value(metrics),
        "evidence_ids": evidence_ids,
        "evidence": evidence,
        "general_mechanism": general_mechanism,
        "recommended_focus": recommended_focus,
        "risks": risks,
        "confidence": confidence,
    }


__all__ = ["DIAGNOSIS_SCHEMA_VERSION", "DIAGNOSIS_TYPE", "normalize_diagnosis"]
