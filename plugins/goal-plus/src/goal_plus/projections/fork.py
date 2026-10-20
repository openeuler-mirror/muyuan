"""Read-only projection of persisted fork provenance and receipts."""

from typing import Any

from goal_plus.domain.search import DispatchGroupState


def dispatch_group_summary(state: DispatchGroupState | None) -> dict[str, Any] | None:
    return state.model_dump(mode="json") if state is not None else None
