"""Candidate Message identity/replay and MCP forwarding, without pool orchestration."""

from collections.abc import Callable
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any

from goal_plus.domain.evidence import CandidateAttemptAccepted
from goal_plus.execution.thinkthread_rpc import (
    PROTOCOL, RESPONSE_RETRY_SECONDS, ReliableRpcChannel,
    message_payload, request_hash, response_hash,
)
from goal_plus.infrastructure.clock import SystemClock, utc_timestamp
from goal_plus.infrastructure.session_diagnostics import archive_diagnostic_message
from goal_plus.ports.thinkthread import AgentPosixBridgeError, ThinkThreadClient
from goal_plus.ports.thinkthread_verification import DeferredCandidateAttempt
from goal_plus.tools import CANDIDATE_TOOL_NAMES


def candidate_attempt_id(job: dict[str, Any], request_id: str) -> str:
    identity = json.dumps(
        {
            "run_id": job.get("run_id"),
            "candidate_id": job.get("candidate_id"),
            "agent_session_id": job.get("agent_session_id"),
            "dispatch_index": job.get("dispatch_index"),
            "request_id": request_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"attempt_{hashlib.sha256(identity.encode()).hexdigest()[:24]}"


def _registration_matches(message: dict[str, Any], nonce: str) -> bool:
    payload = message_payload(message)
    return (
        payload is not None
        and payload.get("protocol") == PROTOCOL
        and payload.get("type") == "registration"
        and payload.get("registration_nonce") == nonce
    )


def _dispatch_ack_matches(message: dict[str, Any], nonce: str) -> bool:
    payload = message_payload(message)
    return (
        payload is not None
        and payload.get("protocol") == PROTOCOL
        and payload.get("type") == "dispatch_ack"
        and payload.get("dispatch_nonce") == nonce
    )


def _worker_request_hash(request: dict[str, Any]) -> str:
    if "deadline_unix_ms" in request:
        deadline = request["deadline_unix_ms"]
        if (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(float(deadline))
            or deadline <= 0
        ):
            raise ValueError("worker RPC deadline_unix_ms must be a positive number")
    return request_hash(request)


def _validate_worker_request(
    job: dict[str, Any],
    request: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    if request.get("protocol") != PROTOCOL or request.get("type") != "request":
        raise ValueError("unsupported worker RPC envelope")
    request_id = request.get("request_id")
    tool = request.get("tool")
    params = request.get("params")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("worker RPC request_id is required")
    if tool not in CANDIDATE_TOOL_NAMES:
        raise PermissionError(f"worker RPC tool is not allowed: {tool}")
    if not isinstance(params, dict):
        raise ValueError("worker RPC params must be an object")
    if request.get("agent_session_id") != job.get("agent_session_id"):
        raise PermissionError("worker RPC agent_session_id binding mismatch")
    digest = _worker_request_hash(request)
    if request.get("content_sha256") != digest:
        raise ValueError("worker RPC content_sha256 mismatch")
    return request_id, str(tool), params


def dispatch_worker_tool(
    root_dir: Path | str,
    job: dict[str, Any],
    tool: str,
    params: dict[str, Any],
    *,
    request_id: str,
) -> Any:
    from goal_plus.mcp_client import call_mcp_tool

    params = dict(params)
    private_inner_git = params.pop("_inner_git_attempt", None)
    session_id = str(job["agent_session_id"])
    submission = None
    if tool == "goal_plus_search_run_verifier":
        prior = job.get("attempt_request")
        submission = DeferredCandidateAttempt(
            run_id=str(job["run_id"]), candidate_id=str(job["candidate_id"]),
            agent_session_id=session_id, thinkthread_id=str(job.get("thinkthread_id") or ""),
            branch_id=str(job.get("fs_branch_id") or ""),
            attempt_id=candidate_attempt_id(job, request_id), request_id=request_id,
            prior=CandidateAttemptAccepted.model_validate(prior) if isinstance(prior, dict) else None,
            inner_git_attempt=private_inner_git,
        )
    elif private_inner_git is not None:
        raise PermissionError("private inner Git metadata is valid only for an AutoResearch-Git verifier request")
    result = call_mcp_tool(
        root_dir, tool, params, caller_role="candidate", caller_agent_session_id=session_id,
        candidate_attempt=submission,
    )
    if submission is not None:
        accepted = CandidateAttemptAccepted.model_validate(result)
        if private_inner_git is not None:
            job["inner_git_attempt"] = private_inner_git
        job["attempt_request"] = accepted.model_dump(mode="json")
    return result


def _retry_pending_responses(
    client: ThinkThreadClient,
    job: dict[str, Any],
    persist: Callable[[], None],
) -> None:
    now = time.time()
    for request_id, record in list(job.setdefault("pending_responses", {}).items()):
        if not isinstance(request_id, str) or not isinstance(record, dict):
            raise RuntimeError("ThinkThread worker response queue is invalid")
        last_attempt = record.get("last_attempt_unix")
        if isinstance(last_attempt, (int, float)) and (
            now - float(last_attempt) < RESPONSE_RETRY_SECONDS
        ):
            continue
        try:
            ReliableRpcChannel(client, SystemClock()).send_response(job, request_id)
            record.pop("last_error", None)
        except AgentPosixBridgeError as exc:
            record["last_error"] = {
                "message": str(exc),
                "error_code": exc.code,
                "completion_unknown": exc.completion_unknown,
            }
    persist()


def _process_worker_message(
    client: ThinkThreadClient,
    root_dir: Path | str,
    job: dict[str, Any],
    message: dict[str, Any],
    persist: Callable[[], None],
) -> None:
    payload = message_payload(message)
    if archive_diagnostic_message(root_dir, job, message):
        return
    if payload is None or payload.get("protocol") != PROTOCOL:
        return
    if payload.get("type") in {"session_message_ack", "session_interrupt_ack"}:
        # Session control validates this exact acknowledgement on its own cursor.
        return
    if payload.get("type") == "registration":
        if not _registration_matches(message, str(job["registration_nonce"])):
            raise PermissionError("Child registration nonce mismatch")
        job["registered_at"] = utc_timestamp()
        return
    if payload.get("type") == "dispatch_ack":
        acknowledged_nonce = payload.get("dispatch_nonce")
        known_nonces = {
            item
            for item in job.get("dispatch_nonces", [])
            if isinstance(item, str)
        }
        known_nonces.add(str(job["dispatch_nonce"]))
        if not isinstance(acknowledged_nonce, str):
            raise PermissionError("Child dispatch acknowledgement mismatch")
        if acknowledged_nonce not in known_nonces:
            # Older durable jobs did not retain nonce history. A delayed ack from
            # an earlier dispatch must not block later worker RPCs, and it cannot
            # acknowledge the current wake because recovery checks the exact nonce.
            if int(job.get("dispatch_index", 1)) <= 1:
                raise PermissionError("Child dispatch acknowledgement mismatch")
            stale = set(job.get("stale_dispatch_acknowledgements", []))
            stale.add(acknowledged_nonce)
            job["stale_dispatch_acknowledgements"] = sorted(stale)
            return
        if not _dispatch_ack_matches(message, acknowledged_nonce):
            raise PermissionError("Child dispatch acknowledgement mismatch")
        message_id = payload.get("message_id")
        if message_id is not None:
            if not isinstance(message_id, str) or not message_id:
                raise PermissionError("Child dispatch acknowledgement has an invalid Message identity")
            messages = dict(job.get("acknowledged_messages", {}))
            if messages.get(acknowledged_nonce) not in {None, message_id}:
                raise PermissionError("Child dispatch acknowledgement changed its Message identity")
            if acknowledged_nonce == job["dispatch_nonce"] and job.get("active_message_id") not in {None, message_id}:
                raise PermissionError("Child dispatch acknowledgement does not match the sent Message")
            messages[acknowledged_nonce] = message_id
            job["acknowledged_messages"] = messages
        acknowledgements = set(job.get("acknowledged_dispatch_nonces", []))
        acknowledgements.add(acknowledged_nonce)
        job["acknowledged_dispatch_nonces"] = sorted(acknowledgements)
        return
    if payload.get("type") == "dispatch_error":
        dispatch_nonce = payload.get("dispatch_nonce")
        known_nonces = {
            item
            for item in job.get("dispatch_nonces", [])
            if isinstance(item, str)
        }
        known_nonces.add(str(job["dispatch_nonce"]))
        if not isinstance(dispatch_nonce, str) or dispatch_nonce not in known_nonces:
            raise PermissionError("Child dispatch error nonce mismatch")
        error_type = payload.get("error_type")
        message_text = payload.get("message")
        if (
            not isinstance(error_type, str)
            or not error_type
            or not isinstance(message_text, str)
            or not message_text
        ):
            raise ValueError("Child dispatch error payload is invalid")
        job["dispatch_error"] = {
            "dispatch_nonce": dispatch_nonce,
            "error_type": error_type[:200],
            "message": message_text[:2000],
            "reported_at": utc_timestamp(),
        }
        return
    if payload.get("type") == "response_ack":
        request_id = payload.get("request_id")
        pending = job.setdefault("pending_responses", {})
        record = pending.get(request_id)
        if not isinstance(request_id, str) or not isinstance(record, dict):
            raise ValueError("unknown worker response acknowledgement")
        if payload.get("response_sha256") != record.get("response_sha256"):
            raise ValueError(
                "worker response acknowledgement hash mismatch: "
                f"request_id={request_id}, "
                f"observed={payload.get('response_sha256')!r}, "
                f"expected={record.get('response_sha256')!r}"
            )
        pending.pop(request_id, None)
        return
    if not isinstance(job.get("registered_at"), str):
        raise PermissionError("worker RPC arrived before Child registration")
    request_id, tool, params = _validate_worker_request(job, payload)
    request_hash = _worker_request_hash(payload)
    settled = job.setdefault("settled_requests", {})
    prior = settled.get(request_id)
    if isinstance(prior, dict):
        if prior.get("request_hash") != request_hash:
            raise ValueError("settled worker RPC request id was reused")
        response = prior.get("response")
        if not isinstance(response, dict):
            raise RuntimeError("settled worker RPC omitted its response")
    else:
        intents = job.setdefault("request_intents", {})
        intent = intents.get(request_id)
        if isinstance(intent, dict) and intent.get("request_hash") != request_hash:
            raise ValueError("in-flight worker RPC request id was reused")
        if not isinstance(intent, dict):
            intent = {
                "request_hash": request_hash,
                "tool": tool,
                "state": "prepared",
                "prepared_at": utc_timestamp(),
            }
            intents[request_id] = intent
        persist()
        intent["state"] = "runtime_mutation_started"
        persist()
        try:
            result = dispatch_worker_tool(
                root_dir,
                job,
                tool,
                params,
                request_id=request_id,
            )
            response = {"ok": True, "result": result}
        except Exception as exc:
            response = {
                "ok": False,
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
        intent["state"] = "settled"
        intent["settled_at"] = utc_timestamp()
        settled[request_id] = {
            "request_hash": request_hash,
            "tool": tool,
            "response": response,
            "settled_at": utc_timestamp(),
        }
    job.setdefault("pending_responses", {})[request_id] = {
        "response_sha256": response_hash(response),
        "reply_to_message_id": message.get("messageId"),
        "attempts": 0,
        "queued_at": utc_timestamp(),
    }
    persist()
    try:
        ReliableRpcChannel(client, SystemClock()).send_response(job, request_id)
    except AgentPosixBridgeError as exc:
        pending = job["pending_responses"][request_id]
        pending["last_error"] = {
            "message": str(exc),
            "error_code": exc.code,
            "completion_unknown": exc.completion_unknown,
        }
    persist()


def receive_worker_messages(
    client: ThinkThreadClient, root_dir: Path | str, job: dict[str, Any],
    *, persist: Callable[[], None],
) -> None:
    if not isinstance(job.get("thinkthread_id"), str):
        return
    _retry_pending_responses(client, job, persist)
    ReliableRpcChannel(client, SystemClock()).receive(
        job, lambda message: _process_worker_message(client, root_dir, job, message, persist),
        persist,
    )
