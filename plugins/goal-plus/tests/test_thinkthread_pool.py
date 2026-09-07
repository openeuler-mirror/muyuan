from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

import goal_plus.host_scope as host_scope
from goal_plus.host_scope import ensure_execution_scope
from goal_plus.host_services import SearchHostService
from goal_plus.pi_tool import SEARCH_TOOL_NAMES
from goal_plus.thinkthread_agent_posix import AgentPosixBridgeError
from goal_plus.execution.thinkthread_child import ChildDriver
from goal_plus.execution.thinkthread_requests import ThinkThreadRequests
from goal_plus.execution.thinkthread_rpc import response_hash, response_payload
from goal_plus.infrastructure.clock import SystemClock
from goal_plus.thinkthread_pool import (
    PROTOCOL,
    WORKER_TOOL_PARAMETERS,
    WORKER_TOOLS,
    _continuation_feedback,
    _controller_lock_path,
    _dispatch_worker_tool,
    _load_job,
    _load_pool,
    _TryControllerLock,
    _write_job,
    close_pool,
    continue_pool,
    open_pool,
    validate_root,
    wait_any,
)


def test_worker_response_hash_is_stable_across_durable_json_replay(
    tmp_path: Path,
) -> None:
    response = {
        "ok": True,
        "result": {
            "state": "queued_at_turn_boundary",
            "request_id": "req-test",
            "message": "finish the turn",
        },
    }
    path = tmp_path / "response.json"
    path.write_text(json.dumps(response, sort_keys=True), encoding="utf-8")
    replayed = json.loads(path.read_text(encoding="utf-8"))

    assert response_hash(response) == response_hash(replayed)
    assert json.loads(response_payload("req-test", response)[0])[
        "response_sha256"
    ] == response_hash(response)

    response["result"]["payload"] = "x" * 100_000
    endpoint = Path(__file__).resolve().parents[1] / ".pi/extensions/lib/goal-plus-thinkthread-rpc.ts"
    checked = subprocess.run(
        ["node", "--input-type=module", "-e", '''
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
const { requestWorkerTool } = await import(process.argv[1]);
const chunks = JSON.parse(readFileSync(0, "utf8"));
let request, acknowledged = false;
const client = { async invoke(method, params) {
    if (method === "message.send") {
        const payload = JSON.parse(params.text);
        if (payload.type === "request") {
            request = payload;
            if (createHash("sha256").update(payload.content_json).digest("hex") !== payload.content_sha256)
                throw Error("request hash mismatch");
        } else if (payload.type === "response_ack") {
            if (payload.response_sha256 !== chunks[0].response_sha256) throw Error("ack hash mismatch");
            acknowledged = true;
        }
        return {};
    }
    return { messages: [...chunks.slice().reverse(), chunks[0]].map(chunk => ({
        senderThinkthreadId: "tt-parent", text: JSON.stringify({...chunk, request_id: request.request_id}),
    })), nextCursor: "done", hasMore: false };
}};
const result = await requestWorkerTool(client, "tt-parent", "agent-1", "search_get_agent_context", {}, new AbortController().signal);
if (!acknowledged) throw Error("response was not acknowledged");
process.stdout.write(JSON.stringify(result));
''', endpoint.as_uri()],
        input=json.dumps([json.loads(chunk) for chunk in response_payload("req-test", response)]),
        text=True, capture_output=True, check=True, timeout=10,
    )
    assert json.loads(checked.stdout) == response["result"]


def test_worker_proxy_tools_are_an_explicit_public_search_subset() -> None:
    assert WORKER_TOOLS == set(WORKER_TOOL_PARAMETERS)
    assert WORKER_TOOLS < SEARCH_TOOL_NAMES


def test_worker_proxy_accepts_only_one_stable_attempt_per_turn(tmp_path: Path) -> None:
    from goal_plus.runtime import FileSearchRuntime
    from tests._runtime_helpers import make_project, spec_for

    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(spec_for(project), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    job = {
        "agent_session_id": "agent-1",
        "run_id": run_id,
        "candidate_id": "c001",
        "dispatch_index": 1,
    }
    params = {
        "run_id": run_id,
        "candidate_id": "c001",
        "agent_session_id": "agent-1",
        "hypothesis": "Try one change",
        "toolization_decision": {
            "outcome": "not_applicable",
            "signals": [],
            "exclusion": "no_toolizable_material",
            "rationale": "No reusable material was created.",
            "tool_names": [],
        },
    }

    accepted = _dispatch_worker_tool(
        tmp_path / ".gp",
        job,
        "search_run_verifier",
        params,
        request_id="req-one",
    )
    replayed = _dispatch_worker_tool(
        tmp_path / ".gp",
        job,
        "search_run_verifier",
        params,
        request_id="req-one",
    )

    assert accepted == replayed
    assert accepted["state"] == "accepted"
    assert accepted["attempt_id"].startswith("attempt_")
    assert accepted["toolization_decision"] is None
    with pytest.raises(ValueError, match="reused with new content"):
        _dispatch_worker_tool(
            tmp_path / ".gp",
            job,
            "search_run_verifier",
            {**params, "hypothesis": "Different content"},
            request_id="req-one",
        )
    with pytest.raises(RuntimeError, match="only one candidate attempt"):
        _dispatch_worker_tool(
            tmp_path / ".gp",
            job,
            "search_run_verifier",
            params,
            request_id="req-two",
        )


@pytest.mark.parametrize("shared_enabled", [False, True])
def test_worker_proxy_checks_toolization_before_accepting_attempt(tmp_path, shared_enabled):
    from copy import deepcopy

    from goal_plus.models import SearchSpec
    from goal_plus.runtime import FileSearchRuntime
    from tests._runtime_helpers import make_project, spec_for

    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    data = spec_for(project).model_dump(mode="json")
    data["shared_dir"] = {"enabled": shared_enabled}
    frozen = runtime.freeze_spec(SearchSpec.model_validate(data), [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    job = {
        "agent_session_id": "agent-1", "run_id": run_id, "candidate_id": "c001",
        "dispatch_index": 1, "inner_git_binding": {"protocol": "goal-plus.inner-git.v1"},
    }
    params = {
        "agent_session_id": "agent-1", "run_id": run_id, "candidate_id": "c001",
        "hypothesis": "Verify one changed artifact",
        "_inner_git_attempt": {
            "protocol": "goal-plus.inner-git.v1", "adapter": "autoresearch-git",
            "attempt_git_head": "a" * 40, "baseline_git_head": "b" * 40,
            "result_count_before": 0,
        },
        "toolization_decision": {
            "outcome": "staged", "signals": ["behavior_or_invariant_checker"],
            "rationale": "Reusable checker", "tool_names": ["checker"], "exclusion": None,
        },
    }
    before_job = deepcopy(job)
    before_run = runtime.search_run.state.read_run(run_id)
    if not shared_enabled:
        with pytest.raises(ValueError, match="shared_dir.enabled=true"):
            _dispatch_worker_tool(runtime.root_dir, job, "search_run_verifier", params, request_id="bad")
        assert job == before_job
        assert runtime.search_run.state.read_run(run_id) == before_run
        params.pop("toolization_decision")
    accepted = _dispatch_worker_tool(
        runtime.root_dir, job, "search_run_verifier", params, request_id="corrected",
    )
    assert accepted["state"] == "accepted"
    assert job["inner_git_attempt"] == params["_inner_git_attempt"]
    assert accepted["toolization_decision"] == params.get("toolization_decision")
    assert runtime.search_run.state.read_run(run_id) == before_run


def test_legacy_settlement_without_key_uses_stable_attempt_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    sentinel = object()

    def recover(self, **kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(
        SearchHostService,
        "recover_legacy_thinkthread_attempt_feedback",
        recover,
    )
    pool = {"pool_id": "ttpool-1", "run_id": "run-1"}
    job = {
        "job_id": "job-1",
        "dispatch_index": 2,
        "active_message_id": "msg-2",
        "candidate_id": "c001",
        "agent_session_id": "agent-1",
        "settlement": {"settled_at": "2026-09-03T00:00:00Z"},
        "pending_verifier": {"request_id": "request-1"},
    }

    feedback = _continuation_feedback(tmp_path / ".gp", pool, job)

    assert feedback is sentinel
    assert captured["attempt_id"] == "ttpool-1:job-1:2:msg-2"
    assert captured["request_id"] == "request-1"


@pytest.mark.parametrize(
    ("tool", "params", "message"),
    [
        (
            "search_get_agent_context",
            {"agent_session_id": "agent-1", "extra": True},
            "unexpected parameters: extra",
        ),
        (
            "search_get_global_evidence",
            {},
            "requires agent_session_id",
        ),
        (
            "search_get_evidence_detail",
            {
                "agent_session_id": "agent-1",
                "candidate_id": "c002",
                "iteration": "1",
            },
            "iteration must be positive",
        ),
        (
            "search_list_iterations",
            {},
            "requires agent_session_id",
        ),
        (
            "search_run_verifier",
            {
                "run_id": "run_rpc",
                "candidate_id": "c001",
                "agent_session_id": "agent-1",
                "hypothesis": "Try one change",
                "toolization_decision": {"outcome": "not_needed"},
            },
            "Input should be 'staged' or 'not_applicable'",
        ),
    ],
)
def test_worker_proxy_rejects_parameter_contract_drift(
    tmp_path: Path,
    tool: str,
    params: dict[str, object],
    message: str,
) -> None:
    job = {
        "agent_session_id": "agent-1",
        "run_id": "run_rpc",
        "candidate_id": "c001",
    }

    with pytest.raises((ValueError, PermissionError), match=message):
        _dispatch_worker_tool(
            tmp_path / ".gp",
            job,
            tool,
            params,
            request_id="req-contract",
        )


def test_completed_wake_uses_ready_agent_with_resident_execution() -> None:
    job = {"active_message_id": "msg-active"}
    child = {
        "agentState": "running",
        "executionState": "running",
        "pendingWake": False,
        "lastWakeOutcome": {
            "messageId": "msg-active",
            "outcome": "completed",
        },
    }

    driver = ChildDriver(StatefulAgentPosix(), SystemClock())
    assert driver.turn_terminal(child, job["active_message_id"]) is None
    child["agentState"] = "ready"
    assert driver.turn_terminal(child, job["active_message_id"]) == "completed"


class StatefulAgentPosix:
    def __init__(self) -> None:
        self.operations: list[tuple[str, dict[str, object]]] = []
        self.children: list[dict[str, object]] = []
        self.messages: dict[str, list[dict[str, object]]] = {}
        self.branches: dict[str, dict[str, object]] = {}
        self.request_results: dict[str, dict[str, object]] = {}
        self.sent: list[dict[str, object]] = []
        self.spawn_completion_unknown = False
        self.wake_completion_unknown = False
        self.branch_remove_completion_unknown = False
        self.destroy_completion_unknown = False
        self.close_completion_unknown_before_once = False
        self.close_completion_unknown_after_once = False
        self.close_failure_once = False
        self.close_calls = 0
        self.closed_requests: set[str] = set()

    def preflight(self) -> dict[str, object]:
        return {"controlProtocolVersion": 2}

    def self_view(self) -> dict[str, object]:
        return {
            "thinkthreadId": "tt-root",
            "parentThinkthreadId": None,
            "capabilities": [
                {"id": "thinkthread.child", "version": 1},
                {"id": "thinkthread.message", "version": 1},
                {"id": "thinkthread.fs.self", "version": 1},
                {"id": "thinkthread.fs", "version": 1},
            ],
            "profiles": [
                {
                    "alias": "self",
                    "agent": "pi",
                    "allowedDerivations": [
                        {"session": "fresh", "fs": "private_branch"},
                        {
                            "session": "fork_current_checkpoint",
                            "fs": "private_branch",
                        },
                    ],
                    "allowedCapabilities": [
                        {"id": "thinkthread.message", "version": 1},
                        {"id": "thinkthread.fs.self", "version": 1},
                    ],
                    "allowedModels": [],
                }
            ],
        }

    def enqueue(self, child_id: str, payload: dict[str, object]) -> None:
        messages = self.messages.setdefault(child_id, [])
        messages.append(
            {
                "messageId": f"msg-worker-{len(messages) + 1}",
                "senderThinkthreadId": child_id,
                "recipientThinkthreadId": "tt-root",
                "text": json.dumps(payload, separators=(",", ":")),
                "truncated": False,
            }
        )

    def child(self, child_id: str) -> dict[str, object]:
        return next(
            item for item in self.children if item["thinkthreadId"] == child_id
        )

    def invoke(
        self,
        operation: str,
        params: dict[str, object] | None = None,
        **_: object,
    ) -> dict[str, object]:
        normalized = dict(params or {})
        self.operations.append((operation, normalized))
        if operation == "fs.stat":
            return {
                "kind": "direct",
                "state": "attached",
                "storage": {
                    "activeRequestCount": 0,
                    "activeRequestLimit": 16,
                    "snapshotCount": 0,
                    "pendingSnapshotCreations": 0,
                    "snapshotLimit": 1024,
                },
            }
        if operation == "fs.snapshot.create":
            request_id = str(normalized["requestId"])
            result = {"snapshotId": "fsnap-S0"}
            self.request_results[request_id] = {
                "requestId": request_id,
                "method": "fs.snapshot.create",
                "state": "succeeded",
                "result": result,
            }
            return result
        if operation == "fs.request.status":
            request_id = str(normalized["requestId"])
            if request_id in self.closed_requests:
                raise AgentPosixBridgeError(
                    "request is closed",
                    error={
                        "rejection": {
                            "error": {
                                "code": "RequestNotFound",
                                "message": "request is closed",
                                "retryable": False,
                            }
                        }
                    },
                )
            return self.request_results[request_id]
        if operation == "fs.request.close":
            self.close_calls += 1
            request_id = str(normalized["requestId"])
            if self.close_failure_once:
                self.close_failure_once = False
                raise AgentPosixBridgeError(
                    "request close failed",
                    error={
                        "rejection": {
                            "error": {
                                "code": "FsRequestCloseFailed",
                                "message": "request close failed",
                                "retryable": False,
                            }
                        }
                    },
                )
            if self.close_completion_unknown_before_once:
                self.close_completion_unknown_before_once = False
                raise AgentPosixBridgeError(
                    "request close response lost before completion",
                    error={"delivery": "completion_unknown"},
                )
            self.request_results.pop(request_id, None)
            self.closed_requests.add(request_id)
            if self.close_completion_unknown_after_once:
                self.close_completion_unknown_after_once = False
                raise AgentPosixBridgeError(
                    "request close response lost after completion",
                    error={"delivery": "completion_unknown"},
                )
            return {}
        if operation == "thinkthread.list":
            return {"children": list(self.children)}
        if operation == "thinkthread.spawn":
            index = len(self.children) + 1
            child_id = f"tt-child-{index}"
            branch_id = f"fsbranch-child-{index}"
            result: dict[str, object] = {
                "thinkthreadId": child_id,
                "capabilities": [{"id": "thinkthread.message", "version": 1}],
                "fs": {"kind": "private", "fsBranchId": branch_id},
                "initialMessageId": f"msg-initial-{index}",
            }
            child = {
                **result,
                "parentThinkthreadId": "tt-root",
                "agentState": "running",
                "executionState": "running",
                "pendingWake": False,
                "lastWakeOutcome": None,
                "completion": None,
            }
            self.children.append(child)
            self.messages[child_id] = []
            self.branches[branch_id] = {
                "branchId": branch_id,
                "thinkthreadId": child_id,
                "state": "attached",
                "controlGeneration": 0,
            }
            if self.spawn_completion_unknown:
                self.spawn_completion_unknown = False
                child.pop("initialMessageId")
                registration_nonce = str(normalized["initialMessage"]).split(
                    "registration_nonce=", 1
                )[1].split(";", 1)[0]
                self.enqueue(
                    child_id,
                    {
                        "protocol": PROTOCOL,
                        "type": "registration",
                        "registration_nonce": registration_nonce,
                    },
                )
                raise AgentPosixBridgeError(
                    "spawn response lost",
                    error={"delivery": "completion_unknown"},
                )
            return result
        if operation == "message.receive":
            child_id = str(normalized["senderThinkthreadId"])
            after = int(str(normalized.get("after") or "0"))
            messages = self.messages[child_id][after:]
            return {
                "messages": messages,
                "nextCursor": str(after + len(messages)),
                "hasMore": False,
            }
        if operation == "thinkthread.get":
            try:
                return dict(self.child(str(normalized["id"])))
            except StopIteration as exc:
                raise AgentPosixBridgeError(
                    "Child not found",
                    error={
                        "rejection": {
                            "error": {
                                "code": "ThinkThreadNotFound",
                                "message": "Child not found",
                                "retryable": False,
                            }
                        }
                    },
                ) from exc
        if operation == "message.send":
            index = len(self.sent) + 1
            sent = {**normalized, "messageId": f"msg-wake-{index}"}
            self.sent.append(sent)
            child = self.child(str(normalized["recipientThinkthreadId"]))
            if normalized.get("wake") is True:
                child["agentState"] = "running"
                child["executionState"] = "running"
                child["lastWakeOutcome"] = None
            if self.wake_completion_unknown:
                self.wake_completion_unknown = False
                dispatch_nonce = str(normalized["text"]).split(
                    "dispatch_nonce=", 1
                )[1].split(".", 1)[0]
                self.enqueue(
                    str(normalized["recipientThinkthreadId"]),
                    {
                        "protocol": PROTOCOL,
                        "type": "dispatch_ack",
                        "dispatch_nonce": dispatch_nonce,
                    },
                )
                raise AgentPosixBridgeError(
                    "wake response lost",
                    error={"delivery": "completion_unknown"},
                )
            return {"messageId": sent["messageId"]}
        if operation == "thinkthread.signal":
            child = self.child(str(normalized["id"]))
            child["executionState"] = "absent"
            child["agentState"] = "ready"
            return {}
        if operation == "thinkthread.wait":
            return {"child": dict(self.child(str(normalized["id"])))}
        if operation == "fs.branch.stat":
            return dict(self.branches[str(normalized["branchId"])])
        if operation == "fs.branch.remove":
            branch = self.branches[str(normalized["branchId"])]
            branch["state"] = "removed"
            branch["controlGeneration"] = int(branch["controlGeneration"]) + 1
            if self.branch_remove_completion_unknown:
                self.branch_remove_completion_unknown = False
                raise AgentPosixBridgeError(
                    "branch removal response lost",
                    error={"delivery": "completion_unknown"},
                )
            return dict(branch)
        if operation == "thinkthread.destroy":
            child_id = str(normalized["id"])
            self.children = [
                item for item in self.children if item["thinkthreadId"] != child_id
            ]
            if self.destroy_completion_unknown:
                self.destroy_completion_unknown = False
                raise AgentPosixBridgeError(
                    "destroy response lost",
                    error={"delivery": "completion_unknown"},
                )
            return {}
        raise AssertionError(operation)


def candidates(count: int = 2) -> list[dict[str, object]]:
    return [
        {
            "candidate_id": f"c{index:03d}",
            "agent_session_id": f"agent-{index}",
            "message": f"Work candidate {index}",
            "max_runtime_seconds": 60,
        }
        for index in range(1, count + 1)
    ]


def worker_request(
    *,
    request_id: str = "req-worker-1",
    tool: str = "search_list_iterations",
    params: dict[str, object] | None = None,
    deadline_unix_ms: int | None = None,
) -> dict[str, object]:
    normalized = params or {"agent_session_id": "agent-1"}
    content = {
        "agent_session_id": "agent-1",
        "params": normalized,
        "tool": tool,
    }
    if deadline_unix_ms is not None:
        content["deadline_unix_ms"] = deadline_unix_ms
    content_json = json.dumps(content, separators=(",", ":"))
    request = {
        "protocol": PROTOCOL,
        "type": "request",
        "request_id": request_id,
        "agent_session_id": "agent-1",
        "tool": tool,
        "params": normalized,
        "content_json": content_json,
        "content_sha256": hashlib.sha256(content_json.encode()).hexdigest(),
    }
    if deadline_unix_ms is not None:
        request["deadline_unix_ms"] = deadline_unix_ms
    return request


def test_k2_candidates_spawn_from_one_durable_baseline(tmp_path: Path) -> None:
    client = StatefulAgentPosix()

    opened = open_pool(
        root_dir=tmp_path / ".gp",
        run_id="run_test",
        candidates=candidates(),
        max_parallel=2,
        client=client,  # type: ignore[arg-type]
    )

    assert opened["host"] == "thinkthread"
    assert opened["baseline_snapshot_id"] == "fsnap-S0"
    assert opened["active_count"] == 2
    assert {job["thinkthread_id"] for job in opened["jobs"]} == {
        "tt-child-1",
        "tt-child-2",
    }
    assert {job["fs_branch_id"] for job in opened["jobs"]} == {
        "fsbranch-child-1",
        "fsbranch-child-2",
    }
    spawn_params = [
        params for operation, params in client.operations if operation == "thinkthread.spawn"
    ]
    assert len(spawn_params) == 2
    assert {item["fsSnapshotId"] for item in spawn_params} == {"fsnap-S0"}
    assert all(item["profile"] == "self" for item in spawn_params)
    assert all(item["fs"] == "private" for item in spawn_params)
    assert all(item["capabilities"] == ["thinkthread.message"] for item in spawn_params)
    pool = _load_pool(tmp_path / ".gp", opened["pool_id"])
    assert pool["baseline_intent"]["state"] == "closed"
    assert pool["baseline_intent"]["snapshot_id"] == "fsnap-S0"


def test_thinkthread_continuation_is_rejected_after_scope_run_fence(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".gp"
    client = StatefulAgentPosix()
    scope = ensure_execution_scope(
        root_dir=root,
        host="pi",
        owner_session_id="thinkthread-pool-owner",
        owner_pid=os.getpid(),
        run_id="run_fenced",
        start_guardian=False,
    )
    opened = open_pool(
        root_dir=root,
        run_id="run_fenced",
        candidates=candidates(1),
        execution_scope_id=scope["scope_id"],
        client=client,  # type: ignore[arg-type]
    )
    host_scope.build_scope_cleanup(root, host_scope._HostScopeCleanupServices(root))._fence_run(
        scope_id=scope["scope_id"],
        attachment=host_scope.ScopeAttachment("search_run", "run_fenced"),
    )

    with pytest.raises(RuntimeError, match="fenced against new host work"):
        continue_pool(
            root_dir=root,
            pool_id=opened["pool_id"],
            candidate_id="c001",
            message="continue",
            max_runtime_seconds=30,
            client=client,  # type: ignore[arg-type]
        )

    closed = close_pool(
        root_dir=root,
        pool_id=opened["pool_id"],
        mode="interrupt",
        client=client,  # type: ignore[arg-type]
    )
    assert closed["state"] == "closed"


def test_thinkthread_pool_open_cannot_publish_after_scope_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / ".gp"
    client = StatefulAgentPosix()
    scope = ensure_execution_scope(
        root_dir=root,
        host="pi",
        owner_session_id="thinkthread-open-race-owner",
        owner_pid=os.getpid(),
        run_id="run_open_race",
        start_guardian=False,
    )
    validated = threading.Event()
    release = threading.Event()
    original = validate_root

    def blocking_validate(client_arg: object) -> dict[str, object]:
        result = original(client_arg)  # type: ignore[arg-type]
        validated.set()
        assert release.wait(timeout=5)
        return result

    monkeypatch.setattr(
        "goal_plus.thinkthread_pool.validate_root",
        blocking_validate,
    )
    outcome: dict[str, object] = {}

    def open_in_thread() -> None:
        try:
            outcome["result"] = open_pool(
                root_dir=root,
                run_id="run_open_race",
                candidates=candidates(1),
                execution_scope_id=scope["scope_id"],
                client=client,  # type: ignore[arg-type]
            )
        except RuntimeError as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=open_in_thread)
    thread.start()
    assert validated.wait(timeout=5)
    host_scope.close_run_candidates(
        root_dir=root,
        scope_id=scope["scope_id"],
        run_id="run_open_race",
    )
    release.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert isinstance(outcome.get("error"), RuntimeError)
    assert "fenced against new host work" in str(outcome["error"])
    assert list((root / "host-pools" / "thinkthread").glob("*/pool.json")) == []


def test_close_keeps_unbound_platform_spawn_in_needs_recovery(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".gp"
    client = StatefulAgentPosix()
    opened = open_pool(
        root_dir=root,
        run_id="run_unbound_spawn",
        candidates=candidates(1),
        client=client,  # type: ignore[arg-type]
    )
    job = _load_job(root, opened["pool_id"], opened["jobs"][0]["job_id"])
    job["status"] = "starting"
    job["thinkthread_id"] = None
    job["fs_branch_id"] = None
    job["spawn_intent"]["state"] = "platform_mutation_started"
    _write_job(root, opened["pool_id"], job)

    closed = close_pool(
        root_dir=root,
        pool_id=opened["pool_id"],
        mode="interrupt",
        client=client,  # type: ignore[arg-type]
    )

    assert closed["state"] == "needs_recovery"
    assert closed["cleanup_observation"]["unbound_unknown_jobs"] == [job["job_id"]]
    assert client.children[0]["thinkthreadId"] == "tt-child-1"


def test_close_recovers_and_removes_spawn_with_lost_response(tmp_path: Path) -> None:
    root = tmp_path / ".gp"
    client = StatefulAgentPosix()
    client.spawn_completion_unknown = True
    opened = open_pool(
        root_dir=root,
        run_id="run_recover_on_close",
        candidates=candidates(1),
        client=client,  # type: ignore[arg-type]
    )

    closed = close_pool(
        root_dir=root,
        pool_id=opened["pool_id"],
        mode="interrupt",
        client=client,  # type: ignore[arg-type]
    )

    assert closed["state"] == "closed"
    assert closed["cleanup_observation"]["unbound_unknown_jobs"] == []
    assert client.children == []


@pytest.mark.parametrize("field,value", [("requestId", "req-peer"), ("method", "fs.apply")])
def test_request_recovery_rejects_another_operation_result(field, value):
    client = StatefulAgentPosix()
    client.request_results["req-baseline"] = {
        "requestId": "req-baseline", "method": "fs.snapshot.create",
        "state": "succeeded", "result": {"snapshotId": "fsnap-peer"},
        field: value,
    }
    requests = ThinkThreadRequests(client)
    with pytest.raises(RuntimeError, match="identity mismatch"):
        requests.observe("req-baseline", "fs.snapshot.create")
    client.close_completion_unknown_before_once = True
    result = requests.close("req-baseline", "fs.snapshot.create")
    assert result.state == "needs_recovery"
    assert "identity mismatch" in result.error["message"]
    assert client.closed_requests == set()


@pytest.mark.parametrize("fault", ["completion_unknown_before", "definitive_failure"])
def test_baseline_request_close_retries_without_recapturing_snapshot(
    tmp_path: Path,
    fault: str,
) -> None:
    client = StatefulAgentPosix()
    if fault == "completion_unknown_before":
        client.close_completion_unknown_before_once = True
    else:
        client.close_failure_once = True

    opened = open_pool(
        root_dir=tmp_path / ".gp",
        run_id="run_close_recovery",
        candidates=candidates(1),
        client=client,  # type: ignore[arg-type]
    )

    assert opened["baseline_intent"]["state"] == "close_needs_recovery"
    assert opened["baseline_intent"]["close_attempts"] == 1
    assert client.close_calls == 1
    snapshot_calls = len(
        [item for item in client.operations if item[0] == "fs.snapshot.create"]
    )

    recovered = wait_any(
        root_dir=tmp_path / ".gp",
        pool_id=opened["pool_id"],
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )

    assert recovered["pool"]["baseline_intent"]["state"] == "closed"
    assert recovered["pool"]["baseline_intent"]["close_attempts"] == 2
    assert client.close_calls == 2
    assert len(
        [item for item in client.operations if item[0] == "fs.snapshot.create"]
    ) == snapshot_calls


def test_baseline_request_close_completion_unknown_after_close_is_confirmed(
    tmp_path: Path,
) -> None:
    client = StatefulAgentPosix()
    client.close_completion_unknown_after_once = True

    opened = open_pool(
        root_dir=tmp_path / ".gp",
        run_id="run_close_confirmed",
        candidates=candidates(1),
        client=client,  # type: ignore[arg-type]
    )

    assert opened["baseline_intent"]["state"] == "closed"
    assert opened["baseline_intent"]["close_attempts"] == 1
    assert client.close_calls == 1


def test_root_preflight_fails_closed_for_wrong_profile(tmp_path: Path) -> None:
    del tmp_path
    client = StatefulAgentPosix()
    root = client.self_view()
    root["profiles"][0]["agent"] = "codex"  # type: ignore[index]
    client.self_view = lambda: root  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="must select the Pi Agent"):
        validate_root(client)  # type: ignore[arg-type]


def test_spawn_completion_unknown_binds_by_registration_nonce(tmp_path: Path) -> None:
    client = StatefulAgentPosix()
    client.spawn_completion_unknown = True
    opened = open_pool(
        root_dir=tmp_path / ".gp",
        run_id="run_recover",
        candidates=candidates(1),
        client=client,  # type: ignore[arg-type]
    )
    assert opened["jobs"][0]["status"] == "needs_recovery"

    result = wait_any(
        root_dir=tmp_path / ".gp",
        pool_id=opened["pool_id"],
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )

    assert result["event"] is None
    assert result["pool"]["jobs"][0]["status"] == "running"
    assert result["pool"]["jobs"][0]["thinkthread_id"] == "tt-child-1"


def test_message_cursor_replay_and_retained_continuation(tmp_path: Path) -> None:
    client = StatefulAgentPosix()
    opened = open_pool(
        root_dir=tmp_path / ".gp",
        run_id="run_cursor",
        candidates=candidates(1),
        client=client,  # type: ignore[arg-type]
    )
    pool_id = opened["pool_id"]
    job_id = opened["jobs"][0]["job_id"]
    job = _load_job(tmp_path / ".gp", pool_id, job_id)
    client.enqueue(
        "tt-child-1",
        {
            "protocol": PROTOCOL,
            "type": "registration",
            "registration_nonce": job["registration_nonce"],
        },
    )

    first = wait_any(
        root_dir=tmp_path / ".gp",
        pool_id=pool_id,
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )
    second = wait_any(
        root_dir=tmp_path / ".gp",
        pool_id=pool_id,
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )
    assert first["event"] is None
    assert second["event"] is None
    job = _load_job(tmp_path / ".gp", pool_id, job_id)
    assert job["message_cursor"] == "1"
    assert job["seen_message_ids"] == ["msg-worker-1"]

    child = client.child("tt-child-1")
    child["agentState"] = "ready"
    child["executionState"] = "absent"
    child["lastWakeOutcome"] = {
        "messageId": job["active_message_id"],
        "outcome": "completed",
    }
    ready = wait_any(
        root_dir=tmp_path / ".gp",
        pool_id=pool_id,
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )
    assert ready["event"]["kind"] == "candidate_ready"
    retained_id = ready["event"]["result"]["thinkthread_id"]

    continued = continue_pool(
        root_dir=tmp_path / ".gp",
        pool_id=pool_id,
        candidate_id="c001",
        message="Continue the same candidate",
        max_runtime_seconds=30,
        client=client,  # type: ignore[arg-type]
    )
    assert continued["jobs"][0]["thinkthread_id"] == retained_id
    assert continued["jobs"][0]["dispatch_index"] == 2
    assert client.sent[-1]["recipientThinkthreadId"] == retained_id
    assert client.sent[-1]["wake"] is True


def test_initial_and_continuation_dispatches_end_with_attempt_commands(
    tmp_path: Path,
) -> None:
    client = StatefulAgentPosix()
    opened = open_pool(
        root_dir=tmp_path / ".gp",
        run_id="run_dispatch_commands",
        candidates=candidates(1),
        client=client,  # type: ignore[arg-type]
    )
    initial_message = next(
        params["initialMessage"]
        for operation, params in client.operations
        if operation == "thinkthread.spawn"
    )
    assert initial_message.endswith(
        "不得只读取 context/Evidence、只总结任务或不提交 verifier 就结束本次 wake。"
    )

    pool_id = opened["pool_id"]
    job_id = opened["jobs"][0]["job_id"]
    job = _load_job(tmp_path / ".gp", pool_id, job_id)
    client.enqueue(
        "tt-child-1",
        {
            "protocol": PROTOCOL,
            "type": "registration",
            "registration_nonce": job["registration_nonce"],
        },
    )
    child = client.child("tt-child-1")
    child["agentState"] = "ready"
    child["executionState"] = "absent"
    child["lastWakeOutcome"] = {
        "messageId": job["active_message_id"],
        "outcome": "completed",
    }
    wait_any(
        root_dir=tmp_path / ".gp",
        pool_id=pool_id,
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )
    continue_pool(
        root_dir=tmp_path / ".gp",
        pool_id=pool_id,
        candidate_id="c001",
        message="Continue the same candidate",
        max_runtime_seconds=30,
        client=client,  # type: ignore[arg-type]
    )
    assert client.sent[-1]["text"].endswith(
        "不得只读取 context/Evidence、只总结上一轮或不提交 verifier 就结束本次 wake。"
    )


def test_late_prior_dispatch_ack_does_not_block_retained_worker_rpc(
    tmp_path: Path,
) -> None:
    client = StatefulAgentPosix()
    opened = open_pool(
        root_dir=tmp_path / ".gp",
        run_id="run_late_ack",
        candidates=candidates(1),
        client=client,  # type: ignore[arg-type]
    )
    pool_id = opened["pool_id"]
    job_id = opened["jobs"][0]["job_id"]
    first_job = _load_job(tmp_path / ".gp", pool_id, job_id)
    first_nonce = first_job["dispatch_nonce"]
    client.enqueue(
        "tt-child-1",
        {
            "protocol": PROTOCOL,
            "type": "registration",
            "registration_nonce": first_job["registration_nonce"],
        },
    )
    client.child("tt-child-1")["agentState"] = "ready"
    client.child("tt-child-1")["executionState"] = "absent"
    client.child("tt-child-1")["lastWakeOutcome"] = {
        "messageId": first_job["active_message_id"],
        "outcome": "completed",
    }
    ready = wait_any(
        root_dir=tmp_path / ".gp",
        pool_id=pool_id,
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )
    assert ready["event"]["kind"] == "candidate_ready"

    continue_pool(
        root_dir=tmp_path / ".gp",
        pool_id=pool_id,
        candidate_id="c001",
        message="Continue the same candidate",
        max_runtime_seconds=30,
        client=client,  # type: ignore[arg-type]
    )
    client.enqueue(
        "tt-child-1",
        {
            "protocol": PROTOCOL,
            "type": "dispatch_ack",
            "dispatch_nonce": first_nonce,
        },
    )

    waiting = wait_any(
        root_dir=tmp_path / ".gp",
        pool_id=pool_id,
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )

    assert waiting["event"] is None
    job = _load_job(tmp_path / ".gp", pool_id, job_id)
    assert first_nonce in job["acknowledged_dispatch_nonces"]
    assert job["dispatch_nonce"] != first_nonce


def test_legacy_job_ignores_unknown_stale_dispatch_ack_after_continuation(
    tmp_path: Path,
) -> None:
    client = StatefulAgentPosix()
    opened = open_pool(
        root_dir=tmp_path / ".gp",
        run_id="run_legacy_late_ack",
        candidates=candidates(1),
        client=client,  # type: ignore[arg-type]
    )
    pool_id = opened["pool_id"]
    job_id = opened["jobs"][0]["job_id"]
    first_job = _load_job(tmp_path / ".gp", pool_id, job_id)
    client.enqueue(
        "tt-child-1",
        {
            "protocol": PROTOCOL,
            "type": "registration",
            "registration_nonce": first_job["registration_nonce"],
        },
    )
    client.child("tt-child-1")["agentState"] = "ready"
    client.child("tt-child-1")["executionState"] = "absent"
    client.child("tt-child-1")["lastWakeOutcome"] = {
        "messageId": first_job["active_message_id"],
        "outcome": "completed",
    }
    wait_any(
        root_dir=tmp_path / ".gp",
        pool_id=pool_id,
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )
    continue_pool(
        root_dir=tmp_path / ".gp",
        pool_id=pool_id,
        candidate_id="c001",
        message="Continue the same candidate",
        max_runtime_seconds=30,
        client=client,  # type: ignore[arg-type]
    )
    legacy_job = _load_job(tmp_path / ".gp", pool_id, job_id)
    legacy_job.pop("dispatch_nonces")
    _write_job(tmp_path / ".gp", pool_id, legacy_job)
    stale_nonce = "00000000-0000-4000-8000-000000000001"
    client.enqueue(
        "tt-child-1",
        {
            "protocol": PROTOCOL,
            "type": "dispatch_ack",
            "dispatch_nonce": stale_nonce,
        },
    )

    waiting = wait_any(
        root_dir=tmp_path / ".gp",
        pool_id=pool_id,
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )

    assert waiting["event"] is None
    job = _load_job(tmp_path / ".gp", pool_id, job_id)
    assert job["stale_dispatch_acknowledgements"] == [stale_nonce]
    assert stale_nonce not in job["acknowledged_dispatch_nonces"]


def test_worker_rpc_is_bound_replay_safe_chunked_and_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StatefulAgentPosix()
    opened = open_pool(
        root_dir=tmp_path / ".gp",
        run_id="run_rpc",
        candidates=candidates(1),
        client=client,  # type: ignore[arg-type]
    )
    pool_id = opened["pool_id"]
    job_id = opened["jobs"][0]["job_id"]
    job = _load_job(tmp_path / ".gp", pool_id, job_id)
    client.enqueue(
        "tt-child-1",
        {
            "protocol": PROTOCOL,
            "type": "registration",
            "registration_nonce": job["registration_nonce"],
        },
    )
    request = worker_request(deadline_unix_ms=int(time.time() * 1000) + 60_000)
    client.enqueue("tt-child-1", request)
    dispatches: list[tuple[str, dict[str, object]]] = []

    def dispatch(
        _root_dir: Path,
        _job: dict[str, object],
        tool: str,
        params: dict[str, object],
        *,
        request_id: str,
    ) -> dict[str, object]:
        dispatches.append((request_id, {"tool": tool, **params}))
        return {"payload": "x" * (70 * 1024)}

    monkeypatch.setattr("goal_plus.thinkthread_pool._dispatch_worker_tool", dispatch)
    first = wait_any(
        root_dir=tmp_path / ".gp",
        pool_id=pool_id,
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )
    assert first["event"] is None
    assert len(dispatches) == 1
    response_chunks = [
        json.loads(str(item["text"]))
        for item in client.sent
        if json.loads(str(item["text"])).get("type") == "response_chunk"
    ]
    assert len(response_chunks) == 3
    assert [item["chunk_index"] for item in response_chunks] == [0, 1, 2]
    assert {item["chunk_count"] for item in response_chunks} == {3}

    client.enqueue("tt-child-1", request)
    wait_any(
        root_dir=tmp_path / ".gp",
        pool_id=pool_id,
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )
    assert len(dispatches) == 1

    job = _load_job(tmp_path / ".gp", pool_id, job_id)
    job["pending_responses"]["req-worker-1"]["last_attempt_unix"] = 0
    _write_job(tmp_path / ".gp", pool_id, job)
    sent_before_retry = len(client.sent)
    wait_any(
        root_dir=tmp_path / ".gp",
        pool_id=pool_id,
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )
    assert len(client.sent) == sent_before_retry + 3

    response_hash = response_chunks[0]["response_sha256"]
    client.enqueue(
        "tt-child-1",
        {
            "protocol": PROTOCOL,
            "type": "response_ack",
            "request_id": "req-worker-1",
            "response_sha256": response_hash,
        },
    )
    wait_any(
        root_dir=tmp_path / ".gp",
        pool_id=pool_id,
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )
    assert _load_job(tmp_path / ".gp", pool_id, job_id)["pending_responses"] == {}


def test_worker_rpc_fails_closed_on_binding_hash_or_truncation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StatefulAgentPosix()
    opened = open_pool(
        root_dir=tmp_path / ".gp",
        run_id="run_rpc",
        candidates=candidates(1),
        client=client,  # type: ignore[arg-type]
    )
    pool_id = opened["pool_id"]
    job = _load_job(tmp_path / ".gp", pool_id, opened["jobs"][0]["job_id"])
    client.enqueue(
        "tt-child-1",
        {
            "protocol": PROTOCOL,
            "type": "registration",
            "registration_nonce": job["registration_nonce"],
        },
    )
    wait_any(
        root_dir=tmp_path / ".gp",
        pool_id=pool_id,
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        "goal_plus.thinkthread_pool._dispatch_worker_tool",
        lambda *_args, **_kwargs: pytest.fail("invalid RPC must not dispatch"),
    )

    truncated = worker_request(request_id="req-truncated")
    client.enqueue("tt-child-1", truncated)
    client.messages["tt-child-1"][-1]["truncated"] = True
    wait_any(
        root_dir=tmp_path / ".gp",
        pool_id=pool_id,
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )

    changed = worker_request(request_id="req-bad-hash")
    changed["content_sha256"] = "0" * 64
    client.enqueue("tt-child-1", changed)
    with pytest.raises(ValueError, match="content_sha256 mismatch"):
        wait_any(
            root_dir=tmp_path / ".gp",
            pool_id=pool_id,
            timeout_seconds=0,
            client=client,  # type: ignore[arg-type]
        )


def test_candidate_child_cannot_call_annotation_tools(tmp_path: Path) -> None:
    client = StatefulAgentPosix()
    opened = open_pool(
        root_dir=tmp_path / ".gp",
        run_id="run_rpc",
        candidates=candidates(1),
        client=client,  # type: ignore[arg-type]
    )
    job = _load_job(tmp_path / ".gp", opened["pool_id"], opened["jobs"][0]["job_id"])
    client.enqueue(
        "tt-child-1",
        {
            "protocol": PROTOCOL,
            "type": "registration",
            "registration_nonce": job["registration_nonce"],
        },
    )
    client.enqueue(
        "tt-child-1",
        worker_request(
            request_id="req-annotation-tool",
            tool="thinkthread_annotation_next",
            params={"run_id": "run_rpc"},
        ),
    )

    with pytest.raises(PermissionError, match="worker RPC tool is not allowed"):
        wait_any(
            root_dir=tmp_path / ".gp",
            pool_id=opened["pool_id"],
            timeout_seconds=0,
            client=client,  # type: ignore[arg-type]
        )


def test_wake_completion_unknown_recovers_from_dispatch_ack(tmp_path: Path) -> None:
    client = StatefulAgentPosix()
    opened = open_pool(
        root_dir=tmp_path / ".gp",
        run_id="run_wake_recover",
        candidates=candidates(1),
        client=client,  # type: ignore[arg-type]
    )
    pool_id = opened["pool_id"]
    job_id = opened["jobs"][0]["job_id"]
    child = client.child("tt-child-1")
    child["agentState"] = "ready"
    child["executionState"] = "absent"
    child["completion"] = {"reason": "completed"}
    wait_any(
        root_dir=tmp_path / ".gp",
        pool_id=pool_id,
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )

    client.wake_completion_unknown = True
    continued = continue_pool(
        root_dir=tmp_path / ".gp",
        pool_id=pool_id,
        candidate_id="c001",
        message="Continue after an uncertain response",
        max_runtime_seconds=30,
        client=client,  # type: ignore[arg-type]
    )
    assert continued["jobs"][0]["status"] == "needs_recovery"

    recovered = wait_any(
        root_dir=tmp_path / ".gp",
        pool_id=pool_id,
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )
    assert recovered["event"] is None
    assert recovered["pool"]["jobs"][0]["status"] == "running"
    restored = _load_job(tmp_path / ".gp", pool_id, job_id)
    assert restored["wake_intent"]["state"] == "acknowledged_after_recovery"


def test_controller_lock_prevents_duplicate_cursor_processing(tmp_path: Path) -> None:
    client = StatefulAgentPosix()
    opened = open_pool(
        root_dir=tmp_path / ".gp",
        run_id="run_lock",
        candidates=candidates(1),
        client=client,  # type: ignore[arg-type]
    )
    pool_id = opened["pool_id"]
    with _TryControllerLock(_controller_lock_path(tmp_path / ".gp", pool_id)) as acquired:
        assert acquired is True
        result = wait_any(
            root_dir=tmp_path / ".gp",
            pool_id=pool_id,
            timeout_seconds=0,
            client=client,  # type: ignore[arg-type]
        )
    assert result["controller_busy"] is True


def test_close_interrupts_and_removes_only_pool_resources(tmp_path: Path) -> None:
    client = StatefulAgentPosix()
    opened = open_pool(
        root_dir=tmp_path / ".gp",
        run_id="run_close",
        candidates=candidates(2),
        client=client,  # type: ignore[arg-type]
    )
    client.children.append(
        {
            "thinkthreadId": "tt-unrelated",
            "agentState": "ready",
            "executionState": "absent",
            "capabilities": [],
            "fs": {"kind": "direct"},
        }
    )

    closed = close_pool(
        root_dir=tmp_path / ".gp",
        pool_id=opened["pool_id"],
        mode="interrupt",
        client=client,  # type: ignore[arg-type]
    )

    assert closed["state"] == "closed"
    assert {item["state"] for item in closed["cleanup"]} == {"removed"}
    assert [item["thinkthreadId"] for item in client.children] == ["tt-unrelated"]
    assert {item["state"] for item in client.branches.values()} == {"removed"}


def test_close_recovers_lost_cleanup_responses_and_is_idempotent(
    tmp_path: Path,
) -> None:
    client = StatefulAgentPosix()
    opened = open_pool(
        root_dir=tmp_path / ".gp",
        run_id="run_close_recovery",
        candidates=candidates(1),
        client=client,  # type: ignore[arg-type]
    )
    client.branch_remove_completion_unknown = True
    client.destroy_completion_unknown = True

    closed = close_pool(
        root_dir=tmp_path / ".gp",
        pool_id=opened["pool_id"],
        mode="interrupt",
        client=client,  # type: ignore[arg-type]
    )
    calls_before_replay = len(client.operations)
    replayed = close_pool(
        root_dir=tmp_path / ".gp",
        pool_id=opened["pool_id"],
        mode="interrupt",
        client=client,  # type: ignore[arg-type]
    )

    assert closed["state"] == replayed["state"] == "closed"
    assert closed["cleanup"][0]["state"] == "removed"
    assert closed["cleanup_observation"]["remaining_pool_children"] == []
    assert len(client.operations) == calls_before_replay
    assert len(
        [item for item in client.operations if item[0] == "fs.branch.remove"]
    ) == 1
    assert len(
        [item for item in client.operations if item[0] == "thinkthread.destroy"]
    ) == 1


def test_close_rejects_child_ownership_mismatch_before_destructive_calls(
    tmp_path: Path,
) -> None:
    client = StatefulAgentPosix()
    opened = open_pool(
        root_dir=tmp_path / ".gp",
        run_id="run_close_ownership",
        candidates=candidates(1),
        client=client,  # type: ignore[arg-type]
    )
    client.child("tt-child-1")["parentThinkthreadId"] = "tt-other-root"

    closed = close_pool(
        root_dir=tmp_path / ".gp",
        pool_id=opened["pool_id"],
        mode="interrupt",
        client=client,  # type: ignore[arg-type]
    )

    assert closed["state"] == "needs_recovery"
    assert closed["cleanup"][0]["state"] == "failed"
    assert "ownership mismatch" in closed["cleanup"][0]["message"]
    assert client.branches["fsbranch-child-1"]["state"] == "attached"
    assert client.child("tt-child-1")["thinkthreadId"] == "tt-child-1"
    assert not any(
        operation in {"fs.branch.remove", "thinkthread.destroy"}
        for operation, _params in client.operations
    )


def test_deadline_interrupts_the_exact_child(tmp_path: Path) -> None:
    client = StatefulAgentPosix()
    opened = open_pool(
        root_dir=tmp_path / ".gp",
        run_id="run_deadline",
        candidates=candidates(1),
        client=client,  # type: ignore[arg-type]
    )
    pool_id = opened["pool_id"]
    job = _load_job(tmp_path / ".gp", pool_id, opened["jobs"][0]["job_id"])
    job["deadline_unix"] = 0
    _write_job(tmp_path / ".gp", pool_id, job)

    result = wait_any(
        root_dir=tmp_path / ".gp",
        pool_id=pool_id,
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )

    assert result["event"]["kind"] == "timed_out"
    signals = [
        params for operation, params in client.operations if operation == "thinkthread.signal"
    ]
    assert signals == [{"id": "tt-child-1", "signal": "INT"}]
