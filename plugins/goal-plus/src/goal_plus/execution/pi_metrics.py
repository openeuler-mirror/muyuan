"""Pi RPC usage and runtime observations shared by native sessions and workers."""

from collections.abc import Callable
from typing import Any


def _number(value: Any) -> int | float:
    return value if isinstance(value, int | float) else 0


def summarize_pi_entries(entries: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "assistantMessages": 0,
        "input": 0,
        "output": 0,
        "cacheRead": 0,
        "cacheWrite": 0,
        "costTotal": 0.0,
        "latestCacheHitRate": None,
    }
    for entry in entries:
        message = entry.get("message") or {}
        if message.get("role") != "assistant":
            continue
        usage = message.get("usage") or {}
        input_tokens = int(_number(usage.get("input")))
        output_tokens = int(_number(usage.get("output")))
        cache_read = int(_number(usage.get("cacheRead")))
        cache_write = int(_number(usage.get("cacheWrite")))
        cost = usage.get("cost") or {}
        cost_total = _number(cost.get("total")) if isinstance(cost, dict) else 0

        summary["assistantMessages"] += 1
        summary["input"] += input_tokens
        summary["output"] += output_tokens
        summary["cacheRead"] += cache_read
        summary["cacheWrite"] += cache_write
        summary["costTotal"] += float(cost_total)

        prompt_tokens = input_tokens + cache_read + cache_write
        summary["latestCacheHitRate"] = (
            (cache_read / prompt_tokens) * 100 if prompt_tokens else None
        )

    summary["costTotal"] = round(float(summary["costTotal"]), 12)
    return summary


def rpc_data(
    command: Callable[..., dict[str, Any]],
    payload: dict[str, Any],
    *,
    timeout: float,
) -> dict[str, Any]:
    response = command(payload, timeout=timeout)
    return dict((response or {}).get("data") or {})


def rpc_entries(
    command: Callable[..., dict[str, Any]],
    *,
    timeout: float,
    since: str | None = None,
) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {"type": "get_entries"}
    if since:
        payload["since"] = since
    data = rpc_data(command, payload, timeout=timeout)
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise ValueError("get_entries response requires entries")
    return entries


def _last_entry_id(entries: list[dict[str, Any]]) -> str | None:
    if not entries:
        return None
    value = entries[-1].get("id")
    return str(value) if value is not None else None


_PI_RUNTIME_START_ENTRY_TYPE = "goal-plus-runtime-start"
_PI_RUNTIME_START_STRING_FIELDS = (
    "agent",
    "agent_harness",
    "mode",
    "pi_sandbox",
    "pi_builtin_tool_approval",
    "filesystem_access",
    "network_access",
    "cwd",
    "captured_at",
)
_PI_RUNTIME_START_TOOL_FIELDS = (
    "active_tools",
    "active_builtin_tools",
    "active_non_builtin_tools",
)


def latest_pi_runtime_start(
    entries: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for entry in reversed(entries):
        if (
            entry.get("type") != "custom"
            or entry.get("customType") != _PI_RUNTIME_START_ENTRY_TYPE
        ):
            continue
        data = entry.get("data")
        if not isinstance(data, dict):
            continue
        snapshot: dict[str, Any] = {
            "schema_version": (
                int(data["schema_version"])
                if isinstance(data.get("schema_version"), int)
                else 1
            ),
            "project_trusted": (
                data.get("project_trusted")
                if isinstance(data.get("project_trusted"), bool)
                else None
            ),
        }
        for field in _PI_RUNTIME_START_STRING_FIELDS:
            value = data.get(field)
            snapshot[field] = value if isinstance(value, str) else None
        for field in _PI_RUNTIME_START_TOOL_FIELDS:
            value = data.get(field)
            snapshot[field] = (
                [str(item) for item in value if isinstance(item, str)][:256]
                if isinstance(value, list)
                else []
            )
        return snapshot
    return None


def _combine_pi_usage(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
) -> dict[str, Any]:
    previous = previous or {}
    combined = {
        field: _number(previous.get(field)) + _number(current.get(field))
        for field in (
            "assistantMessages",
            "input",
            "output",
            "cacheRead",
            "cacheWrite",
            "costTotal",
        )
    }
    combined["assistantMessages"] = int(combined["assistantMessages"])
    for field in ("input", "output", "cacheRead", "cacheWrite"):
        combined[field] = int(combined[field])
    combined["costTotal"] = round(float(combined["costTotal"]), 12)
    combined["latestCacheHitRate"] = (
        current.get("latestCacheHitRate")
        if current.get("latestCacheHitRate") is not None
        else previous.get("latestCacheHitRate")
    )
    return combined


def collect_pi_metrics(
    command: Callable[..., dict[str, Any]],
    *,
    session_id: str,
    metrics_baseline: dict[str, Any],
    last_state_data: dict[str, Any],
    started_at: str,
    ended_at: str,
    duration_seconds: float,
) -> dict[str, Any]:
    errors: list[str] = []

    state_data = dict(last_state_data)
    try:
        state_data.update(rpc_data(command, {"type": "get_state"}, timeout=5))
    except Exception as exc:
        errors.append(f"get_state: {exc}")

    try:
        delta_entries = rpc_entries(
            command,
            timeout=10,
            since=(
                str(metrics_baseline["last_entry_id"])
                if metrics_baseline.get("last_entry_id")
                else None
            ),
        )
    except Exception as exc:
        errors.append(f"get_entries: {exc}")
        delta_entries = None

    try:
        session_stats: dict[str, Any] | None = rpc_data(
            command,
            {"type": "get_session_stats"},
            timeout=10,
        )
    except Exception as exc:
        errors.append(f"get_session_stats: {exc}")
        session_stats = None

    baseline_count = int(_number(metrics_baseline.get("entry_count")))
    final_count = baseline_count + len(delta_entries or [])
    baseline_last_entry_id = metrics_baseline.get("last_entry_id")
    final_last_entry_id = _last_entry_id(delta_entries or []) or (
        str(baseline_last_entry_id) if baseline_last_entry_id else None
    )
    usage_delta = summarize_pi_entries(delta_entries) if delta_entries is not None else None
    previous_usage = metrics_baseline.get("usage_total")
    previous_usage = previous_usage if isinstance(previous_usage, dict) else None
    usage_total = _combine_pi_usage(previous_usage, usage_delta) if usage_delta is not None else None
    prior_duration = float(_number(metrics_baseline.get("duration_seconds")))
    cumulative_duration = prior_duration + duration_seconds
    original_started_at = metrics_baseline.get("started_at") or started_at
    scope = (
        "session_cumulative_incremental"
        if baseline_last_entry_id
        else "session_total_incremental"
    )
    state_model = state_data.get("model")
    model = (
        state_model.get("id") or state_model.get("modelId")
        if isinstance(state_model, dict)
        else state_model
    )
    provider = (
        state_model.get("provider") or state_model.get("providerId")
        if isinstance(state_model, dict)
        else state_data.get("provider")
    )
    metrics: dict[str, Any] = {
        "scope": scope,
        "session_id": session_id,
        "session_file": state_data.get("sessionFile") or state_data.get("session_file"),
        "model": model,
        "provider": provider,
        "thinking_level": state_data.get("thinkingLevel") or state_data.get("thinking_level"),
        "started_at": original_started_at,
        "ended_at": ended_at,
        "duration_seconds": cumulative_duration,
        "dispatch_started_at": started_at,
        "dispatch_ended_at": ended_at,
        "dispatch_duration_seconds": duration_seconds,
        "baseline_entry_count": baseline_count,
        "final_entry_count": final_count,
        "baseline_last_entry_id": baseline_last_entry_id,
        "final_last_entry_id": final_last_entry_id,
        "usage_delta": usage_delta,
        "usage_delta_scope": "since_last_observation",
        "usage_total": usage_total,
        "session_stats": session_stats,
        "runtime_start": latest_pi_runtime_start(delta_entries or []) or metrics_baseline.get("runtime_start"),
    }
    if errors:
        metrics["errors"] = errors
    return metrics
