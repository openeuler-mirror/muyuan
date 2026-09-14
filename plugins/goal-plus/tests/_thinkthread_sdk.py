"""Controlled ThinkThread SDK for Child and transport behavior tests."""

import base64
import json
import time
from pathlib import Path
from typing import Any

from goal_plus.domain.search import SearchSpec
from goal_plus.execution.thinkthread_child import TURN_QUIESCENCE_SECONDS
from tests._runtime_helpers import spec_for

from goal_plus.ports.thinkthread import AgentPosixBridgeError
from goal_plus.execution.thinkthread_rpc import PROTOCOL


def fs_path(path: str) -> dict[str, str]:
    return {
        "utf8": path,
        "bytesBase64": base64.b64encode(path.encode()).decode("ascii"),
    }


class IntegratedAgentPosix:
    def __init__(self, project: Path) -> None:
        self.project = project
        self.operations: list[tuple[str, dict[str, Any]]] = []
        self.request_status: dict[str, dict[str, Any]] = {}
        self.children: list[dict[str, Any]] = []
        self.branches: dict[str, dict[str, Any]] = {}
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.sent: list[dict[str, Any]] = []
        self.active_messages: dict[str, str] = {}
        self.snapshot_index = 0
        self.snapshots: set[str] = set()
        self.completion_unknown_once: set[str] = set()
        self.completion_unknown_before_once: set[str] = set()
        self.request_status_completion_unknown_once: set[str] = set()
        self.close_failure_once: set[str] = set()
        self.definitive_failure_once: set[str] = set()
        self.forced_verifier_scores: list[float] = []
        self.inner_git_settlement_completion_unknown_once = False

    def preflight(self) -> dict[str, Any]:
        return {"controlProtocolVersion": 2}

    def self_view(self) -> dict[str, Any]:
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
                        {"session": "fresh", "fs": "private_branch"}
                    ],
                    "allowedCapabilities": [
                        {"id": "thinkthread.message", "version": 1},
                        {"id": "thinkthread.fs.self", "version": 1},
                    ],
                    "allowedModels": [],
                }
            ],
        }

    def snapshot_diff_all(
        self,
        base_snapshot_id: str,
        target_snapshot_id: str,
    ) -> list[dict[str, Any]]:
        self.operations.append(
            (
                "fs.snapshot.diff.all",
                {
                    "baseSnapshotId": base_snapshot_id,
                    "targetSnapshotId": target_snapshot_id,
                },
            )
        )
        if base_snapshot_id == target_snapshot_id:
            return []
        return [{"path": fs_path("project/initial_program.py"), "kind": "modified"}]

    def snapshot_read_file(
        self,
        snapshot_id: str,
        path: str,
        *,
        max_bytes: int,
    ) -> bytes:
        content = self._file(snapshot_id, path)
        if len(content) > max_bytes:
            raise ValueError("file exceeds max_bytes")
        return content

    def snapshot_readdir_all(
        self,
        snapshot_id: str,
        path: str,
    ) -> list[dict[str, Any]]:
        self.operations.append(
            ("fs.snapshot.readdir.all", {"snapshotId": snapshot_id, "path": path})
        )
        assert path in {".", "project"}
        entries = []
        for name in ("evaluator.py", "initial_program.py"):
            full_path = f"project/{name}"
            content = self._file(snapshot_id, full_path)
            entries.append(
                {
                    "path": fs_path(full_path),
                    "kind": "file",
                    "mode": 0o644,
                    "len": len(content),
                }
            )
        return entries

    def child(self, child_id: str) -> dict[str, Any]:
        return next(item for item in self.children if item["thinkthreadId"] == child_id)

    def finish(self, child_id: str) -> None:
        child = self.child(child_id)
        child["executionState"] = "absent"
        child["agentState"] = "ready"
        child["lastWakeOutcome"] = {
            "messageId": self.active_messages.get(child_id, child["initialMessageId"]),
            "outcome": "completed",
            "finishedAtUnixMs": (time.time() - TURN_QUIESCENCE_SECONDS) * 1000,
        }
        child["completion"] = {"reason": "completed"}

    def enqueue(self, child_id: str, payload: dict[str, Any]) -> None:
        messages = self.messages.setdefault(child_id, [])
        messages.append(
            {
                "messageId": f"msg-worker-{child_id}-{len(messages) + 1}",
                "senderThinkthreadId": child_id,
                "recipientThinkthreadId": "tt-root",
                "text": json.dumps(payload, separators=(",", ":")),
                "truncated": False,
            }
        )

    def _file(self, snapshot_id: str, path: str) -> bytes:
        if path == "project/evaluator.py":
            return (self.project / "evaluator.py").read_bytes()
        if path == "project/initial_program.py":
            value = (
                0
                if snapshot_id == "fsnap-S0"
                else 3
                if snapshot_id == "fsnap-P"
                else int(snapshot_id.rsplit("-", 1)[1])
            )
            return f"VALUE = {value}\n".encode()
        raise AssertionError((snapshot_id, path))

    def _record_request(
        self,
        operation: str,
        params: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        request_id = str(params["requestId"])
        self.request_status[request_id] = {
            "requestId": request_id,
            "method": operation,
            "state": "succeeded",
            "result": result,
        }
        if operation in self.completion_unknown_once:
            self.completion_unknown_once.remove(operation)
            raise AgentPosixBridgeError(
                f"{operation} response lost",
                error={"delivery": "completion_unknown"},
            )
        return result

    def invoke(
        self,
        operation: str,
        params: dict[str, Any] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        normalized = dict(params or {})
        self.operations.append((operation, normalized))
        if operation in self.completion_unknown_before_once:
            self.completion_unknown_before_once.remove(operation)
            raise AgentPosixBridgeError(
                f"{operation} completion unknown",
                error={"delivery": "completion_unknown"},
            )
        if operation in self.definitive_failure_once:
            self.definitive_failure_once.remove(operation)
            request_id = str(normalized["requestId"])
            self.request_status[request_id] = {
                "requestId": request_id,
                "method": operation,
                "state": "failed",
                "error": {
                    "code": "FsRunRejected",
                    "message": "verifier execution rejected",
                    "retryable": False,
                },
            }
            raise AgentPosixBridgeError(
                f"{operation} definitively failed",
                error={
                    "rejection": {
                        "error": {
                            "code": "FsRunRejected",
                            "message": "verifier execution rejected",
                            "retryable": False,
                        }
                    }
                },
            )
        if operation == "fs.stat":
            return {
                "kind": "direct",
                "state": "attached",
                "storage": {
                    "activeRequestCount": 0,
                    "activeRequestLimit": 32,
                    "snapshotCount": self.snapshot_index,
                    "pendingSnapshotCreations": 0,
                    "snapshotLimit": 1024,
                },
            }
        if operation == "fs.snapshot.create":
            self.snapshot_index += 1
            self.snapshots.add("fsnap-S0")
            return self._record_request(
                operation,
                normalized,
                {"snapshotId": "fsnap-S0", "ownerThinkthreadId": "tt-root"},
            )
        if operation == "fs.request.status":
            status = dict(self.request_status[str(normalized["requestId"])])
            method = str(status.get("method") or "")
            if method in self.request_status_completion_unknown_once:
                self.request_status_completion_unknown_once.remove(method)
                raise AgentPosixBridgeError(
                    f"{method} status response lost",
                    error={"delivery": "completion_unknown"},
                )
            return status
        if operation == "fs.request.close":
            request_id = str(normalized["requestId"])
            method = str(self.request_status[request_id]["method"])
            if method in self.close_failure_once:
                self.close_failure_once.remove(method)
                raise AgentPosixBridgeError(
                    f"{method} request close failed",
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
            return {}
        if operation == "fs.run":
            if normalized.get("writes") == "snapshot":
                argv = normalized["invocation"]["argv"]
                iteration = int(argv[argv.index("--iteration") + 1])
                attempt_head = str(argv[argv.index("--attempt-git-head") + 1])
                restore_head = (
                    str(argv[argv.index("--restore-git-head") + 1])
                    if "--restore-git-head" in argv
                    else None
                )
                self.snapshot_index += 1
                target_snapshot = f"fsnap-{self.snapshot_index}"
                self.snapshots.add(target_snapshot)
                payload = {
                    "ok": True,
                    "protocol": "goal-plus.inner-git.v1",
                    "adapter": "autoresearch-git",
                    "attempt_git_head": attempt_head,
                    "ledger_git_head": f"{1000 + iteration:040x}",
                    "baseline_git_head": "b" * 40,
                    "result_count": iteration,
                    "restored_to_git_head": restore_head,
                }
                stdout = json.dumps(payload).encode() + b"\n"
                if self.inner_git_settlement_completion_unknown_once:
                    self.inner_git_settlement_completion_unknown_once = False
                    self.completion_unknown_once.add(operation)
                return self._record_request(
                    operation,
                    normalized,
                    {
                        "exit": {"kind": "code", "code": 0},
                        "outputChunks": [
                            {
                                "sequence": 0,
                                "stream": "stdout",
                                "dataBase64": base64.b64encode(stdout).decode("ascii"),
                            }
                        ],
                        "outputTruncated": False,
                        "retainedOutputBytes": len(stdout),
                        "observedOutputBytes": len(stdout),
                        "runKey": f"inner-git-{iteration}",
                        "targetSnapshotId": target_snapshot,
                        "metrics": {},
                    },
                )
            snapshot_id = str(normalized["snapshotId"])
            score = self.forced_verifier_scores.pop(0) if self.forced_verifier_scores else (
                0
                if snapshot_id == "fsnap-S0"
                else 3
                if snapshot_id == "fsnap-P"
                else int(snapshot_id.rsplit("-", 1)[1])
            )
            stdout = json.dumps({"combined_score": score}).encode() + b"\n"
            result = {
                "exit": {"kind": "code", "code": 0},
                "outputChunks": [
                    {
                        "sequence": 0,
                        "stream": "stdout",
                        "dataBase64": base64.b64encode(stdout).decode("ascii"),
                    }
                ],
                "outputTruncated": False,
                "retainedOutputBytes": len(stdout),
                "observedOutputBytes": len(stdout),
                "runKey": f"run-{snapshot_id}",
                "metrics": {},
            }
            return self._record_request(operation, normalized, result)
        if operation == "fs.snapshot.stat":
            path = str(normalized["path"])
            content = self._file(str(normalized["snapshotId"]), path)
            return {
                "path": fs_path(path),
                "kind": "file",
                "mode": 0o644,
                "len": len(content),
            }
        if operation == "fs.snapshot.pread":
            content = self._file(
                str(normalized["snapshotId"]), str(normalized["path"])
            )
            offset = int(normalized["offset"])
            chunk = content[offset : offset + int(normalized["length"])]
            return {
                "offset": offset,
                "dataBase64": base64.b64encode(chunk).decode("ascii"),
                "bytesRead": len(chunk),
                "eof": offset + len(chunk) >= len(content),
            }
        if operation == "self":
            return self.self_view()
        if operation == "thinkthread.list":
            return {"children": [dict(item) for item in self.children]}
        if operation == "thinkthread.spawn":
            index = len(self.children) + 1
            child_id = f"tt-child-{index}"
            branch_id = f"fsbranch-child-{index}"
            result = {
                "thinkthreadId": child_id,
                "capabilities": [{"id": "thinkthread.message", "version": 1}],
                "fs": {"kind": "private", "fsBranchId": branch_id},
                "initialMessageId": f"msg-child-{index}",
            }
            self.children.append(
                {
                    **result,
                    "parentThinkthreadId": "tt-root",
                    "agentState": "running",
                    "executionState": "running",
                    "lastWakeOutcome": None,
                    "completion": None,
                }
            )
            self.branches[branch_id] = {
                "branchId": branch_id,
                "thinkthreadId": child_id,
                "baseSnapshotId": normalized["fsSnapshotId"],
                "controlGeneration": 0,
                "state": "attached",
            }
            self.messages[child_id] = []
            if operation in self.completion_unknown_once:
                self.completion_unknown_once.remove(operation)
                registration_nonce = str(normalized["initialMessage"]).split(
                    "registration_nonce=", 1
                )[1].split(";", 1)[0]
                self.enqueue(
                    child_id,
                    {
                        "protocol": "goal-plus.thinkthread.v3",
                        "type": "registration",
                        "registration_nonce": registration_nonce,
                    },
                )
                raise AgentPosixBridgeError(
                    "spawn response lost",
                    error={"delivery": "completion_unknown"},
                )
            return result
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
        if operation == "message.receive":
            child_id = str(normalized["senderThinkthreadId"])
            after = int(str(normalized.get("after") or "0"))
            messages = self.messages[child_id][after:]
            return {
                "messages": [dict(item) for item in messages],
                "nextCursor": str(after + len(messages)),
                "hasMore": False,
            }
        if operation == "message.send":
            self.sent.append(normalized)
            if normalized.get("wake") is True:
                child = self.child(str(normalized["recipientThinkthreadId"]))
                child["executionState"] = "running"
                child["agentState"] = "running"
                child["lastWakeOutcome"] = None
                self.active_messages[str(child["thinkthreadId"])] = f"msg-root-{len(self.sent)}"
                if operation in self.completion_unknown_once:
                    self.completion_unknown_once.remove(operation)
                    dispatch_nonce = str(normalized["text"]).split(
                        "dispatch_nonce=", 1
                    )[1].split(";", 1)[0].split(".", 1)[0]
                    self.enqueue(
                        str(normalized["recipientThinkthreadId"]),
                        {
                            "protocol": "goal-plus.thinkthread.v3",
                            "type": "dispatch_ack",
                            "dispatch_nonce": dispatch_nonce,
                        },
                    )
                    raise AgentPosixBridgeError(
                        "wake response lost",
                        error={"delivery": "completion_unknown"},
                    )
            return {"messageId": f"msg-root-{len(self.sent)}"}
        if operation == "thinkthread.signal":
            child = self.child(str(normalized["id"]))
            child["executionState"] = "absent"
            child["agentState"] = "ready"
            return {}
        if operation == "thinkthread.wait":
            return {"child": dict(self.child(str(normalized["id"])))}
        if operation == "fs.branch.snapshot":
            self.snapshot_index += 1
            snapshot_id = f"fsnap-{self.snapshot_index}"
            result = {
                "snapshotId": snapshot_id,
                "createdFromBranchId": normalized["branchId"],
            }
            self.snapshots.add(snapshot_id)
            return self._record_request(operation, normalized, result)
        if operation == "workflow.fs.snapshot.patchBytes":
            self.snapshots.add("fsnap-P")
            return self._record_request(
                "fs.snapshot.patch",
                normalized,
                {
                    "snapshot": {
                        "snapshotId": "fsnap-P",
                        "ownerThinkthreadId": "tt-root",
                    },
                    "changedPaths": len(normalized["mutations"]),
                    "changedBytes": sum(
                        len(base64.b64decode(item.get("dataBase64", "")))
                        for item in normalized["mutations"]
                        if item["kind"] == "put_file"
                    ),
                },
            )
        if operation == "fs.apply":
            return self._record_request(
                operation,
                normalized,
                {
                    "status": "applied",
                    "baseSnapshotId": normalized["baseSnapshotId"],
                    "targetSnapshotId": normalized["targetSnapshotId"],
                    "changedPaths": 1,
                    "changedBytes": len(b"VALUE = 3\n"),
                },
            )
        if operation == "fs.branch.stat":
            return dict(self.branches[str(normalized["branchId"])])
        if operation == "fs.branch.reset":
            branch = self.branches[str(normalized["branchId"])]
            branch["baseSnapshotId"] = normalized["toSnapshotId"]
            branch["controlGeneration"] += 1
            if operation in self.completion_unknown_once:
                self.completion_unknown_once.remove(operation)
                raise AgentPosixBridgeError(
                    "branch reset response lost",
                    error={"delivery": "completion_unknown"},
                )
            return dict(branch)
        if operation == "fs.branch.remove":
            branch = self.branches[str(normalized["branchId"])]
            branch["state"] = "removed"
            branch["controlGeneration"] += 1
            if operation in self.completion_unknown_once:
                self.completion_unknown_once.remove(operation)
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
            if operation in self.completion_unknown_once:
                self.completion_unknown_once.remove(operation)
                raise AgentPosixBridgeError(
                    "destroy response lost",
                    error={"delivery": "completion_unknown"},
                )
            return {}
        if operation == "fs.snapshot.remove":
            snapshot_id = str(normalized["snapshotId"])
            self.snapshots.discard(snapshot_id)
            return self._record_request(operation, normalized, {})
        raise AssertionError(operation)


def thinkthread_spec(project: Path) -> SearchSpec:
    payload = spec_for(project, max_parallel=2).model_dump(mode="json")
    payload["strategy"].update(
        {
            "worker_budget": {"max_runtime_seconds": 60},
            "worker_launch": {},
        }
    )
    payload["workspace"] = {"provider": "thinkthread", "promotion_mode": "apply"}
    return SearchSpec.model_validate(payload)


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
