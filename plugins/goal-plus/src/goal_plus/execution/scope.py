"""Scope admission and creation-order policy, independent of storage and host drivers."""

from collections.abc import Iterator
from typing import Any

from goal_plus.ports.scope import ScopeAttachment, ScopeHost, ScopeRecord


PI_NATIVE_AGENT_CONFIG_FIELDS = frozenset({"model_pattern", "thinking_level"})


def normalize_native_agent_config(
    host: ScopeHost, native_agent_config: dict[str, Any] | None,
) -> dict[str, str] | None:
    if native_agent_config is None:
        return None
    if not isinstance(native_agent_config, dict):
        raise ValueError("native agent config must be an object")
    if host != "pi":
        raise ValueError("native agent config is only supported for the Pi host")
    unknown = sorted(set(native_agent_config) - PI_NATIVE_AGENT_CONFIG_FIELDS)
    if unknown:
        raise ValueError("unsupported Pi native agent config fields: " + ", ".join(unknown))
    normalized: dict[str, str] = {}
    for name in PI_NATIVE_AGENT_CONFIG_FIELDS:
        value = native_agent_config.get(name)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Pi native agent config {name} must be non-empty")
        normalized[name] = value.strip()
    if "model_pattern" not in normalized:
        raise ValueError("Pi native agent config requires model_pattern")
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
