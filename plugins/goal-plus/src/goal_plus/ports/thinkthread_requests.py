"""Provider request observations, independent of each owner's durable journal."""

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class RequestObservation:
    state: Literal["absent", "pending", "succeeded", "cancelled", "failed", "unknown"]
    request_state: str | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] = field(default_factory=dict)

    def terminal_result(self, request_id: str) -> dict[str, Any]:
        if self.state not in {"succeeded", "cancelled"} or self.result is None:
            raise RuntimeError(f"terminal ThinkThread request {request_id} omitted result")
        return dict(self.result)


@dataclass(frozen=True)
class RequestCloseResult:
    state: Literal["closed", "needs_recovery", "rejected"]
    error: dict[str, Any] = field(default_factory=dict)
