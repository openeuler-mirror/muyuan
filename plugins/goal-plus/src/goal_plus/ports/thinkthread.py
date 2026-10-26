"""ThinkThread transport contract, independent of the SDK subprocess adapter."""

from dataclasses import dataclass
from typing import Any, Protocol, TypedDict


class AgentPosixBridgeError(RuntimeError):
    def __init__(self, message: str, *, error: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error = error or {}

    @property
    def delivery(self) -> str | None:
        value = self.error.get("delivery")
        return value if isinstance(value, str) else None

    @property
    def completion_unknown(self) -> bool:
        if self.delivery == "completion_unknown":
            return True
        cause = self.error.get("cause")
        return (
            isinstance(cause, dict)
            and cause.get("delivery") == "completion_unknown"
        )

    @property
    def rejection(self) -> dict[str, Any] | None:
        value = self.error.get("response") or self.error.get("rejection")
        return value if isinstance(value, dict) else None

    @property
    def code(self) -> str | None:
        rejection = self.rejection
        nested = rejection.get("error") if rejection else None
        if isinstance(nested, dict) and isinstance(nested.get("code"), str):
            return str(nested["code"])
        return None

    @property
    def retryable(self) -> bool | None:
        rejection = self.rejection
        nested = rejection.get("error") if rejection else None
        if isinstance(nested, dict) and isinstance(nested.get("retryable"), bool):
            return bool(nested["retryable"])
        return None


class AgentPosixSdkMismatch(AgentPosixBridgeError):
    pass


class ThinkThreadClient(Protocol):
    def invoke(
        self, operation: str, params: dict[str, Any] | None = None, *,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]: ...


class RpcDeliveryState(TypedDict, total=False):
    thinkthread_id: str | None
    pending_responses: dict[str, dict[str, Any]]
    settled_requests: dict[str, dict[str, Any]]
    seen_message_ids: list[str]
    message_cursor: str | None


@dataclass(frozen=True)
class ChildBinding:
    child_id: str
    branch_id: str
    initial_message_id: str | None


def capability_ids(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {
        str(item["id"]) for item in value
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
