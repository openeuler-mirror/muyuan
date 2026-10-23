"""Controller-owned shared-knowledge overlay for spec freezing.

When the control plane exports ``GOAL_PLUS_CONTROLLER_SHARED_KNOWLEDGE`` as
JSON, the parsed :class:`SharedKnowledgeSpec` replaces whatever the agent
supplied at freeze time, so enabling and templating the shared knowledge
board no longer depends on model instruction following.  Without the
variable, freezing keeps the agent-supplied spec unchanged.

The legacy variable name ``GOAL_PLUS_CONTROLLER_SHARED_CACHE`` is still read
as a deprecated alias so existing control-plane environments keep working
during the migration.

The overlay is a transport channel only: goal-plus never interprets or
defaults the template content — the control plane (the benchmark) owns and
version-controls it.  This module stays pure; the host tool surface reads
the environment and passes the raw value in.
"""

from __future__ import annotations

import json

from goal_plus.domain.search import SearchSpec
from goal_plus.domain.shared_knowledge import SharedKnowledgeSpec

CONTROLLER_SHARED_KNOWLEDGE_ENV = "GOAL_PLUS_CONTROLLER_SHARED_KNOWLEDGE"
CONTROLLER_LEGACY_SHARED_CACHE_ENV = "GOAL_PLUS_CONTROLLER_SHARED_CACHE"


def controller_shared_knowledge_overlay(raw: str | None) -> SharedKnowledgeSpec | None:
    """Parse the control-plane overlay, when provided.

    An unset or blank value means no overlay, matching the "not exported"
    contract: a control plane that exports the variable empty must not
    break every spec freeze.
    """

    text = (raw or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except Exception as exc:  # noqa: BLE001 - surfaced verbatim to the caller
        raise ValueError(
            f"{CONTROLLER_SHARED_KNOWLEDGE_ENV} is not valid JSON: {exc}"
        ) from exc
    try:
        return SharedKnowledgeSpec.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - surfaced verbatim to the caller
        raise ValueError(
            f"{CONTROLLER_SHARED_KNOWLEDGE_ENV} is not a valid shared "
            f"knowledge spec: {exc}"
        ) from exc


def apply_controller_shared_knowledge_overlay(
    spec: SearchSpec,
    raw: str | None,
) -> SearchSpec:
    """Return ``spec`` with the controller overlay applied, if provided."""
    overlay = controller_shared_knowledge_overlay(raw)
    if overlay is None:
        return spec
    return spec.model_copy(update={"shared_knowledge": overlay})


__all__ = [
    "CONTROLLER_SHARED_KNOWLEDGE_ENV",
    "CONTROLLER_LEGACY_SHARED_CACHE_ENV",
    "apply_controller_shared_knowledge_overlay",
    "controller_shared_knowledge_overlay",
]
