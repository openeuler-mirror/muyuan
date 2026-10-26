"""Reliable controller endpoint for both ThinkThread worker roles."""

import base64
import hashlib
import json
from collections.abc import Callable
from typing import Any

from goal_plus.domain.tool_catalog import CandidateToolCapabilities
from goal_plus.ports.clock import Clock
from goal_plus.ports.thinkthread import (
    AgentPosixBridgeError,
    RpcDeliveryState,
    ThinkThreadClient,
)

PROTOCOL = "goal-plus.thinkthread.v3"
MESSAGE_RECEIVE_LIMIT = 32
RESPONSE_CHUNK_BYTES = 32 * 1024
RESPONSE_RETRY_SECONDS = 1.0


def candidate_capabilities_marker(capabilities: CandidateToolCapabilities) -> str:
    """Encode the narrow frozen projection in a trusted Parent dispatch."""
    encoded = base64.urlsafe_b64encode(capabilities.model_dump_json().encode()).decode("ascii").rstrip("=")
    return f"goal_plus_candidate_capabilities={encoded};"


def message_payload(message: dict[str, Any]) -> dict[str, Any] | None:
    if message.get("truncated") is True or not isinstance(message.get("text"), str):
        return None
    try:
        payload = json.loads(message["text"])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def registration_matches(message: dict[str, Any], nonce: str) -> bool:
    payload = message_payload(message)
    return (
        payload is not None
        and payload.get("protocol") == PROTOCOL
        and payload.get("type") == "registration"
        and payload.get("registration_nonce") == nonce
    )


def request_hash(request: dict[str, Any]) -> str:
    content_json = request.get("content_json")
    if not isinstance(content_json, str):
        raise ValueError("worker RPC content_json is required")
    expected = {
        "agent_session_id": request.get("agent_session_id"),
        "params": request.get("params"),
        "tool": request.get("tool"),
    }
    if "deadline_unix_ms" in request:
        expected["deadline_unix_ms"] = request["deadline_unix_ms"]
    try:
        decoded = json.loads(content_json)
    except json.JSONDecodeError as exc:
        raise ValueError("worker RPC content_json is invalid") from exc
    if decoded != expected:
        raise ValueError("worker RPC content_json does not match request fields")
    return hashlib.sha256(content_json.encode("utf-8")).hexdigest()


def _response_bytes(response: dict[str, Any]) -> bytes:
    return json.dumps(
        response, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def response_hash(response: dict[str, Any]) -> str:
    return hashlib.sha256(_response_bytes(response)).hexdigest()


def response_payload(request_id: str, response: dict[str, Any]) -> list[str]:
    data = _response_bytes(response)
    digest = hashlib.sha256(data).hexdigest()
    pieces = [
        data[offset:offset + RESPONSE_CHUNK_BYTES]
        for offset in range(0, len(data), RESPONSE_CHUNK_BYTES)
    ] or [b""]
    return [
        json.dumps({
            "protocol": PROTOCOL,
            "type": "response_chunk",
            "request_id": request_id,
            "chunk_index": index,
            "chunk_count": len(pieces),
            "data_base64": base64.b64encode(piece).decode("ascii"),
            "chunk_sha256": hashlib.sha256(piece).hexdigest(),
            "response_sha256": digest,
        }, separators=(",", ":"))
        for index, piece in enumerate(pieces)
    ]


class ReliableRpcChannel:
    def __init__(self, client: ThinkThreadClient, clock: Clock) -> None:
        self.client = client
        self.clock = clock

    def send_response(self, state: RpcDeliveryState, request_id: str) -> None:
        record = state.setdefault("pending_responses", {}).get(request_id)
        settled = state.setdefault("settled_requests", {}).get(request_id)
        if not isinstance(record, dict) or not isinstance(settled, dict):
            return
        response = settled.get("response")
        if not isinstance(response, dict):
            return
        for chunk in response_payload(request_id, response):
            self.client.invoke("message.send", {
                "recipientThinkthreadId": state["thinkthread_id"],
                "text": chunk,
                "wake": False,
                "replyToMessageId": record.get("reply_to_message_id"),
            })
        record["attempts"] = int(record.get("attempts", 0)) + 1
        record["last_attempt_at"] = self.clock.timestamp()
        record["last_attempt_unix"] = self.clock.now()

    def receive(
        self, state: RpcDeliveryState, process: Callable[[dict[str, Any]], None],
        persist: Callable[[], None],
    ) -> None:
        child_id = state.get("thinkthread_id")
        if not isinstance(child_id, str):
            return
        params: dict[str, Any] = {
            "senderThinkthreadId": child_id, "limit": MESSAGE_RECEIVE_LIMIT,
        }
        if isinstance(state.get("message_cursor"), str):
            params["after"] = state["message_cursor"]
        page = self.client.invoke("message.receive", params)
        messages = page.get("messages")
        if not isinstance(messages, list) or not isinstance(page.get("nextCursor"), str):
            raise AgentPosixBridgeError("message.receive returned an invalid page")
        self.accept_page(state, messages, process)
        state["message_cursor"] = page["nextCursor"]
        persist()

    def accept_page(
        self, state: RpcDeliveryState, messages: list[Any],
        process: Callable[[dict[str, Any]], None],
    ) -> None:
        seen = set(state.get("seen_message_ids", []))
        for message in messages:
            if not isinstance(message, dict):
                continue
            if message.get("senderThinkthreadId") != state["thinkthread_id"]:
                raise PermissionError("worker Message sender does not match bound Child")
            message_id = message.get("messageId")
            if not isinstance(message_id, str) or message_id in seen:
                continue
            seen.add(message_id)
            process(message)
        state["seen_message_ids"] = sorted(seen)
