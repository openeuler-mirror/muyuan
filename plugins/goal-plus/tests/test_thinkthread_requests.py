"""Exact provider request observation and close recovery."""

import pytest

from goal_plus.execution.thinkthread_requests import ThinkThreadRequests
from tests._thinkthread_sdk import StatefulAgentPosix


@pytest.mark.parametrize("field,value", [("requestId", "req-peer"), ("method", "fs.apply")])
def test_request_recovery_rejects_another_operation_result(field, value):
    sdk = StatefulAgentPosix()
    sdk.request_results["req-baseline"] = {
        "requestId": "req-baseline", "method": "fs.snapshot.create",
        "state": "succeeded", "result": {"snapshotId": "fsnap-peer"}, field: value,
    }
    requests = ThinkThreadRequests(sdk)
    with pytest.raises(RuntimeError, match="identity mismatch"):
        requests.observe("req-baseline", "fs.snapshot.create")
    sdk.close_completion_unknown_before_once = True
    result = requests.close("req-baseline", "fs.snapshot.create")
    assert result.state == "needs_recovery"
    assert "identity mismatch" in result.error["message"]
    assert sdk.closed_requests == set()
