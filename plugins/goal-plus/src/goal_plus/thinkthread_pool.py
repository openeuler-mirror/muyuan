from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import time
import uuid
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Any, Literal

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

from goal_plus.host_services import (
    SearchHostService,
)
from goal_plus.application.candidate_host import append_candidate_attempt_feedback
from goal_plus.domain.evidence import (
    CandidateAttemptAccepted,
    CandidateAttemptFeedback,
)
from goal_plus.domain.shared_tools import ToolizationDecision
from goal_plus.runtime import FileSearchRuntime
from goal_plus.infrastructure.state.files import exclusive_file_lock, load_json, write_json
from goal_plus.infrastructure.clock import SystemClock, utc_timestamp
from goal_plus.execution.thinkthread_child import ChildDriver
from goal_plus.execution.thinkthread_requests import ThinkThreadRequests
from goal_plus.execution.thinkthread_rpc import (
    PROTOCOL, RESPONSE_RETRY_SECONDS, ReliableRpcChannel,
    message_payload, request_hash, response_hash,
)
from goal_plus.thinkthread_agent_posix import (
    AgentPosixBridgeError,
    AgentPosixSdkClient,
    validate_thinkthread_root,
)

POOL_SCHEMA_VERSION = 1
ACTIVE_JOB_STATES = {"starting", "running", "needs_recovery"}
TERMINAL_JOB_STATES = {"ready", "failed", "interrupted", "timed_out"}
WORKER_TOOLS = {
    "search_get_agent_context",
    "search_get_global_evidence",
    "search_get_evidence_detail",
    "search_run_verifier",
    "search_list_iterations",
}
WORKER_TOOL_PARAMETERS = {
    "search_get_agent_context": {"agent_session_id"},
    "search_get_global_evidence": {"agent_session_id"},
    "search_get_evidence_detail": {
        "agent_session_id",
        "candidate_id",
        "iteration",
    },
    "search_run_verifier": {
        "run_id",
        "candidate_id",
        "scope",
        "agent_session_id",
        "hypothesis",
        "toolization_decision",
        "_inner_git_attempt",
    },
    "search_list_iterations": {"agent_session_id"},
}

_GIT_HEAD_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def _safe_identifier(value: str, label: str) -> str:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    if not value or any(character not in allowed for character in value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def _candidate_attempt_id(job: dict[str, Any], request_id: str) -> str:
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


def _pool_root(root_dir: Path | str) -> Path:
    return Path(root_dir).expanduser().resolve() / "host-pools" / "thinkthread"


def _pool_dir(root_dir: Path | str, pool_id: str) -> Path:
    return _pool_root(root_dir) / _safe_identifier(pool_id, "pool_id")


def _pool_path(root_dir: Path | str, pool_id: str) -> Path:
    return _pool_dir(root_dir, pool_id) / "pool.json"


def _scope_admission(
    root_dir: Path | str,
    execution_scope_id: str | None,
    run_id: str,
) -> AbstractContextManager[Any]:
    if execution_scope_id is None:
        return nullcontext()
    from goal_plus.host_scope import execution_scope_admission

    return execution_scope_admission(
        root_dir=root_dir,
        scope_id=execution_scope_id,
        run_id=run_id,
    )


def _job_path(root_dir: Path | str, pool_id: str, job_id: str) -> Path:
    return (
        _pool_dir(root_dir, pool_id)
        / "jobs"
        / _safe_identifier(job_id, "job_id")
        / "job.json"
    )


def _lock_path(root_dir: Path | str, pool_id: str) -> Path:
    return _pool_dir(root_dir, pool_id) / "pool.lock"


def _controller_lock_path(root_dir: Path | str, pool_id: str) -> Path:
    return _pool_dir(root_dir, pool_id) / "controller.lock"


class _TryControllerLock(AbstractContextManager[bool]):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any | None = None
        self.fallback_dir: Path | None = None
        self.acquired = False

    def __enter__(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if fcntl is not None:
            self.handle = self.path.open("a", encoding="utf-8")
            try:
                fcntl.flock(
                    self.handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError:
                self.handle.close()
                self.handle = None
                return False
            self.acquired = True
            return True
        self.fallback_dir = self.path.with_suffix(".dir")
        try:
            self.fallback_dir.mkdir()
        except FileExistsError:  # pragma: no cover - non-POSIX fallback
            return False
        self.acquired = True
        return True

    def __exit__(self, *_exc: object) -> None:
        if not self.acquired:
            return
        if self.handle is not None:
            assert fcntl is not None
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
        elif self.fallback_dir is not None:  # pragma: no cover
            self.fallback_dir.rmdir()


def _load_pool(root_dir: Path | str, pool_id: str) -> dict[str, Any]:
    path = _pool_path(root_dir, pool_id)
    if not path.is_file():
        raise FileNotFoundError(f"unknown ThinkThread pool: {pool_id}")
    pool = load_json(path)
    if pool.get("host") != "thinkthread":
        raise ValueError(f"pool {pool_id} is not a ThinkThread pool")
    return pool


def _write_pool(root_dir: Path | str, pool: dict[str, Any]) -> None:
    pool["updated_at"] = utc_timestamp()
    write_json(_pool_path(root_dir, str(pool["pool_id"])), pool)


def _load_job(
    root_dir: Path | str,
    pool_id: str,
    job_id: str,
) -> dict[str, Any]:
    return load_json(_job_path(root_dir, pool_id, job_id))


def _write_job(
    root_dir: Path | str,
    pool_id: str,
    job: dict[str, Any],
) -> None:
    job["updated_at"] = utc_timestamp()
    write_json(_job_path(root_dir, pool_id, str(job["job_id"])), job)


def _jobs(root_dir: Path | str, pool: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _load_job(root_dir, str(pool["pool_id"]), str(job_id))
        for job_id in pool.get("jobs", [])
    ]


def validate_root(client: AgentPosixSdkClient) -> dict[str, Any]:
    return validate_thinkthread_root(client)


def _stable_request_id(kind: str, *identity: str) -> str:
    digest = hashlib.sha256(
        ":".join(("goal-plus", "thinkthread", kind, *identity)).encode("utf-8")
    ).hexdigest()[:32]
    return (
        f"req-{digest[:8]}-{digest[8:12]}-{digest[12:16]}-"
        f"{digest[16:20]}-{digest[20:]}"
    )


def _resolve_durable_result(
    client: AgentPosixSdkClient,
    request_id: str,
) -> dict[str, Any] | None:
    observation = ThinkThreadRequests(client).observe(request_id, "fs.snapshot.create")
    if observation.state == "absent":
        return None
    if observation.state == "succeeded":
        return observation.terminal_result(request_id)
    if observation.state == "failed":
        error = observation.error
        message = error.get("message")
        raise AgentPosixBridgeError(
            str(message or f"ThinkThread request {request_id} failed"),
            error={"rejection": {"error": error or {}}},
        )
    raise AgentPosixBridgeError(
        f"ThinkThread request {request_id} has no terminal result",
        error={"delivery": "completion_unknown", "requestId": request_id},
    )


def _capture_baseline(
    client: AgentPosixSdkClient,
    root_dir: Path | str,
    pool: dict[str, Any],
) -> str:
    intent = pool["baseline_intent"]
    request_id = str(intent["request_id"])
    intent["state"] = "platform_mutation_started"
    _write_pool(root_dir, pool)
    try:
        result = client.invoke(
            "fs.snapshot.create",
            {"requestId": request_id},
            timeout_seconds=120.0,
        )
    except AgentPosixBridgeError as exc:
        if not exc.completion_unknown:
            intent["state"] = "failed"
            intent["error_code"] = exc.code
            _write_pool(root_dir, pool)
            raise
        intent["state"] = "outcome_unknown"
        _write_pool(root_dir, pool)
        result = _resolve_durable_result(client, request_id)
        if result is None:
            result = client.invoke(
                "fs.snapshot.create",
                {"requestId": request_id},
                timeout_seconds=120.0,
            )
    snapshot_id = result.get("snapshotId")
    if not isinstance(snapshot_id, str) or not snapshot_id.startswith("fsnap-"):
        raise RuntimeError("fs.snapshot.create omitted an exact snapshot identity")
    intent.update(
        {
            "state": "captured",
            "snapshot_id": snapshot_id,
            "captured_at": utc_timestamp(),
        }
    )
    pool["baseline_snapshot_id"] = snapshot_id
    _write_pool(root_dir, pool)
    _close_baseline_request(client, root_dir, pool)
    return snapshot_id


def _close_baseline_request(
    client: AgentPosixSdkClient,
    root_dir: Path | str,
    pool: dict[str, Any],
) -> bool:
    intent = pool.get("baseline_intent")
    if not isinstance(intent, dict) or intent.get("state") in {"closed", "provided"}:
        return True
    if not isinstance(pool.get("baseline_snapshot_id"), str):
        request_id = intent.get("request_id")
        if not isinstance(request_id, str):
            return False
        try:
            result = _resolve_durable_result(client, request_id)
        except AgentPosixBridgeError as exc:
            intent["state"] = "close_needs_recovery"
            intent["close_error"] = {
                "message": str(exc),
                "error_code": exc.code,
                "delivery": exc.delivery,
            }
            _write_pool(root_dir, pool)
            return False
        if result is None:
            intent.update(
                {
                    "state": "closed",
                    "closed_at": utc_timestamp(),
                    "close_result": "request_not_found",
                }
            )
            _write_pool(root_dir, pool)
            return True
        snapshot_id = result.get("snapshotId")
        if not isinstance(snapshot_id, str) or not snapshot_id.startswith("fsnap-"):
            intent["state"] = "close_needs_recovery"
            intent["close_error"] = {
                "message": "recovered baseline result omitted snapshot identity"
            }
            _write_pool(root_dir, pool)
            return False
        pool["baseline_snapshot_id"] = snapshot_id
        intent.update(
            {
                "state": "captured_after_recovery",
                "snapshot_id": snapshot_id,
                "captured_at": utc_timestamp(),
            }
        )
        _write_pool(root_dir, pool)
    request_id = intent.get("request_id")
    if not isinstance(request_id, str):
        raise RuntimeError("ThinkThread baseline intent omitted its RequestId")
    intent["state"] = "closing"
    intent["close_attempts"] = int(intent.get("close_attempts") or 0) + 1
    intent["close_attempted_at"] = utc_timestamp()
    _write_pool(root_dir, pool)
    closed = ThinkThreadRequests(client).close(request_id, "fs.snapshot.create")
    if closed.state != "closed":
        intent["state"] = "close_needs_recovery"
        intent["close_error"] = {
            "error_code" if key == "code" else key: value
            for key, value in closed.error.items()
        }
        _write_pool(root_dir, pool)
        return False
    intent["state"] = "closed"
    intent["closed_at"] = utc_timestamp()
    intent["close_error"] = {}
    _write_pool(root_dir, pool)
    return True


def _inner_git_binding_marker(binding: object) -> str:
    if not isinstance(binding, dict):
        return ""
    encoded = base64.urlsafe_b64encode(
        json.dumps(
            binding,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"\ngoal_plus_inner_git_binding={encoded}\n"


def _spawn_params(
    candidate: dict[str, Any],
    baseline_snapshot_id: str,
    registration_nonce: str,
    dispatch_nonce: str,
) -> dict[str, Any]:
    message = str(candidate.get("message") or "")
    message += _inner_git_binding_marker(candidate.get("inner_git_binding"))
    message += (
        "\n\nThinkThread Goal Plus dispatch: "
        f"protocol={PROTOCOL}; registration_nonce={registration_nonce}; "
        f"dispatch_nonce={dispatch_nonce}.\n"
        "这是一个新的 initial Child wake。先使用上方 agent_session_id 调用 "
        "search_get_agent_context，然后执行返回的 candidate_task.instructions，"
        "完成本轮修改并恰好调用一次 search_run_verifier；收到 accepted receipt 后结束"
        "当前 turn。不得只读取 context/Evidence、只总结任务或不提交 verifier 就结束本次 wake。"
    )
    params: dict[str, Any] = {
        "profile": "self",
        "fs": "private",
        "fsSnapshotId": baseline_snapshot_id,
        "capabilities": ["thinkthread.message"],
        "initialMessage": message,
    }
    model = candidate.get("model")
    if isinstance(model, dict):
        params["model"] = model
    return params


def _bind_spawn(result: dict[str, Any], job: dict[str, Any]) -> None:
    binding = ChildDriver.binding(
        result, model=job.get("model") if isinstance(job.get("model"), dict) else None,
    )
    child_id, branch_id = binding.child_id, binding.branch_id
    initial_message_id = binding.initial_message_id
    job.update(
        {
            "thinkthread_id": child_id,
            "fs_branch_id": branch_id,
            "initial_message_id": initial_message_id,
            "active_message_id": initial_message_id,
            "status": "running",
            "started_at": utc_timestamp(),
        }
    )


def _recover_spawn(
    client: AgentPosixSdkClient,
    root_dir: Path | str,
    pool: dict[str, Any],
    job: dict[str, Any],
) -> bool:
    intent = job.get("spawn_intent")
    if job.get("status") not in {"starting", "needs_recovery"} or not isinstance(
        intent, dict
    ):
        return False
    if intent.get("state") not in {"platform_mutation_started", "outcome_unknown"}:
        return False
    before = {item for item in intent.get("children_before", []) if isinstance(item, str)}
    bound = {
        item.get("thinkthread_id")
        for item in _jobs(root_dir, pool)
        if item.get("job_id") != job.get("job_id")
    }
    matches = ChildDriver(client, SystemClock()).recovery_matches(
        before=before, bound=bound, nonce=str(job["registration_nonce"]),
    )
    if len(matches) != 1:
        intent["recovery_candidates"] = [item[0].get("thinkthreadId") for item in matches]
        intent["last_recovery_at"] = utc_timestamp()
        _write_job(root_dir, str(pool["pool_id"]), job)
        return False
    child, batch = matches[0]
    _bind_spawn(child, job)
    job["message_cursor"] = batch.get("nextCursor")
    intent["state"] = "bound_after_recovery"
    intent["recovered_at"] = utc_timestamp()
    _write_job(root_dir, str(pool["pool_id"]), job)
    return True


def open_pool(
    *,
    root_dir: Path | str,
    run_id: str,
    candidates: list[dict[str, Any]],
    max_parallel: int | None = None,
    baseline_snapshot_id: str | None = None,
    runtime_integrated: bool = False,
    execution_scope_id: str | None = None,
    client: AgentPosixSdkClient | None = None,
) -> dict[str, Any]:
    if not candidates:
        raise ValueError("ThinkThread pool requires at least one candidate")
    candidate_ids = [str(item.get("candidate_id") or "") for item in candidates]
    if any(not item for item in candidate_ids) or len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("ThinkThread candidate IDs must be non-empty and unique")
    selected_parallel = int(max_parallel or len(candidates))
    if selected_parallel <= 0 or selected_parallel < len(candidates):
        raise ValueError("initial candidates exceed ThinkThread max_parallel")
    for candidate in candidates:
        max_runtime = candidate.get("max_runtime_seconds")
        if (
            not isinstance(max_runtime, int)
            or isinstance(max_runtime, bool)
            or max_runtime <= 0
        ):
            raise ValueError(
                f"ThinkThread candidate {candidate.get('candidate_id')} requires "
                "max_runtime_seconds"
            )
    sdk = client or AgentPosixSdkClient()
    root = validate_root(sdk)
    if baseline_snapshot_id is not None and not baseline_snapshot_id.startswith(
        "fsnap-"
    ):
        raise ValueError("ThinkThread pool baseline must be an exact fs_snapshot")
    pool_id = f"ttpool_{uuid.uuid4().hex[:12]}"
    now = utc_timestamp()
    pool: dict[str, Any] = {
        "schema_version": POOL_SCHEMA_VERSION,
        "pool_id": pool_id,
        "host": "thinkthread",
        "run_id": _safe_identifier(run_id, "run_id"),
        "root_thinkthread_id": root.get("thinkthreadId"),
        "max_parallel": selected_parallel,
        "runtime_integrated": runtime_integrated,
        "execution_scope_id": execution_scope_id,
        "state": "baselining",
        "baseline_snapshot_id": baseline_snapshot_id,
        "baseline_intent": {
            "request_id": _stable_request_id("baseline", run_id),
            "state": "provided" if baseline_snapshot_id is not None else "prepared",
            "prepared_at": now,
            **(
                {"snapshot_id": baseline_snapshot_id}
                if baseline_snapshot_id is not None
                else {}
            ),
        },
        "jobs": [],
        "cleanup": [],
        "created_at": now,
        "updated_at": now,
    }
    with _scope_admission(
        root_dir, execution_scope_id, run_id
    ), exclusive_file_lock(_lock_path(root_dir, pool_id)):
        _write_pool(root_dir, pool)
    with _scope_admission(root_dir, execution_scope_id, run_id):
        baseline = baseline_snapshot_id or _capture_baseline(sdk, root_dir, pool)
        pool["state"] = "open"
        _write_pool(root_dir, pool)

    for candidate in candidates:
        candidate_id = str(candidate["candidate_id"])
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        registration_nonce = str(uuid.uuid4())
        dispatch_nonce = str(uuid.uuid4())
        max_runtime = int(candidate["max_runtime_seconds"])
        job: dict[str, Any] = {
            "job_id": job_id,
            "pool_id": pool_id,
            "run_id": run_id,
            "candidate_id": candidate_id,
            "agent_session_id": candidate.get("agent_session_id"),
            "status": "starting",
            "dispatch_index": 1,
            "registration_nonce": registration_nonce,
            "dispatch_nonce": dispatch_nonce,
            "dispatch_nonces": [dispatch_nonce],
            "message_cursor": None,
            "seen_message_ids": [],
            "thinkthread_id": None,
            "fs_branch_id": None,
            "active_message_id": None,
            "model": candidate.get("model"),
            "inner_git_binding": candidate.get("inner_git_binding"),
            "max_runtime_seconds": max_runtime,
            "deadline_unix": time.time() + max_runtime,
            "created_at": utc_timestamp(),
            "updated_at": utc_timestamp(),
            "started_at": None,
            "finished_at": None,
            "delivered_at": None,
            "error": None,
            "request_intents": {},
            "settled_requests": {},
            "pending_responses": {},
            "acknowledged_dispatch_nonces": [],
        }
        params = _spawn_params(
            candidate,
            baseline,
            registration_nonce,
            dispatch_nonce,
        )
        with _scope_admission(root_dir, execution_scope_id, run_id):
            with exclusive_file_lock(_lock_path(root_dir, pool_id)):
                latest_pool = _load_pool(root_dir, pool_id)
                if latest_pool.get("state") != "open":
                    raise RuntimeError("cannot add a Child to a non-open ThinkThread pool")
                _write_job(root_dir, pool_id, job)
                latest_pool["jobs"].append(job_id)
                _write_pool(root_dir, latest_pool)
                pool = latest_pool
            children = sdk.invoke("thinkthread.list").get("children")
            job["spawn_intent"] = {
                "state": "platform_mutation_started",
                "params_sha256": hashlib.sha256(
                    json.dumps(params, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                "children_before": [
                    item.get("thinkthreadId")
                    for item in children
                    if isinstance(item, dict)
                ]
                if isinstance(children, list)
                else [],
                "prepared_at": utc_timestamp(),
            }
            _write_job(root_dir, pool_id, job)
            try:
                result = ChildDriver(sdk, SystemClock()).spawn(params)
                _bind_spawn(result, job)
                job["spawn_intent"]["state"] = "bound"
            except AgentPosixBridgeError as exc:
                job["status"] = (
                    "needs_recovery" if exc.completion_unknown else "failed"
                )
                job["spawn_intent"]["state"] = (
                    "outcome_unknown" if exc.completion_unknown else "failed"
                )
                job["error"] = {
                    "stage": "spawn",
                    "error_code": exc.code,
                    "completion_unknown": exc.completion_unknown,
                }
            except (TypeError, ValueError, RuntimeError) as exc:
                job["status"] = "failed"
                job["error"] = {
                    "stage": "spawn_binding",
                    "error_type": type(exc).__name__,
                }
            _write_job(root_dir, pool_id, job)
    return snapshot_pool(root_dir=root_dir, pool_id=pool_id)


def open_thinkthread_search_pool(
    *,
    root_dir: Path | str,
    run_id: str,
    candidate_ids: list[str] | None = None,
    worker_budgets: dict[str, dict[str, Any]] | None = None,
    max_parallel: int | None = None,
    execution_scope_id: str | None = None,
    client: AgentPosixSdkClient | None = None,
) -> dict[str, Any]:
    host = SearchHostService(root_dir)
    dispatch = host.prepare_thinkthread_pool_dispatch(
        run_id=run_id,
        candidate_ids=candidate_ids,
        worker_budgets=worker_budgets,
        max_parallel=max_parallel,
    )
    opened = open_pool(
        root_dir=root_dir,
        run_id=run_id,
        candidates=[item.pool_payload() for item in dispatch.candidates],
        max_parallel=dispatch.max_parallel,
        baseline_snapshot_id=dispatch.baseline_snapshot_id,
        runtime_integrated=True,
        execution_scope_id=execution_scope_id,
        client=client,
    )
    try:
        for item in opened["jobs"]:
            candidate = next(
                candidate
                for candidate in dispatch.candidates
                if candidate.candidate_id == item["candidate_id"]
            )
            host.bind_thinkthread_candidate(
                run_id=run_id,
                candidate_id=candidate.candidate_id,
                agent_session_id=candidate.agent_session_id,
                thinkthread_id=str(item["thinkthread_id"]),
                branch_id=str(item["fs_branch_id"]),
            )
    except Exception:
        close_pool(
            root_dir=root_dir,
            pool_id=str(opened["pool_id"]),
            mode="interrupt",
            client=client,
        )
        raise
    return snapshot_pool(root_dir=root_dir, pool_id=str(opened["pool_id"]))


def snapshot_thinkthread_search_pool(
    *,
    root_dir: Path | str,
    pool_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    if pool_id is not None:
        return snapshot_pool(root_dir=root_dir, pool_id=pool_id)
    if run_id is None:
        raise ValueError("pool_id or run_id is required")
    matches = [
        path.parent.name
        for path in _pool_root(root_dir).glob("*/pool.json")
        if load_json(path).get("run_id") == run_id
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one ThinkThread pool for run {run_id}, found {len(matches)}"
        )
    return snapshot_pool(root_dir=root_dir, pool_id=matches[0])


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
    if tool not in WORKER_TOOLS:
        raise PermissionError(f"worker RPC tool is not allowed: {tool}")
    if not isinstance(params, dict):
        raise ValueError("worker RPC params must be an object")
    if request.get("agent_session_id") != job.get("agent_session_id"):
        raise PermissionError("worker RPC agent_session_id binding mismatch")
    digest = _worker_request_hash(request)
    if request.get("content_sha256") != digest:
        raise ValueError("worker RPC content_sha256 mismatch")
    return request_id, str(tool), params


def _dispatch_worker_tool(
    root_dir: Path | str,
    job: dict[str, Any],
    tool: str,
    params: dict[str, Any],
    *,
    request_id: str,
) -> Any:
    from goal_plus.tools import SearchTools

    params = dict(params)
    private_inner_git = params.pop("_inner_git_attempt", None)
    session_id = str(job["agent_session_id"])
    run_id = str(job["run_id"])
    candidate_id = str(job["candidate_id"])
    allowed = WORKER_TOOL_PARAMETERS[tool] - {"_inner_git_attempt"}
    unexpected = sorted(set(params) - allowed)
    if unexpected:
        raise ValueError(
            f"worker RPC {tool} received unexpected parameters: "
            + ", ".join(unexpected)
        )
    binding = job.get("inner_git_binding")
    inner_git_required = isinstance(binding, dict) and (
        binding.get("protocol") == "goal-plus.inner-git.v1"
    )
    if tool == "search_run_verifier" and inner_git_required:
        if not isinstance(private_inner_git, dict):
            raise RuntimeError(
                "ThinkThread AutoResearch-Git verifier request omitted its private "
                "attempt commit"
            )
        if (
            private_inner_git.get("protocol") != "goal-plus.inner-git.v1"
            or private_inner_git.get("adapter") != "autoresearch-git"
            or not isinstance(private_inner_git.get("attempt_git_head"), str)
            or _GIT_HEAD_PATTERN.fullmatch(private_inner_git["attempt_git_head"])
            is None
            or not isinstance(private_inner_git.get("baseline_git_head"), str)
            or _GIT_HEAD_PATTERN.fullmatch(private_inner_git["baseline_git_head"])
            is None
            or not isinstance(private_inner_git.get("result_count_before"), int)
            or isinstance(private_inner_git.get("result_count_before"), bool)
            or private_inner_git["result_count_before"] < 0
        ):
            raise ValueError("ThinkThread private inner Git attempt metadata is invalid")
    elif private_inner_git is not None:
        raise PermissionError(
            "private inner Git metadata is valid only for an AutoResearch-Git "
            "verifier request"
        )
    for name in {"agent_session_id", "run_id", "candidate_id"} & set(params):
        if not isinstance(params[name], str) or not params[name]:
            raise ValueError(f"worker RPC {name} must be a non-empty string")
    if "agent_session_id" in params and params["agent_session_id"] != session_id:
        raise PermissionError("worker RPC session parameter mismatch")
    if "run_id" in params and params["run_id"] != run_id:
        raise PermissionError("worker RPC run parameter mismatch")
    if (
        tool != "search_get_evidence_detail"
        and "candidate_id" in params
        and params["candidate_id"] != candidate_id
    ):
        raise PermissionError("worker RPC candidate parameter mismatch")
    runtime = FileSearchRuntime(root_dir)
    tools = SearchTools(runtime)
    if tool == "search_get_agent_context":
        if set(params) != {"agent_session_id"}:
            raise ValueError("search_get_agent_context requires agent_session_id")
        return tools.search_get_agent_context(session_id)
    if tool == "search_get_global_evidence":
        if set(params) != {"agent_session_id"}:
            raise ValueError("search_get_global_evidence requires agent_session_id")
        return tools.search_get_global_evidence(session_id)
    if tool == "search_get_evidence_detail":
        if set(params) != {"agent_session_id", "candidate_id", "iteration"}:
            raise ValueError(
                "search_get_evidence_detail requires agent_session_id, "
                "candidate_id, and iteration"
            )
        iteration = params["iteration"]
        if (
            isinstance(iteration, bool)
            or not isinstance(iteration, int)
            or iteration <= 0
        ):
            raise ValueError("search_get_evidence_detail iteration must be positive")
        return tools.search_get_evidence_detail(
            session_id,
            str(params["candidate_id"]),
            iteration,
        )
    if tool == "search_list_iterations":
        if set(params) != {"agent_session_id"}:
            raise ValueError(
                "search_list_iterations requires agent_session_id"
            )
        return tools.search_list_iterations(session_id)
    if tool == "search_run_verifier":
        required = {"run_id", "candidate_id", "agent_session_id", "hypothesis"}
        missing = sorted(required - set(params))
        if missing:
            raise ValueError(
                "worker search_run_verifier requires parameters: "
                + ", ".join(missing)
            )
        scope_value = params.get("scope")
        if scope_value is not None and not isinstance(scope_value, str):
            raise ValueError("worker search_run_verifier scope must be a string")
        scope = scope_value or "process"
        if scope != "process":
            raise PermissionError("ThinkThread Child may request only process verification")
        raw_toolization_decision = params.get("toolization_decision")
        toolization_decision = (
            ToolizationDecision.model_validate(raw_toolization_decision)
            if raw_toolization_decision is not None
            else None
        )
        if toolization_decision is not None:
            toolization_decision = runtime.shared_tools.prepare_worker_toolization_decision(
                run_id=run_id, agent_session_id=session_id,
                toolization_decision=toolization_decision,
            )
        hypothesis_value = params.get("hypothesis")
        if not isinstance(hypothesis_value, str):
            raise ValueError("worker process verifier hypothesis must be a string")
        hypothesis = hypothesis_value.strip()
        if not hypothesis:
            raise ValueError("worker process verifier requires a non-empty hypothesis")
        message = (
            "The attempt was accepted. Its exact snapshot verifier will run after "
            "this Child turn reaches a quiescent boundary. Finish the turn without "
            "further writes; the exact ScoreReport will be injected into the next wake."
        )
        prior = job.get("attempt_request")
        if not isinstance(prior, dict):
            legacy = job.get("pending_verifier")
            if isinstance(legacy, dict):
                prior = {
                    "state": "accepted",
                    "attempt_id": _candidate_attempt_id(
                        job,
                        str(legacy.get("request_id") or request_id),
                    ),
                    "request_id": str(legacy.get("request_id") or request_id),
                    "run_id": str(job["run_id"]),
                    "candidate_id": str(job["candidate_id"]),
                    "agent_session_id": str(job["agent_session_id"]),
                    "hypothesis": str(legacy.get("hypothesis") or hypothesis),
                    "accepted_at": str(
                        legacy.get("queued_at") or utc_timestamp()
                    ),
                    "message": message,
                }
                job["attempt_request"] = prior
        if isinstance(prior, dict):
            accepted = CandidateAttemptAccepted.model_validate(prior)
            if accepted.request_id != request_id:
                raise RuntimeError(
                    "only one candidate attempt may be accepted per Child turn"
                )
            if accepted.hypothesis != hypothesis:
                raise ValueError("verifier request id was reused with new content")
            if accepted.toolization_decision != toolization_decision:
                raise ValueError("verifier request id was reused with new content")
            return accepted.model_dump(mode="json")
        accepted = CandidateAttemptAccepted(
            attempt_id=_candidate_attempt_id(job, request_id),
            request_id=request_id,
            run_id=str(job["run_id"]),
            candidate_id=str(job["candidate_id"]),
            agent_session_id=str(job["agent_session_id"]),
            hypothesis=hypothesis,
            toolization_decision=toolization_decision,
            accepted_at=utc_timestamp(),
            message=message,
        )
        if inner_git_required:
            job["inner_git_attempt"] = private_inner_git
        job["attempt_request"] = accepted.model_dump(mode="json")
        return accepted.model_dump(mode="json")
    raise AssertionError(tool)


def _retry_pending_responses(
    client: AgentPosixSdkClient,
    root_dir: Path | str,
    job: dict[str, Any],
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
    _write_job(root_dir, str(job["pool_id"]), job)


def _process_worker_message(
    client: AgentPosixSdkClient,
    root_dir: Path | str,
    job: dict[str, Any],
    message: dict[str, Any],
) -> None:
    payload = message_payload(message)
    if payload is None or payload.get("protocol") != PROTOCOL:
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
        _write_job(root_dir, str(job["pool_id"]), job)
        intent["state"] = "runtime_mutation_started"
        _write_job(root_dir, str(job["pool_id"]), job)
        try:
            result = _dispatch_worker_tool(
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
    _write_job(root_dir, str(job["pool_id"]), job)
    try:
        ReliableRpcChannel(client, SystemClock()).send_response(job, request_id)
    except AgentPosixBridgeError as exc:
        pending = job["pending_responses"][request_id]
        pending["last_error"] = {
            "message": str(exc),
            "error_code": exc.code,
            "completion_unknown": exc.completion_unknown,
        }
    _write_job(root_dir, str(job["pool_id"]), job)


def _receive_messages(
    client: AgentPosixSdkClient, root_dir: Path | str, job: dict[str, Any],
) -> None:
    if not isinstance(job.get("thinkthread_id"), str):
        return
    _retry_pending_responses(client, root_dir, job)
    ReliableRpcChannel(client, SystemClock()).receive(
        job, lambda message: _process_worker_message(client, root_dir, job, message),
        lambda: _write_job(root_dir, str(job["pool_id"]), job),
    )


def _recover_wake(
    client: AgentPosixSdkClient,
    root_dir: Path | str,
    job: dict[str, Any],
) -> bool:
    intent = job.get("wake_intent")
    if job.get("status") not in {"starting", "needs_recovery"} or not isinstance(
        intent, dict
    ):
        return False
    if intent.get("state") not in {"platform_mutation_started", "outcome_unknown"}:
        return False
    child_id = job.get("thinkthread_id")
    if not isinstance(child_id, str):
        return False
    _receive_messages(client, root_dir, job)
    if str(job.get("dispatch_nonce")) not in set(
        job.get("acknowledged_dispatch_nonces", [])
    ):
        return False
    job["status"] = "running"
    intent["state"] = "acknowledged_after_recovery"
    intent["recovered_at"] = utc_timestamp()
    job["error"] = None
    _write_job(root_dir, str(job["pool_id"]), job)
    return True


def _reset_branch_to_snapshot(
    client: AgentPosixSdkClient,
    child_id: str,
    branch_id: str,
    snapshot_id: str,
) -> None:
    child = client.invoke("thinkthread.get", {"id": child_id})
    if child.get("executionState") != "absent":
        stopped = ChildDriver(client, SystemClock()).interrupt(child_id, 5.0)
        if stopped is None or stopped.get("executionState") != "absent":
            raise RuntimeError(
                "ThinkThread Child must stop before its branch can be reset"
            )
    branch = client.invoke("fs.branch.stat", {"branchId": branch_id})
    if branch.get("baseSnapshotId") == snapshot_id:
        return
    generation = branch.get("controlGeneration")
    if not isinstance(generation, int):
        raise RuntimeError("fs.branch.stat omitted controlGeneration")
    try:
        result = client.invoke(
            "fs.branch.reset",
            {
                "branchId": branch_id,
                "toSnapshotId": snapshot_id,
                "ifGeneration": generation,
            },
            timeout_seconds=60.0,
        )
    except AgentPosixBridgeError as exc:
        if not exc.completion_unknown:
            raise
        observed = client.invoke("fs.branch.stat", {"branchId": branch_id})
        if observed.get("baseSnapshotId") != snapshot_id:
            raise
        result = observed
    observed = result if isinstance(result, dict) else {}
    if observed.get("baseSnapshotId") not in {None, snapshot_id}:
        raise RuntimeError("fs.branch.reset returned the wrong snapshot")


def _legacy_settlement_key(pool: dict[str, Any], job: dict[str, Any]) -> str:
    return (
        f"{pool['pool_id']}:{job['job_id']}:{job['dispatch_index']}:"
        f"{job.get('active_message_id') or 'turn'}"
    )


def _accepted_attempt(
    pool: dict[str, Any],
    job: dict[str, Any],
) -> CandidateAttemptAccepted | None:
    raw = job.get("attempt_request")
    if isinstance(raw, dict):
        return CandidateAttemptAccepted.model_validate(raw)
    legacy = job.get("pending_verifier")
    recovery = job.get("recovery")
    is_legacy_recovery = (
        isinstance(recovery, dict) and recovery.get("stage") == "settlement"
    )
    if not isinstance(legacy, dict) or (
        legacy.get("state") == "settled" and not is_legacy_recovery
    ):
        return None
    request_id = str(legacy.get("request_id") or "legacy-turn-boundary")
    attempt_id = (
        recovery.get("attempt_id") or recovery.get("settlement_key")
        if isinstance(recovery, dict)
        else None
    )
    accepted = CandidateAttemptAccepted(
        attempt_id=str(
            attempt_id
            or _legacy_settlement_key(pool, job)
            or _candidate_attempt_id(job, request_id)
        ),
        request_id=request_id,
        run_id=str(pool["run_id"]),
        candidate_id=str(job["candidate_id"]),
        agent_session_id=str(job["agent_session_id"]),
        hypothesis=str(
            legacy.get("hypothesis")
            or f"Complete ThinkThread candidate dispatch {job['dispatch_index']}"
        ),
        accepted_at=str(legacy.get("queued_at") or utc_timestamp()),
        message=(
            "The attempt was accepted by a legacy pool record and will be verified "
            "at this quiescent boundary."
        ),
    )
    job["attempt_request"] = accepted.model_dump(mode="json")
    return accepted


def _verify_completed_attempt(
    client: AgentPosixSdkClient,
    root_dir: Path | str,
    pool: dict[str, Any],
    job: dict[str, Any],
    *,
    attempt_id: str | None = None,
    recovery_deadline: float | None = None,
) -> None:
    host = SearchHostService(root_dir)
    accepted = _accepted_attempt(pool, job)
    if accepted is None:
        raise RuntimeError(
            "MissingCandidateAttempt: completed ThinkThread turn did not submit "
            "search_run_verifier"
        )
    exact_attempt_id = attempt_id or accepted.attempt_id
    if exact_attempt_id != accepted.attempt_id:
        raise RuntimeError("ThinkThread attempt recovery identity changed")
    report = host.verify_thinkthread_candidate_attempt(
        run_id=str(pool["run_id"]),
        candidate_id=str(job["candidate_id"]),
        agent_session_id=str(job["agent_session_id"]),
        thinkthread_id=str(job["thinkthread_id"]),
        branch_id=str(job["fs_branch_id"]),
        hypothesis=accepted.hypothesis,
        attempt_id=exact_attempt_id,
        inner_git_attempt=(
            dict(job["inner_git_attempt"])
            if isinstance(job.get("inner_git_attempt"), dict)
            else None
        ),
        toolization_decision=accepted.toolization_decision,
        client=client,
        recovery_deadline=recovery_deadline,
    )
    payload = report.model_dump(mode="json")
    attempt_artifact = payload.get("attempt_artifact_ref")
    workspace_artifact = payload.get("workspace_artifact_after_settlement")
    if not isinstance(workspace_artifact, dict):
        workspace_artifact = payload.get("best_artifact_ref")
    snapshot_id = (
        workspace_artifact.get("id")
        if isinstance(workspace_artifact, dict)
        else None
    )
    if not isinstance(snapshot_id, str) or not snapshot_id.startswith("fsnap-"):
        raise RuntimeError("ThinkThread attempt omitted its authoritative workspace snapshot")
    if not isinstance(attempt_artifact, dict):
        raise RuntimeError("ThinkThread attempt omitted its exact attempted snapshot")
    attempt_iteration = report.attempt_iteration
    if not isinstance(attempt_iteration, int):
        raise RuntimeError("ThinkThread attempt omitted its exact iteration")
    if attempt_artifact.get("id") != snapshot_id:
        _reset_branch_to_snapshot(
            client,
            str(job["thinkthread_id"]),
            str(job["fs_branch_id"]),
            snapshot_id,
        )
    feedback = CandidateAttemptFeedback(
        attempt_id=exact_attempt_id,
        request_id=accepted.request_id,
        run_id=str(pool["run_id"]),
        candidate_id=str(job["candidate_id"]),
        agent_session_id=str(job["agent_session_id"]),
        attempt_iteration=attempt_iteration,
        score_report=report,
        attempt_artifact_ref=attempt_artifact,
        workspace_artifact_after_attempt=workspace_artifact,
        verified_at=utc_timestamp(),
    )
    job["artifact_ref"] = workspace_artifact
    job["attempt_result"] = feedback.model_dump(mode="json")
    try:
        from goal_plus.thinkthread_annotation import kick_thinkthread_annotation

        annotation = kick_thinkthread_annotation(
            root_dir=root_dir,
            run_id=str(pool["run_id"]),
            client=client,
        )
        if isinstance(annotation, dict):
            job["annotation_host"] = {
                "status": annotation.get("status"),
                "thinkthread_id": annotation.get("thinkthread_id"),
                "target_revision": annotation.get("target_revision"),
            }
    except Exception as exc:
        job["annotation_host_error"] = {
            "type": type(exc).__name__,
            "message": str(exc),
        }


def _attempt_needs_recovery(exc: Exception) -> bool:
    return (
        isinstance(exc, AgentPosixBridgeError) and exc.completion_unknown
    ) or "ThinkThreadRequestNeedsRecovery" in str(exc)


def _attempt_error(exc: Exception) -> dict[str, Any]:
    return {
        "stage": "attempt_verification",
        "message": str(exc),
        "error_code": exc.code if isinstance(exc, AgentPosixBridgeError) else None,
        "completion_unknown": _attempt_needs_recovery(exc),
    }


def _begin_attempt_recovery(
    root_dir: Path | str,
    pool: dict[str, Any],
    job: dict[str, Any],
) -> bool:
    accepted = _accepted_attempt(pool, job)
    if accepted is None:
        dispatch_error = job.get("dispatch_error")
        if isinstance(dispatch_error, dict) and dispatch_error.get(
            "dispatch_nonce"
        ) == job.get("dispatch_nonce"):
            job["status"] = "failed"
            job["error"] = {
                "stage": "worker_start",
                "message": str(
                    dispatch_error.get("message") or "ThinkThread Child startup failed"
                ),
                "error_code": "ChildDispatchError",
                "error_type": dispatch_error.get("error_type"),
                "completion_unknown": False,
            }
            job["finished_at"] = job.get("finished_at") or utc_timestamp()
            _write_job(root_dir, str(pool["pool_id"]), job)
            return False
        job["status"] = "failed"
        job["error"] = {
            "stage": "attempt_verification",
            "message": (
                "MissingCandidateAttempt: completed ThinkThread turn did not "
                "submit search_run_verifier"
            ),
            "error_code": "MissingCandidateAttempt",
            "completion_unknown": False,
        }
        job["finished_at"] = job.get("finished_at") or utc_timestamp()
        _write_job(root_dir, str(pool["pool_id"]), job)
        return False
    attempt_id = accepted.attempt_id
    recovery = job.get("recovery")
    if isinstance(recovery, dict) and recovery.get("stage") in {
        "attempt_verification",
        "settlement",
    }:
        recorded_id = recovery.get("attempt_id") or recovery.get("settlement_key")
        if recorded_id not in {None, attempt_id}:
            raise RuntimeError("ThinkThread attempt recovery identity changed")
    else:
        recovery = {
            "stage": "attempt_verification",
            "state": "prepared",
            "attempt_id": attempt_id,
            "attempts": 0,
            "started_at": utc_timestamp(),
        }
        job["recovery"] = recovery
    job["status"] = "needs_recovery"
    job["error"] = None
    _write_job(root_dir, str(pool["pool_id"]), job)
    return True


def _recover_attempt_verification(
    client: AgentPosixSdkClient,
    root_dir: Path | str,
    pool: dict[str, Any],
    job: dict[str, Any],
    *,
    recovery_deadline: float | None = None,
) -> bool:
    recovery = job.get("recovery")
    if not isinstance(recovery, dict):
        error = job.get("error")
        if not isinstance(error, dict) or error.get("stage") not in {
            "attempt_verification",
            "settlement",
        }:
            return False
        accepted = _accepted_attempt(pool, job)
        if accepted is None:
            return False
        recovery = {
            "stage": "attempt_verification",
            "state": "outcome_unknown",
            "attempt_id": accepted.attempt_id,
            "attempts": 0,
            "started_at": job.get("finished_at") or utc_timestamp(),
        }
        job["recovery"] = recovery
    if recovery.get("stage") not in {"attempt_verification", "settlement"}:
        return False
    accepted = _accepted_attempt(pool, job)
    if accepted is None:
        raise RuntimeError("ThinkThread attempt recovery is missing its accepted request")
    attempt_id = str(
        recovery.get("attempt_id")
        or recovery.get("settlement_key")
        or accepted.attempt_id
    )
    if attempt_id != accepted.attempt_id:
        raise RuntimeError("ThinkThread attempt recovery identity changed")
    is_recovery_attempt = (
        recovery.get("state") == "outcome_unknown"
        or int(recovery.get("attempts") or 0) > 0
    )
    recovery.update(
        {
            "state": "platform_mutation_started",
            "attempt_id": attempt_id,
            "attempts": int(recovery.get("attempts") or 0) + 1,
            "last_attempt_at": utc_timestamp(),
        }
    )
    _write_job(root_dir, str(pool["pool_id"]), job)
    try:
        _verify_completed_attempt(
            client,
            root_dir,
            pool,
            job,
            attempt_id=attempt_id,
            recovery_deadline=(recovery_deadline if is_recovery_attempt else None),
        )
    except (AgentPosixBridgeError, RuntimeError, ValueError) as exc:
        error = _attempt_error(exc)
        if _attempt_needs_recovery(exc):
            recovery["state"] = "outcome_unknown"
            recovery["last_error"] = error
            job["status"] = "needs_recovery"
            job["error"] = error
            _write_job(root_dir, str(pool["pool_id"]), job)
            return False
        recovery["state"] = "failed"
        recovery["last_error"] = error
        job["status"] = "failed"
        job["error"] = error
        job["finished_at"] = job.get("finished_at") or utc_timestamp()
        _write_job(root_dir, str(pool["pool_id"]), job)
        return True
    job["status"] = "ready"
    job["error"] = None
    job.pop("recovery", None)
    _write_job(root_dir, str(pool["pool_id"]), job)
    return True


def _recovery_stage(job: dict[str, Any]) -> str | None:
    recovery = job.get("recovery")
    if isinstance(recovery, dict) and isinstance(recovery.get("stage"), str):
        stage = str(recovery["stage"])
        return "attempt_verification" if stage == "settlement" else stage
    error = job.get("error")
    if isinstance(error, dict) and isinstance(error.get("stage"), str):
        stage = str(error["stage"])
        return "attempt_verification" if stage == "settlement" else stage
    wake_intent = job.get("wake_intent")
    if isinstance(wake_intent, dict) and wake_intent.get("state") in {
        "platform_mutation_started",
        "outcome_unknown",
    }:
        return "wake"
    spawn_intent = job.get("spawn_intent")
    if isinstance(spawn_intent, dict) and spawn_intent.get("state") in {
        "platform_mutation_started",
        "outcome_unknown",
    }:
        return "spawn"
    return None


def _event(pool: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    kind = {
        "ready": "candidate_ready",
        "failed": "failed",
        "interrupted": "interrupted",
        "timed_out": "timed_out",
    }[str(job["status"])]
    event = {
        "event_id": f"event-{job['job_id']}-{job['dispatch_index']}",
        "host": "thinkthread",
        "pool_id": pool["pool_id"],
        "kind": kind,
        "run_id": pool["run_id"],
        "candidate_id": job["candidate_id"],
        "job_id": job["job_id"],
        "terminal": True,
        "agent_session_id": job.get("agent_session_id"),
        "result": {
            "execution_scope": "thinkthread_private",
            "thinkthread_id": job.get("thinkthread_id"),
            "artifact_ref": job.get("artifact_ref"),
        },
    }
    if isinstance(job.get("attempt_result"), dict):
        event["attempt_result"] = job["attempt_result"]
    if isinstance(job.get("error"), dict):
        event["error"] = job["error"]
    return event


def _wait_any_owned(
    *,
    root_dir: Path | str,
    pool_id: str,
    timeout_seconds: float,
    client: AgentPosixSdkClient,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        with exclusive_file_lock(_lock_path(root_dir, pool_id)):
            pool_probe = _load_pool(root_dir, pool_id)
        _close_baseline_request(client, root_dir, pool_probe)
        if pool_probe.get("runtime_integrated") is True:
            try:
                from goal_plus.thinkthread_annotation import (
                    service_thinkthread_annotation,
                )

                service_thinkthread_annotation(
                    root_dir=root_dir,
                    run_id=str(pool_probe["run_id"]),
                    client=client,
                )
                if getattr(client, "supports_detached_service", False):
                    from goal_plus.thinkthread_annotation import (
                        kick_thinkthread_annotation_service,
                    )

                    kick_thinkthread_annotation_service(
                        root_dir=root_dir,
                        run_id=str(pool_probe["run_id"]),
                    )
            except Exception:
                # Annotation is best effort and cannot change candidate settlement.
                pass
        delivered_event: dict[str, Any] | None = None
        with exclusive_file_lock(_lock_path(root_dir, pool_id)):
            pool = _load_pool(root_dir, pool_id)
            jobs = _jobs(root_dir, pool)
            undelivered = [
                job
                for job in jobs
                if job.get("status") in TERMINAL_JOB_STATES
                and not job.get("delivered_at")
            ]
            if undelivered:
                job = undelivered[0]
                delivered_event = _event(pool, job)
                job["delivered_at"] = utc_timestamp()
                _write_job(root_dir, pool_id, job)
        if delivered_event is not None:
            return {
                "event": delivered_event,
                "pool": snapshot_pool(root_dir=root_dir, pool_id=pool_id),
            }

        changed = False
        for job in jobs:
            if job.get("status") in {"starting", "needs_recovery"}:
                recovery_stage = _recovery_stage(job)
                if recovery_stage == "attempt_verification":
                    changed = (
                        _recover_attempt_verification(
                            client,
                            root_dir,
                            pool,
                            job,
                            recovery_deadline=deadline,
                        )
                        or changed
                    )
                elif recovery_stage == "wake":
                    changed = _recover_wake(client, root_dir, job) or changed
                elif recovery_stage == "spawn":
                    changed = _recover_spawn(client, root_dir, pool, job) or changed
            if job.get("status") != "running":
                continue
            child_id = job.get("thinkthread_id")
            if not isinstance(child_id, str):
                continue
            _receive_messages(client, root_dir, job)
            # A late controller poll must settle an already-completed turn before
            # applying the watchdog to jobs that are still running.
            child = client.invoke("thinkthread.get", {"id": child_id})
            outcome = ChildDriver(client, SystemClock()).turn_terminal(child, job.get("active_message_id"))
            if outcome is not None:
                if (
                    outcome == "completed"
                    and pool.get("runtime_integrated") is True
                    and _accepted_attempt(pool, job) is None
                    and not isinstance(job.get("dispatch_error"), dict)
                    and time.time() < float(job["deadline_unix"])
                ):
                    job["awaiting_candidate_attempt"] = {
                        "dispatch_nonce": job.get("dispatch_nonce"),
                        "last_observed_outcome": outcome,
                        "observed_at": utc_timestamp(),
                    }
                    _write_job(root_dir, pool_id, job)
                    # Pi can retry provider failures or trigger its missing-attempt
                    # follow-up after this transient ready boundary. An accepted
                    # verifier request, not a brief native turn completion, is the
                    # authoritative signal that this dispatch may settle.
                    continue
                job.pop("awaiting_candidate_attempt", None)
                job["turn_outcome"] = outcome
                job["finished_at"] = utc_timestamp()
                if outcome == "completed" and pool.get("runtime_integrated") is True:
                    if _begin_attempt_recovery(root_dir, pool, job):
                        changed = (
                            _recover_attempt_verification(
                                client,
                                root_dir,
                                pool,
                                job,
                                recovery_deadline=deadline,
                            )
                            or changed
                        )
                    else:
                        changed = True
                    continue
                job["status"] = "ready" if outcome == "completed" else "failed"
                _write_job(root_dir, pool_id, job)
                changed = True
                continue
            if time.time() >= float(job["deadline_unix"]):
                ChildDriver(client, SystemClock()).interrupt(child_id, 5.0)
                job["status"] = "timed_out"
                job["finished_at"] = utc_timestamp()
                _write_job(root_dir, pool_id, job)
                changed = True
        if changed:
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {
                "event": None,
                "pool": snapshot_pool(root_dir=root_dir, pool_id=pool_id),
            }
        time.sleep(min(0.1, remaining))


def wait_any(
    *,
    root_dir: Path | str,
    pool_id: str,
    timeout_seconds: float = 30.0,
    client: AgentPosixSdkClient | None = None,
) -> dict[str, Any]:
    if timeout_seconds < 0:
        raise ValueError("ThinkThread wait timeout must be non-negative")
    with _TryControllerLock(_controller_lock_path(root_dir, pool_id)) as acquired:
        if not acquired:
            return {
                "event": None,
                "controller_busy": True,
                "pool": snapshot_pool(root_dir=root_dir, pool_id=pool_id),
            }
        return _wait_any_owned(
            root_dir=root_dir,
            pool_id=pool_id,
            timeout_seconds=timeout_seconds,
            client=client or AgentPosixSdkClient(),
        )


def _continuation_feedback(
    root_dir: Path | str,
    pool: dict[str, Any],
    job: dict[str, Any],
) -> CandidateAttemptFeedback | None:
    raw = job.get("attempt_result")
    if isinstance(raw, dict):
        return CandidateAttemptFeedback.model_validate(raw)
    legacy = job.get("settlement")
    if not isinstance(legacy, dict):
        return None
    pending = job.get("pending_verifier")
    request_id = (
        str(pending.get("request_id"))
        if isinstance(pending, dict) and pending.get("request_id")
        else "legacy-turn-boundary"
    )
    attempt_id = str(
        legacy.get("key")
        or _legacy_settlement_key(pool, job)
    )
    return SearchHostService(root_dir).recover_legacy_thinkthread_attempt_feedback(
        run_id=str(pool["run_id"]),
        candidate_id=str(job["candidate_id"]),
        agent_session_id=str(job["agent_session_id"]),
        attempt_id=attempt_id,
        request_id=request_id,
        verified_at=str(legacy.get("settled_at") or utc_timestamp()),
    )


def continue_pool(
    *,
    root_dir: Path | str,
    pool_id: str,
    candidate_id: str,
    message: str,
    max_runtime_seconds: int,
    client: AgentPosixSdkClient | None = None,
) -> dict[str, Any]:
    pool = _load_pool(root_dir, pool_id)
    scope_id = pool.get("execution_scope_id")
    with _scope_admission(
        root_dir,
        scope_id if isinstance(scope_id, str) else None,
        str(pool["run_id"]),
    ):
        return _continue_pool_admitted(
            root_dir=root_dir,
            pool_id=pool_id,
            candidate_id=candidate_id,
            message=message,
            max_runtime_seconds=max_runtime_seconds,
            client=client,
        )


def _continue_pool_admitted(
    *,
    root_dir: Path | str,
    pool_id: str,
    candidate_id: str,
    message: str,
    max_runtime_seconds: int,
    client: AgentPosixSdkClient | None = None,
) -> dict[str, Any]:
    sdk = client or AgentPosixSdkClient()
    with _TryControllerLock(_controller_lock_path(root_dir, pool_id)) as acquired:
        if not acquired:
            raise RuntimeError(f"ThinkThread pool {pool_id} controller is busy")
        with exclusive_file_lock(_lock_path(root_dir, pool_id)):
            pool = _load_pool(root_dir, pool_id)
            if pool.get("state") != "open":
                raise RuntimeError("cannot continue a non-open ThinkThread pool")
            matches = [
                job for job in _jobs(root_dir, pool) if job.get("candidate_id") == candidate_id
            ]
            if len(matches) != 1:
                raise ValueError(f"unknown ThinkThread candidate: {candidate_id}")
            job = matches[0]
            if job.get("status") != "ready":
                raise RuntimeError("ThinkThread candidate is not ready for continuation")
            if not isinstance(max_runtime_seconds, int) or max_runtime_seconds <= 0:
                raise ValueError("ThinkThread continuation requires max_runtime_seconds")
            child_id = job.get("thinkthread_id")
            if not isinstance(child_id, str):
                raise AgentPosixBridgeError(
                    "ThinkThread candidate has no retained Child"
                )
            feedback = _continuation_feedback(root_dir, pool, job)
            if pool.get("runtime_integrated") is True and feedback is None:
                raise RuntimeError(
                    "runtime-integrated ThinkThread continuation requires attempt feedback"
                )
            if feedback is not None:
                message = append_candidate_attempt_feedback(message, feedback)
            dispatch_nonce = str(uuid.uuid4())
            body = (
                message
                + _inner_git_binding_marker(job.get("inner_git_binding"))
                + "\n\nThinkThread Goal Plus dispatch: "
                + f"protocol={PROTOCOL}; dispatch_nonce={dispatch_nonce}.\n"
                + "这是一个新的 retained Child wake。现在根据上方 authoritative "
                + "verifier feedback 和 continuation 目标完成下一个有界 attempt，"
                + "并使用当前 agent_session_id 恰好调用一次 search_run_verifier；"
                + "收到 accepted receipt 后结束当前 turn。不得只读取 context/Evidence、"
                + "只总结上一轮或不提交 verifier 就结束本次 wake。"
            )
            job["dispatch_index"] = int(job["dispatch_index"]) + 1
            job["dispatch_nonce"] = dispatch_nonce
            dispatch_nonces = {
                item
                for item in job.get("dispatch_nonces", [])
                if isinstance(item, str)
            }
            dispatch_nonces.add(dispatch_nonce)
            job["dispatch_nonces"] = sorted(dispatch_nonces)
            job["status"] = "starting"
            job["active_message_id"] = None
            job["delivered_at"] = None
            job["finished_at"] = None
            job["deadline_unix"] = time.time() + max_runtime_seconds
            history = job.setdefault("attempt_history", [])
            if isinstance(history, list) and (
                isinstance(job.get("attempt_request"), dict)
                or isinstance(job.get("attempt_result"), dict)
                or isinstance(job.get("settlement"), dict)
            ):
                request = job.get("attempt_request") or job.get("pending_verifier")
                result = job.get("attempt_result") or job.get("settlement")
                history.append(
                    {
                        "dispatch_index": int(job["dispatch_index"]) - 1,
                        "attempt_id": (
                            result.get("attempt_id") or result.get("key")
                            if isinstance(result, dict)
                            else None
                        ),
                        "request_id": (
                            request.get("request_id")
                            if isinstance(request, dict)
                            else None
                        ),
                        "attempt_iteration": (
                            result.get("attempt_iteration") or result.get("iteration")
                            if isinstance(result, dict)
                            else None
                        ),
                        "verified_at": (
                            result.get("verified_at") or result.get("settled_at")
                            if isinstance(result, dict)
                            else None
                        ),
                        "inner_git_attempt_head": (
                            job.get("inner_git_attempt", {}).get("attempt_git_head")
                            if isinstance(job.get("inner_git_attempt"), dict)
                            else None
                        ),
                    }
                )
            for field in (
                "attempt_request",
                "attempt_result",
                "pending_verifier",
                "settlement",
                "recovery",
                "inner_git_attempt",
                "awaiting_candidate_attempt",
                "dispatch_error",
            ):
                job.pop(field, None)
            job["error"] = None
            job["wake_intent"] = {
                "state": "platform_mutation_started",
                "dispatch_nonce": dispatch_nonce,
                "message_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "prepared_at": utc_timestamp(),
            }
            _write_job(root_dir, pool_id, job)
        try:
            message_id = ChildDriver(sdk, SystemClock()).wake(child_id, body)
        except AgentPosixBridgeError as exc:
            job["status"] = "needs_recovery" if exc.completion_unknown else "failed"
            job["wake_intent"]["state"] = (
                "outcome_unknown" if exc.completion_unknown else "failed"
            )
            job["error"] = {
                "stage": "wake",
                "error_code": exc.code,
                "completion_unknown": exc.completion_unknown,
            }
            _write_job(root_dir, pool_id, job)
            return snapshot_pool(root_dir=root_dir, pool_id=pool_id)
        job["active_message_id"] = message_id
        job["status"] = "running"
        job["wake_intent"].update(
            {"state": "sent", "message_id": message_id, "sent_at": utc_timestamp()}
        )
        _write_job(root_dir, pool_id, job)
        return snapshot_pool(root_dir=root_dir, pool_id=pool_id)


def continue_thinkthread_search_pool(
    *,
    root_dir: Path | str,
    pool_id: str,
    candidate_id: str,
    worker_budget: dict[str, Any] | None = None,
    client: AgentPosixSdkClient | None = None,
) -> dict[str, Any]:
    pool = _load_pool(root_dir, pool_id)
    job = next(
        (
            item
            for item in _jobs(root_dir, pool)
            if item.get("candidate_id") == candidate_id
        ),
        None,
    )
    if job is None:
        raise ValueError(f"unknown ThinkThread candidate: {candidate_id}")
    feedback = _continuation_feedback(root_dir, pool, job)
    continuation = SearchHostService(root_dir).prepare_thinkthread_continuation(
        run_id=str(pool["run_id"]),
        candidate_id=candidate_id,
        agent_session_id=str(job["agent_session_id"]),
        attempt_feedback=feedback,
        worker_budget=worker_budget,
    )
    return continue_pool(
        root_dir=root_dir,
        pool_id=pool_id,
        candidate_id=candidate_id,
        message=continuation.message,
        max_runtime_seconds=continuation.max_runtime_seconds,
        client=client,
    )


def snapshot_pool(*, root_dir: Path | str, pool_id: str) -> dict[str, Any]:
    with exclusive_file_lock(_lock_path(root_dir, pool_id)):
        pool = _load_pool(root_dir, pool_id)
        jobs = _jobs(root_dir, pool)
    active = [job for job in jobs if job.get("status") in ACTIVE_JOB_STATES]
    terminal = [job for job in jobs if job.get("status") in TERMINAL_JOB_STATES]
    return {
        "schema_version": pool.get("schema_version"),
        "pool_id": pool_id,
        "host": "thinkthread",
        "run_id": pool.get("run_id"),
        "state": pool.get("state"),
        "root_thinkthread_id": pool.get("root_thinkthread_id"),
        "baseline_snapshot_id": pool.get("baseline_snapshot_id"),
        "baseline_intent": pool.get("baseline_intent"),
        "runtime_integrated": pool.get("runtime_integrated", False),
        "max_parallel": pool.get("max_parallel"),
        "active_count": len(active),
        "terminal_count": len(terminal),
        "undelivered_count": len(
            [job for job in terminal if not job.get("delivered_at")]
        ),
        "cleanup": pool.get("cleanup", []),
        "cleanup_observation": pool.get("cleanup_observation"),
        "jobs": [
            {
                key: job.get(key)
                for key in (
                    "job_id",
                    "candidate_id",
                    "agent_session_id",
                    "status",
                    "dispatch_index",
                    "thinkthread_id",
                    "fs_branch_id",
                    "message_cursor",
                    "artifact_ref",
                    "attempt_request",
                    "attempt_result",
                    "attempt_history",
                    "settlement",
                    "recovery",
                    "error",
                )
            }
            for job in jobs
        ],
    }


def interrupt_candidate(
    *,
    root_dir: Path | str,
    pool_id: str,
    candidate_id: str,
    client: AgentPosixSdkClient | None = None,
) -> dict[str, Any]:
    """Send one cancellation signal to a Child without waiting for exit."""
    _safe_identifier(candidate_id, "candidate_id")
    sdk = client or AgentPosixSdkClient()
    with _TryControllerLock(_controller_lock_path(root_dir, pool_id)) as acquired:
        if not acquired:
            raise RuntimeError(f"ThinkThread pool {pool_id} controller is busy")
        with exclusive_file_lock(_lock_path(root_dir, pool_id)):
            pool = _load_pool(root_dir, pool_id)
            matching_jobs = [
                job
                for job in _jobs(root_dir, pool)
                if job.get("candidate_id") == candidate_id
            ]
            jobs = [
                job
                for job in matching_jobs
                if job.get("status") in ACTIVE_JOB_STATES
            ]
            requested_at = utc_timestamp()
            for job in jobs:
                job["interrupt_requested_at"] = requested_at
                _write_job(root_dir, pool_id, job)

        errors: list[str] = []
        accepted_job_ids: list[str] = []
        already_terminal_job_ids: list[str] = []
        interrupted_job_ids: list[str] = []
        for job in jobs:
            child_id = job.get("thinkthread_id")
            if not isinstance(child_id, str):
                errors.append(
                    f"ThinkThread candidate {candidate_id} has no bound Child"
                )
                continue
            try:
                sdk.invoke(
                    "thinkthread.signal",
                    {"id": child_id, "signal": "INT"},
                )
            except AgentPosixBridgeError as exc:
                if exc.code == "ThinkThreadNotFound":
                    already_terminal_job_ids.append(str(job["job_id"]))
                else:
                    errors.append(f"{exc.code}: {exc}")
            else:
                accepted_job_ids.append(str(job["job_id"]))

        terminal_job_ids = accepted_job_ids + already_terminal_job_ids
        for job_id in terminal_job_ids:
            with exclusive_file_lock(_lock_path(root_dir, pool_id)):
                current = _load_job(root_dir, pool_id, job_id)
                if current.get("status") in ACTIVE_JOB_STATES:
                    current.update(
                        {
                            "status": "interrupted",
                            "finished_at": utc_timestamp(),
                            "error": {
                                "stage": "controller",
                                "error_type": "CandidateInterrupted",
                                "message": (
                                    "ThinkThread candidate cancellation was "
                                    "accepted by the host"
                                ),
                            },
                        }
                    )
                    _write_job(root_dir, pool_id, current)
            interrupted_job_ids.append(job_id)

        return {
            "pool_id": pool_id,
            "candidate_id": candidate_id,
            "status": (
                "rejected"
                if errors
                else "accepted"
                if accepted_job_ids
                else "already_terminal"
                if matching_jobs
                else "not_found"
            ),
            "errors": errors,
            "interrupted_job_ids": interrupted_job_ids,
            "state": pool["state"],
        }


def close_pool(
    *,
    root_dir: Path | str,
    pool_id: str,
    mode: Literal["drain", "interrupt"] = "drain",
    timeout_seconds: float = 15.0,
    client: AgentPosixSdkClient | None = None,
) -> dict[str, Any]:
    if mode not in {"drain", "interrupt"}:
        raise ValueError("ThinkThread close mode must be drain or interrupt")
    sdk = client or AgentPosixSdkClient()
    with _TryControllerLock(_controller_lock_path(root_dir, pool_id)) as acquired:
        if not acquired:
            raise RuntimeError(f"ThinkThread pool {pool_id} controller is busy")
        already_closed = False
        with exclusive_file_lock(_lock_path(root_dir, pool_id)):
            pool = _load_pool(root_dir, pool_id)
            if pool.get("state") == "closed":
                already_closed = True
                jobs = []
            else:
                jobs = _jobs(root_dir, pool)
                active = [
                    job for job in jobs if job.get("status") in ACTIVE_JOB_STATES
                ]
                if mode == "drain" and active:
                    raise RuntimeError(
                        f"cannot drain ThinkThread pool with {len(active)} active job(s)"
                    )
                pool["state"] = "closing"
                _write_pool(root_dir, pool)
        if already_closed:
            return snapshot_pool(root_dir=root_dir, pool_id=pool_id)
        _close_baseline_request(sdk, root_dir, pool)
        try:
            sdk.preflight()
            root_view = sdk.self_view()
            if (
                root_view.get("parentThinkthreadId") is not None
                or root_view.get("thinkthreadId") != pool.get("root_thinkthread_id")
            ):
                raise RuntimeError("ThinkThread pool cleanup Root ownership mismatch")
        except (AgentPosixBridgeError, RuntimeError) as exc:
            with exclusive_file_lock(_lock_path(root_dir, pool_id)):
                failed_pool = _load_pool(root_dir, pool_id)
                failed_pool["state"] = "needs_recovery"
                failed_pool["cleanup_observation"] = {
                    "remaining_pool_children": [],
                    "unbound_unknown_jobs": [],
                    "storage": None,
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                    "observed_at": utc_timestamp(),
                }
                _write_pool(root_dir, failed_pool)
            return snapshot_pool(root_dir=root_dir, pool_id=pool_id)
        for job in jobs:
            intent = job.get("spawn_intent")
            if (
                not isinstance(job.get("thinkthread_id"), str)
                and isinstance(intent, dict)
                and intent.get("state")
                in {"platform_mutation_started", "outcome_unknown"}
            ):
                try:
                    _recover_spawn(sdk, root_dir, pool, job)
                except (AgentPosixBridgeError, RuntimeError) as exc:
                    job["status"] = "needs_recovery"
                    intent["last_recovery_error"] = {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    }
                    intent["last_recovery_at"] = utc_timestamp()
                    _write_job(root_dir, pool_id, job)
        jobs = _jobs(root_dir, pool)
        raw_cleanup = pool.get("cleanup")
        cleanup: list[dict[str, Any]] = (
            raw_cleanup if isinstance(raw_cleanup, list) else []
        )

        def persist() -> None:
            with exclusive_file_lock(_lock_path(root_dir, pool_id)):
                latest_pool = _load_pool(root_dir, pool_id)
                latest_pool["cleanup"] = cleanup
                _write_pool(root_dir, latest_pool)

        for job in jobs:
            child_id = job.get("thinkthread_id")
            branch_id = job.get("fs_branch_id")
            if not isinstance(child_id, str):
                continue
            record = next(
                (
                    item
                    for item in cleanup
                    if isinstance(item, dict)
                    and item.get("job_id") == job.get("job_id")
                    and item.get("thinkthread_id") == child_id
                ),
                None,
            )
            if record is None:
                record = {
                    "job_id": job.get("job_id"),
                    "candidate_id": job.get("candidate_id"),
                    "thinkthread_id": child_id,
                    "fs_branch_id": branch_id,
                    "state": "prepared",
                    "created_at": utc_timestamp(),
                }
                cleanup.append(record)
            if record.get("state") in {"removed", "already_removed"}:
                continue
            persist()
            try:
                child = ChildDriver(sdk, SystemClock()).observe(
                    child_id, root_id=pool.get("root_thinkthread_id"), branch_id=branch_id,
                )
                if child is not None and child.get("executionState") != "absent":
                    record["state"] = "stopping"
                    persist()
                    if ChildDriver(sdk, SystemClock()).interrupt(child_id, timeout_seconds) is None:
                        raise RuntimeError("ThinkThread Child execution remained active")
                if isinstance(branch_id, str):
                    try:
                        branch = sdk.invoke("fs.branch.stat", {"branchId": branch_id})
                    except AgentPosixBridgeError as exc:
                        if exc.code != "FsBranchNotFound":
                            raise
                        branch = None
                    if branch is not None and branch.get("branchId") != branch_id:
                        raise PermissionError(
                            "ThinkThread candidate branch ownership mismatch"
                        )
                    if branch is not None and branch.get("thinkthreadId") != child_id:
                        raise PermissionError(
                            "ThinkThread candidate branch Child ownership mismatch"
                        )
                    if branch is not None and branch.get("state") != "removed":
                        generation = branch.get("controlGeneration")
                        if not isinstance(generation, int):
                            raise RuntimeError("fs.branch.stat omitted controlGeneration")
                        record["state"] = "removing_branch"
                        record["branch_generation"] = generation
                        persist()
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
                if child is not None:
                    record["state"] = "destroying_child"
                    persist()
                    ChildDriver(sdk, SystemClock()).destroy(child_id)
                record["state"] = "removed"
                record["removed_at"] = utc_timestamp()
            except AgentPosixBridgeError as exc:
                if exc.code in {"ThinkThreadNotFound", "FsBranchNotFound"}:
                    record["state"] = "already_removed"
                else:
                    record["state"] = (
                        "needs_recovery" if exc.completion_unknown else "failed"
                    )
                    record["error_code"] = exc.code
                    record["message"] = str(exc)
            except (PermissionError, RuntimeError) as exc:
                record["state"] = "failed"
                record["error_type"] = type(exc).__name__
                record["message"] = str(exc)
            persist()
        for job in jobs:
            spawn_intent = job.get("spawn_intent")
            unresolved_spawn = (
                not isinstance(job.get("thinkthread_id"), str)
                and isinstance(spawn_intent, dict)
                and spawn_intent.get("state")
                in {"platform_mutation_started", "outcome_unknown"}
            )
            if job.get("status") in ACTIVE_JOB_STATES and not unresolved_spawn:
                job["status"] = "interrupted"
                job["finished_at"] = utc_timestamp()
                _write_job(root_dir, pool_id, job)
        unbound_unknown = [
            str(job["job_id"])
            for job in jobs
            if not isinstance(job.get("thinkthread_id"), str)
            and isinstance(job.get("spawn_intent"), dict)
            and job["spawn_intent"].get("state")
            in {"platform_mutation_started", "outcome_unknown"}
        ]
        remaining_children: list[str] = []
        try:
            listed = sdk.invoke("thinkthread.list").get("children")
            if not isinstance(listed, list):
                raise AgentPosixBridgeError("thinkthread.list omitted children")
            owned_ids = {
                str(job["thinkthread_id"])
                for job in jobs
                if isinstance(job.get("thinkthread_id"), str)
            }
            remaining_children = sorted(
                str(item["thinkthreadId"])
                for item in listed
                if isinstance(item, dict)
                and item.get("thinkthreadId") in owned_ids
            )
            storage = sdk.invoke("fs.stat").get("storage")
        except AgentPosixBridgeError as exc:
            remaining_children = [f"observation_failed:{exc}"]
            storage = None
        with exclusive_file_lock(_lock_path(root_dir, pool_id)):
            pool = _load_pool(root_dir, pool_id)
            pool["state"] = (
                "needs_recovery"
                if (
                    any(
                        item.get("state") in {"failed", "needs_recovery"}
                        for item in cleanup
                        if isinstance(item, dict)
                    )
                    or remaining_children
                    or unbound_unknown
                    or (
                        isinstance(pool.get("baseline_intent"), dict)
                        and pool["baseline_intent"].get("state")
                        not in {"closed", "provided"}
                    )
                )
                else "closed"
            )
            pool["cleanup"] = cleanup
            pool["cleanup_observation"] = {
                "remaining_pool_children": remaining_children,
                "unbound_unknown_jobs": unbound_unknown,
                "storage": storage,
                "observed_at": utc_timestamp(),
            }
            _write_pool(root_dir, pool)
        return snapshot_pool(root_dir=root_dir, pool_id=pool_id)
