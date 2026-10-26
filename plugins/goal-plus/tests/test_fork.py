import json
import math
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from goal_plus.application.evidence_reads import global_evidence_entry
from goal_plus.domain.evidence import IterationRecord
from goal_plus.domain.search import (
    CandidateOrigin,
    CandidateProposal,
    CandidateTask,
    StrategySpec,
    UnsupportedDispatchState,
)
from goal_plus.execution.fork import DispatchGroup
from goal_plus.execution.pi_checkpoint import (
    completed_checkpoint_prefix,
    select_completed_checkpoint,
)
from goal_plus.infrastructure.state.candidate_queries import FileCandidateQueries
from goal_plus.ports.session import (
    ParentCheckpoint,
    ProviderDispatchReceipt,
    ProviderDispatchRequest,
)


def _request(index: int, workspace: Path) -> ProviderDispatchRequest:
    return ProviderDispatchRequest(
        dispatch_group_id="fork-group",
        dispatch_target_index=index,
        parent_checkpoint=ParentCheckpoint(
            parent_native_session_id="main-session",
            checkpoint_id="turn-7",
            checkpoint_kind="completed_turn",
            frozen_spec_id="spec_123",
        ),
        workspace=workspace,
        worker_identity={"agent_session_id": f"agent-{index}"},
        budget={"max_turns": 3},
        tool_capabilities=("goal_plus_search_get_agent_context",),
    )


def test_provider_dispatch_dtos_accept_generic_identity_names(tmp_path: Path) -> None:
    checkpoint = ParentCheckpoint("main-session", "turn-7", "completed_turn", "spec_123")
    request = ProviderDispatchRequest(
        dispatch_group_id="dispatch-group",
        dispatch_target_index=1,
        parent_checkpoint=checkpoint,
        workspace=tmp_path / "one",
        worker_identity={"agent_session_id": "agent-1"},
        budget={"max_turns": 3},
    )
    assert (request.dispatch_group_id, request.dispatch_target_index, request.parent_checkpoint) == (
        "dispatch-group", 1, checkpoint
    )
    receipt = ProviderDispatchReceipt(
        provider_session_id="branch-1",
        parent_native_session_id="main-session",
        workspace=tmp_path / "one",
        launch_state="started",
    )
    assert receipt.provider_session_id == "branch-1"


def test_old_dispatch_state_is_rejected_before_schema_defaults(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps({"run_id": "run-1", "fork": {"fork_group_id": "old"}}),
        encoding="utf-8",
    )
    with pytest.raises(UnsupportedDispatchState, match="legacy top-level fork"):
        FileCandidateQueries(tmp_path).read_run("run-1")


def test_old_dispatch_audit_field_is_rejected_before_verifier_defaults() -> None:
    from goal_plus.domain.verifier import VerifierInput

    with pytest.raises(ValueError, match="legacy fork_audit_context"):
        VerifierInput.model_validate({"fork_audit_context": {}})


def test_fork_mode_requires_explicit_llm_policy_only_in_fork_mode() -> None:
    strategy = StrategySpec(initial_context="fork", llm_validation="required")
    assert strategy.llm_validation == "required"
    with pytest.raises(ValueError, match="initial_context=fork"):
        StrategySpec(initial_context="fresh", llm_validation="required")


def test_fork_group_cleans_started_branches_after_partial_failure(tmp_path: Path) -> None:
    requests = [_request(1, tmp_path / "one"), _request(2, tmp_path / "two")]
    cleaned: list[str] = []

    class DispatchProvider:
        def __init__(self) -> None:
            self.calls = 0

        def fork(self, request, *, timeout):
            self.calls += 1
            if self.calls == 2:
                return ProviderDispatchReceipt(
                    None, "main-session", request.workspace, "failed", failure="spawn failed"
                )
            return ProviderDispatchReceipt(
                f"branch-{request.dispatch_target_index}", "main-session", request.workspace, "started"
            )

    with pytest.raises(RuntimeError, match="spawn failed"):
        DispatchGroup(DispatchProvider(), cleanup=lambda receipt: cleaned.append(receipt.provider_session_id)).start(
            requests, timeout=5
        )
    assert cleaned == ["branch-1"]


def test_candidate_origin_and_public_metrics_preserve_identity_and_json_types(tmp_path: Path) -> None:
    binding = CandidateOrigin(
        dispatch_group_id="fork-group",
        dispatch_target_index=1,
        checkpoint_id="turn-7",
        checkpoint_kind="completed_turn",
        parent_native_session_id="main-session",
        provider_session_id="branch-1",
        frozen_spec_id="spec_123",
        spec_hash="a" * 64,
        workspace=tmp_path / "one",
        worker_identity={"agent_session_id": "agent-1"},
    )
    task = CandidateTask(
        run_id="run-1",
        candidate_id="c001",
        hypothesis="try branch",
        workspace=tmp_path / "one",
        allowed_files=["program.py"],
        denied_files=[],
        candidate_origin=binding,
    )
    assert task.candidate_origin == binding
    with pytest.raises(ValueError, match="workspace"):
        CandidateTask.model_validate(
            {**task.model_dump(mode="python"), "workspace": tmp_path / "other"}
        )

    iteration = IterationRecord(
        iteration=1,
        score=0,
        process_passed=True,
        metrics={
            "verifier_a": {
                "zero": 0,
                "disabled": False,
                "label": "ok",
                "_coverage": 1,
                "labels": {"_id": "business"},
                "nan": math.nan,
            },
            "_candidate_attempt": {"id": "private"},
        },
        created_at="2026-10-14T00:00:00Z",
    )
    entry = global_evidence_entry("c001", iteration, None, frozen_spec_id="spec_123")
    assert entry["frozen_spec_id"] == "spec_123"
    assert entry["metrics"] == {"verifier_a": {
        "zero": 0, "disabled": False, "label": "ok", "_coverage": 1,
        "labels": {"_id": "business"},
    }}
    assert entry["metrics_omitted"] is True


def test_pi_checkpoint_uses_active_leaf_ancestry_not_entry_order() -> None:
    entries = [
        {"id": "root", "parentId": None, "type": "session"},
        {"id": "a", "parentId": "root", "type": "message",
         "message": {"role": "assistant", "stopReason": "stop", "content": []}},
        {"id": "a-child", "parentId": "a", "type": "message",
         "message": {"role": "user", "content": [{"type": "text", "text": "next"}]}},
        {"id": "b", "parentId": "root", "type": "message",
         "message": {"role": "assistant", "stopReason": "stop", "content": []}},
        {"id": "marker-b", "parentId": "b", "type": "custom",
         "customType": "goal-plus-fork-checkpoint", "data": {"run_id": "run-1"}},
    ]
    for run_id in (None, "run-1"):
        assert select_completed_checkpoint(entries, "a", run_id=run_id) == "a"
        assert select_completed_checkpoint(entries, "a-child", run_id=run_id) == "a"
    assert select_completed_checkpoint(entries, "marker-b", run_id="run-1") == "b"


@pytest.mark.parametrize("leaf,expected", [("first-result", "prepared"), ("result", "result"), ("later", "later")])
def test_pi_checkpoint_selects_nearest_completed_boundary(leaf, expected) -> None:
    entries = [
        {"id": "root", "parentId": None, "type": "session"},
        {"id": "prepared", "parentId": "root", "type": "message",
         "message": {"role": "assistant", "stopReason": "stop", "content": []}},
        {"id": "call", "parentId": "prepared", "type": "message",
         "message": {"role": "assistant", "stopReason": "toolUse",
                      "content": [{"type": "toolCall", "id": "call-1"},
                                  {"type": "toolCall", "id": "call-2"}]}},
        {"id": "first-result", "parentId": "call", "type": "message",
         "message": {"role": "toolResult", "toolCallId": "call-1", "content": []}},
        {"id": "result", "parentId": "first-result", "type": "message",
         "message": {"role": "toolResult", "toolCallId": "call-2",
                      "content": [{"type": "text", "text": "done"}]}},
        {"id": "later", "parentId": "result", "type": "message",
         "message": {"role": "assistant", "stopReason": "stop", "content": []}},
    ]
    assert select_completed_checkpoint(entries, leaf) == expected


def test_pi_checkpoint_marker_freezes_direct_fork_boundary() -> None:
    entries = [
        {"id": "root", "parentId": None, "type": "session"},
        {"id": "prepared", "parentId": "root", "type": "message",
         "message": {"role": "assistant", "stopReason": "stop", "content": []}},
        {"id": "marker", "parentId": "prepared", "type": "custom",
         "customType": "goal-plus-fork-checkpoint",
         "data": {"version": 1, "run_id": "run-1"}},
        {"id": "later", "parentId": "marker", "type": "message",
         "message": {"role": "assistant", "stopReason": "stop", "content": []}},
    ]
    assert select_completed_checkpoint(entries, "later", run_id="run-1") == "prepared"
    assert completed_checkpoint_prefix(entries, "later", run_id="run-1") == ("prepared", entries[:2])
    assert select_completed_checkpoint(entries, "later") == "later"


@pytest.mark.parametrize("run_id", [None, "run-1"])
@pytest.mark.parametrize("malformation,reason", [
    ("missing", "active leaf is missing"),
    ("unknown", "active leaf is unknown"),
    ("broken", "ancestry is broken"),
    ("duplicate", "entry ids are ambiguous"),
])
def test_pi_checkpoint_rejects_invalid_history_before_freezing_marker(run_id, malformation, reason) -> None:
    entries = [
        {"id": "prepared", "parentId": None, "type": "message",
         "message": {"role": "assistant", "stopReason": "stop", "content": []}},
        {"id": "marker", "parentId": "prepared", "type": "custom",
         "customType": "goal-plus-fork-checkpoint", "data": {"run_id": "run-1"}},
    ]
    leaf = None if malformation == "missing" else "leaf"
    if malformation == "broken":
        entries.append({"id": "leaf", "parentId": "gone", "type": "message"})
    elif malformation == "duplicate":
        entries.append({"id": "prepared", "parentId": "marker", "type": "message"})
        leaf = "prepared"
    with pytest.raises(RuntimeError, match=reason):
        completed_checkpoint_prefix(entries, leaf, run_id=run_id)


@pytest.mark.parametrize("checkpoint,accepted", [("result", True), ("first-result", False), ("later", True)])
def test_pi_fork_helper_materializes_only_completed_boundaries(tmp_path, checkpoint, accepted) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the Pi fork helper protocol")
    # This controlled SDK persists whichever prefix it receives; completion
    # admission remains entirely in the production helper under test.
    sdk = tmp_path / "session-sdk.mjs"
    sdk.write_text('''
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
export class SessionManager {
  static open(path, sessionDir) {
    return new SessionManager(JSON.parse(readFileSync(path, "utf8")), path, sessionDir);
  }
  constructor(data, path, sessionDir) {
    this.data = data; this.path = path; this.sessionDir = sessionDir;
  }
  getSessionId() { return this.data.id; }
  getSessionFile() { return this.path; }
  getEntry(id) { return this.data.entries.find(entry => entry.id === id); }
  getBranch(id) {
    return this.data.entries.slice(0, this.data.entries.findIndex(entry => entry.id === id) + 1);
  }
  createBranchedSession(id) {
    this.data.entries = this.getBranch(id);
    const path = join(this.sessionDir, "snapshot.json");
    writeFileSync(path, JSON.stringify(this.data));
    return path;
  }
  buildSessionContext() { return { messages: this.data.entries.map(entry => entry.message) }; }
  static forkFrom(source, cwd, sessionDir, options) {
    const data = JSON.parse(readFileSync(source, "utf8"));
    data.id = options.id; data.cwd = cwd;
    const path = join(sessionDir, "branch.json");
    writeFileSync(path, JSON.stringify(data));
    return SessionManager.open(path, sessionDir);
  }
}
''', encoding="utf-8")
    entries = [
        {"id": "call", "parentId": None, "type": "message",
         "message": {"role": "assistant", "stopReason": "toolUse", "content": [
             {"type": "toolCall", "id": "call-1"}, {"type": "toolCall", "id": "call-2"}]}},
        {"id": "first-result", "parentId": "call", "type": "message",
         "message": {"role": "toolResult", "toolCallId": "call-1", "content": []}},
        {"id": "result", "parentId": "first-result", "type": "message",
         "message": {"role": "toolResult", "toolCallId": "call-2", "content": []}},
        {"id": "later", "parentId": "result", "type": "message",
         "message": {"role": "assistant", "stopReason": "stop", "content": []}},
    ]
    source = tmp_path / "parent.json"
    source.write_text(json.dumps({"id": "parent", "entries": entries}), encoding="utf-8")
    helper = Path(__file__).resolve().parents[1] / "assets/pi/extensions/lib/fork.mjs"
    result = subprocess.run(
        [node, str(helper)],
        input=json.dumps({"id": "request", "sourcePath": str(source), "sessionDir": str(tmp_path),
                          "parentSessionId": "parent", "checkpointId": checkpoint,
                          "targetCwd": str(tmp_path / "workspace"), "sessionId": "child"}) + "\n",
        text=True, capture_output=True, check=True, timeout=10,
        env={**os.environ, "GP_PI_MODULE": sdk.as_uri()},
    )
    response = json.loads(result.stdout)
    assert response["id"] == "request"
    if accepted:
        assert "error" not in response
        assert response["result"]["sessionId"] == "child"
        assert response["result"]["checkpointId"] == checkpoint
        branch = json.loads(Path(response["result"]["sessionFile"]).read_text(encoding="utf-8"))
        assert [entry["id"] for entry in branch["entries"]] == (
            ["call", "first-result", "result"] if checkpoint == "result"
            else ["call", "first-result", "result", "later"]
        )
    else:
        assert "completed" in response["error"]
        assert not (tmp_path / "branch.json").exists()


@pytest.mark.parametrize("mismatch", ["parent", "workspace", "duplicate", "checkpoint_kind"])
def test_fork_group_rejects_inconsistent_receipts_and_cleans_created_branches(tmp_path, mismatch):
    requests = [_request(1, tmp_path / "one"), _request(2, tmp_path / "two")]
    if mismatch == "checkpoint_kind":
        requests[1] = replace(requests[1], parent_checkpoint=replace(requests[1].checkpoint, checkpoint_kind="entry"))
    created, cleaned = [], []

    class DispatchProvider:
        def fork(self, request, *, timeout):
            receipt = ProviderDispatchReceipt(
                f"branch-{1 if mismatch == 'duplicate' else request.dispatch_target_index}",
                "wrong-parent" if mismatch == "parent" else "main-session",
                tmp_path / "wrong" if mismatch == "workspace" else request.workspace,
                "started",
            )
            created.append(receipt)
            return receipt

    with pytest.raises((ValueError, RuntimeError), match="fork"):
        DispatchGroup(DispatchProvider(), cleanup=cleaned.append).start(requests, timeout=5)
    if mismatch == "checkpoint_kind":
        assert not created
    else:
        assert cleaned
        assert {r.provider_session_id for r in cleaned} == {r.provider_session_id for r in created}


@pytest.fixture
def fork_runtime(tmp_path, monkeypatch):
    from goal_plus.goal_plus import FileGoalPlusRuntime
    from goal_plus.host_scope import ensure_execution_scope
    from goal_plus.infrastructure.direct_fork import DirectDispatchAdapter
    from goal_plus.runtime import FileSearchRuntime
    from goal_plus.session_host import NativeCandidateHost
    from tests._runtime_helpers import make_project, spec_for, start_host_verified_goal

    project = make_project(tmp_path)
    root = tmp_path / ".gp"
    goal = start_host_verified_goal(root, project)
    runtime = FileSearchRuntime(root)
    spec = spec_for(project, max_parallel=2)
    spec.workspace.provider = "git_worktree"
    spec.strategy = StrategySpec(initial_context="fork", llm_validation="required")
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"], goal_plus_id=goal.goal_plus_id)
    run_id = runtime.create_run(frozen.frozen_spec_id)
    FileGoalPlusRuntime(root).link_search_run(goal.goal_plus_id, frozen.frozen_spec_id, run_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    candidates = runtime.start_batch(run_id, plan.plan_id, [CandidateProposal(intent="first"), CandidateProposal(intent="second")])
    ensure_execution_scope(root_dir=root, agent_harness="codex", owner_session_id=goal.active_session.session_id,
                           run_id=run_id, activation_id=f"{goal.goal_plus_id}:{goal.control.version}",
                           native_agent_config={"model": "test-model", "reasoning_effort": "low"})
    host = NativeCandidateHost(root, agent_harness="codex", caller=lambda: goal.active_session.session_id)
    forks = []

    def prepare_checkpoint(adapter, run_id, caller_id, checkpoint_id="latest_completed"):
        run = adapter.runtime.search_run.state.read_run(run_id)
        return ParentCheckpoint(
            parent_native_session_id=caller_id, checkpoint_id="turn-7", checkpoint_kind="completed_turn",
            frozen_spec_id=run.frozen_spec_id, source_artifact_ref=run.source_artifact_ref,
            runtime_provider="direct", main_session_id=caller_id,
        )

    def fork(adapter, requests, *, timeout, on_receipt=None):
        receipts = []
        for request in requests:
            receipt = ProviderDispatchReceipt(
                f"branch-{request.dispatch_target_index}", request.checkpoint.parent_native_session_id,
                request.workspace, "started",
            )
            forks.append(receipt)
            receipts.append(receipt)
            if on_receipt:
                on_receipt(request, receipt)
        return tuple(receipts)

    monkeypatch.setattr(DirectDispatchAdapter, "start_group", fork)
    monkeypatch.setattr(DirectDispatchAdapter, "prepare_checkpoint", prepare_checkpoint)
    return runtime, run_id, candidates, host, forks


def test_fork_latest_checkpoint_replay_does_not_fork_again(fork_runtime):
    runtime, run_id, candidates, host, forks = fork_runtime
    first = host.fork_search_run(run_id)
    assert host.fork_search_run(run_id) == first
    assert len(forks) == 2


def test_prepared_dispatch_group_restarts_provider_after_prelaunch_crash(
    fork_runtime, monkeypatch,
):
    """A crash before the provider fence leaves a safely retryable group."""
    from goal_plus.infrastructure.direct_fork import DirectDispatchAdapter

    runtime, run_id, _, host, forks = fork_runtime
    calls = 0

    # Keep the fixture's provider behavior while making the first adapter call
    # fail before it can invoke any provider operation or receipt callback.
    def provider_after_crash(
        adapter, requests, *, timeout, on_receipt=None, on_submitted=None, on_unknown=None,
    ):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated pre-provider crash")
        receipts = []
        for request in requests:
            receipt = ProviderDispatchReceipt(
                f"branch-{request.dispatch_target_index}",
                request.checkpoint.parent_native_session_id,
                request.workspace,
                "started",
            )
            forks.append(receipt)
            receipts.append(receipt)
            if on_receipt:
                on_receipt(request, receipt)
        return tuple(receipts)

    monkeypatch.setattr(DirectDispatchAdapter, "start_group", provider_after_crash)
    with pytest.raises(RuntimeError, match="simulated pre-provider crash"):
        host.fork_search_run(run_id)

    prepared = runtime.search_run.state.read_run(run_id).dispatch_group
    assert prepared is not None
    assert prepared.status == "prepared"
    assert [target.launch_state for target in prepared.targets] == ["prepared", "prepared"]
    assert all(not target.binding.launch_receipt for target in prepared.targets)
    assert calls == 1
    assert forks == []

    restarted = host.fork_search_run(run_id)
    assert restarted["status"] == "started"
    assert calls == 2
    assert len(forks) == len(prepared.targets) == 2
    started = runtime.search_run.state.read_run(run_id).dispatch_group
    assert started is not None
    assert started.status == "started"
    assert all(target.launch_state == "started" for target in started.targets)
    assert all(target.binding.launch_receipt is not None for target in started.targets)


def test_fork_monitor_distinguishes_preparation_from_launched_group(fork_runtime):
    from goal_plus.monitor import goal_plus_monitor_snapshot

    runtime, run_id, _, host, _ = fork_runtime
    prepared = goal_plus_monitor_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    assert prepared["strategy"]["initial_context"] == "fork"
    assert prepared["strategy"]["latest_plan"]["status"] == "started"
    assert prepared["run"]["dispatch_group"] is None

    host.fork_search_run(run_id)
    launched = goal_plus_monitor_snapshot(root_dir=runtime.root_dir, run_id=run_id)
    assert launched["run"]["dispatch_group"]["status"] == "started"
    assert launched["run"]["dispatch_group"]["checkpoint_id"] == "turn-7"
    assert launched["run"]["dispatch_group"]["checkpoint_kind"] == "completed_turn"
    fork = launched["run"]["dispatch_group"]
    assert fork["runtime_provider"] == "direct"
    assert fork["main_session_id"] == fork["parent_native_session_id"]
    assert len(fork["targets"]) == 2
    assert fork["targets"][0]["provider_session_id"] == "branch-1"
    assert fork["targets"][0]["audit_state"] == "pending"
    assert fork["targets"][0]["binding"]["launch_receipt"]["launch_state"] == "started"
    assert launched["subagents"][0]["candidate_origin"]["checkpoint_id"] == "turn-7"


def test_fork_closes_only_after_every_exact_branch_receipt(fork_runtime):
    from goal_plus.application.candidate_execution import require_fork_ready

    runtime, run_id, _, host, _ = fork_runtime
    host.fork_search_run(run_id)
    state = runtime.candidate_sessions.state
    fork = state.read_run(run_id).fork
    first, second = fork.branches

    runtime.candidate_sessions.record_dispatch_close(
        first.agent_session_id, first.provider_session_id,
    )
    partly_closed = state.read_run(run_id).fork
    assert partly_closed.status == "started"
    assert partly_closed.branches[0].closed_at is not None
    assert partly_closed.branches[1].closed_at is None
    runtime.candidate_sessions.record_dispatch_close(
        first.agent_session_id, first.provider_session_id,
    )
    assert state.read_run(run_id).fork == partly_closed

    runtime.candidate_sessions.record_dispatch_close(
        second.agent_session_id, second.provider_session_id,
    )
    closed = state.read_run(run_id)
    assert closed.fork.status == "closed"
    assert all(branch.closed_at is not None for branch in closed.fork.branches)
    assert [branch.binding for branch in closed.fork.branches] == [
        branch.binding for branch in fork.branches
    ]
    assert [branch.audit_state for branch in closed.fork.branches] == [
        branch.audit_state for branch in fork.branches
    ]
    require_fork_ready(
        closed, state.read_frozen_spec(closed.frozen_spec_id), state.read_candidates(run_id),
    )
    runtime.candidate_sessions.record_dispatch_close(
        second.agent_session_id, second.provider_session_id,
    )
    assert state.read_run(run_id) == closed


@pytest.mark.parametrize("mismatch", ["native_id", "group_id", "provider_session_id", "candidate_id"])
def test_fork_close_rejects_wrong_identity_without_mutation(fork_runtime, mismatch):
    runtime, run_id, _, host, _ = fork_runtime
    host.fork_search_run(run_id)
    state = runtime.candidate_sessions.state
    fork = state.read_run(run_id).fork
    first, second = fork.branches
    native_id = first.provider_session_id
    if mismatch == "native_id":
        native_id = second.provider_session_id
    elif mismatch == "group_id":
        state.write_dispatch_group(run_id, fork.model_copy(update={"dispatch_group_id": "another-group"}))
    else:
        changed = first.model_copy(update={mismatch: getattr(second, mismatch)})
        state.write_dispatch_group(run_id, fork.model_copy(update={"targets": [changed, second]}))
    before = state.read_run(run_id)
    sessions = state.read_agent_sessions(run_id)

    with pytest.raises(ValueError, match="fork close receipt"):
        runtime.candidate_sessions.record_dispatch_close(first.agent_session_id, native_id)

    assert state.read_run(run_id) == before
    assert state.read_agent_sessions(run_id) == sessions


def test_bound_fork_handle_cannot_be_changed(fork_runtime):
    runtime, run_id, _, host, _ = fork_runtime
    host.fork_search_run(run_id)
    session = runtime.candidate_sessions.state.read_agent_sessions(run_id)[0]
    with pytest.raises(ValueError, match="fork.*session|fork.*identity"):
        runtime.candidate_sessions.bind_agent_handle(
            session.agent_session_id, {"external_id": "another-native-branch"}
        )
    assert runtime.candidate_sessions.state.read_agent_session(session.agent_session_id) == session


def test_fork_preparation_rejects_previously_launched_candidate_before_writes(fork_runtime):
    runtime, run_id, candidates, host, _ = fork_runtime
    session = runtime.start_agent_session(run_id, candidates[-1].candidate_id)
    runtime.candidate_sessions.bind_agent_handle(
        session.agent_session_id, {"metadata": {"transport": "app-server"}}
    )
    before_run = runtime.search_run.state.read_run(run_id)
    before_sessions = runtime.candidate_sessions.state.read_agent_sessions(run_id)
    with pytest.raises(RuntimeError, match="previously launched"):
        host.fork_search_run(run_id)
    assert runtime.search_run.state.read_run(run_id) == before_run
    assert runtime.candidate_sessions.state.read_agent_sessions(run_id) == before_sessions


@pytest.mark.parametrize("operation", ["redispatch", "refresh", "replace", "recovery"])
def test_bound_fork_rejects_fresh_session_without_mutating_state(fork_runtime, operation):
    runtime, run_id, _, host, _ = fork_runtime
    host.fork_search_run(run_id)
    sessions = runtime.candidate_sessions.state.read_agent_sessions(run_id)
    session = sessions[0]
    run = runtime.search_run.state.read_run(run_id)
    candidate = runtime.candidate_creation.state.read_candidate(run_id, session.candidate_id)
    with pytest.raises(RuntimeError, match="Fork.*successor|Fork.*native"):
        if operation == "redispatch":
            runtime.redispatch_candidate(run_id, session.candidate_id)
        elif operation == "refresh":
            runtime.candidate_sessions.prepare_invocation(
                run_id, session.candidate_id, agent_harness="codex", mode="refresh"
            )
        elif operation == "replace":
            runtime.candidate_sessions.replace_session(
                session.agent_session_id, call_id="replacement", fingerprint="replacement"
            )
        else:
            runtime.prepare_candidate_recovery(run_id, session.candidate_id)
    assert runtime.candidate_sessions.state.read_agent_sessions(run_id) == sessions
    assert runtime.search_run.state.read_run(run_id) == run
    assert runtime.candidate_creation.state.read_candidate(run_id, session.candidate_id) == candidate


def test_fork_execution_rejects_session_with_copied_worker_identity(fork_runtime):
    runtime, run_id, _, host, _ = fork_runtime
    host.fork_search_run(run_id)
    session = runtime.candidate_sessions.state.read_agent_sessions(run_id)[0]
    copied = session.model_copy(update={"agent_session_id": "agent_copied"})
    runtime.candidate_sessions.state.write_agent_session(copied)
    with pytest.raises(RuntimeError, match="ForkBindingMismatch"):
        host.queries.require_native_execution(copied.agent_session_id, now=0)


def test_fork_run_reuses_bound_branch_when_new_session_is_requested(
    fork_runtime, monkeypatch
):
    """A fork branch cannot be replaced by a new logical/native identity."""
    runtime, run_id, candidates, host, _ = fork_runtime
    host.fork_search_run(run_id)
    branch = runtime.candidate_sessions.state.read_agent_sessions(run_id)[0]

    class Control:
        current_call_id = None

        def wait(self, _call_id, *, timeout):
            return SimpleNamespace(state="completed")

        def wake(self, _prompt, *, call_id, timeout):
            self.current_call_id = call_id
            return SimpleNamespace(state="running")

    control = Control()
    monkeypatch.setattr(
        runtime.candidate_sessions,
        "replace_session",
        lambda *args, **kwargs: pytest.fail("fork branch must not be replaced"),
    )
    monkeypatch.setattr(
        host,
        "open",
        lambda session_id: SimpleNamespace(session_id=session_id),
    )
    monkeypatch.setattr(
        type(host), "__call__", lambda _self, _session_id, _operation: control
    )
    monkeypatch.setattr(host, "_wake_verifier", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "goal_plus.session_host.session_call_receipt",
        lambda _snapshot: {"state": "running"},
    )

    receipt = host.run_candidate(
        run_id,
        branch.candidate_id,
        "fork-attempt-1",
        "run one bound branch attempt",
        30,
        new_session=True,
    )

    assert receipt["session_id"] == branch.agent_session_id
    persisted = runtime.candidate_sessions.state.read_agent_session(branch.agent_session_id)
    assert persisted.candidate_origin == branch.candidate_origin
    assert persisted.run_requests[-1].call_id == "fork-attempt-1"


@pytest.mark.parametrize("mismatch", ["artifact", "record", "context"])
def test_required_audit_rejects_inexact_evidence_without_changing_hard_settlement(fork_runtime, mismatch):
    from tests.test_llm_verifier import invocation

    runtime, run_id, candidates, host, _ = fork_runtime
    host.fork_search_run(run_id)
    for candidate in candidates:
        (candidate.workspace / "initial_program.py").write_text("VALUE = 1\n")
        runtime.run_verifier(run_id, candidate.candidate_id)
    workflow = runtime.selection.verifier
    before = [runtime.list_iterations(run_id, c.candidate_id) for c in candidates]
    while task := workflow.claim_task("fork-audit", 60, run_id=run_id):
        workflow.complete_task(task.task_id, {
            "quality_score": 0.8, "scores": None,
            "score_rationales": {"quality_score": "Exact fork audit"},
        }, invocation(), worker_id=task.lease.worker_id, attempt=task.attempts)
        workflow.acknowledge_delivery(task.task_id)
    task = workflow.list_tasks(run_id)[0]
    if mismatch == "record":
        record = workflow.list_records(run_id)[0]
        workflow.state.write_verifier_task(task.model_copy(update={"output_record_id": workflow.list_records(run_id)[1].record_id}))
    elif mismatch == "artifact":
        subject = task.subject.model_copy(update={"settlement": task.subject.settlement.model_copy(
            update={"attempt_artifact_hash": "f" * 64})})
        workflow.state.write_verifier_task(task.model_copy(update={"subject": subject}))
    else:
        context = task.input.dispatch_audit_context.model_copy(update={"workspace": candidates[1].workspace})
        workflow.state.write_verifier_task(task.model_copy(update={"input": task.input.model_copy(
            update={"dispatch_audit_context": context})}))
    with pytest.raises(RuntimeError, match="audit"):
        runtime.select(run_id)
    assert runtime.search_run.state.read_run(run_id).selected_candidate_id is None
    assert [runtime.list_iterations(run_id, c.candidate_id) for c in candidates] == before


def test_fork_spec_revision_preserves_old_results_and_requires_its_branch(fork_runtime):
    runtime, run_id, candidates, host, _ = fork_runtime
    group = host.fork_search_run(run_id)
    frozen = runtime.search_bootstrap.state.read(group["frozen_spec_id"])
    for candidate in candidates:
        runtime.run_verifier(run_id, candidate.candidate_id)
    before = runtime.candidate_creation.state.read_candidates(run_id)
    spec = frozen.spec.model_copy(deep=True)
    spec.candidate_acceptance = ["Document the measured attempt"]
    kwargs = dict(goal_plus_id=frozen.goal_plus_id, parent_frozen_spec_id=frozen.frozen_spec_id,
                  revision_reason="Clarify candidate acceptance", dispatch_group_id=group["dispatch_group_id"])
    revised = runtime.freeze_spec(spec, [Path(spec.source_path) / "evaluator.py"], dispatch_target_index=1, **kwargs)
    assert revised.frozen_spec_id != frozen.frozen_spec_id
    assert revised.spec_hash != frozen.spec_hash
    inherited = runtime.freeze_spec(
        spec,
        [Path(spec.source_path) / "evaluator.py"],
        goal_plus_id=frozen.goal_plus_id,
        parent_frozen_spec_id=frozen.frozen_spec_id,
        revision_reason="Inherit the existing branch lineage",
    )
    assert (inherited.dispatch_group_id, inherited.dispatch_target_index) == (
        frozen.dispatch_group_id, frozen.dispatch_target_index
    )
    assert runtime.search_bootstrap.state.read(frozen.frozen_spec_id) == frozen
    assert runtime.candidate_creation.state.read_candidates(run_id) == before
    with pytest.raises(ValueError, match="lineage|Lineage"):
        runtime.freeze_spec(spec, [Path(spec.source_path) / "evaluator.py"], dispatch_target_index=99, **kwargs)


@pytest.mark.parametrize("selected_version,parent_recheck", [("initial", False), ("revised", False), ("initial", True), ("revised", True)])
def test_candidate_revision_executes_new_contract_without_reinterpreting_history(fork_runtime, monkeypatch, selected_version, parent_recheck):
    from goal_plus.runtime import FileSearchRuntime
    from tests.test_llm_verifier import invocation

    runtime, run_id, candidates, host, _ = fork_runtime
    host.fork_search_run(run_id)
    state = runtime.candidate_creation.state
    for task in candidates[:1] if parent_recheck else candidates:
        runtime.run_verifier(run_id, task.candidate_id)
    first, other = state.read_candidates(run_id)
    original = state.read_frozen_spec(state.read_run(run_id).frozen_spec_id)
    session_id = first.task.candidate_origin.worker_identity["agent_session_id"]
    patch = {
        "metric_name": "revised_score",
        "process_verifiers": [{
            "name": "revised_evaluator", "role": "ranking_signal",
            "command": ["python", "-c", "print('{\"revised_score\": 0.25}')"],
        }],
    }
    revised = runtime.revise_candidate_spec(
        session_id, "revision-1", original.frozen_spec_id, "Explore another measure", patch,
    )
    restarted = FileSearchRuntime(runtime.root_dir)
    assert restarted.revise_candidate_spec(
        session_id, "revision-1", original.frozen_spec_id, "Explore another measure", patch,
    ) == revised
    from goal_plus.application.candidate_ranking import record_ranking_score
    assert record_ranking_score(state.read_candidate(run_id, first.candidate_id), revised.spec) is None
    report = restarted.run_verifier(run_id, first.candidate_id)
    current = state.read_candidate(run_id, first.candidate_id)
    assert report.process_passed and report.aggregate_score == 0.25
    assert report.disposition == "keep"  # V1's larger number is a different contract.
    assert report.frozen_spec_id == revised.frozen_spec_id
    assert current.iterations[:-1] == first.iterations
    assert state.read_candidate(run_id, other.candidate_id) == other
    assert current.task.candidate_origin == first.task.candidate_origin
    latest = current.iterations[-1]
    assert latest.frozen_spec_id == revised.frozen_spec_id
    assert latest.settlement_receipt.frozen_spec_id == revised.frozen_spec_id
    assert current.results_ledger[-1].frozen_spec_id == revised.frozen_spec_id
    assert current.results_ledger[-1].spec_hash == revised.spec_hash
    assert latest.verifier_submission.subject.frozen_spec_id == revised.frozen_spec_id
    assert latest.verifier_submission.input.dispatch_audit_context.frozen_spec_id == revised.frozen_spec_id
    assert state.read_frozen_spec(original.frozen_spec_id) == original
    with pytest.raises(ValueError, match="request|Request"):
        restarted.revise_candidate_spec(
            session_id, "revision-1", original.frozen_spec_id, "different", patch,
        )
    with pytest.raises(RuntimeError, match="parent|Parent"):
        restarted.revise_candidate_spec(
            session_id, "revision-2", original.frozen_spec_id, "stale parent", patch,
        )
    workflow = restarted.selection.verifier
    while task := workflow.claim_task("revision-audit", 60, run_id=run_id):
        workflow.complete_task(task.task_id, {
            "quality_score": 0.8, "scores": None,
            "score_rationales": {"quality_score": "Exact version and artifact"},
        }, invocation(), worker_id=task.lease.worker_id, attempt=task.attempts)
        workflow.acknowledge_delivery(task.task_id)
    before_selection = state.read_run(run_id)
    with pytest.raises((ValueError, FileNotFoundError)):
        restarted.select(run_id, frozen_spec_id="spec_missing")
    assert state.read_run(run_id) == before_selection
    before_recheck = state.read_candidate(run_id, first.candidate_id)
    if parent_recheck:
        artifact_state = restarted.selection.artifacts.candidate_artifact_state

        def require_parent_recheck(run, frozen, record):
            observed = artifact_state(run, frozen, record)
            return replace(observed, artifact_clean=False) if record.task.workspace == first.task.workspace else observed

        monkeypatch.setattr(restarted.selection.artifacts, "candidate_artifact_state", require_parent_recheck)
    if selected_version == "revised":
        selected = restarted.select(run_id, frozen_spec_id=revised.frozen_spec_id)
        assert selected["selected_candidate_id"] == first.candidate_id
        assert selected["selected_iteration"] == 2
        expected_id, expected_score = revised.frozen_spec_id, 0.25
    else:
        selected = restarted.select(run_id)
        assert selected["selected_iteration"] == 1
        expected_id, expected_score = original.frozen_spec_id, 0.0
    assert state.read_run(run_id).selected_frozen_spec_id == expected_id
    if parent_recheck:
        assert selected["selected_candidate_id"] == first.candidate_id
        assert selected["selection_evidence_source"] == "parent_reverify"
        assert selected["final_verifier_score"] == expected_score
        assert state.read_candidate(run_id, first.candidate_id) == before_recheck
    promoted = restarted.run_verifier(run_id, selected["selected_candidate_id"], scope="promotion")
    assert promoted.promotion_passed
    assert promoted.aggregate_score == expected_score
    assert promoted.frozen_spec_id == expected_id


def test_candidate_revision_recovers_commit_gap_and_serializes_parent_changes(fork_runtime, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from pydantic import ValidationError

    runtime, run_id, _, host, _ = fork_runtime
    host.fork_search_run(run_id)
    state = runtime.candidate_spec_revisions.state
    candidate = state.read_candidates(run_id)[0]
    session_id = candidate.task.candidate_origin.worker_identity["agent_session_id"]
    initial = state.read_run(run_id).frozen_spec_id
    before = state.read_candidate(run_id, candidate.candidate_id)
    for forbidden in ("budget", "edit_surface", "source_path", "workspace", "constraints"):
        with pytest.raises(ValidationError):
            runtime.revise_candidate_spec(session_id, "denied", initial, "Cannot expand authority", {forbidden: {}})
    assert state.read_candidate(run_id, candidate.candidate_id) == before
    write = state.write_candidate

    def interrupted(*_args):
        raise OSError("commit interruption")

    monkeypatch.setattr(state, "write_candidate", interrupted)
    with pytest.raises(OSError, match="commit interruption"):
        runtime.revise_candidate_spec(session_id, "resume-revision", initial, "first revision", {"objective": "revised objective"})
    assert state.read_candidate(run_id, candidate.candidate_id) == before
    monkeypatch.setattr(state, "write_candidate", write)
    revised = runtime.revise_candidate_spec(
        session_id, "resume-revision", initial, "first revision", {"objective": "revised objective"},
    )
    assert len(state.read_candidate(run_id, candidate.candidate_id).spec_revisions) == 1
    barrier = Barrier(2)

    def compete(index):
        barrier.wait(timeout=10)
        try:
            return runtime.revise_candidate_spec(
                session_id, f"competing-{index}", revised.frozen_spec_id, "concurrent revision",
                {"objective": f"branch direction {index}"},
            )
        except RuntimeError as exc:
            assert "ParentMismatch" in str(exc) or "VerifierInProgress" in str(exc)
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compete, (1, 2)))
    assert sum(result is not None for result in results) == 1
    committed = state.read_candidate(run_id, candidate.candidate_id)
    assert len(committed.spec_revisions) == 2
    assert committed.active_frozen_spec_id == next(result.frozen_spec_id for result in results if result is not None)


def test_candidate_revision_cannot_enable_frozen_verifier_capability(tmp_path):
    from goal_plus.runtime import FileSearchRuntime
    from tests._runtime_helpers import make_project, spec_for

    project = make_project(tmp_path)
    spec = spec_for(project, max_parallel=1)
    spec.strategy = StrategySpec(name="random", initial_context="fresh")
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"], agent_harness="codex")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    candidate = runtime.start_batch(run_id, plan.plan_id)[0]
    session = runtime.start_agent_session(run_id, candidate.candidate_id)
    state = runtime.candidate_creation.state
    before = state.read_candidate(run_id, candidate.candidate_id)
    with pytest.raises(ValueError, match="capabilit"):
        runtime.revise_candidate_spec(
            session.agent_session_id, "enable-quality", frozen.frozen_spec_id,
            "Try enabling an unfrozen evaluator", {"selection": {
                "ranking_keys": ["quality_score", "hard_score"], "ranking_tolerance": [0.0, 0.0],
            }},
        )
    assert state.read_candidate(run_id, candidate.candidate_id) == before


def test_candidate_revision_successor_ledger_preserves_creation_order_across_contracts(tmp_path):
    from goal_plus.runtime import FileSearchRuntime
    from tests._runtime_helpers import make_project, spec_for

    project = make_project(tmp_path)
    spec = spec_for(project, max_parallel=2)
    spec.strategy = StrategySpec(name="random", initial_context="fresh")
    runtime = FileSearchRuntime(tmp_path / ".gp")
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"], agent_harness="codex")
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    candidates = runtime.start_batch(run_id, plan.plan_id)
    for candidate, score in zip(candidates, (1, 100)):
        session = runtime.start_agent_session(run_id, candidate.candidate_id)
        runtime.revise_candidate_spec(
            session.agent_session_id, "own-contract", frozen.frozen_spec_id,
            "Evaluate this branch independently", {"process_verifiers": [{
                "name": "revised", "role": "ranking_signal",
                "command": ["python", "-c", f"print('{{\"combined_score\": {score}}}')"],
            }]},
        )
        runtime.run_verifier(run_id, candidate.candidate_id)
    state = runtime.candidate_creation.state
    assert state.read_run(run_id).best_candidate_id is None
    runtime.invalidate_run(run_id, reason="verifier_coverage_inadequate", summary="Continue with a reviewed contract", evidence=[{"case": "candidate evaluations use distinct contracts"}])
    successor = runtime.create_run(frozen.frozen_spec_id, source_run_id=run_id)
    successor_plan = runtime.plan_next(successor, requested_k=2)
    runtime.start_batch(successor, successor_plan.plan_id)
    for record in state.read_candidates(successor):
        assert [entry.source_candidate_id for entry in record.results_ledger] == [candidates[0].candidate_id]

