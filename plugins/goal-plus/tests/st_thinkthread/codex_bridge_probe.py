"""Native Root MCP startup probe; only the Child performs model work."""

import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback
import uuid

from fastmcp import FastMCP

from goal_plus.execution.thinkthread_child import ChildDriver, TURN_QUIESCENCE_SECONDS
from goal_plus.execution.thinkthread_rpc import PROTOCOL, message_payload, request_hash, response_payload
from goal_plus.infrastructure.clock import SystemClock
from goal_plus.thinkthread_agent_posix import AgentPosixSdkClient, new_request_id, validate_thinkthread_root


def run_probe(report_path: Path) -> None:
    first_dispatch = sys.argv[1] if len(sys.argv) > 1 else "initial_message"
    report = {"stage": "root_start", "ok": False, "first_dispatch": first_dispatch}
    report_path.write_text(json.dumps(report))
    client = AgentPosixSdkClient()
    driver = ChildDriver(client, SystemClock())
    binding = None
    root_id = None
    try:
        root = validate_thinkthread_root(client, agent_harness="codex")
        root_id = root["thinkthreadId"]
        report["root_id"] = root_id
        baseline = client.invoke("fs.snapshot.create", {"requestId": new_request_id()})["snapshotId"]
        registration = str(uuid.uuid4())
        rounds = []
        cursor = None
        for index in (1, 2):
            nonce = str(uuid.uuid4())
            secret = str(uuid.uuid4())
            filename = f"round-{index}.txt"
            prompt = (
                "This is a minimal MCP transport check. Call the Goal Plus MCP tool "
                "goal_plus_search_get_agent_context with agent_session_id=agent-probe. "
                f"Write exactly the returned probe_token to {filename}, then finish. "
                "Use the MCP tool directly; do not run Python or Node to call it. "
                "No verifier or other Goal Plus operation is needed.\n"
                f"agent_session_id=agent-probe; registration_nonce={registration}; dispatch_nonce={nonce}."
            )
            if binding is None:
                params = {
                    "profile": "self", "fs": "private", "fsSnapshotId": baseline,
                    "capabilities": ["thinkthread.message"],
                }
                if first_dispatch == "initial_message":
                    params["initialMessage"] = prompt
                binding = driver.binding(driver.spawn(params))
                if first_dispatch == "separate_wake":
                    deadline = time.monotonic() + 60
                    while time.monotonic() < deadline:
                        child = driver.observe(binding.child_id, root_id=root_id, branch_id=binding.branch_id)
                        if child and child.get("agentState") == "ready" and not child.get("pendingWake"):
                            report["before_first_wake"] = {
                                key: child.get(key) for key in ("agentState", "executionState", "pendingWake")
                            }
                            break
                        time.sleep(0.2)
                    else:
                        raise TimeoutError("Empty Codex Child did not reach its native ready state")
                    message_id = driver.wake(binding.child_id, prompt)
                else:
                    message_id = binding.initial_message_id
            else:
                message_id = driver.wake(binding.child_id, prompt)
            report.update(stage=f"round_{index}", child_id=binding.child_id)
            report_path.write_text(json.dumps(report))
            deadline = time.monotonic() + 240
            requested, acknowledged = set(), False
            while time.monotonic() < deadline:
                page = client.invoke("message.receive", {
                    "senderThinkthreadId": binding.child_id, "limit": 32,
                    **({"after": cursor} if cursor else {}),
                })
                cursor = page["nextCursor"]
                for message in page["messages"]:
                    assert message["senderThinkthreadId"] == binding.child_id
                    payload = message_payload(message)
                    if not payload or payload.get("protocol") != PROTOCOL:
                        continue
                    if payload.get("type") == "dispatch_ack":
                        if payload.get("dispatch_nonce") == nonce:
                            assert payload["message_id"] == message_id
                            acknowledged = True
                    if payload.get("type") != "request":
                        continue
                    assert acknowledged, "Tool request arrived before its native dispatch acknowledgement"
                    assert payload["agent_session_id"] == "agent-probe"
                    assert payload["tool"] == "goal_plus_search_get_agent_context"
                    assert payload["params"] == {"agent_session_id": "agent-probe"}
                    assert request_hash(payload) == payload["content_sha256"]
                    requested.add(payload["request_id"])
                    for chunk in response_payload(payload["request_id"], {"ok": True, "result": {"probe_token": secret}}):
                        client.invoke("message.send", {
                            "recipientThinkthreadId": binding.child_id, "text": chunk, "wake": False,
                            "replyToMessageId": message["messageId"],
                        })
                child = driver.observe(binding.child_id, root_id=root_id, branch_id=binding.branch_id)
                outcome = child.get("lastWakeOutcome") or {}
                if outcome.get("messageId") == message_id and outcome.get("outcome") == "completed":
                    finished = outcome.get("finishedAtUnixMs", 0) / 1000
                    if time.time() - finished >= TURN_QUIESCENCE_SECONDS:
                        assert requested and acknowledged
                        break
                time.sleep(0.2)
            else:
                raise TimeoutError(f"Codex Child round {index} did not finish its MCP task")
            stopped = driver.interrupt(binding.child_id, 10)
            assert stopped and stopped["executionState"] == "absent"
            snapshot = client.invoke("fs.branch.snapshot", {
                "branchId": binding.branch_id, "requestId": new_request_id(),
            })["snapshotId"]
            content = client.snapshot_read_file(snapshot, filename, max_bytes=1024).decode().strip()
            assert content == secret, "Native Child did not consume the exact MCP response"
            assert not Path(filename).exists(), "Child changed Root workspace"
            rounds.append({"message_id": message_id, "requests": sorted(requested), "snapshot_id": snapshot})
        report.update(rounds=rounds, ok=True)
    except BaseException:
        report["error"] = traceback.format_exc()
    finally:
        try:
            if binding is not None:
                driver.release(binding.child_id, root_id=root_id, branch_id=binding.branch_id, timeout=10)
            report["cleanup_state"] = "closed"
        except BaseException:
            report.update(ok=False, cleanup_error=traceback.format_exc())
        report["stage"] = "finished"
        report_path.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    if os.environ.get("GOAL_PLUS_THINKTHREAD_ROLE") == "main":
        threading.Thread(target=run_probe, args=(Path.cwd() / ".codex-bridge-report.json",), daemon=True).start()
    FastMCP("native-codex-bridge-probe").run(transport="stdio")
