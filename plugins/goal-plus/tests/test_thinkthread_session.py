"""Retained Child execution through common session control, without Search policy."""

import base64
import json
import threading
import time
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace

import pytest

from goal_plus.execution.session_control import NativeSessionControl
from goal_plus.execution.thinkthread_child import TURN_QUIESCENCE_SECONDS, ChildDriver
from goal_plus.execution.thinkthread_session import ThinkThreadSession
from goal_plus.infrastructure.clock import SystemClock
from goal_plus.ports.thinkthread_session import (
    ThinkThreadSessionHooks,
    ThinkThreadSessionInput,
)
from tests._thinkthread_sdk import StatefulAgentPosix


@pytest.fixture
def child_actor():
    sdk, clock = StatefulAgentPosix(), SystemClock()
    record, saved, settled = {"agent_session_id": "agent-child"}, [], []

    def receive(state):
        child = sdk.child(state["thinkthread_id"])
        message = state.get("active_message_id") or (
            sdk.sent[-1]["messageId"] if sdk.sent else "msg-initial-1"
        )
        state.setdefault("acknowledged_messages", {})[state["dispatch_nonce"]] = message
        child.update(agentState="ready", lastWakeOutcome={
            "messageId": message, "outcome": "completed", "finishedAtUnixMs": (time.time() - TURN_QUIESCENCE_SECONDS) * 1000,
        })

    def settle(state):
        settled.append(state["active_message_id"])
        return "exact-result"

    actor = ThinkThreadSession(
        ThinkThreadSessionInput("tt-root", "fsnap-baseline"), ChildDriver(sdk, clock), clock, record,
        ThinkThreadSessionHooks(nullcontext, lambda state: saved.append(deepcopy(state)), lambda _: None, receive, settle),
    )
    yield actor, sdk, record, saved, settled
    actor.close()


@pytest.fixture
def codex_actor():
    sdk, clock = StatefulAgentPosix(), SystemClock()
    record, saved, settled, bound = {"agent_session_id": "agent-codex"}, [], [], []

    def receive(state):
        child = sdk.child(state["thinkthread_id"])
        message = state.get("active_message_id") or (
            sdk.sent[-1]["messageId"] if sdk.sent else "msg-initial-1"
        )
        state.setdefault("acknowledged_messages", {})[state["dispatch_nonce"]] = message
        child.update(agentState="ready", lastWakeOutcome={
            "messageId": message, "outcome": "completed", "finishedAtUnixMs": (time.time() - TURN_QUIESCENCE_SECONDS) * 1000,
        })

    def settle(state):
        settled.append(state["active_message_id"])
        return "exact-result"

    actor = ThinkThreadSession(
        ThinkThreadSessionInput("tt-root", "fsnap-baseline", agent_harness="codex"),
        ChildDriver(sdk, clock, agent_harness="codex"), clock, record,
        ThinkThreadSessionHooks(
            nullcontext, lambda state: saved.append(deepcopy(state)), bound.append, receive, settle,
        ),
    )
    yield actor, sdk, record, saved, settled, bound
    actor.close()


def test_codex_reaps_stopped_child_and_respawns_from_baseline(codex_actor):
    actor, sdk, record, _, _, bound = codex_actor
    control = NativeSessionControl(actor)
    control.wake("attempt 1", call_id="1", timeout=10)
    assert control.wait("1", timeout=5).state == "completed"
    assert actor.native_session_id == "tt-child-1"
    control.close()
    assert sdk.child("tt-child-1")["executionState"] == "absent"
    actor.reopen()
    control = NativeSessionControl(actor)
    try:
        control.wake("attempt 2", call_id="2", timeout=10)
        outcome = control.wait("2", timeout=5)
        assert outcome.state == "completed"
        assert outcome.native_session_id == "tt-child-2"
        assert outcome.result.agent_harness == "codex"
        assert record["thinkthread_id"] == "tt-child-2"
        assert record["fs_branch_id"] == "fsbranch-child-2"
        assert "replaced_thinkthread_id" not in record
        spawns = [params for operation, params in sdk.operations if operation == "thinkthread.spawn"]
        assert [params["fsSnapshotId"] for params in spawns] == ["fsnap-baseline"] * 2
        assert sdk.branches["fsbranch-child-1"]["state"] == "removed"
        assert sum(operation == "thinkthread.destroy" for operation, _ in sdk.operations) == 1
        signals = [params for operation, params in sdk.operations if operation == "thinkthread.signal"]
        assert all(params["signal"] == "TERM" for params in signals)
        assert [binding.child_id for binding in bound] == ["tt-child-1", "tt-child-2"]
    finally:
        control.close()


def test_codex_retired_child_respawns_from_settled_snapshot(codex_actor):
    actor, sdk, record, _, _, bound = codex_actor
    control = NativeSessionControl(actor)
    try:
        control.wake("attempt 1", call_id="1", timeout=10)
        assert control.wait("1", timeout=5).state == "completed"
        assert sdk.child("tt-child-1")["executionState"] == "running"
        # A diverging settlement cannot reset the retained Codex branch; it retires
        # the Child and records the settled snapshot as the exact respawn point.
        record["child_retired"] = True
        record["spawn_snapshot_id"] = "fsnap-settled-1"
        control.wake("attempt 2", call_id="2", timeout=10)
        outcome = control.wait("2", timeout=5)
        assert outcome.state == "completed"
        assert outcome.native_session_id == "tt-child-2"
        assert "child_retired" not in record
        spawns = [params for operation, params in sdk.operations if operation == "thinkthread.spawn"]
        assert spawns[-1]["fsSnapshotId"] == "fsnap-settled-1"
        assert sdk.branches["fsbranch-child-1"]["state"] == "removed"
        assert sum(operation == "thinkthread.destroy" for operation, _ in sdk.operations) == 1
        assert [binding.child_id for binding in bound] == ["tt-child-1", "tt-child-2"]
    finally:
        control.close()


def test_codex_retained_child_still_wakes_without_replacement(codex_actor):
    from goal_plus.domain.tool_catalog import CandidateToolCapabilities

    actor, sdk, record, _, settled, bound = codex_actor
    actor.config = replace(actor.config, tool_capabilities=CandidateToolCapabilities(
        supplemental=True, shared_tools=False, shared_cache=True, references=False, spec_revision=False,
    ))
    control = NativeSessionControl(actor)
    try:
        for number in (1, 2):
            control.wake(f"attempt {number}", call_id=str(number), timeout=10)
            outcome = control.wait(str(number), timeout=5)
            assert outcome.state == "completed"
            assert outcome.native_session_id == "tt-child-1"
        assert settled == ["msg-initial-1", "msg-wake-1"]
        assert sum(operation == "thinkthread.spawn" for operation, _ in sdk.operations) == 1
        assert not any(operation == "thinkthread.signal" for operation, _ in sdk.operations)
        assert [binding.child_id for binding in bound] == ["tt-child-1"]
        dispatches = [
            params["initialMessage"] if operation == "thinkthread.spawn" else params["text"]
            for operation, params in sdk.operations
            if operation == "thinkthread.spawn" or operation == "message.send" and params.get("wake") is True
        ]
        assert len(dispatches) == 2
        for text in dispatches:
            markers = [line for line in text.splitlines() if line.startswith("goal_plus_candidate_capabilities=")]
            assert len(markers) == 1
            encoded = markers[0].split("=", 1)[1].removesuffix(";")
            assert json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))) == {
                "supplemental": True, "shared_tools": False, "shared_cache": True, "references": False, "spec_revision": False,
            }
    finally:
        control.close()


@pytest.mark.parametrize("loss", ["stopped", "retired", "missing"])
def test_fork_bound_codex_child_never_respawns_after_continuation_loss(codex_actor, loss):
    actor, sdk, record, _, _, _ = codex_actor
    assert actor.run("first retained attempt", timeout=10).native_session_id == "tt-child-1"
    record["dispatch_provenance"] = {"checkpoint_id": "original-completed-turn"}
    if loss == "stopped":
        sdk.child("tt-child-1")["executionState"] = "absent"
    elif loss == "retired":
        record["child_retired"] = True
    else:
        sdk.children.clear()
    with pytest.raises(RuntimeError, match="ForkContinuationUnavailable"):
        actor.run("second retained attempt", timeout=10)
    assert actor.native_session_id == "tt-child-1"
    assert sum(operation == "thinkthread.spawn" for operation, _ in sdk.operations) == 1


def test_common_controller_retains_child_for_two_exact_calls(child_actor):
    actor, sdk, record, saved, settled = child_actor
    assert actor.native_session_id is None
    assert sdk.operations == []
    control = NativeSessionControl(actor)
    try:
        for number in (1, 2):
            control.wake(f"attempt {number}", call_id=str(number), timeout=10)
            outcome = control.wait(str(number), timeout=5)
            assert outcome.state == "completed"
            assert outcome.native_session_id == "tt-child-1"
            assert outcome.result.text == "exact-result"
        assert settled == ["msg-initial-1", "msg-wake-1"]
        assert len([operation for operation, _ in sdk.operations if operation == "thinkthread.spawn"]) == 1
        assert saved[0]["status"] == "running"
    finally:
        control.close()
    assert sdk.child("tt-child-1")["executionState"] == "absent"


def test_dispatch_message_carries_the_bound_agent_session_id(child_actor):
    actor, sdk, record, _, _ = child_actor

    actor.run("first", timeout=5)
    spawn = next(params for operation, params in sdk.operations if operation == "thinkthread.spawn")
    assert f"agent_session_id={record['agent_session_id']};" in spawn["initialMessage"]

    actor.run("second", timeout=5)
    wake = [params for operation, params in sdk.operations if operation == "message.send" and params.get("wake") is True]
    assert f"agent_session_id={record['agent_session_id']};" in wake[-1]["text"]


@pytest.mark.parametrize("lost", ["spawn", "wake"])
def test_unknown_delivery_recovers_exact_message_without_second_mutation(child_actor, lost):
    actor, sdk, record, _, settled = child_actor
    if lost == "wake":
        actor.run("first", timeout=5)
    setattr(sdk, lost + "_completion_unknown", True)
    result = actor.run("lost reply", timeout=5)
    assert result.state == "completed"
    assert result.invocation_id == settled[-1]
    assert len([op for op, _ in sdk.operations if op == "thinkthread.spawn"]) == 1
    assert len(sdk.sent) == (1 if lost == "wake" else 0)


@pytest.mark.parametrize("lost", ["spawn", "wake"])
def test_new_actor_observes_unknown_delivery_from_durable_intent(child_actor, lost):
    actor, sdk, record, saved, settled = child_actor
    if lost == "wake":
        actor.run("first", timeout=5)
    setattr(sdk, lost + "_completion_unknown", True)
    expected = actor.run("lost reply", timeout=5)
    retained = next(deepcopy(item) for item in saved
                    if item.get(lost + "_intent", {}).get("state") == "unknown"
                    and item.get("status") == "running")
    mutations_before = [item for item in sdk.operations if item[0] in {"thinkthread.spawn", "message.send"}]
    recovered = ThinkThreadSession(actor.config, actor.driver, actor.clock, retained, actor.hooks)
    try:
        result = recovered.resume()
        assert result.invocation_id == expected.invocation_id
        assert retained["dispatch_index"] == record["dispatch_index"]
        assert retained["execution_deadline_unix"] == record["execution_deadline_unix"]
        assert [item for item in sdk.operations if item[0] in {"thinkthread.spawn", "message.send"}] == mutations_before
    finally:
        recovered.close()


def test_recovery_does_not_grant_another_execution_allowance(child_actor, monkeypatch):
    actor, sdk, _, saved, _ = child_actor
    actor.run("first", timeout=5)
    retained = next(deepcopy(item) for item in saved if item.get("thinkthread_id") is not None)
    child = sdk.child(retained["thinkthread_id"])
    child.update(agentState="running", lastWakeOutcome=None)
    monkeypatch.setattr(actor.clock, "now", lambda: retained["execution_deadline_unix"] + 1)
    recovered = ThinkThreadSession(actor.config, actor.driver, actor.clock, retained,
                                  replace(actor.hooks, receive=lambda _: None))
    try:
        with pytest.raises(TimeoutError, match="original execution allowance"):
            recovered.resume()
        assert retained["status"] == "needs_recovery"
        assert child["executionState"] == "absent"
        assert sum(op == "thinkthread.spawn" for op, _ in sdk.operations) == 1
        assert sdk.sent == []
    finally:
        recovered.close()


def test_scope_fence_prevents_spawn_and_does_not_settle(child_actor):
    from dataclasses import replace

    actor, sdk, _, _, settled = child_actor

    def rejected():
        raise PermissionError("scope fenced")

    actor.hooks = replace(actor.hooks, admission=rejected)
    with pytest.raises(PermissionError, match="scope fenced"):
        actor.run("first", timeout=5)
    assert sdk.operations == []
    assert settled == []


def test_exact_dispatch_error_is_returned_without_settlement(child_actor):
    actor, _, record, _, settled = child_actor
    receive = actor.hooks.receive

    def startup_failed(state):
        receive(state)
        state["dispatch_error"] = {
            "dispatch_nonce": state["dispatch_nonce"],
            "message": "private Git initialization failed",
            "error_type": "Error",
        }

    actor.hooks = replace(actor.hooks, receive=startup_failed)
    result = actor.run("first", timeout=5)
    assert result.state == "failed"
    assert result.error == "ChildDispatchError: private Git initialization failed"
    assert record["invocation_result"]["error"] == result.error
    assert settled == []


def test_completed_turn_can_settle_after_controller_deadline(child_actor, monkeypatch):
    actor, _, _, saved, settled = child_actor
    first = actor.run("first", timeout=5)
    retained = next(deepcopy(item) for item in saved if item.get("thinkthread_id") is not None)
    monkeypatch.setattr(actor.clock, "now", lambda: retained["execution_deadline_unix"] + 1)
    recovered = ThinkThreadSession(actor.config, actor.driver, actor.clock, retained, actor.hooks)
    try:
        result = recovered.resume()
        assert result.state == "completed"
        assert result.invocation_id == first.invocation_id
        assert settled == [first.invocation_id, first.invocation_id]
    finally:
        recovered.close()


@pytest.mark.parametrize("ack", ["accepted", "wrong_hash", "rejected"])
@pytest.mark.parametrize("operation", ["message", "interrupt"])
def test_control_requires_exact_native_ack_without_another_wake(child_actor, monkeypatch, ack, operation):
    actor, sdk, record, _, settled = child_actor
    record["agent_session_id"] = "agent-one"
    active, release = threading.Event(), threading.Event()
    receive = actor.hooks.receive

    def wait_for_message(state):
        active.set()
        if release.is_set():
            receive(state)

    actor.hooks = replace(actor.hooks, receive=wait_for_message)
    invoke = sdk.invoke
    acknowledgements, sent = [], []

    def message_io(method, params=None, **kwargs):
        if method == "message.send" and params.get("wake") is False:
            request = json.loads(params["text"])
            sent.append(request)
            assert request["type"] == "session_" + operation
            content = json.loads(request["content_json"])
            assert content["message_id"] == record["active_message_id"]
            assert content["dispatch_nonce"] == record["dispatch_nonce"]
            acknowledgements.append({
                "senderThinkthreadId": actor.native_session_id, "truncated": False,
                "text": json.dumps({
                    "protocol": request["protocol"], "type": request["type"] + "_ack",
                    "request_id": request["request_id"], "accepted": ack == "accepted",
                    "content_sha256": "wrong" if ack == "wrong_hash" else request["content_sha256"],
                    "reason": "inactive_or_mismatched_invocation",
                }),
            })
            return {"messageId": "msg-steering"}
        if method == "message.receive":
            return {"messages": acknowledgements, "nextCursor": "cursor", "hasMore": False}
        return invoke(method, params, **kwargs)

    monkeypatch.setattr(sdk, "invoke", message_io)
    control = NativeSessionControl(actor)
    try:
        control.wake("first", call_id="1", timeout=10)
        assert active.wait(5)
        def invoke_control():
            if operation == "message":
                control.message("1", "use the revised target", timeout=2)
            else:
                control.interrupt("1", timeout=2)
        if ack == "accepted":
            invoke_control()
        else:
            with pytest.raises(RuntimeError, match="acknowledgement content changed|steering rejected"):
                invoke_control()
        assert len(sent) == 1
        assert record["dispatch_index"] == 1
        assert sdk.sent == []
        assert not any(op == "thinkthread.signal" for op, _ in sdk.operations)
        release.set()
        result = control.wait("1", timeout=5)
        assert result.state == ("interrupted" if operation == "interrupt" and ack == "accepted" else "completed")
        if operation == "interrupt" and ack == "accepted":
            assert settled == []
            control.wake("second", call_id="2", timeout=10)
            assert control.wait("2", timeout=5).state == "completed"
            assert len(settled) == 1
            assert control.wait("1", timeout=0) == result
    finally:
        release.set()
        control.close()
