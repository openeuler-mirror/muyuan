"""Native Root MCP startup probe; only the annotator Child performs model work."""

import json
import os
import threading
import time
import traceback
import uuid
from pathlib import Path

from fastmcp import FastMCP

from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.execution.thinkthread_child import TURN_QUIESCENCE_SECONDS, ChildDriver
from goal_plus.execution.thinkthread_rpc import (
    PROTOCOL,
    message_payload,
    request_hash,
    response_payload,
)
from goal_plus.infrastructure.clock import SystemClock
from goal_plus.thinkthread_agent_posix import (
    AgentPosixSdkClient,
    new_request_id,
    validate_thinkthread_root,
)
from goal_plus.thinkthread_artifacts import ThinkThreadArtifactViewRouter

VIEW_TOOLS = {
    "goal_plus_thinkthread_artifact_view_list",
    "goal_plus_thinkthread_artifact_view_stat",
    "goal_plus_thinkthread_artifact_view_pread",
    "goal_plus_thinkthread_artifact_view_diff",
}


def run_probe(report_path: Path) -> None:
    report = {"stage": "root_start", "ok": False}
    report_path.write_text(json.dumps(report))
    client = AgentPosixSdkClient()
    driver = ChildDriver(client, SystemClock())
    binding = None
    root_id = None
    try:
        root = validate_thinkthread_root(client, agent_harness="codex")
        root_id = root["thinkthreadId"]
        report["root_id"] = root_id
        # Two immutable snapshots of the Root tree serve as the authorized target and
        # peer Artifact Views; the Child itself is spawned from the peer baseline.
        workspace = Path.cwd()
        secret = f"annotator-secret-{uuid.uuid4().hex[:12]}"
        (workspace / "solution.txt").write_text(f"score: 7\nnote: {secret}\n")
        target_snapshot = client.invoke("fs.snapshot.create", {"requestId": new_request_id()})["snapshotId"]
        (workspace / "solution.txt").write_text("score: 5\nnote: baseline\n")
        peer_snapshot = client.invoke("fs.snapshot.create", {"requestId": new_request_id()})["snapshotId"]
        router = ThinkThreadArtifactViewRouter(client, {
            "view-target": ArtifactRef(kind="fs_snapshot", id=target_snapshot, provider="thinkthread"),
            "view-peer": ArtifactRef(kind="fs_snapshot", id=peer_snapshot, provider="thinkthread"),
        })
        registration = str(uuid.uuid4())
        nonce = str(uuid.uuid4())
        prompt = (
            "This is a minimal Evidence Annotation transport check through the real Goal Plus "
            "annotator MCP. Call goal_plus_thinkthread_annotation_next once to claim the single "
            "admitted task. Inspect the authorized immutable Artifact Views: stat then pread "
            "solution.txt on the target view, and diff the peer view against the target view. "
            "Then publish exactly one annotation with goal_plus_thinkthread_annotation_submit: "
            "the description must be one concise objective line quoting the complete note token "
            "from the target solution.txt; supplemental_evaluation must contain at least one "
            "dimension with a numeric score. After publishing, call "
            "goal_plus_thinkthread_annotation_next again; when it reports idle, finish the turn "
            "immediately. Use only the six annotation tools; do not run commands, code, tests or "
            "verifiers, and do not modify any file.\n"
            f"goal_plus_thinkthread_role=annotator; run_id=run-probe; agent_session_id=annotator-probe; "
            f"registration_nonce={registration}; dispatch_nonce={nonce}; "
            f"queue_generation=1; queue_revision=1."
        )
        params = {
            "profile": "self", "fs": "private", "fsSnapshotId": peer_snapshot,
            "capabilities": ["thinkthread.message"],
            "initialMessage": prompt,
        }
        binding = driver.binding(driver.spawn(params))
        message_id = binding.initial_message_id
        report.update(stage="annotation_turn", child_id=binding.child_id)
        report_path.write_text(json.dumps(report))
        state = {"next_calls": 0, "submitted": None}
        tools_by_request: dict[str, str] = {}
        observations: list[dict] = []
        inbound: list[str] = []
        deadline = time.monotonic() + 480
        registered, acknowledged = False, False
        cursor = None
        while time.monotonic() < deadline:
            page = client.invoke("message.receive", {
                "senderThinkthreadId": binding.child_id, "limit": 32,
                **({"after": cursor} if cursor else {}),
            })
            cursor = page["nextCursor"]
            for message in page["messages"]:
                assert message["senderThinkthreadId"] == binding.child_id
                inbound.append(str(message.get("text"))[:500])
                payload = message_payload(message)
                if not payload or payload.get("protocol") != PROTOCOL:
                    continue
                if (
                    payload.get("type") == "registration"
                    and payload.get("registration_nonce") == registration
                ):
                    registered = True
                if (
                    payload.get("type") == "dispatch_ack"
                    and payload.get("dispatch_nonce") == nonce
                ):
                    assert payload["message_id"] == message_id
                    acknowledged = True
                if payload.get("type") != "request":
                    continue
                assert registered and acknowledged, "Tool request arrived before its native registration and dispatch acknowledgement"
                assert payload["agent_session_id"] == "annotator-probe"
                tool = payload["tool"]
                assert tool in VIEW_TOOLS | {
                    "goal_plus_thinkthread_annotation_next",
                    "goal_plus_thinkthread_annotation_submit",
                }, f"Annotator Child called a non-annotation tool: {tool}"
                assert request_hash(payload) == payload["content_sha256"]
                tools_by_request.setdefault(payload["request_id"], tool)
                result = _serve_annotation_tool(router, state, tool, payload["params"])
                for chunk in response_payload(payload["request_id"], {"ok": True, "result": result}):
                    client.invoke("message.send", {
                        "recipientThinkthreadId": binding.child_id, "text": chunk, "wake": False,
                        "replyToMessageId": message["messageId"],
                    })
            child = driver.observe(binding.child_id, root_id=root_id, branch_id=binding.branch_id)
            outcome = child.get("lastWakeOutcome") or {}
            if outcome.get("messageId") == message_id and outcome.get("outcome") == "completed":
                finished = outcome.get("finishedAtUnixMs", 0) / 1000
                if time.time() - finished >= TURN_QUIESCENCE_SECONDS:
                    observations.append({
                        "final": True,
                        "agentState": child.get("agentState"),
                        "executionState": child.get("executionState"),
                        "completion": child.get("completion"),
                        "lastWakeOutcome": child.get("lastWakeOutcome"),
                    })
                    break
            if len(observations) < 5:
                observations.append({
                    "agentState": child.get("agentState"),
                    "executionState": child.get("executionState"),
                    "pendingWake": child.get("pendingWake"),
                    "lastWakeOutcome": child.get("lastWakeOutcome"),
                })
            time.sleep(0.2)
        else:
            raise TimeoutError("Codex annotator Child did not finish its annotation turn")
        submitted = state["submitted"]
        assert submitted is not None, "Annotator Child did not submit any annotation"
        description = submitted.get("description")
        assert isinstance(description, str) and description.strip(), submitted
        assert secret in description, "Annotation description did not quote the target view evidence"
        supplemental = submitted.get("supplemental_evaluation")
        assert isinstance(supplemental, dict) and supplemental, "Annotation omitted its supplemental evaluation"
        tools = list(tools_by_request.values())
        assert tools[0] == "goal_plus_thinkthread_annotation_next"
        assert "goal_plus_thinkthread_annotation_submit" in tools
        assert tools[-1] == "goal_plus_thinkthread_annotation_next"
        view_reads = [tool for tool in tools if tool in VIEW_TOOLS]
        assert view_reads, "Annotator Child did not inspect any immutable Artifact View"
        assert any("pread" in tool or "diff" in tool for tool in view_reads), view_reads
        stopped = driver.interrupt(binding.child_id, 10)
        assert stopped and stopped["executionState"] == "absent"
        assert (workspace / "solution.txt").read_text() == "score: 5\nnote: baseline\n", "Annotator changed Root workspace"
        report.update(
            ok=True,
            tools=tools,
            view_reads=view_reads,
            annotation={
                "description": description,
                "supplemental_evaluation": supplemental,
                "tool_views": submitted.get("tool_views") or [],
            },
            requests=sorted(tools_by_request),
        )
    except BaseException:
        report["error"] = traceback.format_exc()
        report.setdefault("observations", observations)
        report.setdefault("inbound", inbound)
    finally:
        try:
            if binding is not None:
                driver.release(binding.child_id, root_id=root_id, branch_id=binding.branch_id, timeout=10)
            report["cleanup_state"] = "closed"
        except BaseException:
            report.update(ok=False, cleanup_error=traceback.format_exc())
        report["stage"] = "finished"
        report_path.write_text(json.dumps(report, indent=2) + "\n")


def _serve_annotation_tool(
    router: ThinkThreadArtifactViewRouter,
    state: dict,
    tool: str,
    params: dict,
) -> dict | list:
    if tool == "goal_plus_thinkthread_annotation_next":
        state["next_calls"] += 1
        if state["next_calls"] > 1:
            return {"state": "idle", "generation": 1, "revision": 1}
        return {
            "state": "claimed",
            "task": {
                "run_id": "run-probe",
                "candidate_id": "c001",
                "iteration": 1,
                "attempt": {
                    "attempt_id": "attempt-c001-1",
                    "hypothesis": "Reorder the launch schedule to raise the combined score.",
                },
                "artifact_views": [
                    {
                        "view_id": "view-target",
                        "role": "target",
                        "artifact_ref": {"kind": "fs_snapshot", "provider": "thinkthread", "id": router.views["view-target"].id},
                        "workspace_provider": "thinkthread",
                    },
                    {
                        "view_id": "view-peer",
                        "role": "peer",
                        "artifact_ref": {"kind": "fs_snapshot", "provider": "thinkthread", "id": router.views["view-peer"].id},
                        "workspace_provider": "thinkthread",
                    },
                ],
                "comparison_basis": [{"candidate_id": "c002", "iteration": 1, "relation": "peer"}],
                "supplemental_evaluation": {"required": True, "dimensions": ["correctness", "clarity"]},
            },
        }
    if tool == "goal_plus_thinkthread_annotation_submit":
        if state["submitted"] is None:
            state["submitted"] = dict(params)
        return {"published": True}
    if tool == "goal_plus_thinkthread_artifact_view_list":
        return router.readdir(str(params["view_id"]), str(params.get("path") or "."))
    if tool == "goal_plus_thinkthread_artifact_view_stat":
        return router.stat(str(params["view_id"]), str(params.get("path") or "."))
    if tool == "goal_plus_thinkthread_artifact_view_pread":
        return router.pread(
            str(params["view_id"]), str(params["path"]),
            offset=int(params.get("offset") or 0),
            length=int(params.get("length") or 64 * 1024),
        )
    return router.diff(str(params["base_view_id"]), str(params["target_view_id"]))


if __name__ == "__main__":
    if os.environ.get("GOAL_PLUS_THINKTHREAD_ROLE") == "main":
        threading.Thread(target=run_probe, args=(Path.cwd() / ".codex-annotator-report.json",), daemon=True).start()
    FastMCP("native-codex-annotator-probe").run(transport="stdio", show_banner=False)
