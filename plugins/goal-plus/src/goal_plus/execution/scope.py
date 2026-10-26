"""Scope admission and creation-order policy, independent of storage and host drivers."""

from collections.abc import Iterator
from typing import Any

from goal_plus.ports.scope import ScopeAttachment, ScopeHost, ScopeRecord


PI_NATIVE_AGENT_CONFIG_FIELDS = frozenset({"model_pattern", "thinking_level"})
CODEX_NATIVE_AGENT_CONFIG_FIELDS = frozenset({"model", "reasoning_effort"})


def normalize_native_agent_config(
    agent_harness: ScopeHost, native_agent_config: dict[str, Any] | None,
) -> dict[str, str] | None:
    if native_agent_config is None:
        return None
    if not isinstance(native_agent_config, dict):
        raise ValueError("native agent config must be an object")
    fields = PI_NATIVE_AGENT_CONFIG_FIELDS if agent_harness == "pi" else CODEX_NATIVE_AGENT_CONFIG_FIELDS
    label = "Pi" if agent_harness == "pi" else "Codex"
    model_field = "model_pattern" if agent_harness == "pi" else "model"
    unknown = sorted(set(native_agent_config) - fields)
    if unknown:
        raise ValueError(f"unsupported {label} native agent config fields: " + ", ".join(unknown))
    normalized: dict[str, str] = {}
    for name in fields:
        value = native_agent_config.get(name)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} native agent config {name} must be non-empty")
        normalized[name] = value.strip()
    if model_field not in normalized:
        raise ValueError(f"{label} native agent config requires {model_field}")
    return normalized


def assert_scope_admission(
    scope: ScopeRecord,
    *,
    scope_id: str,
    attachment: ScopeAttachment,
    internal: bool = False,
) -> None:
    if scope.get("state") != "open":
        raise RuntimeError(f"execution scope {scope_id} is not open")
    if attachment not in scope_attachments(scope):
        raise PermissionError(
            f"execution scope {scope_id} does not own "
            f"{'Goal' if attachment.kind == 'goal' else 'Search run'} {attachment.identity}"
        )
    if internal:
        attachment.require_search_run()
    elif attachment.identity in scope.get("fenced_run_ids", []):
        raise RuntimeError(f"Search run {attachment.identity} is fenced against new host work")


def scope_attachments(scope: ScopeRecord) -> tuple[ScopeAttachment, ...]:
    return tuple(ScopeAttachment.from_id(identity) for identity in scope.get("run_ids", []))


def newest_scope(scopes: Iterator[ScopeRecord]) -> ScopeRecord | None:
    # Immutable creation order, including sub-second launches; cleanup never makes a scope current.
    return max(
        scopes,
        key=lambda item: (
            str(item.get("created_at") or ""),
            int(item.get("created_at_ns", 0)),
            int(item.get("generation", 0)),
            str(item["scope_id"]),
        ),
        default=None,
    )
