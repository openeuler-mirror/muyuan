"""Verifier provider routing, bounded RPC and Child cleanup without model calls."""

from contextlib import nullcontext
from dataclasses import replace
import hashlib
import json
import re
import time
from types import SimpleNamespace

import pytest

from goal_plus.application.verifier import VerifierWorkflow
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.verifier import verifier_score_contract
from goal_plus.execution.thinkthread_rpc import PROTOCOL
from goal_plus.infrastructure.clock import SystemClock
from goal_plus.infrastructure.inference_transcript import FileInferenceTranscript
from goal_plus.infrastructure.state.files import load_json
from goal_plus.infrastructure.state.verifier import FileVerifierState
from goal_plus.llm_verifier import LlmVerifier, VerifierExecutionUnavailable, VerifierQueue
from goal_plus.ports.inference import InferenceTranscriptIdentity, StructuredInferenceRequest
from goal_plus.ports.thinkthread import AgentPosixBridgeError
from goal_plus.thinkthread_verifier import ThinkThreadVerifier, VerifierRpc
from tests._thinkthread_sdk import IntegratedAgentPosix
from tests.test_llm_verifier import subject, verifier_input, result


def request(harness="pi"):
    return StructuredInferenceRequest(harness, "fixed rubric", "immutable evidence", {}, None, None,
                                     2, "timeout", "cancelled", runtime_provider="thinkthread")


def claimed(root, harness="pi"):
    workflow = VerifierWorkflow(FileVerifierState(root), SystemClock())
    workflow.ensure_assessment(subject(), verifier_score_contract(),
        verifier_input().model_copy(update={"agent_harness": harness}))
    return workflow, workflow.claim_task("test-worker", 60, run_id="run_1")


def envelope(session, tool, params, key="request-1"):
    content = {"agent_session_id": session, "tool": tool, "params": params,
               "deadline_unix_ms": int((time.time() + 60) * 1000)}
    encoded = json.dumps(content, separators=(",", ":"))
    return {"protocol": PROTOCOL, "type": "request", "request_id": key,
            **content, "content_json": encoded,
            "content_sha256": hashlib.sha256(encoded.encode()).hexdigest()}


class VerifierSdk(IntegratedAgentPosix):
    def __init__(self, root, harness="pi", *, fail_cleanup=False, lost_spawn=False, output=True):
        super().__init__(root, agent=harness)
        self.fail_cleanup, self.lost_spawn, self.output = fail_cleanup, lost_spawn, output

    def self_view(self):
        return {**super().self_view(), "model": {"provider": "deepseek", "model": "deepseek-flash"}}

    def invoke(self, operation, params=None, **kwargs):
        if operation == "fs.branch.remove" and self.fail_cleanup:
            raise RuntimeError("branch removal failed")
        value = super().invoke(operation, params, **kwargs)
        if operation == "thinkthread.spawn":
            child = value["thinkthreadId"]
            text = params["initialMessage"]
            session = re.search(r"agent_session_id=([^;]+)", text).group(1)
            nonce = re.search(r"registration_nonce=([^;]+)", text).group(1)
            self.enqueue(child, {"protocol": PROTOCOL, "type": "registration", "registration_nonce": nonce})
            self.enqueue(child, envelope(session, "goal_plus_thinkthread_verifier_input", {}, "input"))
            if self.output:
                self.enqueue(child, envelope(session, "goal_plus_thinkthread_verifier_submit",
                                             {"result": result().model_dump(mode="json")}))
            self.finish(child)
            if self.lost_spawn:
                self.lost_spawn = False
                raise AgentPosixBridgeError("lost spawn reply", error={"delivery": "completion_unknown"})
        return value


@pytest.mark.parametrize("harness", ["pi", "codex"])
@pytest.mark.parametrize("lost_spawn", [False, True])
def test_child_inference_releases_exact_resource_and_records_context(tmp_path, monkeypatch, harness, lost_spawn):
    workflow, task = claimed(tmp_path, harness)
    sdk = VerifierSdk(tmp_path, harness, lost_spawn=lost_spawn)
    archives = []
    monkeypatch.setattr("goal_plus.thinkthread_verifier.collect_child_diagnostics",
                        lambda root, record, client: archives.append(record["thinkthread_id"]))
    monkeypatch.setattr("goal_plus.execution.structured_inference.StructuredInference.execute",
                        lambda *a, **kw: pytest.fail("ThinkThread must not invoke a direct sandbox"))
    capture = FileInferenceTranscript(tmp_path, InferenceTranscriptIdentity(
        "verifier", task.run_id, task.task_id, task.subject.settlement_id,
        task.candidate_id, task.iteration, task.attempts))
    outcome = ThinkThreadVerifier(tmp_path, client=sdk).execute(
        task, request(harness), baseline_snapshot_id="fsnap-S0", is_active=lambda: True,
        validate=lambda payload: LlmVerifier._validate_result(task, payload), transcript=capture)
    assert outcome.payload == result().model_dump(mode="json")
    assert outcome.invocation.runtime_provider == "thinkthread"
    assert outcome.invocation.model == "deepseek/deepseek-flash"
    assert outcome.invocation.native_session_id.startswith("tt-")
    assert archives == ([outcome.invocation.native_session_id] if harness == "pi" else [])
    spawns = [params for op, params in sdk.operations if op == "thinkthread.spawn"]
    assert len(spawns) == 1
    assert spawns[0]["capabilities"] == ["thinkthread.message"]
    assert spawns[0]["fsSnapshotId"] == "fsnap-S0"
    assert all(branch["state"] == "removed" for branch in sdk.branches.values())
    assert not sdk.children
    assert load_json(capture.directory / "input.json")["prompt"] == "immutable evidence"
    observed = load_json(capture.directory / "record.json")
    assert observed["native_session_id"] == outcome.invocation.native_session_id
    assert observed["capture_status"] == "partial"
    assert observed["provider_visibility"] == "native_session_archive"
    assert workflow.list_records(task.run_id) == []


def test_cleanup_failure_blocks_later_calls_without_spawning(tmp_path):
    _, task = claimed(tmp_path)
    sdk = VerifierSdk(tmp_path, fail_cleanup=True)
    runner = ThinkThreadVerifier(tmp_path, client=sdk)
    for _ in range(2):
        with pytest.raises(VerifierExecutionUnavailable):
            runner.execute(task, request(), baseline_snapshot_id="fsnap-S0", is_active=lambda: True,
                           validate=lambda payload: LlmVerifier._validate_result(task, payload))
    assert sum(op == "thinkthread.spawn" for op, _ in sdk.operations) == 1


def test_missing_submission_cannot_produce_score_and_releases_child(tmp_path):
    _, task = claimed(tmp_path)
    sdk = VerifierSdk(tmp_path, output=False)
    with pytest.raises(ValueError, match="without a structured result"):
        ThinkThreadVerifier(tmp_path, client=sdk).execute(
            task, request(), baseline_snapshot_id="fsnap-S0", is_active=lambda: True,
            validate=lambda payload: LlmVerifier._validate_result(task, payload))
    assert not sdk.children


def test_cancelled_claim_never_spawns(tmp_path):
    _, task = claimed(tmp_path)
    sdk = VerifierSdk(tmp_path)
    with pytest.raises(PermissionError, match="claim"):
        ThinkThreadVerifier(tmp_path, client=sdk).execute(
            task, request(), baseline_snapshot_id="fsnap-S0", is_active=lambda: False,
            validate=lambda _: pytest.fail("cancelled"))
    assert sdk.operations == []


def test_timeout_releases_child_without_a_result(tmp_path, monkeypatch):
    _, task = claimed(tmp_path)
    sdk = VerifierSdk(tmp_path)
    monkeypatch.setattr("goal_plus.execution.thinkthread_child.ChildDriver.turn_terminal", lambda *args: None)
    with pytest.raises(TimeoutError):
        ThinkThreadVerifier(tmp_path, client=sdk).execute(
            task, replace(request(), timeout_seconds=0.03), baseline_snapshot_id="fsnap-S0",
            is_active=lambda: True, validate=lambda payload: LlmVerifier._validate_result(task, payload))
    assert not sdk.children


def test_invalid_root_profile_fails_before_spawn(tmp_path):
    _, task = claimed(tmp_path)
    sdk = VerifierSdk(tmp_path, harness="codex")
    with pytest.raises(VerifierExecutionUnavailable, match="select the pi"):
        ThinkThreadVerifier(tmp_path, client=sdk).execute(
            task, request(), baseline_snapshot_id="fsnap-S0", is_active=lambda: True,
            validate=lambda payload: LlmVerifier._validate_result(task, payload))
    assert not any(op == "thinkthread.spawn" for op, _ in sdk.operations)


def test_rpc_rejects_cross_role_stale_session_and_conflicting_result(tmp_path):
    _, task = claimed(tmp_path)
    state = {"agent_session_id": "verifier-session", "thinkthread_id": "tt-verifier", "registered": True}
    rpc = VerifierRpc(tmp_path, VerifierSdk(tmp_path), SystemClock(), state, request(),
                      lambda payload: LlmVerifier._validate_result(task, payload), nullcontext, lambda _: None)
    for session, tool in [("wrong-session", "goal_plus_thinkthread_verifier_input"),
                          ("verifier-session", "goal_plus_search_run_verifier")]:
        with pytest.raises(PermissionError):
            rpc.process({"text": json.dumps(envelope(session, tool, {}))})
    assert "submitted_result" not in state
    value = result().model_dump(mode="json")
    assert rpc.dispatch("goal_plus_thinkthread_verifier_submit", {"result": value}) == {"accepted": True}
    assert rpc.dispatch("goal_plus_thinkthread_verifier_submit", {"result": value}) == {"accepted": True}
    with pytest.raises(ValueError, match="already submitted"):
        rpc.dispatch("goal_plus_thinkthread_verifier_submit", {"result": {**value, "quality_score": 0.1}})
    with pytest.raises(ValueError):
        rpc.dispatch("goal_plus_thinkthread_verifier_submit", {"result": {**value, "quality_score": 2}})


@pytest.mark.parametrize("provider", ["direct", "thinkthread"])
def test_binding_uses_frozen_provider_not_environment(tmp_path, monkeypatch, provider):
    _, task = claimed(tmp_path)
    runner = LlmVerifier(tmp_path)
    baseline = ArtifactRef(kind="fs_snapshot", provider="thinkthread", id="fsnap-S0")
    if provider == "thinkthread":
        task.subject.settlement.attempt_artifact_ref = baseline
    runner.source = SimpleNamespace(
        read_run=lambda _: SimpleNamespace(frozen_spec_id="frozen_1", source_artifact_ref=baseline),
        read_frozen_spec=lambda _: SimpleNamespace(frozen_spec_id="frozen_1", agent_harness="pi", runtime_provider=provider))
    monkeypatch.setenv("THINKTHREAD_ID", "tt-untrusted-environment")
    assert runner._execution_binding(task)[0] == provider
    task.input.agent_harness = "codex"
    with pytest.raises(VerifierExecutionUnavailable):
        runner._execution_binding(task)


def test_environment_failure_is_not_retried(tmp_path):
    from goal_plus.execution.sandbox import SandboxUnavailable
    workflow = VerifierWorkflow(FileVerifierState(tmp_path), SystemClock())
    task = workflow.ensure_assessment(subject(), verifier_score_contract(), verifier_input())

    def fail(_):
        raise SandboxUnavailable("missing bwrap")

    queue = VerifierQueue(tmp_path, task.run_id, lambda _: None,
                          workflow=workflow, runner=SimpleNamespace(execute=fail))
    assert queue.step()
    failed = workflow.get_task(task.task_id)
    assert failed.state == "abandoned" and failed.attempts == 1


@pytest.mark.parametrize("harness", ["pi", "codex"])
def test_llm_verifier_routes_thinkthread_without_direct_registration(tmp_path, monkeypatch, harness):
    _, task = claimed(tmp_path, harness)
    baseline = ArtifactRef(kind="fs_snapshot", provider="thinkthread", id="fsnap-S0")
    task.subject.settlement.attempt_artifact_ref = baseline
    runner = LlmVerifier(tmp_path)
    runner.source = SimpleNamespace(
        read_run=lambda _: SimpleNamespace(frozen_spec_id="frozen_1", source_artifact_ref=baseline),
        read_frozen_spec=lambda _: SimpleNamespace(frozen_spec_id="frozen_1", agent_harness=harness,
                                                 runtime_provider="thinkthread"))
    runner.thinkthread_inference = ThinkThreadVerifier(tmp_path, client=VerifierSdk(tmp_path, harness))
    monkeypatch.setattr(runner.inference, "execute", lambda *a, **kw: pytest.fail("direct inference"))
    monkeypatch.setattr("goal_plus.bootstrap.build_inference_registration",
                        lambda *a, **kw: pytest.fail("local process registration"))
    parsed, invocation, _ = runner.execute(task)
    assert parsed == result()
    assert invocation.runtime_provider == "thinkthread"
    assert invocation.native_session_id and invocation.native_invocation_id


def test_codex_verifier_mcp_only_exposes_input_and_submit(monkeypatch):
    import asyncio
    from fastmcp import Client
    from goal_plus.infrastructure.thinkthread_codex_tools import _verifier_mcp

    calls = []
    transport = SimpleNamespace(invoke=lambda operation, params, **kwargs: calls.append(params) or {"toolResult": {"accepted": True}})
    monkeypatch.setattr("goal_plus.server.native_mcp_caller", lambda: "native-verifier")
    mcp = _verifier_mcp(transport, "verifier-task")

    async def scenario():
        async with Client(mcp) as client:
            assert {tool.name for tool in await client.list_tools()} == {
                "goal_plus_thinkthread_verifier_input", "goal_plus_thinkthread_verifier_submit"}
            await client.call_tool("goal_plus_thinkthread_verifier_input", {})
            await client.call_tool("goal_plus_thinkthread_verifier_submit", {"result": result().model_dump(mode="json")})
    asyncio.run(scenario())
    assert all(call["agent_session_id"] == "verifier-task" for call in calls)


def test_pi_verifier_activates_only_its_tools_and_blocks_native_tools():
    from pathlib import Path
    import subprocess

    extension = Path(__file__).resolve().parents[1] / "assets/pi/extensions/goal-plus.ts"
    checked = subprocess.run(["node", "--input-type=module", "-e", r'''
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {stripTypeScriptTypes} from "node:module";
import vm from "node:vm";
const source = readFileSync(process.argv[1], "utf8");
const constants = source.slice(source.indexOf("const THINKTHREAD_ANNOTATION_TOOL_NAMES"),
    source.indexOf("interface ThinkThreadInnerGitBinding"));
const scope = source.slice(source.indexOf("function scopeThinkThreadWorkerTools("),
    source.indexOf("async function acknowledgeThinkThreadDispatch("));
const guard = source.slice(source.indexOf("function workspaceGuard("), source.indexOf("async function mainGate("));
const context = {isThinkThreadWorker: true, role: "worker", workerGeneration: undefined,
    latestDispatchMetadata: () => ({thinkthreadRole: "verifier"})};
vm.createContext(context);
vm.runInContext(stripTypeScriptTypes(constants + scope + guard), context);
let active;
context.scopeThinkThreadWorkerTools({setActiveTools: tools => active = [...tools]}, {});
assert.deepEqual(active, ["goal_plus_thinkthread_verifier_input", "goal_plus_thinkthread_verifier_submit"]);
for (const toolName of active) assert.equal(context.workspaceGuard({toolName}, {}, "goal-plus"), undefined);
for (const toolName of ["bash", "read", "write", "edit", "goal_plus_search_run_verifier",
                       "goal_plus_thinkthread_annotation_next"]) {
    for (const owner of ["native", "goal-plus"]) {
        assert.equal(context.workspaceGuard({toolName}, {}, owner).block, true);
    }
}
''', str(extension)], capture_output=True, text=True, timeout=15)
    assert checked.returncode == 0, checked.stdout + checked.stderr
