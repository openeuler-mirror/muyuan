"""Pi usage and runtime metadata decoding."""

from typing import Any

import pytest

from goal_plus.execution.pi_metrics import latest_pi_runtime_start, summarize_pi_entries

pytestmark = pytest.mark.pi


def _assistant_usage(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cost_total: float = 0.0,
) -> dict[str, Any]:
    return {
        "type": "message",
        "message": {
            "role": "assistant",
            "usage": {
                "input": input_tokens,
                "output": output_tokens,
                "cacheRead": cache_read,
                "cacheWrite": 0,
                "cost": {"total": cost_total},
            },
        },
    }


def _runtime_start_entry(*, entry_id: str = "runtime_1") -> dict[str, Any]:
    return {
        "type": "custom",
        "id": entry_id,
        "customType": "goal-plus-runtime-start",
        "data": {
            "schema_version": 1,
            "agent": "candidate",
            "agent_harness": "pi",
            "mode": "rpc",
            "project_trusted": True,
            "active_tools": ["read", "bash", "goal_plus_search_get_agent_context"],
            "active_builtin_tools": ["read", "bash"],
            "active_non_builtin_tools": ["goal_plus_search_get_agent_context"],
            "pi_sandbox": "none",
            "pi_builtin_tool_approval": "none",
            "filesystem_access": "launcher-process-user",
            "network_access": "host-policy-unreported",
            "cwd": "/tmp/candidate",
            "captured_at": "2026-08-26T08:00:00.000Z",
            "ignored_secret": "must not propagate",
        },
    }


def test_summarize_pi_entries_reports_totals_and_latest_cache_hit_rate() -> None:
    summary = summarize_pi_entries(
        [
            _assistant_usage(
                input_tokens=100,
                output_tokens=10,
                cache_read=50,
                cost_total=0.01,
            ),
            {"type": "message", "message": {"role": "user"}},
            _assistant_usage(
                input_tokens=25,
                output_tokens=5,
                cache_read=75,
                cost_total=0.02,
            ),
        ]
    )

    assert summary == {
        "assistantMessages": 2,
        "input": 125,
        "output": 15,
        "cacheRead": 125,
        "cacheWrite": 0,
        "costTotal": 0.03,
        "latestCacheHitRate": 75.0,
    }


def test_latest_pi_runtime_start_returns_only_bounded_runtime_fields() -> None:
    snapshot = latest_pi_runtime_start(
        [
            _runtime_start_entry(),
            {"type": "custom", "customType": "other", "data": {"agent": "wrong"}},
        ]
    )

    assert snapshot == {
        "schema_version": 1,
        "project_trusted": True,
        "agent": "candidate",
        "agent_harness": "pi",
        "mode": "rpc",
        "pi_sandbox": "none",
        "pi_builtin_tool_approval": "none",
        "filesystem_access": "launcher-process-user",
        "network_access": "host-policy-unreported",
        "cwd": "/tmp/candidate",
        "captured_at": "2026-08-26T08:00:00.000Z",
        "active_tools": ["read", "bash", "goal_plus_search_get_agent_context"],
        "active_builtin_tools": ["read", "bash"],
        "active_non_builtin_tools": ["goal_plus_search_get_agent_context"],
    }
    assert "ignored_secret" not in snapshot
