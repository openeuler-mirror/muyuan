from typing import Any


def _number(value: Any) -> int | float | None:
    return (
        value
        if isinstance(value, int | float) and not isinstance(value, bool)
        else None
    )


def normalize_pi_cost(
    value: Any, *, processed_tokens: int | float | None
) -> int | float | None:
    cost = _number(value)
    if cost == 0 and processed_tokens not in {None, 0}:
        return None
    return cost


def normalize_pi_usage(value: Any, *, scope: str) -> dict[str, Any]:
    usage = value if isinstance(value, dict) else {}
    input_tokens = _number(usage.get("input"))
    cached_input_tokens = _number(usage.get("cacheRead"))
    cache_write_tokens = _number(usage.get("cacheWrite"))
    output_tokens = _number(usage.get("output"))
    total_tokens = None
    if input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    processed_components = (
        input_tokens,
        cached_input_tokens,
        cache_write_tokens,
        output_tokens,
    )
    processed_tokens = (
        sum(value for value in processed_components if value is not None)
        if any(value is not None for value in processed_components)
        else None
    )
    raw_cost = usage.get("costTotal")
    if raw_cost is None:
        raw_cost = usage.get("cost")
        if isinstance(raw_cost, dict):
            raw_cost = raw_cost.get("total")
    cost = normalize_pi_cost(raw_cost, processed_tokens=processed_tokens)
    return {
        "scope": scope,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cache_write_tokens": cache_write_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": None,
        "total_tokens": total_tokens,
        "processed_tokens": processed_tokens,
        "cost_usd": cost,
        "assistant_messages": _number(usage.get("assistantMessages")),
        "tool_calls": _number(usage.get("toolCalls")),
        "tool_results": _number(usage.get("toolResults")),
    }
