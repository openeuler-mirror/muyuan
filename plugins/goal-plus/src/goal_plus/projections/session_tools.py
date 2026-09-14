"""Compact session receipts, separate from host execution input and diagnostics."""

from typing import Any
from dataclasses import asdict

from goal_plus.domain.search import AgentSessionRecord
from goal_plus.ports.session import SessionCallSnapshot, SessionInvocationResult


def invocation_receipt(result: SessionInvocationResult, *, include_text: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "native_session_id": result.native_session_id,
        "invocation_id": result.invocation_id,
        "state": result.state,
    }
    if include_text and result.text:
        payload["text"] = result.text
    if result.error:
        payload["error"] = result.error
    return payload


def session_call_receipt(snapshot: SessionCallSnapshot, *, detail: bool = False) -> dict[str, Any]:
    if detail:
        return asdict(snapshot)
    payload: dict[str, Any] = {"call_id": snapshot.call_id, "state": snapshot.state}
    if snapshot.native_session_id is not None:
        payload["native_session_id"] = snapshot.native_session_id
    if snapshot.result is not None:
        payload["invocation_id"] = snapshot.result.invocation_id
        if snapshot.result.error:
            payload["error"] = snapshot.result.error
        if snapshot.result.text:
            payload["result_available"] = True
            payload["details"] = {"tool": "goal_plus_session_wait", "detail": True}
    if snapshot.error:
        payload["error"] = snapshot.error
    return payload


def session_receipt(session: AgentSessionRecord) -> dict[str, Any]:
    payload = session.model_dump(mode="json", include={
        "agent_session_id", "run_id", "candidate_id", "execution_generation",
        "agent_harness", "runtime_provider", "workspace", "selected_model",
    })
    payload["session_handle"] = session.session_handle.model_dump(mode="json", include={
        "external_id", "nickname",
    }, exclude_none=True)
    return payload
