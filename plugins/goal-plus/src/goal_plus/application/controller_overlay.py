"""Controller-owned shared-cache overlay for spec freezing.

When the control plane exports ``GOAL_PLUS_CONTROLLER_SHARED_CACHE`` as
JSON, the parsed :class:`SharedCacheSpec` replaces whatever the agent
supplied at freeze time, so enabling and templating the shared cache no
longer depends on model instruction following.  Without the variable,
freezing keeps the agent-supplied spec unchanged.

The overlay is a transport channel only: goal-plus never interprets or
defaults the template content — the control plane (the benchmark) owns
and version-controls it.  This module stays pure; the host tool surface
reads the environment and passes the raw value in.
"""

from __future__ import annotations

import json

from goal_plus.domain.search import SearchSpec
from goal_plus.domain.shared_cache import SharedCacheSpec

CONTROLLER_SHARED_CACHE_ENV = "GOAL_PLUS_CONTROLLER_SHARED_CACHE"


def controller_shared_cache_overlay(raw: str | None) -> SharedCacheSpec | None:
    """Parse the controller overlay, or None when it is empty.

    A malformed overlay raises ``ValueError`` so assembly mistakes fail at
    freeze time instead of silently degrading to the agent-supplied spec.
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{CONTROLLER_SHARED_CACHE_ENV} is not valid JSON: {exc}"
        ) from exc
    try:
        return SharedCacheSpec.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - surfaced verbatim to the caller
        raise ValueError(
            f"{CONTROLLER_SHARED_CACHE_ENV} is not a valid shared cache spec: {exc}"
        ) from exc


def apply_controller_shared_cache_overlay(
    spec: SearchSpec,
    raw: str | None,
) -> SearchSpec:
    """Return ``spec`` with the controller overlay applied, if provided."""
    overlay = controller_shared_cache_overlay(raw)
    if overlay is None:
        return spec
    return spec.model_copy(update={"shared_cache": overlay})


__all__ = [
    "CONTROLLER_SHARED_CACHE_ENV",
    "apply_controller_shared_cache_overlay",
    "controller_shared_cache_overlay",
]
