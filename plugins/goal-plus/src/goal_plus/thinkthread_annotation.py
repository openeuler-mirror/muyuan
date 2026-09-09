from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from goal_plus.execution.python_command import module_arguments
from goal_plus.domain.annotation import (
    EvidenceAnnotationOutput,
    EvidenceAnnotationTask,
    PermanentAnnotationError,
    TransientAnnotationError,
)
from goal_plus.ports.annotation import EvidenceAnnotationResult
from goal_plus.annotation_host import AnnotationHostService
from goal_plus.domain.time import outer_deadline_epoch
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.infrastructure.state.files import exclusive_file_lock, load_json, write_json
from goal_plus.infrastructure.clock import SystemClock, utc_timestamp, utc_timestamp_from_epoch
from goal_plus.execution.thinkthread_child import ChildDriver
from goal_plus.execution.thinkthread_rpc import (
    PROTOCOL, RESPONSE_RETRY_SECONDS, ReliableRpcChannel,
    message_payload, request_hash, response_hash,
)
from goal_plus.thinkthread_agent_posix import (
    AgentPosixBridgeError,
    AgentPosixSdkClient,
    validate_thinkthread_root,
)
from goal_plus.thinkthread_artifacts import ThinkThreadArtifactViewRouter

ANNOTATION_TOOLS = {
    "thinkthread_annotation_next",
    "thinkthread_artifact_view_list",
    "thinkthread_artifact_view_stat",
    "thinkthread_artifact_view_pread",
    "thinkthread_artifact_view_diff",
    "thinkthread_annotation_submit",
}
ANNOTATION_SERVICE_POLL_SECONDS = 0.1
ANNOTATION_SERVICE_ERROR_BACKOFF_SECONDS = 0.25
ANNOTATION_SERVICE_MAX_CONSECUTIVE_ERRORS = 20
ANNOTATION_RPC_MAX_RESPONSE_LIFETIME_SECONDS = 120.0
ANNOTATION_TURN_INTERRUPT_TIMEOUT_SECONDS = 2.0


def _safe_run_id(run_id: str) -> str:
    if not run_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in run_id):
        raise ValueError("invalid ThinkThread annotation run_id")
    return run_id


def _root(root_dir: Path | str, run_id: str) -> Path:
    return (
        Path(root_dir).expanduser().resolve()
        / "host-pools"
        / "thinkthread-annotations"
        / _safe_run_id(run_id)
    )


def _state_path(root_dir: Path | str, run_id: str) -> Path:
    return _root(root_dir, run_id) / "state.json"


def _lock_path(root_dir: Path | str, run_id: str) -> Path:
    return _root(root_dir, run_id) / "state.lock"


def _service_path(root_dir: Path | str, run_id: str) -> Path:
    return _root(root_dir, run_id) / "service.json"


def _service_lock_path(root_dir: Path | str, run_id: str) -> Path:
    return _root(root_dir, run_id) / "service.lock"


def _service_log_path(root_dir: Path | str, run_id: str) -> Path:
    return _root(root_dir, run_id) / "service.log"


def _load_state(root_dir: Path | str, run_id: str) -> dict[str, Any] | None:
    path = _state_path(root_dir, run_id)
    return load_json(path) if path.exists() else None


def _write_state(root_dir: Path | str, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_timestamp()
    write_json(_state_path(root_dir, str(state["run_id"])), state)


def _load_service(root_dir: Path | str, run_id: str) -> dict[str, Any] | None:
    path = _service_path(root_dir, run_id)
    return load_json(path) if path.exists() else None


def _write_service(
    root_dir: Path | str,
    run_id: str,
    service: dict[str, Any],
) -> None:
    service["updated_at"] = utc_timestamp()
    write_json(_service_path(root_dir, run_id), service)


def _append_service_log(root_dir: Path | str, run_id: str, message: str) -> None:
    path = _service_log_path(root_dir, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{utc_timestamp()} {message}\n")


def _process_matches_service(pid: int, run_id: str, generation: str) -> bool:
    if pid <= 0 or not generation:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True

    cmdline_path = Path(f"/proc/{pid}/cmdline")
    if not cmdline_path.exists():  # pragma: no cover - non-Linux POSIX
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        if stat.rsplit(")", 1)[-1].strip().startswith("Z"):
            return False
        command = cmdline_path.read_bytes().replace(b"\0", b" ").decode(
            "utf-8", errors="replace"
        )
    except OSError:
        return True
    return (
        "goal_plus.thinkthread_annotation" in command
        and " serve " in f" {command} "
        and run_id in command
        and generation in command
    )


def _active_task(
    host: AnnotationHostService,
    state: dict[str, Any],
) -> EvidenceAnnotationTask:
    identity = state.get("active_task")
    if not isinstance(identity, dict):
        raise PermissionError("ThinkThread Annotation Child has no claimed task")
    return host.workflow.bound_task(
        run_id=str(state["run_id"]),
        candidate_id=str(identity.get("candidate_id")),
        iteration=int(identity.get("iteration", 0)),
        attempts=int(identity.get("attempts", 0)),
        queue_generation=int(identity.get("queue_generation", 0)),
        queue_revision=int(identity.get("queue_revision", 0)),
    )


def _artifact_router(
    client: AgentPosixSdkClient,
    task: EvidenceAnnotationTask,
) -> ThinkThreadArtifactViewRouter:
    views: dict[str, ArtifactRef] = {}
    for reference in task.artifact_views:
        artifact = reference.artifact_ref
        if artifact.kind != "fs_snapshot" or artifact.provider != "thinkthread":
            raise PermissionError("ThinkThread Annotation Child received a non-snapshot View")
        views[reference.view_id] = artifact
    return ThinkThreadArtifactViewRouter(client, views)


def _claim_next(
    host: AnnotationHostService,
    state: dict[str, Any],
) -> dict[str, Any]:
    queue, task = host.admission.claim_next(str(state["run_id"]))
    if task is None:
        state["active_task"] = None
        return {
            "state": "closed" if queue.state != "open" else "idle",
            "generation": queue.generation,
            "revision": queue.revision,
        }
    state["active_task"] = {
        "candidate_id": task.candidate_id,
        "iteration": task.iteration,
        "attempts": task.attempts,
        "queue_generation": task.queue_generation,
        "queue_revision": task.queue_revision,
    }
    return {
        "state": "claimed",
        "task": host.context.build(task.run_id, task.candidate_id, task.iteration),
    }


def _dispatch_tool(
    client: AgentPosixSdkClient,
    root_dir: Path | str,
    state: dict[str, Any],
    tool: str,
    params: dict[str, Any],
) -> Any:
    host = AnnotationHostService(root_dir)
    run_id = str(state["run_id"])
    if "run_id" in params and params["run_id"] != run_id:
        raise PermissionError("ThinkThread annotation run binding mismatch")
    if tool == "thinkthread_annotation_next":
        return _claim_next(host, state)
    task = _active_task(host, state)
    router = _artifact_router(client, task)
    if tool == "thinkthread_artifact_view_list":
        return router.readdir(str(params["view_id"]), str(params.get("path") or "."))
    if tool == "thinkthread_artifact_view_stat":
        return router.stat(str(params["view_id"]), str(params.get("path") or "."))
    if tool == "thinkthread_artifact_view_pread":
        return router.pread(
            str(params["view_id"]),
            str(params["path"]),
            offset=int(params.get("offset") or 0),
            length=int(params.get("length") or 64 * 1024),
        )
    if tool == "thinkthread_artifact_view_diff":
        return router.diff(
            str(params["base_view_id"]),
            str(params["target_view_id"]),
        )
    if tool == "thinkthread_annotation_submit":
        output = EvidenceAnnotationOutput.model_validate(params)
        result = EvidenceAnnotationResult(
            description=output.description,
            supplemental_evaluation=output.supplemental_evaluation,
            comparison_basis=list(task.comparison_basis),
            tool_views=list(output.tool_views),
            usage={},
        )
        published = host.finish_annotation_task(task, result=result)
        state["active_task"] = None
        state["last_submission"] = {
            "candidate_id": task.candidate_id,
            "iteration": task.iteration,
            "published": published,
            "submitted_at": utc_timestamp(),
        }
        return {"published": published}
    raise PermissionError(f"ThinkThread annotation RPC tool is not allowed: {tool}")


def _request_deadline_epoch(request: dict[str, Any], *, now_epoch: float) -> float:
    value = request.get("deadline_unix_ms")
    if value is None:
        return now_epoch + ANNOTATION_RPC_MAX_RESPONSE_LIFETIME_SECONDS
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise ValueError("annotation RPC deadline_unix_ms must be a positive number")
    return min(
        float(value) / 1000.0,
        now_epoch + ANNOTATION_RPC_MAX_RESPONSE_LIFETIME_SECONDS,
    )


def _expire_pending_response(
    state: dict[str, Any],
    request_id: str,
    record: dict[str, Any],
    *,
    now_epoch: float,
) -> None:
    state.setdefault("pending_responses", {}).pop(request_id, None)
    state.setdefault("expired_responses", {})[request_id] = {
        "response_sha256": record.get("response_sha256"),
        "queued_at": record.get("queued_at"),
        "expires_at": record.get("expires_at"),
        "attempts": int(record.get("attempts", 0)),
        "expired_at": utc_timestamp_from_epoch(now_epoch),
        "reason": "child_request_deadline_elapsed",
    }


def _retry_responses(client: AgentPosixSdkClient, state: dict[str, Any]) -> None:
    now = time.time()
    for request_id, record in list(state.setdefault("pending_responses", {}).items()):
        if not isinstance(request_id, str) or not isinstance(record, dict):
            raise TypeError("ThinkThread annotation response queue is invalid")
        expires_at = record.get("expires_at_unix")
        if not isinstance(expires_at, (int, float)):
            settled = state.setdefault("settled_requests", {}).get(request_id)
            settled_deadline = (
                settled.get("request_deadline_unix")
                if isinstance(settled, dict)
                else None
            )
            queued_at = outer_deadline_epoch(
                str(record.get("queued_at") or "")
            )
            expires_at = min(
                float(settled_deadline)
                if isinstance(settled_deadline, (int, float))
                else now + ANNOTATION_RPC_MAX_RESPONSE_LIFETIME_SECONDS,
                (queued_at or now) + ANNOTATION_RPC_MAX_RESPONSE_LIFETIME_SECONDS,
            )
            record["expires_at_unix"] = expires_at
            record["expires_at"] = utc_timestamp_from_epoch(expires_at)
        if float(expires_at) <= now:
            _expire_pending_response(
                state,
                request_id,
                record,
                now_epoch=now,
            )
            continue
        last_attempt = record.get("last_attempt_unix")
        if isinstance(last_attempt, (int, float)) and (
            now - float(last_attempt) < RESPONSE_RETRY_SECONDS
        ):
            continue
        ReliableRpcChannel(client, SystemClock()).send_response(state, request_id)


def _process_message(
    client: AgentPosixSdkClient,
    root_dir: Path | str,
    state: dict[str, Any],
    message: dict[str, Any],
) -> None:
    payload = message_payload(message)
    if payload is None or payload.get("protocol") != PROTOCOL:
        return
    message_type = payload.get("type")
    if message_type == "registration":
        if payload.get("registration_nonce") != state.get("registration_nonce"):
            raise PermissionError("ThinkThread annotator registration nonce mismatch")
        state["registered_at"] = utc_timestamp()
        return
    if message_type == "dispatch_ack":
        observed_nonce = payload.get("dispatch_nonce")
        if observed_nonce != state.get("dispatch_nonce"):
            if observed_nonce in state.get("issued_dispatch_nonces", []):
                return
            raise PermissionError("ThinkThread annotator dispatch nonce mismatch")
        state["dispatch_acknowledged_at"] = utc_timestamp()
        return
    if message_type == "response_ack":
        request_id = payload.get("request_id")
        record = state.setdefault("pending_responses", {}).get(request_id)
        if not isinstance(request_id, str):
            raise ValueError("unknown ThinkThread annotation response acknowledgement")
        if not isinstance(record, dict):
            expired = state.setdefault("expired_responses", {}).get(request_id)
            if not isinstance(expired, dict):
                raise ValueError("unknown ThinkThread annotation response acknowledgement")
            if payload.get("response_sha256") != expired.get("response_sha256"):
                raise ValueError("ThinkThread annotation response acknowledgement mismatch")
            expired["acknowledged_after_expiry_at"] = utc_timestamp()
            return
        if payload.get("response_sha256") != record.get("response_sha256"):
            raise ValueError("ThinkThread annotation response acknowledgement mismatch")
        state["pending_responses"].pop(request_id, None)
        return
    if not isinstance(state.get("registered_at"), str):
        raise PermissionError("ThinkThread annotation RPC arrived before registration")
    if payload.get("type") != "request" or payload.get("protocol") != PROTOCOL:
        raise ValueError("unsupported ThinkThread annotation RPC envelope")
    request_id = payload.get("request_id")
    tool = payload.get("tool")
    params = payload.get("params")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("ThinkThread annotation RPC request_id is required")
    if tool not in ANNOTATION_TOOLS:
        raise PermissionError(f"ThinkThread annotation RPC tool is not allowed: {tool}")
    if not isinstance(params, dict):
        raise TypeError("ThinkThread annotation RPC params must be an object")
    if payload.get("agent_session_id") != state.get("agent_session_id"):
        raise PermissionError("ThinkThread annotation RPC session binding mismatch")
    if not isinstance(payload.get("content_json"), str):
        raise TypeError("annotation RPC content_json must be a string")
    digest = request_hash(payload)
    if payload.get("content_sha256") != digest:
        raise ValueError("ThinkThread annotation RPC content_sha256 mismatch")
    now_epoch = time.time()
    request_deadline = _request_deadline_epoch(payload, now_epoch=now_epoch)
    settled = state.setdefault("settled_requests", {})
    prior = settled.get(request_id)
    if isinstance(prior, dict):
        if prior.get("request_hash") != digest:
            raise ValueError("settled ThinkThread annotation request id was reused")
        response = prior.get("response")
        if not isinstance(response, dict):
            raise TypeError("settled ThinkThread annotation request omitted response")
    else:
        state.setdefault("request_intents", {})[request_id] = {
            "request_hash": digest,
            "tool": tool,
            "state": "runtime_mutation_started",
            "prepared_at": utc_timestamp(),
        }
        _write_state(root_dir, state)
        if request_deadline <= now_epoch:
            response = {
                "ok": False,
                "error": {
                    "type": "TimeoutError",
                    "message": "ThinkThread annotation RPC request deadline elapsed",
                },
            }
        else:
            try:
                result = _dispatch_tool(
                    client,
                    root_dir,
                    state,
                    str(tool),
                    params,
                )
                response = {"ok": True, "result": result}
            except Exception as exc:  # noqa: BLE001 - RPC serializes tool failures.
                response = {
                    "ok": False,
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                }
        state["request_intents"][request_id]["state"] = "settled"
        state["request_intents"][request_id]["settled_at"] = utc_timestamp()
        settled[request_id] = {
            "request_hash": digest,
            "tool": tool,
            "response": response,
            "request_deadline_unix": request_deadline,
            "settled_at": utc_timestamp(),
        }
    response_digest = response_hash(response)
    pending = state.setdefault("pending_responses", {})
    record = pending.get(request_id)
    if isinstance(record, dict):
        if record.get("response_sha256") != response_digest:
            raise ValueError("pending ThinkThread annotation response hash changed")
    else:
        record = {
            "response_sha256": response_digest,
            "reply_to_message_id": message.get("messageId"),
            "attempts": 0,
            "queued_at": utc_timestamp(),
            "queued_at_unix": now_epoch,
            "expires_at": utc_timestamp_from_epoch(request_deadline),
            "expires_at_unix": request_deadline,
        }
        pending[request_id] = record
    if request_deadline <= now_epoch:
        _expire_pending_response(
            state,
            request_id,
            record,
            now_epoch=now_epoch,
        )
        _write_state(root_dir, state)
        return
    _write_state(root_dir, state)
    ReliableRpcChannel(client, SystemClock()).send_response(state, request_id)


def _receive_messages(
    client: AgentPosixSdkClient, root_dir: Path | str, state: dict[str, Any],
) -> None:
    if not isinstance(state.get("thinkthread_id"), str):
        return
    _retry_responses(client, state)
    ReliableRpcChannel(client, SystemClock()).receive(
        state, lambda message: _process_message(client, root_dir, state, message),
        lambda: _write_state(root_dir, state),
    )


def _model_for_task(task: EvidenceAnnotationTask) -> dict[str, str] | None:
    model = task.profile.model if task.profile is not None else None
    if model is None:
        return None
    if "/" not in model:
        raise ValueError("ThinkThread annotator model requires exact provider/model")
    provider, model_id = model.split("/", 1)
    if not provider or not model_id:
        raise ValueError("ThinkThread annotator model requires exact provider/model")
    return {"provider": provider, "model": model_id}


def _dispatch_message(state: dict[str, Any]) -> str:
    return (
        "你是 Goal Plus retained Evidence Annotation Child。只处理 Runtime 已录取的"
        " annotation task；先调用 thinkthread_annotation_next，只可使用"
        " thinkthread_artifact_view_list、thinkthread_artifact_view_stat、"
        " thinkthread_artifact_view_pread、thinkthread_artifact_view_diff 读取授权的"
        " target/peer immutable snapshots，最后调用 thinkthread_annotation_submit。"
        "不得运行命令、代码、测试或 verifier，也不得调用以上清单外的工具。重复直到"
        " next 返回 idle；不得修改 Root、candidate branch、score 或 selection。\n\n"
        f"goal_plus_thinkthread_role=annotator; run_id={state['run_id']}; "
        f"agent_session_id={state['agent_session_id']}; "
        f"registration_nonce={state['registration_nonce']}; "
        f"dispatch_nonce={state['dispatch_nonce']}; "
        f"queue_generation={state['queue_generation']}; "
        f"queue_revision={state['target_revision']}."
    )


def _bind_spawn_result(state: dict[str, Any], result: dict[str, Any]) -> None:
    binding = ChildDriver.binding(result)
    child_id, branch_id = binding.child_id, binding.branch_id
    state.update(
        {
            "status": "running",
            "thinkthread_id": child_id,
            "fs_branch_id": branch_id,
            "active_message_id": result.get("initialMessageId"),
            "started_at": utc_timestamp(),
        }
    )


def _recover_spawn(
    client: AgentPosixSdkClient,
    root_dir: Path | str,
    state: dict[str, Any],
) -> bool:
    intent = state.get("spawn_intent")
    if not isinstance(intent, dict) or intent.get("state") not in {
        "platform_mutation_started",
        "outcome_unknown",
    }:
        return False
    before = {
        item for item in intent.get("children_before", []) if isinstance(item, str)
    }
    matches = ChildDriver(client, SystemClock()).recovery_matches(
        before=before, bound=set(), nonce=str(state["registration_nonce"]),
        root_id=state.get("root_thinkthread_id"),
    )
    intent["recovery_candidates"] = [
        str(child.get("thinkthreadId")) for child, _page in matches
    ]
    intent["last_recovery_at"] = utc_timestamp()
    if len(matches) != 1:
        _write_state(root_dir, state)
        return False
    child, page = matches[0]
    _bind_spawn_result(state, child)
    seen = set(state.get("seen_message_ids", []))
    for message in page.get("messages", []):
        if not isinstance(message, dict):
            continue
        message_id = message.get("messageId")
        if not isinstance(message_id, str) or message_id in seen:
            continue
        seen.add(message_id)
        _process_message(client, root_dir, state, message)
    state["seen_message_ids"] = sorted(seen)
    state["message_cursor"] = page.get("nextCursor")
    intent["state"] = "bound_after_recovery"
    intent["recovered_at"] = utc_timestamp()
    state.pop("last_error", None)
    _write_state(root_dir, state)
    return True


def _recover_wake(
    client: AgentPosixSdkClient,
    root_dir: Path | str,
    state: dict[str, Any],
) -> bool:
    intent = state.get("wake_intent")
    if not isinstance(intent, dict) or intent.get("state") not in {
        "platform_mutation_started",
        "outcome_unknown",
    }:
        return False
    _receive_messages(client, root_dir, state)
    if not isinstance(state.get("dispatch_acknowledged_at"), str):
        intent["last_recovery_at"] = utc_timestamp()
        _write_state(root_dir, state)
        return False
    state["status"] = "running"
    intent["state"] = "acknowledged_after_recovery"
    intent["recovered_at"] = utc_timestamp()
    state.pop("last_error", None)
    _write_state(root_dir, state)
    return True


def _spawn(
    client: AgentPosixSdkClient,
    root_dir: Path | str,
    host: AnnotationHostService,
    run_id: str,
) -> dict[str, Any]:
    root_view = validate_thinkthread_root(client)
    dispatch = host.admission.prepare_thinkthread_dispatch(run_id)
    task = dispatch.task
    queue = dispatch.queue
    state: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "root_thinkthread_id": root_view.get("thinkthreadId"),
        "agent_session_id": f"annotator-{run_id}",
        "status": "starting",
        "thinkthread_id": None,
        "fs_branch_id": None,
        "registration_nonce": str(uuid.uuid4()),
        "dispatch_nonce": str(uuid.uuid4()),
        "issued_dispatch_nonces": [],
        "queue_generation": queue.generation,
        "target_revision": queue.revision,
        "message_cursor": None,
        "seen_message_ids": [],
        "request_intents": {},
        "settled_requests": {},
        "pending_responses": {},
        "active_task": None,
        "created_at": utc_timestamp(),
        "updated_at": utc_timestamp(),
    }
    state["issued_dispatch_nonces"].append(state["dispatch_nonce"])
    _write_state(root_dir, state)
    params: dict[str, Any] = {
        "profile": "self",
        "fs": "private",
        "fsSnapshotId": dispatch.baseline_snapshot_id,
        "capabilities": ["thinkthread.message"],
        "initialMessage": _dispatch_message(state),
    }
    model = _model_for_task(task)
    if model is not None:
        params["model"] = model
    children = client.invoke("thinkthread.list").get("children")
    if not isinstance(children, list):
        raise AgentPosixBridgeError("thinkthread.list omitted children")
    state["spawn_intent"] = {
        "state": "platform_mutation_started",
        "params_sha256": hashlib.sha256(
            json.dumps(params, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "children_before": [
            item.get("thinkthreadId")
            for item in children
            if isinstance(item, dict) and isinstance(item.get("thinkthreadId"), str)
        ],
        "prepared_at": utc_timestamp(),
    }
    _write_state(root_dir, state)
    try:
        result = ChildDriver(client, SystemClock()).spawn(params)
        _bind_spawn_result(state, result)
        state["spawn_intent"]["state"] = "bound"
    except AgentPosixBridgeError as exc:
        state["status"] = "needs_recovery" if exc.completion_unknown else "failed"
        state["spawn_intent"]["state"] = (
            "outcome_unknown" if exc.completion_unknown else "failed"
        )
        state["last_error"] = {
            "message": str(exc),
            "error_code": exc.code,
            "completion_unknown": exc.completion_unknown,
        }
    _write_state(root_dir, state)
    return state


def _wake(
    client: AgentPosixSdkClient,
    root_dir: Path | str,
    state: dict[str, Any],
    *,
    generation: int,
    revision: int,
) -> dict[str, Any]:
    child_id = state.get("thinkthread_id")
    if not isinstance(child_id, str):
        raise TypeError("ThinkThread annotator has no retained Child")
    state["queue_generation"] = generation
    state["target_revision"] = revision
    state["dispatch_nonce"] = str(uuid.uuid4())
    state.setdefault("issued_dispatch_nonces", []).append(state["dispatch_nonce"])
    state.pop("dispatch_acknowledged_at", None)
    body = _dispatch_message(state)
    state["wake_intent"] = {
        "state": "platform_mutation_started",
        "dispatch_nonce": state["dispatch_nonce"],
        "message_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "prepared_at": utc_timestamp(),
    }
    _write_state(root_dir, state)
    try:
        message_id = ChildDriver(client, SystemClock()).wake(child_id, body)
        state["active_message_id"] = message_id
        state["status"] = "running"
        state["wake_intent"].update(
            {"state": "sent", "message_id": message_id, "sent_at": utc_timestamp()}
        )
    except AgentPosixBridgeError as exc:
        state["status"] = "needs_recovery" if exc.completion_unknown else "failed"
        state["wake_intent"]["state"] = (
            "outcome_unknown" if exc.completion_unknown else "failed"
        )
        state["last_error"] = {
            "message": str(exc),
            "error_code": exc.code,
            "completion_unknown": exc.completion_unknown,
        }
    _write_state(root_dir, state)
    return state


def _settle_active_turn(
    client: AgentPosixSdkClient,
    root_dir: Path | str,
    host: AnnotationHostService,
    state: dict[str, Any],
    *,
    outcome: str,
    deadline_expired: bool,
) -> bool:
    if not isinstance(state.get("active_task"), dict):
        return True
    task = _active_task(host, state)
    if deadline_expired:
        error: Exception = PermanentAnnotationError(
            "annotation outer deadline expired during retained Child turn"
        )
        child_id = state.get("thinkthread_id")
        if not isinstance(child_id, str):
            raise TypeError("ThinkThread annotator state omitted Child identity")
        if (
            ChildDriver(client, SystemClock()).interrupt(
                child_id,
                ANNOTATION_TURN_INTERRUPT_TIMEOUT_SECONDS,
            )
            is None
        ):
            state["status"] = "failed"
            state["last_error"] = {
                "message": "ThinkThread annotation deadline interrupt did not stop Child",
                "completion_unknown": False,
            }
            return False
    else:
        error = TransientAnnotationError(
            f"ThinkThread annotation turn ended as {outcome} before submission"
        )
    host.finish_annotation_task(task, error=error)
    state["active_task"] = None
    state["finished_at"] = utc_timestamp()
    state["last_turn_recovery"] = {
        "candidate_id": task.candidate_id,
        "iteration": task.iteration,
        "attempt": task.attempts,
        "outcome": outcome,
        "deadline_expired": deadline_expired,
        "settled_at": utc_timestamp(),
    }
    return True


def wake_thinkthread_annotation(
    *,
    root_dir: Path | str,
    run_id: str,
    client: AgentPosixSdkClient | None = None,
) -> dict[str, Any] | None:
    sdk = client or AgentPosixSdkClient()
    host = AnnotationHostService(root_dir)
    queue = host.workflow.queue(run_id)
    if queue.state != "open" or not host.admission.eligible(run_id):
        return _load_state(root_dir, run_id)
    queue = host.workflow.queue(run_id)
    with exclusive_file_lock(_lock_path(root_dir, run_id)):
        state = _load_state(root_dir, run_id)
        if state is None:
            return _spawn(sdk, root_dir, host, run_id)
        if state.get("status") == "needs_recovery":
            close_intent = state.get("close_intent")
            if isinstance(close_intent, dict) and close_intent.get("state") != "closed":
                return state
            if isinstance(state.get("wake_intent"), dict):
                _recover_wake(sdk, root_dir, state)
            else:
                _recover_spawn(sdk, root_dir, state)
            return state
        if state.get("status") in {"closed", "closing", "failed"}:
            return state
        state["target_revision"] = queue.revision
        state["queue_generation"] = queue.generation
        if state.get("status") == "ready":
            return _wake(
                sdk,
                root_dir,
                state,
                generation=queue.generation,
                revision=queue.revision,
            )
        _write_state(root_dir, state)
        return state


def kick_thinkthread_annotation_service(
    *,
    root_dir: Path | str,
    run_id: str,
) -> bool:
    """Ensure the retained Child has one live host-side RPC service."""

    with exclusive_file_lock(_service_lock_path(root_dir, run_id)):
        state = _load_state(root_dir, run_id)
        if state is None or state.get("status") not in {"running", "ready"}:
            return False
        current = _load_service(root_dir, run_id)
        # The owned loop's terminal record is authoritative even when the Root
        # denies PID probes. An exited service must not fence a retained wake.
        if isinstance(current, dict) and current.get("status") not in {"exited", "stopped"}:
            try:
                pid = int(current.get("pid") or 0)
            except (TypeError, ValueError):
                pid = 0
            if _process_matches_service(
                pid,
                run_id,
                str(current.get("generation") or ""),
            ):
                return False

        generation = uuid.uuid4().hex
        source_root = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        existing_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            str(source_root)
            if not existing_pythonpath
            else os.pathsep.join((str(source_root), existing_pythonpath))
        )
        command = [
            sys.executable,
            *module_arguments("goal_plus.thinkthread_annotation", []),
            "serve",
            "--root",
            str(Path(root_dir).expanduser().resolve()),
            "--run-id",
            run_id,
            "--generation",
            generation,
        ]
        log_path = _service_log_path(root_dir, run_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_handle:
            launch_options = {
                "stdin": subprocess.DEVNULL,
                "stdout": log_handle,
                "stderr": subprocess.STDOUT,
                "close_fds": True,
                "env": environment,
            }
            start_new_session = True
            try:
                process = subprocess.Popen(
                    command,
                    **launch_options,
                    start_new_session=True,
                )
            except PermissionError as exc:
                # Some protected Roots deny setsid(2). The service still
                # outlives the short-lived tool process with detached stdio.
                _append_service_log(
                    root_dir,
                    run_id,
                    f"detached launch unavailable ({type(exc).__name__}: {exc}); "
                    "retrying without a new session",
                )
                process = subprocess.Popen(
                    command,
                    **launch_options,
                    start_new_session=False,
                )
                start_new_session = False
        _write_service(
            root_dir,
            run_id,
            {
                "schema_version": 1,
                "run_id": run_id,
                "generation": generation,
                "pid": int(process.pid),
                "status": "running",
                "start_new_session": start_new_session,
                "started_at": utc_timestamp(),
            },
        )
        return True


def kick_thinkthread_annotation(
    *,
    root_dir: Path | str,
    run_id: str,
    client: AgentPosixSdkClient | None = None,
) -> dict[str, Any] | None:
    """Wake the retained Child and start its tracked RPC service when supported."""

    from goal_plus.host_scope import internal_agent_run_admission

    with internal_agent_run_admission(root_dir=root_dir, run_id=run_id):
        sdk = client or AgentPosixSdkClient()
        state = wake_thinkthread_annotation(
            root_dir=root_dir,
            run_id=run_id,
            client=sdk,
        )
        if (
            isinstance(state, dict)
            and state.get("status") in {"running", "ready"}
            and getattr(sdk, "supports_detached_service", False)
        ):
            kick_thinkthread_annotation_service(root_dir=root_dir, run_id=run_id)
        return state


def service_thinkthread_annotation(
    *,
    root_dir: Path | str,
    run_id: str,
    client: AgentPosixSdkClient | None = None,
) -> dict[str, Any] | None:
    sdk = client or AgentPosixSdkClient()
    host = AnnotationHostService(root_dir)
    with exclusive_file_lock(_lock_path(root_dir, run_id)):
        state = _load_state(root_dir, run_id)
        if state is None:
            return state
        if state.get("status") == "needs_recovery":
            close_intent = state.get("close_intent")
            if isinstance(close_intent, dict) and close_intent.get("state") != "closed":
                return state
            if isinstance(state.get("wake_intent"), dict):
                _recover_wake(sdk, root_dir, state)
            else:
                _recover_spawn(sdk, root_dir, state)
        if state.get("status") not in {"running", "ready"}:
            return state
        _receive_messages(sdk, root_dir, state)
        child_id = state.get("thinkthread_id")
        if not isinstance(child_id, str):
            raise TypeError("ThinkThread annotator state omitted Child identity")
        child = sdk.invoke("thinkthread.get", {"id": child_id})
        turn_outcome = ChildDriver(sdk, SystemClock()).turn_terminal(child, state.get("active_message_id"))
        deadline_expired = False
        if isinstance(state.get("active_task"), dict):
            task = _active_task(host, state)
            deadline = outer_deadline_epoch(task.outer_deadline_at)
            deadline_expired = deadline is not None and deadline <= time.time()
        if deadline_expired:
            if _settle_active_turn(
                sdk,
                root_dir,
                host,
                state,
                outcome="deadline_expired",
                deadline_expired=True,
            ):
                state["status"] = "ready"
        elif turn_outcome is not None:
            if isinstance(state.get("active_task"), dict):
                settled = _settle_active_turn(
                    sdk,
                    root_dir,
                    host,
                    state,
                    outcome=turn_outcome,
                    deadline_expired=False,
                )
            else:
                settled = True
                state["finished_at"] = utc_timestamp()
            state["status"] = (
                "ready"
                if settled and child.get("agentState") == "ready"
                else "failed"
            )
            if state["status"] == "failed":
                state["last_error"] = {
                    "message": f"ThinkThread annotation turn ended as {turn_outcome}",
                    "completion_unknown": False,
                }
        queue = host.workflow.queue(run_id)
        eligible = host.admission.eligible(run_id)
        queue = host.workflow.queue(run_id)
        if (
            state.get("status") == "ready"
            and queue.state == "open"
            and eligible
        ):
            return _wake(
                sdk,
                root_dir,
                state,
                generation=queue.generation,
                revision=queue.revision,
            )
        _write_state(root_dir, state)
        return state


def _service_owned(root_dir: Path | str, run_id: str, generation: str) -> bool:
    service = _load_service(root_dir, run_id)
    if not isinstance(service, dict) or service.get("generation") != generation:
        return False
    try:
        return int(service.get("pid") or 0) == os.getpid()
    except (TypeError, ValueError):
        return False


def serve_thinkthread_annotation(
    *,
    root_dir: Path | str,
    run_id: str,
    generation: str | None = None,
    client: AgentPosixSdkClient | None = None,
    poll_seconds: float = ANNOTATION_SERVICE_POLL_SECONDS,
) -> int:
    """Service retained-Child Message RPC until the current queue becomes idle."""

    sdk = client or AgentPosixSdkClient()
    host = AnnotationHostService(root_dir)
    consecutive_errors = 0
    last_heartbeat = 0.0
    if generation is not None:
        ownership_deadline = time.monotonic() + 2.0
        while not _service_owned(root_dir, run_id, generation):
            service = _load_service(root_dir, run_id)
            if (
                isinstance(service, dict)
                and service.get("generation") not in {None, generation}
            ) or time.monotonic() >= ownership_deadline:
                return 0
            time.sleep(0.01)
    try:
        while generation is None or _service_owned(root_dir, run_id, generation):
            try:
                state = service_thinkthread_annotation(
                    root_dir=root_dir,
                    run_id=run_id,
                    client=sdk,
                )
                consecutive_errors = 0
            except Exception as exc:  # noqa: BLE001 - durable service records failures.
                consecutive_errors += 1
                _append_service_log(
                    root_dir,
                    run_id,
                    f"service error {consecutive_errors}: {type(exc).__name__}: {exc}",
                )
                if consecutive_errors >= ANNOTATION_SERVICE_MAX_CONSECUTIVE_ERRORS:
                    return 1
                time.sleep(ANNOTATION_SERVICE_ERROR_BACKOFF_SECONDS)
                continue

            if state is None or state.get("status") in {"closed", "closing", "failed"}:
                return 0
            queue = host.workflow.queue(run_id)
            if queue.state != "open":
                return 0
            if (
                state.get("status") == "ready"
                and state.get("active_task") is None
                and not host.admission.eligible(run_id)
            ):
                if host.workflow.retry_delay(run_id) is None:
                    return 0

            now = time.monotonic()
            if generation is not None and now - last_heartbeat >= 1.0:
                with exclusive_file_lock(_service_lock_path(root_dir, run_id)):
                    if _service_owned(root_dir, run_id, generation):
                        service = _load_service(root_dir, run_id)
                        if isinstance(service, dict):
                            service["last_serviced_at"] = utc_timestamp()
                            _write_service(root_dir, run_id, service)
                last_heartbeat = now
            time.sleep(max(0.01, poll_seconds))
        return 0
    finally:
        if generation is not None:
            with exclusive_file_lock(_service_lock_path(root_dir, run_id)):
                service = _load_service(root_dir, run_id)
                if (
                    isinstance(service, dict)
                    and service.get("generation") == generation
                    and int(service.get("pid") or 0) == os.getpid()
                ):
                    service["status"] = "exited"
                    service["finished_at"] = utc_timestamp()
                    _write_service(root_dir, run_id, service)


def _stop_thinkthread_annotation_service(
    root_dir: Path | str,
    run_id: str,
    *,
    timeout_seconds: float,
) -> None:
    with exclusive_file_lock(_service_lock_path(root_dir, run_id)):
        service = _load_service(root_dir, run_id)
        if not isinstance(service, dict):
            return
        if service.get("status") in {"stale", "stopped", "exited"}:
            return
        try:
            pid = int(service.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        generation = str(service.get("generation") or "")
        if not _process_matches_service(pid, run_id, generation):
            service["status"] = "stale"
            service["finished_at"] = utc_timestamp()
            _write_service(root_dir, run_id, service)
            return
        service["status"] = "stopping"
        service["stop_requested_at"] = utc_timestamp()
        _write_service(root_dir, run_id, service)

    def send_signal(signum: signal.Signals) -> None:
        try:
            if service.get("start_new_session") is True:
                os.killpg(pid, signum)
            else:
                os.kill(pid, signum)
        except ProcessLookupError:
            pass

    try:
        send_signal(signal.SIGTERM)
    except PermissionError:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while time.monotonic() < deadline and _process_matches_service(
        pid, run_id, generation
    ):
        time.sleep(0.05)
    if _process_matches_service(pid, run_id, generation):
        try:
            send_signal(signal.SIGKILL)
        except PermissionError:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        kill_deadline = time.monotonic() + 1.0
        while time.monotonic() < kill_deadline and _process_matches_service(
            pid, run_id, generation
        ):
            time.sleep(0.05)
    if _process_matches_service(pid, run_id, generation):
        raise RuntimeError(f"ThinkThread annotation service {pid} did not stop")
    with exclusive_file_lock(_service_lock_path(root_dir, run_id)):
        latest = _load_service(root_dir, run_id)
        if isinstance(latest, dict) and latest.get("generation") == generation:
            latest["status"] = "stopped"
            latest["finished_at"] = utc_timestamp()
            _write_service(root_dir, run_id, latest)


def close_thinkthread_annotation(
    *,
    root_dir: Path | str,
    run_id: str,
    client: AgentPosixSdkClient | None = None,
    timeout_seconds: float = 15.0,
) -> dict[str, Any] | None:
    sdk = client or AgentPosixSdkClient()
    _stop_thinkthread_annotation_service(
        root_dir,
        run_id,
        timeout_seconds=min(timeout_seconds, 5.0),
    )
    with exclusive_file_lock(_lock_path(root_dir, run_id)):
        state = _load_state(root_dir, run_id)
        if state is None or state.get("status") == "closed":
            return state
        state["status"] = "closing"
        state.setdefault(
            "close_intent",
            {"state": "prepared", "prepared_at": utc_timestamp()},
        )
        _write_state(root_dir, state)
        child_id = state.get("thinkthread_id")
        branch_id = state.get("fs_branch_id")
        try:
            sdk.preflight()
            root_view = sdk.self_view()
            if (
                root_view.get("parentThinkthreadId") is not None
                or root_view.get("thinkthreadId") != state.get("root_thinkthread_id")
            ):
                raise PermissionError(
                    "ThinkThread annotation cleanup Root ownership mismatch"
                )
            child: dict[str, Any] | None = None
            if isinstance(child_id, str):
                child = ChildDriver(sdk, SystemClock()).observe(
                    child_id, root_id=state.get("root_thinkthread_id"), branch_id=branch_id,
                )
                if (
                    child is not None
                    and child.get("executionState") != "absent"
                    and ChildDriver(sdk, SystemClock()).interrupt(child_id, timeout_seconds) is None
                ):
                    raise RuntimeError("ThinkThread Annotation Child remained active")
            if isinstance(branch_id, str):
                try:
                    branch = sdk.invoke("fs.branch.stat", {"branchId": branch_id})
                except AgentPosixBridgeError as exc:
                    if exc.code != "FsBranchNotFound":
                        raise
                    branch = None
                if branch is not None and branch.get("branchId") != branch_id:
                    raise PermissionError("ThinkThread annotation branch ownership mismatch")
                if branch is not None and branch.get("thinkthreadId") != child_id:
                    raise PermissionError(
                        "ThinkThread annotation branch Child ownership mismatch"
                    )
                if branch is not None and branch.get("state") != "removed":
                    generation = branch.get("controlGeneration")
                    if not isinstance(generation, int):
                        raise TypeError("annotation branch omitted controlGeneration")
                    state["close_intent"]["state"] = "removing_branch"
                    state["close_intent"]["branch_generation"] = generation
                    _write_state(root_dir, state)
                    try:
                        sdk.invoke(
                            "fs.branch.remove",
                            {"branchId": branch_id, "ifGeneration": generation},
                        )
                    except AgentPosixBridgeError as exc:
                        if not exc.completion_unknown:
                            raise
                        observed = sdk.invoke(
                            "fs.branch.stat", {"branchId": branch_id}
                        )
                        if observed.get("state") != "removed":
                            raise
            if isinstance(child_id, str) and child is not None:
                state["close_intent"]["state"] = "destroying_child"
                _write_state(root_dir, state)
                ChildDriver(sdk, SystemClock()).destroy(child_id)
            state["status"] = "closed"
            state["closed_at"] = utc_timestamp()
            state["close_intent"]["state"] = "closed"
        except AgentPosixBridgeError as exc:
            state["status"] = "needs_recovery" if exc.completion_unknown else "failed"
            state["close_intent"]["state"] = state["status"]
            state["last_error"] = {
                "message": str(exc),
                "error_code": exc.code,
                "completion_unknown": exc.completion_unknown,
            }
        except (PermissionError, RuntimeError, TypeError) as exc:
            state["status"] = "failed"
            state["close_intent"]["state"] = "failed"
            state["last_error"] = {
                "message": str(exc),
                "error_type": type(exc).__name__,
                "completion_unknown": False,
            }
        _write_state(root_dir, state)
        return state


def annotation_snapshot(
    *,
    root_dir: Path | str,
    run_id: str,
) -> dict[str, Any] | None:
    with exclusive_file_lock(_lock_path(root_dir, run_id)):
        state = _load_state(root_dir, run_id)
    if state is not None:
        state["service"] = _load_service(root_dir, run_id)
    return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Service a retained ThinkThread Evidence Annotation Child."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--root", required=True)
    serve_parser.add_argument("--run-id", required=True)
    serve_parser.add_argument("--generation", required=True)
    arguments = parser.parse_args(argv)
    return serve_thinkthread_annotation(
        root_dir=arguments.root,
        run_id=arguments.run_id,
        generation=arguments.generation,
    )


if __name__ == "__main__":
    raise SystemExit(main())
