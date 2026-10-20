"""Circle fork evidence against offline native Pi SessionManager histories."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.st_thinkthread.fork_circle import (
    capture_fork_provider_evidence,
    native_preparation_context,
)


@pytest.mark.parametrize("stage", ["tool_result", "assistant_stop", "marked_tool_result", "prepared"])
def test_pi_preparation_waits_for_marked_assistant_completion(tmp_path, stage):
    session = tmp_path / "instances/root/thinkthreads/root/session"
    session.mkdir(parents=True)
    entries = [
        {"type": "session", "id": "root"},
        {"type": "message", "id": "calls", "parentId": None,
         "message": {"role": "assistant", "stopReason": "toolUse", "content": [
             {"type": "toolCall", "id": "status", "name": "goal_plus_set_status"},
         ]}},
        {"type": "message", "id": "result", "parentId": "calls",
         "message": {"role": "toolResult", "toolCallId": "status", "content": []}},
    ]
    if stage in {"assistant_stop", "prepared"}:
        entries.append({"type": "message", "id": "finished", "parentId": "result",
                        "message": {"role": "assistant", "content": [], "stopReason": "stop"}})
    if stage in {"marked_tool_result", "prepared"}:
        entries.append({"type": "custom", "id": "marker", "parentId": entries[-1]["id"],
                        "customType": "goal-plus-fork-checkpoint", "data": {"run_id": "run"}})
    (session / "active.jsonl").write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    context = native_preparation_context(tmp_path, "root", "pi", run_id="run")
    if stage == "prepared":
        assert context == {"checkpoint_id": "finished", "entries": entries[1:-1]}
    else:
        assert context is None


@pytest.mark.host_process
@pytest.mark.parametrize("history", ["prepared", "later_main", "inactive_prefix"])
def test_pi_circle_provider_evidence_uses_actual_active_native_prefix(tmp_path, history):
    node, pi = shutil.which("node"), shutil.which("pi")
    if node is None or pi is None:
        pytest.skip("Node and the native Pi SessionManager SDK are required")
    sdk = next((parent / "dist/core/session-manager.js" for parent in Path(pi).resolve().parents
                if (parent / "dist/core/session-manager.js").is_file()), None)
    if sdk is None:
        pytest.skip("The installed Pi SessionManager SDK is unavailable")
    # The official SDK creates and branches persisted native histories without
    # starting an agent or contacting a model provider. Expected entries come
    # directly from the SDK's preparation branch, independently of the helper.
    script = """
import { copyFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";
const [modulePath, base, history] = process.argv.slice(1);
const { SessionManager } = await import(modulePath);
const stateRoot = join(base, "provider");
const rootDir = join(stateRoot, "instances", "root", "thinkthreads", "root", "session");
mkdirSync(rootDir, { recursive: true });
const main = SessionManager.create(join(base, "source"), rootDir);
const userId = main.appendMessage({ role: "user", content: "prepare", timestamp: 1 });
const checkpointId = main.appendMessage({ role: "assistant", content: [{ type: "text", text: "prepared" }],
    api: "openai-responses", provider: "fixture", model: "fixture", stopReason: "stop", timestamp: 2 });
const entries = main.getBranch(checkpointId);
const sourceFile = main.getSessionFile();
const markerId = main.appendCustomEntry("goal-plus-fork-checkpoint", { run_id: "run" });
const snapshot = SessionManager.open(sourceFile, join(base, "prepared"));
const preparedFile = snapshot.createBranchedSession(markerId);
main.appendMessage({ role: "user", content: "resume", timestamp: 3 });
copyFileSync(sourceFile, join(rootDir, "active.jsonl"));
for (let index = 1; index <= 2; index++) {
    const childDir = join(stateRoot, "instances", "root", "thinkthreads", `child-${index}`, "session");
    mkdirSync(childDir, { recursive: true });
    const child = SessionManager.forkFrom(history === "later_main" ? sourceFile : preparedFile,
        join(base, `workspace-${index}`), childDir);
    if (history === "inactive_prefix") child.branch(userId);
    child.appendMessage({ role: "assistant", content: [{ type: "text", text: "candidate" }],
        api: "openai-responses", provider: "fixture", model: "fixture", stopReason: "stop", timestamp: 4 });
    copyFileSync(child.getSessionFile(), join(childDir, "active.jsonl"));
}
process.stdout.write(JSON.stringify({ checkpoint_id: checkpointId, entries }));
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", script, sdk.as_uri(), str(tmp_path), history],
        check=True, capture_output=True, text=True, timeout=15,
    )
    context = json.loads(result.stdout)
    runtime_root = tmp_path / "runtime"
    run_path = runtime_root / "runs/run/run.json"
    run_path.parent.mkdir(parents=True)
    group = {
        "schema_version": 2,
        "dispatch_group_id": "dispatch-group",
        "status": "started",
        "checkpoint_id": context["checkpoint_id"],
        "targets": [
        {"dispatch_target_index": index, "provider_session_id": f"child-{index}", "binding": {"workspace_attachment": {
            "fs_branch_id": f"branch-{index}", "baseline_snapshot_id": "baseline",
        }}} for index in (1, 2)
    ]}
    run_path.write_text(json.dumps({"run_id": "run", "dispatch_group": group}))
    state_root = tmp_path / "provider"
    state = {"rootId": "root", "thinkthreads": {
        f"child-{index}": {"parentId": "root", "fsBranchId": f"branch-{index}"} for index in (1, 2)
    }, "fsBranches": {
        f"branch-{index}": {"thinkthreadId": f"child-{index}", "baseSnapshotId": "baseline"} for index in (1, 2)
    }, "childSpecs": {f"child-{index}": {"fsPolicy": "private_branch"} for index in (1, 2)}}
    (state_root / "instances/root/state.json").write_text(json.dumps(state))
    if history == "prepared":
        evidence = capture_fork_provider_evidence(runtime_root, state_root, "root", "pi", context)
        assert evidence["native_prefix_entries"] == 2
        assert evidence["checkpoint_id"] == context["checkpoint_id"]
        assert {child["child_id"] for child in evidence["children"]} == {"child-1", "child-2"}
    else:
        with pytest.raises(AssertionError, match="beyond|active native prefix"):
            capture_fork_provider_evidence(runtime_root, state_root, "root", "pi", context)
