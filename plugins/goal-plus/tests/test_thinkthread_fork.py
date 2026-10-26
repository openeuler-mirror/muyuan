"""Provider derivation, durable binding order, and partial group cleanup."""

import json
from datetime import datetime, timezone

import pytest

from goal_plus.application.candidate_sessions import CandidateSessions
from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.host_scope import ensure_execution_scope
from goal_plus.infrastructure.state.files import load_json
from goal_plus.infrastructure.thinkthread_sessions import (
    ThinkThreadCandidateSession,
    release_session_resource,
    session_resource_path,
)
from goal_plus.ports.thinkthread import AgentPosixBridgeError
from goal_plus.runtime import FileSearchRuntime
from goal_plus.session_host import NativeCandidateHost
from tests._runtime_helpers import make_project, start_host_verified_goal
from tests._thinkthread_sdk import IntegratedAgentPosix, thinkthread_spec


@pytest.fixture
def checkpoint_clock(monkeypatch):
    epoch = [1792000000]
    stamp = lambda: datetime.fromtimestamp(epoch[0], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr("goal_plus.infrastructure.clock.utc_timestamp", stamp)
    monkeypatch.setattr("goal_plus.infrastructure.state.goal_mutations.utc_timestamp", stamp)
    return epoch


class ForkSdk(IntegratedAgentPosix):
    def self_view(self):
        result = super().self_view()
        result["profiles"][0]["allowedDerivations"].append(
            {"session": "fork_current_checkpoint", "fs": "private_branch"})
        return result

    def invoke(self, operation, params=None, **kwargs):
        if operation != "thinkthread.fork":
            return super().invoke(operation, params, **kwargs)
        self.operations.append((operation, dict(params)))
        assert "initialMessage" not in params
        assert params["fsSnapshotId"] == "fsnap-S0"
        if len(self.children) == 1 and self.failure == "rejected":
            raise AgentPosixBridgeError("fork rejected")
        if self.failure == "unknown":
            raise AgentPosixBridgeError("fork response lost", error={"delivery": "completion_unknown"})
        index = len(self.children) + 1
        child_id, branch_id = f"tt-fork-{index}", f"fsbranch-fork-{index}"
        result = {"thinkthreadId": child_id, "fs": {"kind": "private", "fsBranchId": branch_id},
                  "capabilities": [{"id": "thinkthread.message", "version": 1}]}
        if self.failure == "grant":
            result["capabilities"].append({"id": "thinkthread.fs.self", "version": 1})
        self.children.append({**result, "parentThinkthreadId": "tt-root", "agentState": "ready",
                              "executionState": "running", "lastWakeOutcome": None})
        self.branches[branch_id] = {"branchId": branch_id, "thinkthreadId": child_id,
            "baseSnapshotId": params["fsSnapshotId"], "controlGeneration": 0, "state": "attached"}
        self.messages[child_id] = []
        if self.failure == "lost":
            raise OSError("fork controller lost after provider accepted")
        if self.failure in {"drift", "marker_drift"} and index == 2:
            parent_id = "checkpoint"
            if self.failure == "marker_drift":
                parent_id = json.loads(self.history.read_text().splitlines()[-1])["id"]
            with self.history.open("a") as stream:
                stream.write(json.dumps({"type": "message", "id": "new-entry", "parentId": parent_id,
                    "message": {"role": "assistant", "content": [], "stopReason": "stop"}}) + "\n")
        return result


@pytest.mark.parametrize("failure,boundary", [
    (failure, "checkpoint") for failure in [
        None, "rejected", "drift", "unknown", "candidate_write", "receipt_write",
        "finalize_crash", "close_write", "journal_tamper", "grant", "resource_write", "lost",
    ]
] + [
    (None, "marker"), (None, "resume_only"), (None, "incomplete_tool_group"),
    (None, "later_assistant"), (None, "later_tool_group"), ("marker_drift", "resume_only"),
    (None, "missing_marker"), (None, "wrong_marker"), (None, "inactive_marker"),
    (None, "only_tool_group"),
])
def test_main_fork_derives_private_children_once_and_binds_exact_resources(tmp_path, monkeypatch, checkpoint_clock, failure, boundary):
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    sdk = ForkSdk(project)
    sdk.failure = failure
    for module in ("goal_plus.bootstrap", "goal_plus.infrastructure.thinkthread_fork"):
        monkeypatch.setattr(module + ".AgentPosixSdkClient", lambda: sdk)
    root = tmp_path.parent / (tmp_path.name + "-runtime")
    goal = start_host_verified_goal(root, project, agent_harness="pi")
    caller = goal.active_session.session_id
    history = tmp_path / "parent.jsonl"
    sdk.history = history
    initial_entries = [
        {"type": "session", "id": caller},
        {"type": "message", "id": "checkpoint", "parentId": None,
         "message": {"role": "assistant", "content": [{"type": "text", "text": "prepared"}], "stopReason": "stop"}},
    ]
    if boundary == "only_tool_group":
        initial_entries[1:] = [
            {"type": "message", "id": "prepared-call", "parentId": None,
             "message": {"role": "assistant", "stopReason": "toolUse", "content": [
                 {"type": "toolCall", "id": "prepared-call-1"},
             ]}},
            {"type": "message", "id": "checkpoint", "parentId": "prepared-call",
             "message": {"role": "toolResult", "toolCallId": "prepared-call-1", "content": []}},
        ]
    history.write_text("\n".join(json.dumps(row) for row in initial_entries) + "\n")
    runtime = FileSearchRuntime(root)
    spec = thinkthread_spec(project)
    spec.strategy.inner_agent = "common"
    spec.strategy.initial_context = "fork"
    spec.strategy.llm_validation = "off"
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"], agent_harness="pi", goal_plus_id=goal.goal_plus_id)
    run_id = runtime.create_run(frozen.frozen_spec_id)
    goals = FileGoalPlusRuntime(root)
    goals.link_search_run(goal.goal_plus_id, frozen.frozen_spec_id, run_id)
    runtime.start_batch(run_id, runtime.plan_next(run_id, requested_k=2).plan_id)
    checkpoint_clock[0] += 1
    goals.set_status(goal.goal_plus_id, "blocked", reason="Complete native preparation checkpoint")
    goal = goals.control_from_host_command(goal.goal_plus_id, invocation=goal.host_command_invocations[-1].model_copy(
        update={"action": "resume", "invocation_id": "pi:test:resume"}))
    if boundary != "missing_marker":
        entries = [{"type": "custom", "id": "fork-marker", "parentId": "checkpoint",
                    "customType": "goal-plus-fork-checkpoint", "data": {
                        "run_id": "another-run" if boundary == "wrong_marker" else run_id,
                    }}]
        if boundary not in {"checkpoint", "marker"}:
            entries.append({"type": "message", "id": "resume", "parentId": "fork-marker",
                            "message": {"role": "user", "content": "resume"}})
        if boundary == "inactive_marker":
            entries[-1]["parentId"] = "checkpoint"
        if boundary == "later_assistant":
            entries.append({"type": "message", "id": "later", "parentId": "resume",
                            "message": {"role": "assistant", "content": [], "stopReason": "stop"}})
        if boundary in {"incomplete_tool_group", "later_tool_group"}:
            entries.extend([
                {"type": "message", "id": "tool-call", "parentId": "resume",
                 "message": {"role": "assistant", "stopReason": "toolUse", "content": [
                     {"type": "toolCall", "id": "call-1"}, {"type": "toolCall", "id": "call-2"},
                 ]}},
                {"type": "message", "id": "first-result", "parentId": "tool-call",
                 "message": {"role": "toolResult", "toolCallId": "call-1", "content": []}},
            ])
            if boundary == "later_tool_group":
                entries.append({"type": "message", "id": "last-result", "parentId": "first-result",
                                "message": {"role": "toolResult", "toolCallId": "call-2", "content": []}})
        with history.open("a") as stream:
            stream.writelines(json.dumps(entry) + "\n" for entry in entries)
    # Pi's host scope owner is operational (`<goal>:<cwd>`), not the provider
    # ThinkThread root id.  Fork admission must bind the latter through the
    # native session history identity instead of comparing these two ids.
    scope = ensure_execution_scope(root_dir=root, agent_harness="pi",
        owner_session_id=f"{goal.goal_plus_id}:{project}",
        run_id=run_id, native_session_file=str(history), activation_id=f"{goal.goal_plus_id}:{goal.control.version}")
    host = NativeCandidateHost(root, agent_harness="pi", caller=lambda: caller)
    if boundary in {"missing_marker", "wrong_marker", "inactive_marker", "later_assistant", "only_tool_group"}:
        before_run = runtime.search_run.state.read_run(run_id)
        before_candidates = {path: path.read_bytes() for path in (root / "runs" / run_id / "candidates").glob("*/candidate.json")}
        operations_before = len(sdk.operations)
        if boundary.endswith("marker"):
            error = "checkpoint marker"
        elif boundary == "only_tool_group":
            error = "no completed Pi assistant entry"
        else:
            error = "ForkCheckpointChanged"
        with pytest.raises(RuntimeError, match=error):
            host.fork_search_run(run_id)
        assert runtime.search_run.state.read_run(run_id) == before_run
        assert {path: path.read_bytes() for path in before_candidates} == before_candidates
        assert not list((root / "runs" / run_id / "agent_sessions").glob("agent_*.json"))
        assert not any(operation in {"thinkthread.fork", "thinkthread.spawn", "message.send"}
                       for operation, _ in sdk.operations[operations_before:])
        assert sdk.children == []
        return
    if failure in {"candidate_write", "receipt_write"}:
        write_name = "write_candidate_binding" if failure == "candidate_write" else "write_agent_session"
        write = getattr(runtime.candidate_sessions.state, write_name)
        failed = False

        def fail_first_bound_record(record):
            nonlocal failed
            binding = record.task.candidate_origin if failure == "candidate_write" else record.candidate_origin
            if not failed and binding is not None and binding.provider_session_id is not None:
                failed = True
                raise OSError("fork receipt write interrupted")
            return write(record)

        # The provider creates a real successful Child; interrupt its receipt
        # before the candidate file or between candidate and session files.
        monkeypatch.setattr(runtime.candidate_sessions.state, write_name, fail_first_bound_record)
        host.runtime = runtime
    if failure == "resource_write":
        import goal_plus.infrastructure.thinkthread_fork as fork_adapter

        persist = fork_adapter._persist_resource
        interrupted = False

        def interrupt_first_known_resource(path, record):
            nonlocal interrupted
            if not interrupted and record.get("thinkthread_id") is not None:
                interrupted = True
                raise OSError("known fork resource journal write interrupted")
            return persist(path, record)

        monkeypatch.setattr(fork_adapter, "_persist_resource", interrupt_first_known_resource)
    if failure == "finalize_crash":
        def lose_controller_after_receipts(*args, **kwargs):
            raise OSError("controller lost before group finalization")

        monkeypatch.setattr(runtime.candidate_sessions, "finish_dispatch_group", lose_controller_after_receipts)
        host.runtime = runtime
        with pytest.raises(OSError, match="controller lost"):
            host.fork_search_run(run_id)
        before = runtime.search_run.state.read_run(run_id).fork
        assert before.status == "prepared"
        assert len(sdk.children) == 2
        native_operations = [item for item in sdk.operations if item[0] in {"thinkthread.spawn", "thinkthread.fork", "message.send"}]
        rebuilt = NativeCandidateHost(root, agent_harness="pi", caller=lambda: caller)
        result = rebuilt.fork_search_run(run_id)
        assert result["status"] == "started"
        assert [item for item in sdk.operations if item[0] in {"thinkthread.spawn", "thinkthread.fork", "message.send"}] == native_operations
        assert [branch["provider_session_id"] for branch in result["targets"]] == [branch.provider_session_id for branch in before.branches]
        return
    if failure in {"rejected", "drift", "marker_drift", "unknown", "candidate_write", "receipt_write", "grant", "resource_write", "lost"}:
        with pytest.raises((RuntimeError, AgentPosixBridgeError, OSError)):
            host.fork_search_run(run_id)
        fork = runtime.search_run.state.read_run(run_id).fork
        assert fork.status == ("failed" if failure in {"unknown", "grant", "resource_write", "lost"} else "partial")
        assert len(sdk.children) == (1 if failure == "lost" else 0)
        if failure in {"unknown", "lost"}:
            path = session_resource_path(root, scope["scope_id"], fork.branches[0].agent_session_id)
            journal = load_json(path)
            assert journal["fork_intent"]["state"] == ("unknown" if failure == "unknown" else "started")
            assert not journal.get("thinkthread_id")
            before, operations = path.read_bytes(), list(sdk.operations)
            with pytest.raises(RuntimeError, match="unresolved provider derivation"):
                release_session_resource(path, client=sdk, timeout=1)
            assert path.read_bytes() == before
            assert sdk.operations == operations
        else:
            assert all(branch["state"] == "removed" for branch in sdk.branches.values())
        if failure == "resource_write":
            path = session_resource_path(root, scope["scope_id"], fork.branches[0].agent_session_id)
            journal = load_json(path)
            assert journal["cleanup_state"] == "closed"
            assert journal["thinkthread_id"] == "tt-fork-1"
            assert journal["fs_branch_id"] == "fsbranch-fork-1"
        if failure in {"candidate_write", "receipt_write"}:
            branch = fork.branches[0]
            candidate = runtime.candidate_sessions.state.read_candidate(run_id, branch.candidate_id)
            session = runtime.candidate_sessions.state.read_agent_session(branch.agent_session_id)
            journal = load_json(session_resource_path(root, scope["scope_id"], branch.agent_session_id))
            assert candidate.task.candidate_origin == session.candidate_origin == branch.binding
            assert branch.binding.provider_session_id == session.session_handle.external_id == journal["thinkthread_id"]
            assert branch.binding.launch_receipt == journal["launch_receipt"]
            assert branch.binding.launch_receipt["launch_state"] == "started"
            assert branch.binding.launch_receipt["closed_at"] is None
            assert branch.binding.workspace_attachment.fs_branch_id == journal["fs_branch_id"]
            assert branch.launch_state == "cleaned"
            assert branch.closed_at is not None
            assert session.session_handle.metadata["fork_cleanup"] == "closed"
            assert candidate.task.workspace_provider_handle["cleanup_state"] == "closed"
            operations = list(sdk.operations)
            assert host.fork_search_run(run_id)["status"] == "partial"
            assert sdk.operations == operations
        return
    result = host.fork_search_run(run_id)
    assert host.fork_search_run(run_id) == result
    assert result["runtime_provider"] == "thinkthread"
    assert result["main_session_id"] == caller
    assert result["parent_native_session_id"] == "tt-root"
    assert result["checkpoint_id"] == "checkpoint"
    for branch in result["targets"]:
        session = runtime.candidate_sessions.session(branch["agent_session_id"])
        task = runtime.candidate_sessions.state.read_candidate(run_id, branch["candidate_id"]).task
        journal = load_json(session_resource_path(root, scope["scope_id"], session.agent_session_id))
        attachment = branch["binding"]["workspace_attachment"]
        assert session.session_handle.external_id == task.workspace_provider_handle["thinkthread_id"] == journal["thinkthread_id"]
        assert task.workspace_provider_handle["branch_id"] == journal["fs_branch_id"] == attachment["fs_branch_id"]
        assert journal["dispatch_provenance"]["checkpoint_id"] == "checkpoint"
        assert journal["fork_intent"]["state"] == "bound"
        assert journal["candidate_origin_message_id"]
    assert len({branch["binding"]["context_projection_digest"] for branch in result["targets"]}) == 1
    assert sum(operation == "thinkthread.fork" for operation, _ in sdk.operations) == 2
    assert not any(operation == "thinkthread.spawn" for operation, _ in sdk.operations)
    assert all(message["wake"] is False for message in sdk.sent)
    if failure == "journal_tamper":
        from goal_plus.infrastructure.state.files import write_json

        branch = result["targets"][0]
        path = session_resource_path(root, scope["scope_id"], branch["agent_session_id"])
        saved = load_json(path)
        saved["dispatch_provenance"]["operation_id"] = "another-operation"
        write_json(path, saved)
        monkeypatch.setattr("goal_plus.infrastructure.thinkthread_sessions.AgentPosixSdkClient", lambda: sdk)
        with pytest.raises(PermissionError, match="fork resource"):
            ThinkThreadCandidateSession(host, branch["agent_session_id"], caller, scope["scope_id"])
    if failure == "close_write":
        branch = result["targets"][0]
        path = session_resource_path(root, scope["scope_id"], branch["agent_session_id"])
        close = CandidateSessions.record_dispatch_close
        interrupted = False

        def interrupt_domain_close(self, *args):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise OSError("domain close interrupted after provider cleanup")
            return close(self, *args)

        monkeypatch.setattr(CandidateSessions, "record_dispatch_close", interrupt_domain_close)
        with pytest.raises(OSError, match="domain close interrupted"):
            release_session_resource(path, client=sdk, timeout=1)
        assert load_json(path)["cleanup_state"] == "closed"
        operations = list(sdk.operations)
        assert release_session_resource(path, client=sdk, timeout=1)["state"] == "closed"
        assert sdk.operations == operations
        assert runtime.search_run.state.read_run(run_id).fork.branches[0].closed_at


@pytest.mark.parametrize("completion", ["new", "old", "same_second", "missing_time"])
def test_codex_tt_checkpoint_completion_must_follow_preparation(tmp_path, monkeypatch, checkpoint_clock, completion):
    from goal_plus.infrastructure.thinkthread_fork import ThinkThreadDispatchAdapter

    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    sdk = ForkSdk(project)
    sdk.agent = "codex"
    for module in ("goal_plus.bootstrap", "goal_plus.infrastructure.thinkthread_fork"):
        monkeypatch.setattr(module + ".AgentPosixSdkClient", lambda: sdk)
    root = tmp_path.parent / (tmp_path.name + "-codex-runtime")
    goal = start_host_verified_goal(root, project)
    caller = goal.active_session.session_id
    runtime = FileSearchRuntime(root)
    spec = thinkthread_spec(project)
    spec.strategy.inner_agent = "common"
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"], goal_plus_id=goal.goal_plus_id)
    run_id = runtime.create_run(frozen.frozen_spec_id)
    goals = FileGoalPlusRuntime(root)
    goals.link_search_run(goal.goal_plus_id, frozen.frozen_spec_id, run_id)
    runtime.start_batch(run_id, runtime.plan_next(run_id, requested_k=2).plan_id)
    checkpoint_clock[0] += 1
    goals.set_status(goal.goal_plus_id, "blocked", reason="Preparation complete")
    event = next(e for e in reversed(goals.state.read_events(goal.goal_plus_id))
                 if e["event_type"] == "status_changed")
    prepared_at = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00")).timestamp()
    completed_at = prepared_at + {"new": 1, "old": -60, "same_second": 0, "missing_time": 0}[completion]
    native_home = tmp_path / "codex-home"
    sessions = native_home / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(native_home))
    rows = [
        {"type": "session_meta", "payload": {"id": caller}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "completed-turn"}},
    ]
    if completion != "missing_time":
        rows[-1]["timestamp"] = datetime.fromtimestamp(completed_at, timezone.utc).isoformat()
    (sessions / f"rollout-{caller}.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    ensure_execution_scope(root_dir=root, agent_harness="codex", owner_session_id=caller,
                           run_id=run_id, activation_id=f"{goal.goal_plus_id}:{goal.control.version}")
    before = runtime.search_run.state.read_run(run_id)
    operations = list(sdk.operations)
    adapter = ThinkThreadDispatchAdapter(root, runtime, "codex")
    if completion == "new":
        assert adapter.prepare_checkpoint(run_id, caller).checkpoint_id == "completed-turn"
    else:
        with pytest.raises(RuntimeError, match="ForkCheckpointUnavailable"):
            adapter.prepare_checkpoint(run_id, caller)
    assert runtime.search_run.state.read_run(run_id) == before
    assert not any(op in {"thinkthread.fork", "thinkthread.spawn", "message.send"}
                   for op, _ in sdk.operations[len(operations):])
