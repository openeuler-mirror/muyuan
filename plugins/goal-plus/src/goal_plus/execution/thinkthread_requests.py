"""Observe and close exact TT requests without owning retry or journal state."""

from goal_plus.ports.thinkthread import AgentPosixBridgeError, ThinkThreadClient
from goal_plus.ports.thinkthread_requests import RequestCloseResult, RequestObservation


# Preserve the existing bounded confirmation probe after a lost close response.
CLOSE_CONFIRMATION_TIMEOUT_SECONDS = 5.0


class ThinkThreadRequests:
    def __init__(self, client: ThinkThreadClient) -> None:
        self.client = client

    def observe(
        self, request_id: str, method: str, *, timeout_seconds: float = 30.0,
    ) -> RequestObservation:
        try:
            status = self.client.invoke(
                "fs.request.status", {"requestId": request_id},
                timeout_seconds=timeout_seconds,
            )
        except AgentPosixBridgeError as exc:
            if exc.code == "RequestNotFound":
                return RequestObservation("absent")
            raise
        if status.get("requestId") != request_id or status.get("method") != method:
            raise RuntimeError(f"fs.request.status identity mismatch for {request_id}")
        raw_state = status.get("state")
        state = raw_state if isinstance(raw_state, str) else None
        result, error = status.get("result"), status.get("error")
        if state in {"accepted", "running", "closing"}:
            kind = "pending"
        elif state in {"succeeded", "cancelled", "failed"}:
            kind = state
        else:
            kind = "unknown"
        return RequestObservation(
            state=kind,
            request_state=state if isinstance(state, str) else None,
            result=dict(result) if isinstance(result, dict) else None,
            error=dict(error) if isinstance(error, dict) else {},
        )

    def close(self, request_id: str, method: str) -> RequestCloseResult:
        try:
            self.client.invoke("fs.request.close", {"requestId": request_id})
        except AgentPosixBridgeError as exc:
            if exc.code == "RequestNotFound":
                return RequestCloseResult("closed")
            if not exc.completion_unknown:
                return RequestCloseResult("rejected", {
                    "message": str(exc), "code": exc.code,
                    "delivery": exc.delivery, "retryable": exc.retryable,
                })
            try:
                observation = self.observe(
                    request_id, method,
                    timeout_seconds=CLOSE_CONFIRMATION_TIMEOUT_SECONDS,
                )
            except AgentPosixBridgeError as observed:
                return RequestCloseResult("needs_recovery", {
                    "message": str(observed), "code": observed.code,
                    "delivery": observed.delivery, "close_error": str(exc),
                })
            except RuntimeError as observed:
                return RequestCloseResult("needs_recovery", {"message": str(observed)})
            if observation.state == "absent":
                return RequestCloseResult("closed")
            return RequestCloseResult("needs_recovery", {
                "message": str(exc), "code": exc.code,
                "delivery": exc.delivery, "request_state": observation.request_state,
            })
        return RequestCloseResult("closed")
