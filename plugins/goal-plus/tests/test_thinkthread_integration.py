from __future__ import annotations


import base64
import hashlib
import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from goal_plus.ports.annotation import EvidenceAnnotationResult
from goal_plus.annotation_host import AnnotationHostService
from goal_plus.internal_agents import kick_pi_internal_agents
from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.models import EvidenceViewRecord, SearchSpec, SupplementalEvaluation
from goal_plus.monitor import goal_plus_monitor_snapshot
from goal_plus.pi_tool import call_pi_tool
from goal_plus.runtime import FileSearchRuntime
from goal_plus.infrastructure.verifiers.thinkthread_attempt import ProviderThinkThreadAttempt
from goal_plus.thinkthread_artifacts import ThinkThreadSnapshotReader
from goal_plus.thinkthread_agent_posix import AgentPosixBridgeError
from goal_plus.thinkthread_annotation import (
    ANNOTATION_TOOLS,
    _process_message,
    annotation_snapshot,
    close_thinkthread_annotation,
    kick_thinkthread_annotation_service,
    serve_thinkthread_annotation,
    service_thinkthread_annotation,
)
from goal_plus.thinkthread_pool import (
    _load_job,
    _write_job,
    close_pool,
    continue_pool,
    open_thinkthread_search_pool,
    wait_any,
)
from tests._runtime_helpers import make_project, spec_for, start_host_verified_goal


def fs_path(path: str) -> dict[str, str]:
    return {
        "utf8": path,
        "bytesBase64": base64.b64encode(path.encode()).decode("ascii"),
    }


def test_thinkthread_annotation_rpc_allowlist_is_read_only() -> None:
    assert ANNOTATION_TOOLS == {
        "thinkthread_annotation_next",
        "thinkthread_artifact_view_list",
        "thinkthread_artifact_view_stat",
        "thinkthread_artifact_view_pread",
        "thinkthread_artifact_view_diff",
        "thinkthread_annotation_submit",
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
            "messageId": child["initialMessageId"],
            "outcome": "completed",
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
    payload["workspace"] = {"backend": "thinkthread", "promotion_mode": "apply"}
    return SearchSpec.model_validate(payload)


def test_thinkthread_autoresearch_launch_uses_pi_prompt_and_private_git_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    client = IntegratedAgentPosix(project)
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    payload = thinkthread_spec(project).model_dump(mode="json")
    payload["budget"]["max_parallel"] = 1
    payload["strategy"]["inner_agent"] = "autoresearch"

    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(payload),
        [project / "evaluator.py"], native_host="pi",
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)

    binding = plan.worker_policy["inner_agent_binding"]
    assert frozen.spec.strategy.inner_agent == "autoresearch"
    assert binding["inner_agent_id"] == "autoresearch"
    assert binding["adapter_version"] == "builtin-autoresearch-git-v1"
    assert binding["worker_host"] == "thinkthread"
    assert binding["prompt_asset"] == "assets/pi/prompts/search-candidate-worker.md"
    assert binding["workspace_ledger_projection"] == "results_tsv"
    assert "# Search Candidate Worker" in session.launch["message"]
    assert "ThinkThread Child" in session.launch["message"]
    assert "继续当前自主循环" in session.launch["message"]
    assert "不要只读取 context/Evidence 就结束" in session.launch["message"]
    instructions = "\n".join(task.instructions)
    assert "继续同一条自主搜索循环" in instructions
    assert "不要仅因一次失败或同分而停止" in instructions
    assert "当前内层实现是 AutoResearch-Git" in instructions
    assert "本次 host dispatch 只完成一个有界 attempt" not in instructions

    context = runtime.get_agent_context(session.agent_session_id)
    assert context["inner_agent"] == "autoresearch"
    assert context["result_ledger_source"] == "gp"
    assert context["workspace_ledger_projection"] == "results_tsv"
    assert context["results_tsv"] == "results.tsv"
    assert not task.workspace.exists()


def test_thinkthread_autoresearch_settles_private_git_around_outer_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    client = IntegratedAgentPosix(project)
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    payload = thinkthread_spec(project).model_dump(mode="json")
    payload["budget"]["max_parallel"] = 1
    payload["strategy"]["inner_agent"] = "autoresearch"
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(payload),
        [project / "evaluator.py"], native_host="pi",
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    [task] = runtime.start_batch(
        run_id,
        runtime.plan_next(run_id, requested_k=1).plan_id,
    )
    opened = open_thinkthread_search_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    job = opened["jobs"][0]
    durable_job = _load_job(runtime.root_dir, opened["pool_id"], str(job["job_id"]))
    spawn = next(
        params
        for operation, params in client.operations
        if operation == "thinkthread.spawn"
    )
    assert "goal_plus_inner_git_binding=" in spawn["initialMessage"]
    assert durable_job["inner_git_binding"]["adapter"] == "autoresearch-git"
    assert durable_job["inner_git_binding"]["source_subdir"] == "project"

    client.forced_verifier_scores = [5.0, 1.0]
    first_receipt = submit_candidate_attempt(runtime, opened, job, client)
    client.finish(str(job["thinkthread_id"]))
    first = wait_any(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )["event"]
    assert first["kind"] == "candidate_ready"
    assert first["attempt_result"]["attempt_id"] == first_receipt["attempt_id"]
    assert first["attempt_result"]["attempt_artifact_ref"] == {
        "kind": "fs_snapshot",
        "id": "fsnap-2",
        "provider": "thinkthread",
    }
    assert first["attempt_result"]["workspace_artifact_after_attempt"]["id"] == (
        "fsnap-3"
    )
    first_record = runtime.candidate_creation.state.read_candidate(run_id, task.candidate_id)
    assert first_record.iterations[0].git_head is None
    assert first_record.iterations[0].ledger_git_head is None
    assert first_record.iterations[0].artifact_ref.id == "fsnap-2"
    assert first_record.iterations[0].ledger_artifact_ref.id == "fsnap-3"
    assert first_record.results_ledger[0].git_head is None
    assert first_record.results_ledger_artifact_ref.id == "fsnap-3"
    assert first_record.inner_agent_state["result_count"] == 1

    monitor = goal_plus_monitor_snapshot(runtime.root_dir, run_id=run_id)
    projected = monitor["candidates"][task.candidate_id]["results_tsv"]
    assert projected["path"] == "results.tsv"
    assert projected["scope"] == "thinkthread_child_private"
    assert projected["exists"] is True
    assert projected["last_updated_at"] is None
    assert projected["size_bytes"] is None
    assert projected["row_count"] == 1

    continued = continue_pool(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        candidate_id=task.candidate_id,
        message="Try a lower-scoring follow-up",
        max_runtime_seconds=60,
        client=client,  # type: ignore[arg-type]
    )
    continued_job = continued["jobs"][0]
    wake = next(
        params
        for operation, params in reversed(client.operations)
        if operation == "message.send" and params.get("wake") is True
    )
    assert "goal_plus_inner_git_binding=" in wake["text"]
    assert "authoritative" in wake["text"]
    second_receipt = submit_candidate_attempt(
        runtime,
        opened,
        continued_job,
        client,
        hypothesis="try a worse value",
    )
    client.finish(str(job["thinkthread_id"]))
    second = wait_any(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )["event"]
    assert second["attempt_result"]["attempt_id"] == second_receipt["attempt_id"]
    assert second["attempt_result"]["score_report"]["disposition"] == "discard"
    assert second["attempt_result"]["attempt_artifact_ref"]["id"] == "fsnap-4"
    assert second["attempt_result"]["workspace_artifact_after_attempt"]["id"] == (
        "fsnap-5"
    )

    record = runtime.candidate_creation.state.read_candidate(run_id, task.candidate_id)
    assert [item.artifact_ref.id for item in record.iterations] == [
        "fsnap-2",
        "fsnap-4",
    ]
    assert [item.ledger_artifact_ref.id for item in record.iterations] == [
        "fsnap-3",
        "fsnap-5",
    ]
    assert [item.disposition for item in record.iterations] == ["keep", "discard"]
    assert all(item.git_head is None for item in record.iterations)
    assert all(item.git_head is None for item in record.results_ledger)
    assert record.results_ledger_artifact_ref.id == "fsnap-5"
    assert record.inner_agent_state["result_count"] == 2
    assert record.inner_agent_state["attempt_git_heads"] == {
        "1": f"{1:040x}",
        "2": f"{2:040x}",
    }
    assert set(runtime.artifact_lifecycle.owned_snapshots(run_id)) == {
        "fsnap-S0",
        "fsnap-2",
        "fsnap-3",
        "fsnap-4",
        "fsnap-5",
    }
    settlement_runs = [
        params
        for operation, params in client.operations
        if operation == "fs.run" and params.get("writes") == "snapshot"
    ]
    assert len(settlement_runs) == 2
    assert "--restore-git-head" not in settlement_runs[0]["invocation"]["argv"]
    second_argv = settlement_runs[1]["invocation"]["argv"]
    restore_index = second_argv.index("--restore-git-head")
    assert second_argv[restore_index + 1] == f"{1:040x}"
    assert client.branches[str(job["fs_branch_id"])]["baseSnapshotId"] == "fsnap-5"


def test_thinkthread_autoresearch_recovers_inner_git_settlement_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    client = IntegratedAgentPosix(project)
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    payload = thinkthread_spec(project).model_dump(mode="json")
    payload["budget"]["max_parallel"] = 1
    payload["strategy"]["inner_agent"] = "autoresearch"
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(payload),
        [project / "evaluator.py"], native_host="pi",
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    runtime.start_batch(run_id, runtime.plan_next(run_id, requested_k=1).plan_id)
    opened = open_thinkthread_search_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    job = opened["jobs"][0]
    submit_candidate_attempt(runtime, opened, job, client)
    client.inner_git_settlement_completion_unknown_once = True
    client.request_status_completion_unknown_once.add("fs.run")
    client.finish(str(job["thinkthread_id"]))

    unresolved = wait_any(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )

    assert unresolved["event"] is None
    pending = unresolved["pool"]["jobs"][0]
    assert pending["status"] == "needs_recovery"
    assert pending["recovery"]["stage"] == "attempt_verification"

    recovered = wait_any(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )

    assert recovered["event"]["kind"] == "candidate_ready"
    record = runtime.candidate_creation.state.read_candidate(run_id, str(job["candidate_id"]))
    assert len(record.iterations) == 1
    assert len(record.results_ledger) == 1
    assert record.inner_agent_state["result_count"] == 1
    settlement_calls = [
        params
        for operation, params in client.operations
        if operation == "fs.run" and params.get("writes") == "snapshot"
    ]
    assert len(settlement_calls) == 1
    settlement = next(
        item
        for item in record.provider_mutations
        if item.operation == "inner_git_settlement"
    )
    assert settlement.state == "closed"


@pytest.mark.parametrize("mismatch", ["child", "snapshot", "inner_git"])
def test_attempt_rejects_mismatched_typed_provider_facts_before_settlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatch: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    client = IntegratedAgentPosix(project)
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    payload = thinkthread_spec(project).model_dump(mode="json")
    payload["budget"]["max_parallel"] = 1
    payload["strategy"]["inner_agent"] = "autoresearch"
    frozen = runtime.freeze_spec(SearchSpec.model_validate(payload), [project / "evaluator.py"], native_host="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    [task] = runtime.start_batch(run_id, runtime.plan_next(run_id, requested_k=1).plan_id)
    opened = open_thinkthread_search_pool(root_dir=runtime.root_dir, run_id=run_id, client=client)
    job = _load_job(runtime.root_dir, opened["pool_id"], opened["jobs"][0]["job_id"])
    client.finish(job["thinkthread_id"])
    method = {"child": "read_child", "snapshot": "capture", "inner_git": "settle_inner_git"}[mismatch]
    original = getattr(ProviderThinkThreadAttempt, method)

    def wrong_facts(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if mismatch == "child":
            return replace(result, parent_thinkthread_id="tt-other-root")
        if mismatch == "snapshot":
            return replace(result, branch_id="fsbranch-other")
        return replace(result, settlement=replace(result.settlement, baseline_git_head="c" * 40))

    monkeypatch.setattr(ProviderThinkThreadAttempt, method, wrong_facts)
    params = dict(
        run_id=run_id, candidate_id=task.candidate_id,
        agent_session_id=job["agent_session_id"], thinkthread_id=job["thinkthread_id"],
        branch_id=job["fs_branch_id"], hypothesis="Verify exact provider facts",
        attempt_id="attempt-typed-facts", io=runtime.thinkthread_attempt_io(client),
        inner_git_attempt={
            "protocol": "goal-plus.inner-git.v1", "adapter": "autoresearch-git",
            "attempt_git_head": f"{1:040x}", "baseline_git_head": "b" * 40,
            "result_count_before": 0,
        },
    )
    before = runtime.candidate_creation.state.read_candidate(run_id, task.candidate_id)
    with pytest.raises((PermissionError, RuntimeError), match="mismatch"):
        runtime.thinkthread_verification.verify(**params)
    rejected = runtime.candidate_creation.state.read_candidate(run_id, task.candidate_id)
    assert rejected.iterations == before.iterations
    assert rejected.results_ledger == before.results_ledger
    assert rejected.score_report == before.score_report
    assert rejected.inner_agent_state == before.inner_agent_state

    monkeypatch.setattr(ProviderThinkThreadAttempt, method, original)
    report = runtime.thinkthread_verification.verify(**params)
    assert report.process_passed is True
    settled = runtime.candidate_creation.state.read_candidate(run_id, task.candidate_id)
    assert len(settled.iterations) == len(settled.results_ledger) == 1
    assert sum(op == "fs.branch.snapshot" for op, _ in client.operations) == 1
    assert sum(op == "fs.run" and p.get("writes") == "snapshot" for op, p in client.operations) == 1


def annotation_request(
    state: dict[str, Any],
    request_id: str,
    tool: str,
    params: dict[str, Any],
    *,
    deadline_unix_ms: int | None = None,
) -> dict[str, Any]:
    content = {
        "agent_session_id": state["agent_session_id"],
        "params": params,
        "tool": tool,
    }
    if deadline_unix_ms is not None:
        content["deadline_unix_ms"] = deadline_unix_ms
    content_json = json.dumps(content, separators=(",", ":"))
    request = {
        "protocol": "goal-plus.thinkthread.v3",
        "type": "request",
        "request_id": request_id,
        "agent_session_id": state["agent_session_id"],
        "tool": tool,
        "params": params,
        "content_json": content_json,
        "content_sha256": hashlib.sha256(content_json.encode()).hexdigest(),
    }
    if deadline_unix_ms is not None:
        request["deadline_unix_ms"] = deadline_unix_ms
    return request


def response_for(client: IntegratedAgentPosix, request_id: str) -> dict[str, Any]:
    chunks: list[dict[str, Any]] = []
    for sent in client.sent:
        text = sent.get("text")
        if not isinstance(text, str):
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if payload.get("type") == "response_chunk" and payload.get("request_id") == request_id:
            chunks.append(payload)
    assert chunks
    unique = {int(item["chunk_index"]): item for item in chunks}
    data = b"".join(
        base64.b64decode(unique[index]["data_base64"])
        for index in range(int(chunks[0]["chunk_count"]))
    )
    assert hashlib.sha256(data).hexdigest() == chunks[0]["response_sha256"]
    return json.loads(data)


def test_thinkthread_annotation_rpc_rejects_execution_before_provider_call(
    tmp_path: Path,
) -> None:
    client = IntegratedAgentPosix(tmp_path)
    state = {
        "agent_session_id": "as-annotation",
        "registered_at": "2026-09-05T00:00:00Z",
    }
    request = annotation_request(
        state,
        "req-forbidden-run",
        "thinkthread_artifact_view_run",
        {"view_id": "view-target", "argv": ["python", "evaluator.py"]},
    )

    with pytest.raises(PermissionError, match="RPC tool is not allowed"):
        _process_message(
            client,  # type: ignore[arg-type]
            tmp_path,
            state,
            {"text": json.dumps(request)},
        )

    assert client.operations == []


def submit_candidate_attempt(
    runtime: FileSearchRuntime,
    opened: dict[str, Any],
    job: dict[str, Any],
    client: IntegratedAgentPosix,
    *,
    hypothesis: str | None = None,
) -> dict[str, Any]:
    current = _load_job(runtime.root_dir, opened["pool_id"], str(job["job_id"]))
    child_id = str(current["thinkthread_id"])
    if not isinstance(current.get("registered_at"), str):
        client.enqueue(
            child_id,
            {
                "protocol": "goal-plus.thinkthread.v3",
                "type": "registration",
                "registration_nonce": current["registration_nonce"],
            },
        )
    request_id = f"req-attempt-{current['job_id']}-{current['dispatch_index']}"
    params: dict[str, Any] = {
        "run_id": current["run_id"],
        "candidate_id": current["candidate_id"],
        "scope": "process",
        "agent_session_id": current["agent_session_id"],
        "hypothesis": hypothesis
        or f"Verify candidate dispatch {current['dispatch_index']}",
    }
    if isinstance(current.get("inner_git_binding"), dict):
        record = runtime.candidate_creation.state.read_candidate(
            str(current["run_id"]),
            str(current["candidate_id"]),
        )
        params["_inner_git_attempt"] = {
            "protocol": "goal-plus.inner-git.v1",
            "adapter": "autoresearch-git",
            "attempt_git_head": f"{int(current['dispatch_index']):040x}",
            "baseline_git_head": "b" * 40,
            "result_count_before": len(record.results_ledger),
        }
    client.enqueue(
        child_id,
        annotation_request(
            current,
            request_id,
            "search_run_verifier",
            params,
        ),
    )
    waiting = wait_any(
        root_dir=runtime.root_dir,
        pool_id=str(opened["pool_id"]),
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )
    assert waiting["event"] is None
    receipt = response_for(client, request_id)["result"]
    assert receipt["state"] == "accepted"
    assert receipt["request_id"] == request_id
    assert receipt["attempt_id"].startswith("attempt_")
    return receipt


def test_completed_accepted_attempt_settles_before_expired_deadline_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    client = IntegratedAgentPosix(project)
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"], native_host="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    runtime.start_batch(run_id, runtime.plan_next(run_id, requested_k=1).plan_id)
    opened = open_thinkthread_search_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    job = opened["jobs"][0]
    receipt = submit_candidate_attempt(runtime, opened, job, client)
    client.finish(str(job["thinkthread_id"]))
    expired = _load_job(
        runtime.root_dir,
        str(opened["pool_id"]),
        str(job["job_id"]),
    )
    expired["deadline_unix"] = 0
    _write_job(runtime.root_dir, str(opened["pool_id"]), expired)

    settled = wait_any(
        root_dir=runtime.root_dir,
        pool_id=str(opened["pool_id"]),
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )

    assert settled["event"]["kind"] == "candidate_ready"
    assert settled["event"]["attempt_result"]["attempt_id"] == receipt["attempt_id"]
    assert settled["event"]["attempt_result"]["attempt_iteration"] == 1
    assert not any(
        operation == "thinkthread.signal" for operation, _params in client.operations
    )
    record = runtime.candidate_creation.state.read_candidate(run_id, str(job["candidate_id"]))
    assert len(record.iterations) == 1
    assert len(record.results_ledger) == 1


def test_pi_facade_and_worker_message_proxy_preserve_search_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    client = IntegratedAgentPosix(project)
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    monkeypatch.setattr(
        "goal_plus.thinkthread_pool.AgentPosixSdkClient",
        lambda: client,
    )
    monkeypatch.setenv("GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED", "1")
    root_dir = tmp_path / ".gp"
    spec_payload = thinkthread_spec(project).model_dump(mode="json")
    spec_payload["budget"]["max_parallel"] = 1
    spec_payload["strategy"]["config"]["global_evidence_mode"] = "auto"
    start_host_verified_goal(root_dir, project, native_host="pi")
    goal = FileGoalPlusRuntime(root_dir).require_host_verified_active_goal()

    frozen = call_pi_tool(
        root_dir,
        "search_freeze_spec",
        {
            "goal_plus_id": goal.goal_plus_id,
            "spec": spec_payload,
            "verifier_artifact_paths": ["evaluator.py"],
        },
    )
    assert frozen["spec"]["strategy"]["config"]["global_evidence_mode"] == "auto"
    run_id = call_pi_tool(
        root_dir,
        "search_create",
        {
            "goal_plus_id": goal.goal_plus_id,
            "frozen_spec_id": frozen["frozen_spec_id"],
        },
    )["run_id"]
    call_pi_tool(
        root_dir,
        "goal_plus_link_search_run",
        {
            "goal_plus_id": goal.goal_plus_id,
            "frozen_spec_id": frozen["frozen_spec_id"],
            "run_id": run_id,
        },
    )
    plan = call_pi_tool(
        root_dir,
        "search_plan_next",
        {"run_id": run_id, "requested_k": 1},
    )
    tasks = call_pi_tool(
        root_dir,
        "search_start_batch",
        {"run_id": run_id, "plan_id": plan["plan_id"]},
    )
    candidate_id = tasks[0]["candidate_id"]
    opened = call_pi_tool(
        root_dir,
        "thinkthread_search_pool_open",
        {"run_id": run_id, "candidate_ids": [candidate_id], "max_parallel": 1},
    )
    job = _load_job(root_dir, opened["pool_id"], opened["jobs"][0]["job_id"])
    child_id = job["thinkthread_id"]
    agent_session_id = job["agent_session_id"]
    client.enqueue(
        child_id,
        {
            "protocol": "goal-plus.thinkthread.v3",
            "type": "registration",
            "registration_nonce": job["registration_nonce"],
        },
    )

    initial_requests = {
        "req-context-initial": (
            "search_get_agent_context",
            {"agent_session_id": agent_session_id},
        ),
        "req-evidence-initial": (
            "search_get_global_evidence",
            {"agent_session_id": agent_session_id},
        ),
        "req-iterations-initial": (
            "search_list_iterations",
            {"agent_session_id": agent_session_id},
        ),
        "req-verifier": (
            "search_run_verifier",
            {
                "run_id": run_id,
                "candidate_id": candidate_id,
                "scope": "process",
                "agent_session_id": agent_session_id,
                "hypothesis": "Exercise the deferred verifier contract",
                "toolization_decision": {
                    "outcome": "not_applicable",
                    "signals": [],
                    "exclusion": "no_toolizable_material",
                    "rationale": "No reusable material was created.",
                    "tool_names": [],
                },
            },
        ),
    }
    for request_id, (tool, params) in initial_requests.items():
        client.enqueue(
            child_id,
            annotation_request(job, request_id, tool, params),
        )
    waiting = call_pi_tool(
        root_dir,
        "thinkthread_search_pool_wait_any",
        {"pool_id": opened["pool_id"], "timeout_seconds": 0},
    )
    assert waiting["event"] is None
    assert response_for(client, "req-context-initial")["result"] == call_pi_tool(
        root_dir,
        "search_get_agent_context",
        {"agent_session_id": agent_session_id},
    )
    assert response_for(client, "req-evidence-initial")["result"] == []
    assert response_for(client, "req-iterations-initial")["result"] == []
    verifier_receipt = response_for(client, "req-verifier")["result"]
    assert verifier_receipt["state"] == "accepted"
    assert verifier_receipt["request_id"] == "req-verifier"
    assert verifier_receipt["attempt_id"].startswith("attempt_")
    assert verifier_receipt["run_id"] == run_id
    assert verifier_receipt["candidate_id"] == candidate_id
    assert verifier_receipt["agent_session_id"] == agent_session_id
    assert verifier_receipt["toolization_decision"] is None
    assert "next wake" in verifier_receipt["message"]
    assert "global_evidence_snapshot" not in verifier_receipt

    client.finish(child_id)
    settled = call_pi_tool(
        root_dir,
        "thinkthread_search_pool_wait_any",
        {"pool_id": opened["pool_id"], "timeout_seconds": 0},
    )
    assert settled["event"]["kind"] == "candidate_ready"
    attempt_result = settled["event"]["attempt_result"]
    assert attempt_result == settled["pool"]["jobs"][0]["attempt_result"]
    assert attempt_result["attempt_id"] == verifier_receipt["attempt_id"]
    assert attempt_result["attempt_iteration"] == 1
    assert attempt_result["score_report"]["attempt_iteration"] == 1
    assert (
        attempt_result["attempt_artifact_ref"]
        == attempt_result["score_report"]["attempt_artifact_ref"]
    )
    runtime = FileSearchRuntime(root_dir)
    annotation_task = runtime.annotation.task(run_id, candidate_id, 1)
    assert annotation_task is not None
    runtime.annotation.tasks.write_task(
        annotation_task.model_copy(
            update={
                "state": "completed",
                "view": EvidenceViewRecord(
                    run_id=run_id,
                    candidate_id=candidate_id,
                    iteration=1,
                    attempt_commit=annotation_task.attempt_commit,
                    attempt_artifact_ref=annotation_task.attempt_artifact_ref,
                    description="The snapshot improves the measured value.",
                    supplemental_evaluation=SupplementalEvaluation.model_validate(
                        {
                            "summary": "The exact snapshot improves the metric.",
                            "dimensions": [
                                {
                                    "name": "Snapshot behavior",
                                    "finding": "The verifier reads the changed value.",
                                    "confidence": "high",
                                    "evidence": ["exact snapshot verifier"],
                                }
                            ],
                        }
                    ),
                    comparison_basis=list(annotation_task.comparison_basis),
                    created_at="2026-09-03T00:00:00Z",
                ),
            }
        )
    )
    call_pi_tool(
        root_dir,
        "thinkthread_search_pool_continue",
        {"pool_id": opened["pool_id"], "candidate_id": candidate_id},
    )
    wake = next(
        item
        for item in reversed(client.sent)
        if item.get("wake") is True and item.get("recipientThinkthreadId") == child_id
    )
    assert "Goal Plus candidate attempt feedback (authoritative JSON):" in wake["text"]
    assert wake["text"].count(
        "Goal Plus candidate attempt feedback (authoritative JSON):"
    ) == 1
    assert f'"attempt_id":"{verifier_receipt["attempt_id"]}"' in wake["text"]
    assert '"attempt_iteration":1' in wake["text"]
    assert (
        json.dumps(
            attempt_result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        in wake["text"]
    )
    assert "# Search Candidate Common Agent" not in wake["text"]
    assert wake["text"].index(
        "Goal Plus candidate attempt feedback (authoritative JSON):"
    ) < wake["text"].index("ThinkThread Goal Plus dispatch:")
    assert wake["text"].endswith(
        "不得只读取 context/Evidence、只总结上一轮或不提交 verifier 就结束本次 wake。"
    )

    continued_requests = {
        "req-context-continued": (
            "search_get_agent_context",
            {"agent_session_id": agent_session_id},
        ),
        "req-evidence-continued": (
            "search_get_global_evidence",
            {"agent_session_id": agent_session_id},
        ),
        "req-detail-continued": (
            "search_get_evidence_detail",
            {
                "agent_session_id": agent_session_id,
                "candidate_id": candidate_id,
                "iteration": 1,
            },
        ),
        "req-iterations-continued": (
            "search_list_iterations",
            {"agent_session_id": agent_session_id},
        ),
    }
    for request_id, (tool, params) in continued_requests.items():
        client.enqueue(
            child_id,
            annotation_request(job, request_id, tool, params),
        )
    call_pi_tool(
        root_dir,
        "thinkthread_search_pool_wait_any",
        {"pool_id": opened["pool_id"], "timeout_seconds": 0},
    )

    continued_context = response_for(client, "req-context-continued")["result"]
    assert continued_context == call_pi_tool(
        root_dir,
        "search_get_agent_context",
        {"agent_session_id": agent_session_id},
    )
    assert continued_context["recent_iterations"][-1]["iteration"] == 1
    assert (
        continued_context["recent_iterations"][-1]["attempt_id"]
        == attempt_result["attempt_id"]
    )
    assert (
        continued_context["recent_iterations"][-1]["score"]
        == attempt_result["score_report"]["aggregate_score"]
    )
    assert (
        continued_context["recent_iterations"][-1]["disposition"]
        == attempt_result["score_report"]["disposition"]
    )
    assert continued_context["global_evidence_injected"] is True
    assert continued_context["global_evidence_entry_count"] == 1
    proxy_evidence = response_for(client, "req-evidence-continued")["result"]
    assert proxy_evidence == call_pi_tool(
        root_dir,
        "search_get_global_evidence",
        {"agent_session_id": agent_session_id},
    )
    assert [(item["candidate_id"], item["iteration"]) for item in proxy_evidence] == [
        (candidate_id, 1)
    ]
    assert continued_context["global_evidence_snapshot"] == proxy_evidence
    detail_response = response_for(client, "req-detail-continued")
    assert detail_response["ok"] is True, detail_response
    assert detail_response["result"] == call_pi_tool(
        root_dir,
        "search_get_evidence_detail",
        {
            "agent_session_id": agent_session_id,
            "candidate_id": candidate_id,
            "iteration": 1,
        },
    )
    assert response_for(client, "req-iterations-continued")["result"] == call_pi_tool(
        root_dir,
        "search_list_iterations",
        {"agent_session_id": agent_session_id},
    )


def wait_for_response(
    client: IntegratedAgentPosix,
    request_id: str,
    *,
    timeout_seconds: float = 2.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            return response_for(client, request_id)
        except AssertionError:
            pass
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for annotation response {request_id}")


def test_completed_runtime_turn_without_attempt_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    client = IntegratedAgentPosix(project)
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"], native_host="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    runtime.start_batch(run_id, runtime.plan_next(run_id, requested_k=2).plan_id)
    opened = open_thinkthread_search_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    job = opened["jobs"][0]

    client.finish(str(job["thinkthread_id"]))
    waiting = wait_any(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )
    assert waiting["event"] is None
    pending = _load_job(runtime.root_dir, opened["pool_id"], str(job["job_id"]))
    assert pending["status"] == "running"
    assert pending["awaiting_candidate_attempt"]["dispatch_nonce"] == (
        pending["dispatch_nonce"]
    )
    pending["deadline_unix"] = time.time() - 1
    _write_job(runtime.root_dir, opened["pool_id"], pending)
    result = wait_any(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )

    assert result["event"]["kind"] == "failed"
    failed = next(
        item
        for item in result["pool"]["jobs"]
        if item["candidate_id"] == job["candidate_id"]
    )
    assert failed["error"]["error_code"] == "MissingCandidateAttempt"
    assert runtime.candidate_creation.state.read_candidate(
        run_id, str(job["candidate_id"])
    ).iterations == []


def test_completed_wake_waits_for_quiescence_after_provider_retry_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    client = IntegratedAgentPosix(project)
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"], native_host="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    runtime.start_batch(run_id, runtime.plan_next(run_id, requested_k=2).plan_id)
    opened = open_thinkthread_search_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    job = opened["jobs"][0]
    submit_candidate_attempt(runtime, opened, job, client)
    client.finish(str(job["thinkthread_id"]))
    child = client.child(str(job["thinkthread_id"]))
    child["lastWakeOutcome"]["finishedAtMs"] = time.time() * 1000

    still_quieting = wait_any(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )
    assert still_quieting["event"] is None
    assert still_quieting["pool"]["jobs"][0]["status"] == "running"

    child["lastWakeOutcome"]["finishedAtMs"] -= 21_000
    settled = wait_any(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )
    assert settled["event"]["kind"] == "candidate_ready"


def test_child_dispatch_error_is_preserved_in_failed_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    client = IntegratedAgentPosix(project)
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"], native_host="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    runtime.start_batch(run_id, runtime.plan_next(run_id, requested_k=2).plan_id)
    opened = open_thinkthread_search_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    job = opened["jobs"][0]
    durable = _load_job(runtime.root_dir, opened["pool_id"], str(job["job_id"]))
    client.enqueue(
        str(job["thinkthread_id"]),
        {
            "protocol": "goal-plus.thinkthread.v3",
            "type": "dispatch_error",
            "dispatch_nonce": durable["dispatch_nonce"],
            "error_type": "Error",
            "message": "private Git initialization failed",
        },
    )
    client.finish(str(job["thinkthread_id"]))

    result = wait_any(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )

    assert result["event"]["kind"] == "failed"
    assert result["event"]["error"] == {
        "stage": "worker_start",
        "message": "private Git initialization failed",
        "error_code": "ChildDispatchError",
        "error_type": "Error",
        "completion_unknown": False,
    }
    failed = next(
        item
        for item in result["pool"]["jobs"]
        if item["candidate_id"] == job["candidate_id"]
    )
    assert failed["error"] == result["event"]["error"]


def test_annotation_service_retries_when_root_denies_new_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_dir = tmp_path / ".gp"
    run_id = "run_sandboxed"
    service_root = (
        root_dir / "host-pools" / "thinkthread-annotations" / run_id
    )
    service_root.mkdir(parents=True)
    (service_root / "state.json").write_text(
        json.dumps({"run_id": run_id, "status": "running"}),
        encoding="utf-8",
    )
    launches: list[dict[str, Any]] = []

    class Process:
        pid = 4321

    def launch(_command: list[str], **options: Any) -> Process:
        launches.append(options)
        if len(launches) == 1:
            raise PermissionError(1, "Operation not permitted")
        return Process()

    monkeypatch.setattr(
        "goal_plus.thinkthread_annotation.subprocess.Popen",
        launch,
    )

    assert kick_thinkthread_annotation_service(
        root_dir=root_dir,
        run_id=run_id,
    )
    assert [launch["start_new_session"] for launch in launches] == [True, False]
    service = json.loads((service_root / "service.json").read_text(encoding="utf-8"))
    assert service["pid"] == 4321
    assert service["start_new_session"] is False
    assert "retrying without a new session" in (
        service_root / "service.log"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize("previous_status", ["exited", "stopped", "running", None])
def test_annotation_service_restarts_from_terminal_record_in_protected_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    previous_status: str | None,
) -> None:
    root_dir = tmp_path / ".gp"
    run_id = "run_retained_annotation"
    service_root = root_dir / "host-pools" / "thinkthread-annotations" / run_id
    service_root.mkdir(parents=True)
    child_state = {"run_id": run_id, "status": "running", "active_task": {"attempts": 1}}
    (service_root / "state.json").write_text(json.dumps(child_state), encoding="utf-8")
    previous = {"run_id": run_id, "generation": "old-generation", "pid": 4321}
    if previous_status is not None:
        previous["status"] = previous_status
    (service_root / "service.json").write_text(json.dumps(previous), encoding="utf-8")
    launches = []

    class Process:
        pid = 5432

    def launch(command: list[str], **_options: Any) -> Process:
        launches.append(command)
        return Process()

    def denied_signal(*_args: object) -> None:
        raise PermissionError(1, "Operation not permitted")

    # A protected Root may refuse signal probes even after the owned loop exits.
    monkeypatch.setattr("goal_plus.thinkthread_annotation.os.kill", denied_signal)
    monkeypatch.setattr("goal_plus.thinkthread_annotation.subprocess.Popen", launch)

    restarted = kick_thinkthread_annotation_service(root_dir=root_dir, run_id=run_id)
    terminal = previous_status in {"exited", "stopped"}
    assert restarted is terminal
    current = json.loads((service_root / "service.json").read_text(encoding="utf-8"))
    if terminal:
        assert len(launches) == 1
        assert current["generation"] != previous["generation"]
        assert current["pid"] == 5432
        assert current["status"] == "running"
        assert not kick_thinkthread_annotation_service(root_dir=root_dir, run_id=run_id)
        assert len(launches) == 1
    else:
        assert not launches
        assert current == previous
    assert json.loads((service_root / "state.json").read_text()) == child_state


def test_closed_annotation_service_does_not_recheck_terminal_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_dir = tmp_path / ".gp"
    run_id = "run_already_stopped"
    service_root = (
        root_dir / "host-pools" / "thinkthread-annotations" / run_id
    )
    service_root.mkdir(parents=True)
    (service_root / "state.json").write_text(
        json.dumps({"run_id": run_id, "status": "closed"}),
        encoding="utf-8",
    )
    (service_root / "service.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "generation": "generation-1",
                "pid": 4321,
                "status": "stopped",
                "finished_at": "2026-09-04T12:48:23Z",
            }
        ),
        encoding="utf-8",
    )

    def unexpected_process_check(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("terminal annotation service PID was rechecked")

    monkeypatch.setattr(
        "goal_plus.thinkthread_annotation._process_matches_service",
        unexpected_process_check,
    )

    closed = close_thinkthread_annotation(
        root_dir=root_dir,
        run_id=run_id,
        timeout_seconds=0,
    )

    assert closed == {"run_id": run_id, "status": "closed"}
    service = json.loads((service_root / "service.json").read_text(encoding="utf-8"))
    assert service["status"] == "stopped"
    assert service["finished_at"] == "2026-09-04T12:48:23Z"


def test_annotation_service_reclaims_expired_turn_and_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    client = IntegratedAgentPosix(project)
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"], native_host="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    runtime.start_batch(run_id, runtime.plan_next(run_id, requested_k=2).plan_id)
    opened = open_thinkthread_search_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    candidate_child = str(opened["jobs"][0]["thinkthread_id"])
    submit_candidate_attempt(runtime, opened, opened["jobs"][0], client)
    client.finish(candidate_child)
    assert wait_any(
        root_dir=runtime.root_dir,
        pool_id=str(opened["pool_id"]),
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )["event"]["kind"] == "candidate_ready"

    state = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    assert state is not None
    annotation_child = str(state["thinkthread_id"])
    client.enqueue(
        annotation_child,
        {
            "protocol": "goal-plus.thinkthread.v3",
            "type": "registration",
            "registration_nonce": state["registration_nonce"],
        },
    )
    expiring_request = annotation_request(
        state,
        "req-expiring-next",
        "thinkthread_annotation_next",
        {"run_id": run_id},
        deadline_unix_ms=int(time.time() * 1000) + 60_000,
    )
    client.enqueue(annotation_child, expiring_request)
    service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    response = response_for(client, "req-expiring-next")
    assert response["result"]["state"] == "claimed"

    state_path = (
        runtime.root_dir
        / "host-pools"
        / "thinkthread-annotations"
        / run_id
        / "state.json"
    )
    before_replay = json.loads(state_path.read_text(encoding="utf-8"))[
        "pending_responses"
    ]["req-expiring-next"]
    client.enqueue(annotation_child, expiring_request)
    service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    after_replay = json.loads(state_path.read_text(encoding="utf-8"))[
        "pending_responses"
    ]["req-expiring-next"]
    assert after_replay["queued_at_unix"] == before_replay["queued_at_unix"]
    assert after_replay["expires_at_unix"] == before_replay["expires_at_unix"]

    task = runtime.annotation.task(run_id, "c001", 1)
    assert task is not None
    runtime.annotation.tasks.write_task(
        task.model_copy(update={"outer_deadline_at": "1970-01-01T00:00:00Z"})
    )
    raw_state = json.loads(state_path.read_text(encoding="utf-8"))
    raw_state["pending_responses"]["req-expiring-next"]["expires_at_unix"] = 0
    state_path.write_text(json.dumps(raw_state), encoding="utf-8")

    recovered = service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert recovered is not None
    assert recovered["status"] == "ready"
    assert recovered["active_task"] is None
    assert recovered["last_turn_recovery"]["deadline_expired"] is True
    assert "req-expiring-next" not in recovered["pending_responses"]
    expired = recovered["expired_responses"]["req-expiring-next"]
    assert expired["reason"] == "child_request_deadline_elapsed"
    assert runtime.annotation.task(run_id, "c001", 1).state == (
        "terminal_error"
    )
    assert any(operation == "thinkthread.signal" for operation, _ in client.operations)

    client.enqueue(
        annotation_child,
        {
            "protocol": "goal-plus.thinkthread.v3",
            "type": "response_ack",
            "request_id": "req-expiring-next",
            "response_sha256": expired["response_sha256"],
        },
    )
    acknowledged = service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert acknowledged is not None
    assert acknowledged["expired_responses"]["req-expiring-next"].get(
        "acknowledged_after_expiry_at"
    )

    close_pool(
        root_dir=runtime.root_dir,
        pool_id=str(opened["pool_id"]),
        mode="interrupt",
        client=client,  # type: ignore[arg-type]
    )
    closed = close_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert closed is not None and closed["status"] == "closed"


def test_annotation_service_handles_rpc_after_model_pause_without_main_poll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    client = IntegratedAgentPosix(project)
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"], native_host="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    runtime.start_batch(run_id, runtime.plan_next(run_id, requested_k=2).plan_id)
    opened = open_thinkthread_search_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    first_child = str(opened["jobs"][0]["thinkthread_id"])
    submit_candidate_attempt(runtime, opened, opened["jobs"][0], client)
    client.finish(first_child)
    ready = wait_any(
        root_dir=runtime.root_dir,
        pool_id=str(opened["pool_id"]),
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )
    assert ready["event"]["kind"] == "candidate_ready"

    state = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    assert state is not None
    annotation_child = str(state["thinkthread_id"])
    client.enqueue(
        annotation_child,
        {
            "protocol": "goal-plus.thinkthread.v3",
            "type": "registration",
            "registration_nonce": state["registration_nonce"],
        },
    )
    client.enqueue(
        annotation_child,
        annotation_request(
            state,
            "req-delayed-next",
            "thinkthread_annotation_next",
            {"run_id": run_id},
        ),
    )
    result: list[int] = []
    service = threading.Thread(
        target=lambda: result.append(
            serve_thinkthread_annotation(
                root_dir=runtime.root_dir,
                run_id=run_id,
                client=client,  # type: ignore[arg-type]
                poll_seconds=0.01,
            )
        )
    )
    service.start()
    context = wait_for_response(client, "req-delayed-next")["result"]["task"]
    target_view = context["artifact_views"][0]["view_id"]

    # The real annotator may think for seconds before its next tool call. The
    # host-side service must remain available without another Main tool call.
    time.sleep(0.1)
    current = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    assert current is not None
    client.enqueue(
        annotation_child,
        annotation_request(
            current,
            "req-delayed-read",
            "thinkthread_artifact_view_pread",
            {
                "view_id": target_view,
                "path": "project/initial_program.py",
                "offset": 0,
                "length": 64 * 1024,
            },
        ),
    )
    read = wait_for_response(client, "req-delayed-read")
    assert base64.b64decode(read["result"]["dataBase64"]) == b"VALUE = 2\n"

    current = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    assert current is not None
    client.enqueue(
        annotation_child,
        annotation_request(
            current,
            "req-delayed-submit",
            "thinkthread_annotation_submit",
            {
                "description": "The immutable target contains the settled value.",
                "supplemental_evaluation": None,
                "tool_views": [],
            },
        ),
    )
    assert wait_for_response(client, "req-delayed-submit")["result"] == {
        "published": True
    }
    client.finish(annotation_child)
    service.join(timeout=2.0)
    assert not service.is_alive()
    assert result == [0]
    assert (
        runtime.annotation.task(run_id, "c001", 1).state == "completed"
    )

    close_pool(
        root_dir=runtime.root_dir,
        pool_id=str(opened["pool_id"]),
        mode="interrupt",
        client=client,  # type: ignore[arg-type]
    )
    closed = close_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert closed is not None and closed["status"] == "closed"


def test_runtime_owned_s0_k2_exact_snapshot_settlement_and_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    client = IntegratedAgentPosix(project)
    client.completion_unknown_once.update(
        {
            "fs.snapshot.create",
            "fs.branch.snapshot",
            "fs.run",
            "fs.snapshot.patch",
            "fs.apply",
        }
    )
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"], native_host="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    tasks = runtime.start_batch(run_id, plan.plan_id)

    opened = open_thinkthread_search_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert opened["baseline_snapshot_id"] == "fsnap-S0"
    assert len([op for op, _ in client.operations if op == "fs.snapshot.create"]) == 1
    spawn_params = [params for op, params in client.operations if op == "thinkthread.spawn"]
    assert len(spawn_params) == 2
    assert {item["fsSnapshotId"] for item in spawn_params} == {"fsnap-S0"}
    assert len({item["thinkthread_id"] for item in opened["jobs"]}) == 2
    assert len({item["fs_branch_id"] for item in opened["jobs"]}) == 2
    sessions = runtime.candidate_sessions.state.read_agent_sessions(run_id)
    assert len({item.agent_session_id for item in sessions}) == 2
    assert all(not task.workspace.exists() for task in tasks)
    for session in sessions:
        context = runtime.get_agent_context(session.agent_session_id)
        assert "workspace" not in context
        assert "workspace" not in context["candidate_task"]
        assert context["workspace_access"]["logical_root"] == "."
        assert context["workspace_access"]["native_cwd"] is None
        assert context["workspace_access"]["source_subdir"] == "project"

    for item in opened["jobs"]:
        submit_candidate_attempt(runtime, opened, item, client)
    for item in opened["jobs"]:
        client.finish(str(item["thinkthread_id"]))
    events = [
        wait_any(
            root_dir=runtime.root_dir,
            pool_id=opened["pool_id"],
            timeout_seconds=0,
            client=client,  # type: ignore[arg-type]
        )["event"]
        for _ in range(2)
    ]
    assert {item["kind"] for item in events} == {"candidate_ready"}
    branch_snapshots = [
        params for op, params in client.operations if op == "fs.branch.snapshot"
    ]
    assert len(branch_snapshots) == 2
    candidate_runs = [
        params
        for op, params in client.operations
        if op == "fs.run" and params["snapshotId"] != "fsnap-S0"
    ]
    assert {item["snapshotId"] for item in candidate_runs} == {"fsnap-2", "fsnap-3"}
    assert all(item["writes"] == "discard" for item in candidate_runs)

    records = runtime.candidate_creation.state.read_candidates(run_id)
    assert {record.iterations[0].artifact_ref.id for record in records} == {
        "fsnap-2",
        "fsnap-3",
    }
    assert all(record.iterations[0].process_passed for record in records)
    for record in records:
        task = runtime.annotation.task(run_id, record.candidate_id, 1)
        assert task is not None
        assert task.attempt_artifact_ref == record.iterations[0].artifact_ref
        assert task.artifact_views[0].artifact_ref == record.iterations[0].artifact_ref

    annotation = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    assert annotation is not None
    annotation_child_id = str(annotation["thinkthread_id"])
    client.enqueue(
        annotation_child_id,
        {
            "protocol": "goal-plus.thinkthread.v3",
            "type": "registration",
            "registration_nonce": annotation["registration_nonce"],
        },
    )
    client.enqueue(
        annotation_child_id,
        annotation_request(
            annotation,
            "req-claim-before-selection",
            "thinkthread_annotation_next",
            {"run_id": run_id},
        ),
    )
    service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    claimed_context = response_for(client, "req-claim-before-selection")["result"]["task"]
    claimed_task = runtime.annotation.task(
        run_id,
        claimed_context["candidate_id"],
        claimed_context["iteration"],
    )
    assert claimed_task is not None
    assert claimed_task.attempts == 1

    close_pool(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        mode="drain",
        client=client,  # type: ignore[arg-type]
    )
    selection = runtime.select(run_id)
    assert selection["selected_candidate_id"] == "c002"
    queue = runtime.annotation.queue(run_id)
    assert queue.state == "closed"
    assert queue.generation == 2
    assert (
        AnnotationHostService(runtime.root_dir).finish_annotation_task(
            claimed_task,
            result=EvidenceAnnotationResult(
                description="Late annotation after selection.",
                supplemental_evaluation=None,
                comparison_basis=list(claimed_task.comparison_basis),
                tool_views=[],
                usage={},
            ),
        )
        is False
    )
    assert all(
        runtime.annotation.task(run_id, record.candidate_id, 1).state
        == "cancelled"
        for record in records
    )
    assert not any(op == "fs.replace" for op, _ in client.operations)
    annotation = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    assert annotation is not None
    assert annotation["status"] == "running"
    settled = {
        path: path.read_bytes()
        for path in runtime.search_run.state.run_dir(run_id).rglob("*.json")
    }
    monkeypatch.setattr("goal_plus.thinkthread_annotation.AgentPosixSdkClient", lambda: client)
    client.completion_unknown_before_once.add("fs.branch.remove")
    with pytest.raises(RuntimeError, match="cleanup incomplete"):
        kick_pi_internal_agents(runtime.root_dir, run_id)
    incomplete = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    assert incomplete["status"] == "needs_recovery"
    assert incomplete["thinkthread_id"] == annotation_child_id
    assert all(path.read_bytes() == value for path, value in settled.items())
    assert kick_pi_internal_agents(runtime.root_dir, run_id) == {
        "annotation_started": False, "scheduler_started": False,
    }
    assert all(path.read_bytes() == value for path, value in settled.items())
    annotation = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    assert annotation["status"] == "closed"
    assert client.children == []

    manifest = runtime.promote(run_id, "c002")
    prepared = runtime.promotion_record(run_id)
    assert manifest.is_file()
    assert prepared.state == "prepared"
    assert prepared.publication_artifact_ref is not None
    assert prepared.publication_artifact_ref.id == "fsnap-P"
    apply_intent = next(
        item
        for item in runtime.search_run.state.read_run(run_id).provider_mutations
        if item.operation == "apply"
    )
    assert apply_intent.state == "prepared"

    applied = runtime.apply_promotion(run_id)

    assert applied.state == "applied"
    persisted = runtime.search_run.state.read_run(run_id)
    assert persisted.search_result == "passed"
    assert persisted.publication_outcome is not None
    assert persisted.publication_outcome.state == "applied"
    assert persisted.publication_outcome.apply_outcome == "applied"
    assert persisted.publication_outcome.notification_state == "pending"
    assert len([op for op, _ in client.operations if op == "fs.apply"]) == 1
    assert not any(op == "fs.replace" for op, _ in client.operations)
    snapshot = goal_plus_monitor_snapshot(runtime.root_dir, run_id=run_id)
    assert snapshot["run"]["search_result"] == "passed"
    assert snapshot["run"]["publication_result"] == "applied"
    assert snapshot["search_tasks"][0]["search_result"] == "passed"
    assert snapshot["search_tasks"][0]["publication_result"] == "applied"
    pending_cleanup = snapshot["run"]["host_cleanup"]
    assert pending_cleanup["state"] == "pending"
    assert pending_cleanup["candidate_pools"]["complete"] is True
    assert pending_cleanup["annotation"]["state"] == "closed"
    assert pending_cleanup["annotation"]["service_complete"] is True
    assert pending_cleanup["annotation"]["complete"] is True
    assert pending_cleanup["snapshots"]["owned_total"] == 4
    assert pending_cleanup["snapshots"]["state"] == "pending"
    assert pending_cleanup["final_artifacts"]["complete"] is False
    cleanup_json = json.dumps(pending_cleanup, sort_keys=True)
    assert "host-pools" not in cleanup_json
    assert str(runtime.root_dir) not in cleanup_json
    assert snapshot["search_tasks"][0]["host_cleanup"] == pending_cleanup
    client.completion_unknown_once.add("fs.snapshot.remove")
    report = FileSearchRuntime(runtime.root_dir).report(run_id)
    report_text = report.read_text(encoding="utf-8")
    html_text = report.with_suffix(".html").read_text(encoding="utf-8")
    assert "- Search result: `passed`" in report_text
    assert "- Publication result: `applied`" in report_text
    assert "Search result" in html_text
    assert "Publication result" in html_text
    assert "Provider Artifact Lifecycle" in report_text
    assert "Provider Artifact Lifecycle" in html_text
    assert client.snapshots == set()
    cleanup = runtime.search_run.state.read_run(run_id).provider_cleanup
    assert {item.resource_id for item in cleanup} == {
        "fsnap-S0",
        "fsnap-2",
        "fsnap-3",
        "fsnap-P",
    }
    assert {item.state for item in cleanup} == {"removed"}
    remove_mutations = [
        item
        for item in runtime.search_run.state.read_run(run_id).provider_mutations
        if item.operation == "snapshot_remove"
    ]
    assert len(remove_mutations) == 4
    assert {item.state for item in remove_mutations} == {"closed"}
    assert len(
        [operation for operation, _params in client.operations if operation == "fs.snapshot.remove"]
    ) == 4
    evidence = runtime.get_global_evidence(sessions[0].agent_session_id)
    assert {item["artifact_availability"] for item in evidence} == {"removed"}
    assert all(item["reference_available"] is False for item in evidence)
    monitor = goal_plus_monitor_snapshot(runtime.root_dir, run_id=run_id)
    assert {
        item["artifact_availability"]
        for item in monitor["run"]["artifact_availability"]
    } == {"removed"}
    assert {
        item["artifact_ref"]["id"]
        for item in monitor["run"]["artifact_availability"]
    } == {"fsnap-S0", "fsnap-2", "fsnap-3", "fsnap-P"}
    completed_cleanup = monitor["run"]["host_cleanup"]
    assert completed_cleanup["state"] == "complete"
    assert completed_cleanup["candidate_pools"]["complete"] is True
    assert completed_cleanup["annotation"]["complete"] is True
    assert completed_cleanup["snapshots"]["states"] == {"removed": 4}
    assert completed_cleanup["snapshots"]["complete"] is True
    assert completed_cleanup["final_artifacts"]["complete"] is True
    assert completed_cleanup["blockers"] == []

    removed_task = runtime.annotation.task(run_id, "c001", 1)
    assert removed_task is not None
    with pytest.raises(RuntimeError, match="removed after terminal cleanup"):
        runtime.annotation_context.open_views(removed_task)

    remove_call_count = len(
        [operation for operation, _params in client.operations if operation == "fs.snapshot.remove"]
    )
    regenerated = FileSearchRuntime(runtime.root_dir).report(run_id)
    assert regenerated.is_file()
    assert len(
        [operation for operation, _params in client.operations if operation == "fs.snapshot.remove"]
    ) == remove_call_count


@pytest.mark.parametrize("operation", ["fs.branch.snapshot", "fs.run"])
def test_settlement_recovers_provider_journal_without_duplicate_iteration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    client = IntegratedAgentPosix(project)
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"], native_host="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    runtime.start_batch(run_id, runtime.plan_next(run_id, requested_k=2).plan_id)
    opened = open_thinkthread_search_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    job = opened["jobs"][0]
    client.completion_unknown_once.add(operation)
    client.request_status_completion_unknown_once.add(operation)
    submit_candidate_attempt(runtime, opened, job, client)
    client.finish(str(job["thinkthread_id"]))

    unresolved = wait_any(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )

    assert unresolved["event"] is None
    unresolved_job = next(
        item
        for item in unresolved["pool"]["jobs"]
        if item["candidate_id"] == job["candidate_id"]
    )
    assert unresolved_job["status"] == "needs_recovery"
    assert unresolved_job["recovery"]["stage"] == "attempt_verification"
    assert unresolved_job["recovery"]["state"] == "outcome_unknown"
    assert unresolved["pool"]["active_count"] == 2

    recovered = wait_any(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )

    assert recovered["event"]["kind"] == "candidate_ready"
    assert recovered["event"]["candidate_id"] == job["candidate_id"]
    recovered_job = next(
        item
        for item in recovered["pool"]["jobs"]
        if item["candidate_id"] == job["candidate_id"]
    )
    assert recovered_job["status"] == "ready"
    assert recovered_job["recovery"] is None
    assert recovered_job["error"] is None
    operation_calls = [item for item in client.operations if item[0] == operation]
    if operation == "fs.run":
        operation_calls = [
            item for item in operation_calls if item[1]["snapshotId"] != "fsnap-S0"
        ]
    assert len(operation_calls) == 1
    record = runtime.candidate_creation.state.read_candidate(run_id, str(job["candidate_id"]))
    assert len(record.iterations) == 1
    assert record.iterations[0].metrics["_candidate_attempt"]["id"] == (
        recovered_job["attempt_result"]["attempt_id"]
    )
    mutation = next(
        item
        for item in record.provider_mutations
        if item.operation
        == ("branch_snapshot" if operation == "fs.branch.snapshot" else "verifier_run")
    )
    assert mutation.state == "closed"

    duplicate = wait_any(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )
    assert duplicate["event"] is None
    assert len(runtime.candidate_creation.state.read_candidate(run_id, str(job["candidate_id"])).iterations) == 1


def test_candidate_request_close_recovery_does_not_repeat_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    client = IntegratedAgentPosix(project)
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"], native_host="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    runtime.start_batch(run_id, runtime.plan_next(run_id, requested_k=2).plan_id)
    opened = open_thinkthread_search_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    job = opened["jobs"][0]
    client.close_failure_once.add("fs.run")
    submit_candidate_attempt(runtime, opened, job, client)
    client.finish(str(job["thinkthread_id"]))

    ready = wait_any(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )

    assert ready["event"]["kind"] == "candidate_ready"
    record = runtime.candidate_creation.state.read_candidate(run_id, str(job["candidate_id"]))
    verifier = next(
        item for item in record.provider_mutations if item.operation == "verifier_run"
    )
    assert verifier.state == "succeeded"
    assert verifier.close_state == "needs_recovery"
    verifier_calls = len(
        [
            item
            for item in client.operations
            if item[0] == "fs.run" and item[1]["snapshotId"] != "fsnap-S0"
        ]
    )

    reconciled = runtime.reconcile_thinkthread_request_closes(
        run_id,
        client=client,  # type: ignore[arg-type]
    )

    assert reconciled == {"closed": [verifier.request_id], "needs_recovery": []}
    recovered = runtime.candidate_creation.state.read_candidate(run_id, str(job["candidate_id"]))
    recovered_verifier = next(
        item
        for item in recovered.provider_mutations
        if item.operation == "verifier_run"
    )
    assert recovered_verifier.state == "closed"
    assert recovered_verifier.close_state == "closed"
    assert len(
        [
            item
            for item in client.operations
            if item[0] == "fs.run" and item[1]["snapshotId"] != "fsnap-S0"
        ]
    ) == verifier_calls


def test_definitive_thinkthread_verifier_error_records_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    client = IntegratedAgentPosix(project)
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"], native_host="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    runtime.start_batch(run_id, runtime.plan_next(run_id, requested_k=2).plan_id)
    opened = open_thinkthread_search_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    job = opened["jobs"][0]
    receipt = submit_candidate_attempt(runtime, opened, job, client)
    client.definitive_failure_once.add("fs.run")
    client.finish(str(job["thinkthread_id"]))

    result = wait_any(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )

    assert result["event"]["kind"] == "candidate_ready"
    feedback = result["event"]["attempt_result"]
    assert feedback["attempt_id"] == receipt["attempt_id"]
    assert feedback["score_report"]["process_passed"] is False
    assert feedback["score_report"]["disposition"] == "failure"
    verifier = feedback["score_report"]["verifier_results"][0]
    assert verifier["failure_class"] == "VerifierInfrastructureFailure"
    assert verifier["metrics"]["error_code"] == "FsRunRejected"
    record = runtime.candidate_creation.state.read_candidate(run_id, str(job["candidate_id"]))
    assert len(record.iterations) == 1
    assert record.iterations[0].failure_class == "VerifierInfrastructureFailure"


def test_unresolved_settlement_does_not_block_another_ready_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    client = IntegratedAgentPosix(project)
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"], native_host="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    runtime.start_batch(run_id, runtime.plan_next(run_id, requested_k=2).plan_id)
    opened = open_thinkthread_search_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    first_job, second_job = opened["jobs"]
    client.completion_unknown_once.add("fs.branch.snapshot")
    client.request_status_completion_unknown_once.add("fs.branch.snapshot")
    submit_candidate_attempt(runtime, opened, first_job, client)
    client.finish(str(first_job["thinkthread_id"]))
    unresolved = wait_any(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )
    first_record = runtime.candidate_creation.state.read_candidate(
        run_id, str(first_job["candidate_id"])
    )
    snapshot_mutation = next(
        item
        for item in first_record.provider_mutations
        if item.operation == "branch_snapshot"
    )
    pending_status = client.request_status[snapshot_mutation.request_id]
    succeeded_result = dict(pending_status["result"])
    pending_status["state"] = "accepted"
    pending_status.pop("result")
    submit_candidate_attempt(runtime, opened, second_job, client)
    client.finish(str(second_job["thinkthread_id"]))

    other_ready = wait_any(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )

    assert unresolved["event"] is None
    assert other_ready["event"]["kind"] == "candidate_ready"
    assert other_ready["event"]["candidate_id"] == second_job["candidate_id"]
    first_snapshot = next(
        item
        for item in other_ready["pool"]["jobs"]
        if item["candidate_id"] == first_job["candidate_id"]
    )
    assert first_snapshot["status"] == "needs_recovery"
    assert first_snapshot["recovery"]["state"] == "outcome_unknown"

    pending_status["state"] = "succeeded"
    pending_status["result"] = succeeded_result
    recovered = wait_any(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )
    assert recovered["event"]["candidate_id"] == first_job["candidate_id"]
    assert recovered["pool"]["active_count"] == 0


def test_settlement_recovery_definitive_failure_becomes_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    client = IntegratedAgentPosix(project)
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"], native_host="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    runtime.start_batch(run_id, runtime.plan_next(run_id, requested_k=2).plan_id)
    opened = open_thinkthread_search_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    job = opened["jobs"][0]
    client.completion_unknown_once.add("fs.branch.snapshot")
    client.request_status_completion_unknown_once.add("fs.branch.snapshot")
    submit_candidate_attempt(runtime, opened, job, client)
    client.finish(str(job["thinkthread_id"]))
    wait_any(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )
    record = runtime.candidate_creation.state.read_candidate(run_id, str(job["candidate_id"]))
    mutation = next(
        item for item in record.provider_mutations if item.operation == "branch_snapshot"
    )
    client.request_status[mutation.request_id] = {
        "requestId": mutation.request_id,
        "method": "fs.branch.snapshot",
        "state": "failed",
        "error": {
            "code": "FsSnapshotFailed",
            "message": "snapshot could not be completed",
            "retryable": False,
        },
    }

    failed = wait_any(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )

    assert failed["event"]["kind"] == "failed"
    failed_job = next(
        item
        for item in failed["pool"]["jobs"]
        if item["candidate_id"] == job["candidate_id"]
    )
    assert failed_job["status"] == "failed"
    assert failed_job["recovery"]["state"] == "failed"
    assert failed_job["error"]["error_code"] == "FsSnapshotFailed"


def test_discard_reset_recovers_before_and_after_provider_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    client = IntegratedAgentPosix(project)
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"], native_host="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    runtime.start_batch(run_id, runtime.plan_next(run_id, requested_k=2).plan_id)
    opened = open_thinkthread_search_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    job = opened["jobs"][0]
    child_id = str(job["thinkthread_id"])
    submit_candidate_attempt(runtime, opened, job, client)
    client.finish(child_id)
    first = wait_any(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )
    assert first["event"]["kind"] == "candidate_ready"

    continued = continue_pool(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        candidate_id=str(job["candidate_id"]),
        message="Try a lower-scoring follow-up",
        max_runtime_seconds=60,
        client=client,  # type: ignore[arg-type]
    )
    continued_job = next(
        item
        for item in continued["jobs"]
        if item["candidate_id"] == job["candidate_id"]
    )
    submit_candidate_attempt(runtime, opened, continued_job, client)
    durable_job = _load_job(
        runtime.root_dir,
        opened["pool_id"],
        str(continued_job["job_id"]),
    )
    client.snapshot_index = 0
    child = client.child(child_id)
    child["executionState"] = "absent"
    child["agentState"] = "ready"
    child["lastWakeOutcome"] = {
        "messageId": durable_job["active_message_id"],
        "outcome": "completed",
    }
    child["completion"] = {"reason": "completed"}
    client.completion_unknown_before_once.add("fs.branch.reset")

    unresolved = wait_any(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )
    assert unresolved["event"] is None
    unresolved_job = next(
        item
        for item in unresolved["pool"]["jobs"]
        if item["candidate_id"] == job["candidate_id"]
    )
    assert unresolved_job["status"] == "needs_recovery"
    assert unresolved_job["recovery"]["stage"] == "attempt_verification"
    record = runtime.candidate_creation.state.read_candidate(run_id, str(job["candidate_id"]))
    assert len(record.iterations) == 2
    assert record.iterations[-1].disposition == "discard"

    client.completion_unknown_once.add("fs.branch.reset")
    recovered = wait_any(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )
    assert recovered["event"]["kind"] == "candidate_ready"
    assert recovered["event"]["candidate_id"] == job["candidate_id"]
    feedback = recovered["event"]["attempt_result"]
    assert feedback["attempt_iteration"] == 2
    assert feedback["score_report"]["disposition"] == "discard"
    assert feedback["attempt_artifact_ref"] != feedback[
        "workspace_artifact_after_attempt"
    ]
    assert len(
        runtime.candidate_creation.state.read_candidate(run_id, str(job["candidate_id"])).iterations
    ) == 2
    assert len(
        [item for item in client.operations if item[0] == "fs.branch.reset"]
    ) == 2
    assert client.branches[str(job["fs_branch_id"])]["baseSnapshotId"] == "fsnap-2"


def test_annotation_spawn_wake_and_close_recover_without_duplicate_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    client = IntegratedAgentPosix(project)
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"], native_host="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    tasks = runtime.start_batch(
        run_id,
        runtime.plan_next(run_id, requested_k=2).plan_id,
    )
    opened = open_thinkthread_search_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )

    client.completion_unknown_once.add("thinkthread.spawn")
    first_child = str(opened["jobs"][0]["thinkthread_id"])
    submit_candidate_attempt(runtime, opened, opened["jobs"][0], client)
    client.finish(first_child)
    first = wait_any(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )
    assert first["event"]["candidate_id"] == tasks[0].candidate_id
    recovered_at_boundary = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    assert recovered_at_boundary is not None
    assert recovered_at_boundary["status"] == "running"
    assert (
        recovered_at_boundary["spawn_intent"]["state"]
        == "bound_after_recovery"
    )
    annotation_spawn_count = len(
        [operation for operation, _params in client.operations if operation == "thinkthread.spawn"]
    )

    recovered_spawn = service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert recovered_spawn is not None
    assert recovered_spawn["status"] == "running"
    assert recovered_spawn["spawn_intent"]["state"] == "bound_after_recovery"
    assert len(
        [operation for operation, _params in client.operations if operation == "thinkthread.spawn"]
    ) == annotation_spawn_count

    first_task = runtime.annotation.task(
        run_id,
        tasks[0].candidate_id,
        1,
    )
    assert first_task is not None
    assert AnnotationHostService(runtime.root_dir).finish_annotation_task(
        first_task,
        result=EvidenceAnnotationResult(
            description="First candidate changed the program value.",
            supplemental_evaluation=None,
            comparison_basis=list(first_task.comparison_basis),
            tool_views=[],
            usage={},
        ),
    )
    annotation_child = str(recovered_spawn["thinkthread_id"])
    client.finish(annotation_child)
    client.child(annotation_child)["executionState"] = "running"
    ready = service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert ready is not None and ready["status"] == "ready"

    client.completion_unknown_once.add("message.send")
    second_child = str(opened["jobs"][1]["thinkthread_id"])
    submit_candidate_attempt(runtime, opened, opened["jobs"][1], client)
    client.finish(second_child)
    second = wait_any(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        timeout_seconds=0,
        client=client,  # type: ignore[arg-type]
    )
    assert second["event"]["candidate_id"] == tasks[1].candidate_id
    recovered_wake_at_boundary = annotation_snapshot(
        root_dir=runtime.root_dir,
        run_id=run_id,
    )
    assert recovered_wake_at_boundary is not None
    assert recovered_wake_at_boundary["status"] == "running"
    assert (
        recovered_wake_at_boundary["wake_intent"]["state"]
        == "acknowledged_after_recovery"
    )
    wake_count = len([item for item in client.sent if item.get("wake") is True])

    recovered_wake = service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert recovered_wake is not None and recovered_wake["status"] == "running"
    assert recovered_wake["wake_intent"]["state"] == "acknowledged_after_recovery"
    assert len([item for item in client.sent if item.get("wake") is True]) == wake_count

    client.enqueue(
        annotation_child,
        {
            "protocol": "goal-plus.thinkthread.v3",
            "type": "dispatch_ack",
            "dispatch_nonce": recovered_spawn["dispatch_nonce"],
        },
    )
    after_stale_ack = service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert after_stale_ack is not None
    assert after_stale_ack["status"] == "running"

    close_pool(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        mode="interrupt",
        client=client,  # type: ignore[arg-type]
    )
    client.completion_unknown_before_once.add("fs.branch.remove")
    unresolved_close = close_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert unresolved_close is not None
    assert unresolved_close["status"] == "needs_recovery"
    still_fenced = service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert still_fenced is not None and still_fenced["status"] == "needs_recovery"

    client.completion_unknown_once.add("thinkthread.destroy")
    closed = close_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert closed is not None and closed["status"] == "closed"
    assert closed["close_intent"]["state"] == "closed"


def test_retained_annotation_child_uses_full_snapshot_views_and_structured_submit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED", "1")
    project = make_project(tmp_path)
    client = IntegratedAgentPosix(project)
    monkeypatch.setattr("goal_plus.bootstrap.AgentPosixSdkClient", lambda: client)
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(thinkthread_spec(project), [project / "evaluator.py"], native_host="pi")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    runtime.start_batch(run_id, plan.plan_id)
    opened = open_thinkthread_search_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    for item in opened["jobs"]:
        submit_candidate_attempt(runtime, opened, item, client)
    for item in opened["jobs"]:
        client.finish(str(item["thinkthread_id"]))
    for _ in range(2):
        result = wait_any(
            root_dir=runtime.root_dir,
            pool_id=opened["pool_id"],
            timeout_seconds=0,
            client=client,  # type: ignore[arg-type]
        )
        assert result["event"]["kind"] == "candidate_ready"

    annotation_mutations_before = sum(
        operation
        in {
            "fs.run",
            "workflow.fs.snapshot.patchBytes",
            "fs.snapshot.remove",
        }
        for operation, _ in client.operations
    )

    original_snapshot_diff = ThinkThreadSnapshotReader.diff

    def truncated_snapshot_diff(*_args: Any, **_kwargs: Any) -> tuple[str, bool]:
        return "bounded diff payload that must not be inlined", True

    monkeypatch.setattr(ThinkThreadSnapshotReader, "diff", truncated_snapshot_diff)

    state = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    assert state is not None
    child_id = str(state["thinkthread_id"])
    client.enqueue(
        child_id,
        {
            "protocol": "goal-plus.thinkthread.v3",
            "type": "registration",
            "registration_nonce": state["registration_nonce"],
        },
    )
    client.enqueue(
        child_id,
        annotation_request(
            state,
            "req-next-1",
            "thinkthread_annotation_next",
            {"run_id": run_id},
        ),
    )
    service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    first_context = response_for(client, "req-next-1")["result"]["task"]
    assert first_context["artifact_views"][0]["role"] == "target"
    assert first_context["actual_diff"] == ""
    assert first_context["actual_diff_omitted"]
    assert first_context["candidate_diff"] == ""
    assert first_context["candidate_diff_omitted"]
    first_task = runtime.annotation.task(run_id, "c001", 1)
    assert first_task is not None
    client.enqueue(
        child_id,
        annotation_request(
            state,
            "req-submit-1",
            "thinkthread_annotation_submit",
                {
                    "description": "First snapshot is a valid isolated implementation.",
                    "supplemental_evaluation": {
                        "summary": "The first implementation passes its exact verifier.",
                        "dimensions": [
                            {
                                "name": "correctness",
                                "finding": "The immutable target snapshot passed.",
                                "confidence": "high",
                                "evidence": ["settled process-verifier result"],
                            }
                        ],
                        "comparisons": [],
                        "limitations": [],
                    },
                "tool_views": [],
            },
        ),
    )
    service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert response_for(client, "req-submit-1")["result"] == {"published": True}

    current = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    assert current is not None
    client.enqueue(
        child_id,
        annotation_request(
            current,
            "req-next-2",
            "thinkthread_annotation_next",
            {"run_id": run_id},
        ),
    )
    service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    second_context = response_for(client, "req-next-2")["result"]["task"]
    assert second_context["actual_diff"] == ""
    assert second_context["actual_diff_omitted"]
    assert second_context["candidate_diff"] == ""
    assert second_context["candidate_diff_omitted"]
    assert second_context["peer_evidence"][0]["candidate_diff"] is None
    assert second_context["peer_evidence"][0]["diff_omitted"]
    views = second_context["artifact_views"]
    assert [item["role"] for item in views] == ["target", "peer"]
    target_id = views[0]["view_id"]
    peer_id = views[1]["view_id"]
    monkeypatch.setattr(ThinkThreadSnapshotReader, "diff", original_snapshot_diff)

    for request_id, tool, params in (
        (
            "req-list",
            "thinkthread_artifact_view_list",
            {"view_id": target_id, "path": "project"},
        ),
        (
            "req-stat",
            "thinkthread_artifact_view_stat",
            {"view_id": target_id, "path": "project/initial_program.py"},
        ),
        (
            "req-read",
            "thinkthread_artifact_view_pread",
            {
                "view_id": target_id,
                "path": "project/initial_program.py",
                "offset": 0,
                "length": 64 * 1024,
            },
        ),
        (
            "req-diff",
            "thinkthread_artifact_view_diff",
            {"base_view_id": peer_id, "target_view_id": target_id},
        ),
    ):
        current = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
        assert current is not None
        client.enqueue(child_id, annotation_request(current, request_id, tool, params))
        service_thinkthread_annotation(
            root_dir=runtime.root_dir,
            run_id=run_id,
            client=client,  # type: ignore[arg-type]
        )
        assert response_for(client, request_id)["ok"] is True
    read_result = response_for(client, "req-read")["result"]
    assert base64.b64decode(read_result["dataBase64"]) == b"VALUE = 3\n"

    current = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    assert current is not None
    second_submit_request = annotation_request(
        current,
        "req-submit-2",
        "thinkthread_annotation_submit",
        {
            "description": "Second snapshot improves the target and differs from its peer.",
            "supplemental_evaluation": {
                "summary": "The second exact snapshot scores higher than its peer.",
                "dimensions": [
                    {
                        "name": "relative quality",
                        "finding": "The target has the stronger verifier result.",
                        "confidence": "high",
                        "evidence": ["target and peer snapshot evidence"],
                    }
                ],
                "comparisons": [
                    {
                        **second_context["comparison_basis"][0],
                        "relation": "different",
                        "rationale": "The target snapshot has a higher exact score.",
                        "evidence": ["immutable target and peer views"],
                    }
                ],
                "limitations": [],
            },
            "tool_views": [],
        },
    )
    client.enqueue(
        child_id,
        second_submit_request,
    )
    service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert response_for(client, "req-submit-2")["result"] == {"published": True}
    assert runtime.annotation.task(run_id, "c001", 1).state == "completed"
    assert runtime.annotation.task(run_id, "c002", 1).state == "completed"
    assert len([op for op, _ in client.operations if op == "thinkthread.spawn"]) == 3
    assert sum(
        operation
        in {
            "fs.run",
            "workflow.fs.snapshot.patchBytes",
            "fs.snapshot.remove",
        }
        for operation, _ in client.operations
    ) == annotation_mutations_before

    response_chunks_before = len(
        [
            payload
            for item in client.sent
            if isinstance(item.get("text"), str)
            and (payload := json.loads(item["text"])).get("request_id") == "req-submit-2"
        ]
    )
    client.enqueue(child_id, second_submit_request)
    service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    response_chunks = [
        payload
        for item in client.sent
        if isinstance(item.get("text"), str)
        and (payload := json.loads(item["text"])).get("request_id") == "req-submit-2"
    ]
    assert len(response_chunks) == response_chunks_before * 2
    current = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    assert current is not None
    client.enqueue(
        child_id,
        {
            "protocol": "goal-plus.thinkthread.v3",
            "type": "response_ack",
            "request_id": "req-submit-2",
            "response_sha256": response_chunks[0]["response_sha256"],
        },
    )
    service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert "req-submit-2" not in annotation_snapshot(
        root_dir=runtime.root_dir,
        run_id=run_id,
    )["pending_responses"]

    current = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    truncated = annotation_request(
        current,
        "req-truncated",
        "thinkthread_annotation_next",
        {"run_id": run_id},
    )
    client.enqueue(child_id, truncated)
    client.messages[child_id][-1]["truncated"] = True
    service_thinkthread_annotation(
        root_dir=runtime.root_dir,
        run_id=run_id,
        client=client,  # type: ignore[arg-type]
    )
    assert not any(
        isinstance(item.get("text"), str)
        and json.loads(item["text"]).get("request_id") == "req-truncated"
        for item in client.sent
    )

    current = annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    malformed = annotation_request(
        current,
        "req-bad-hash",
        "thinkthread_annotation_next",
        {"run_id": run_id},
    )
    malformed["content_sha256"] = "0" * 64
    client.enqueue(child_id, malformed)
    with pytest.raises(ValueError, match="content_sha256 mismatch"):
        service_thinkthread_annotation(
            root_dir=runtime.root_dir,
            run_id=run_id,
            client=client,  # type: ignore[arg-type]
        )

    close_pool(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        mode="drain",
        client=client,  # type: ignore[arg-type]
    )
    runtime.select(run_id)
    monkeypatch.setattr("goal_plus.thinkthread_annotation.AgentPosixSdkClient", lambda: client)
    kick_pi_internal_agents(runtime.root_dir, run_id)
    assert annotation_snapshot(root_dir=runtime.root_dir, run_id=run_id)["status"] == "closed"
