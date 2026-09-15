"""Shared TT execution facts, independently of candidate and Annotation policy."""

from types import SimpleNamespace

import pytest

from goal_plus.execution.thinkthread_child import ChildDriver, TURN_QUIESCENCE_SECONDS
from goal_plus.infrastructure.clock import SystemClock
from tests._thinkthread_sdk import StatefulAgentPosix


@pytest.mark.parametrize("lost", [False, True])
def test_release_is_owned_confirmed_and_idempotent(lost):
    sdk = StatefulAgentPosix()
    for _ in range(2):
        sdk.invoke("thinkthread.spawn", {"initialMessage": "resource test"})
    sdk.branch_remove_completion_unknown = lost
    sdk.destroy_completion_unknown = lost
    driver = ChildDriver(sdk, SystemClock())
    driver.release("tt-child-1", root_id="tt-root", branch_id="fsbranch-child-1", timeout=1)
    driver.release("tt-child-1", root_id="tt-root", branch_id="fsbranch-child-1", timeout=1)
    assert sdk.child("tt-child-2")["executionState"] == "running"
    assert sdk.branches["fsbranch-child-2"]["state"] == "attached"
    assert sdk.branches["fsbranch-child-1"]["state"] == "removed"
    assert sum(op == "thinkthread.destroy" for op, _ in sdk.operations) == 1
    assert sum(op == "fs.branch.remove" for op, _ in sdk.operations) == 1


@pytest.mark.parametrize("mismatch", ["parent", "branch"])
def test_foreign_child_is_rejected_before_destructive_operations(mismatch):
    sdk = StatefulAgentPosix()
    sdk.invoke("thinkthread.spawn", {"initialMessage": "resource test"})
    if mismatch == "parent":
        sdk.child("tt-child-1")["parentThinkthreadId"] = "tt-foreign"
    else:
        sdk.child("tt-child-1")["fs"]["fsBranchId"] = "fsbranch-foreign"
    with pytest.raises(PermissionError):
        ChildDriver(sdk, SystemClock()).release(
            "tt-child-1", root_id="tt-root", branch_id="fsbranch-child-1", timeout=1,
        )
    assert not any(op in {"thinkthread.signal", "thinkthread.destroy", "fs.branch.remove"}
                   for op, _ in sdk.operations)


def test_turn_waits_for_sdk_completion_timestamp_and_exact_message():
    now = 1000.0
    driver = ChildDriver(None, SimpleNamespace(now=lambda: now))
    child = {
        "agentState": "ready", "executionState": "running", "pendingWake": False,
        "lastWakeOutcome": {"messageId": "msg-current", "outcome": "completed", "finishedAtUnixMs": now * 1000},
    }
    assert driver.turn_terminal(child, "msg-current") is None
    now += TURN_QUIESCENCE_SECONDS
    assert driver.turn_terminal(child, "msg-current") == "completed"
    child["executionState"] = "absent"
    child["completion"] = {"outcome": "exited"}
    assert driver.turn_terminal(child, "msg-new") is None


@pytest.mark.parametrize("finished", [None, True, "1000", float("nan"), float("inf")])
def test_invalid_completion_clock_does_not_authorize_settlement(finished):
    driver = ChildDriver(None, SimpleNamespace(now=lambda: 1000.0))
    child = {
        "agentState": "ready", "pendingWake": False,
        "lastWakeOutcome": {"messageId": "msg-current", "outcome": "completed", "finishedAtUnixMs": finished},
    }
    assert driver.turn_terminal(child, "msg-current") is None
