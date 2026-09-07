"""Process-boundary regressions without model calls or Search bootstrap."""

import json
import subprocess
from pathlib import Path

import pytest

import goal_plus.adaptive_search.scheduler as scheduler_module
from goal_plus.adaptive_search.scheduler import (
    CodexSchedulerExecutor,
    PermanentSchedulerExecutionError,
    PiSchedulerExecutor,
    ResolvedSchedulerProfile,
    SchedulerExecutionRequest,
    TransientSchedulerExecutionError,
)


@pytest.mark.parametrize("host", ["codex", "pi-rpc"])
@pytest.mark.parametrize("reason", ["timeout", "closed"])
@pytest.mark.parametrize("drain_timeout", [False, True])
def test_scheduler_keeps_final_output_after_termination(
    tmp_path, monkeypatch, host, reason, drain_timeout, scripted_process_output,
):
    ticks = [100.0]
    active = [True]
    processes = []
    monkeypatch.setattr(scheduler_module.time, "monotonic", lambda: ticks[0])
    final_stdout = '{"type":"message_end","message":{"role":"assistant"}}\n'
    final_stderr = "final shutdown diagnostic\n"

    class Process:
        pid = 123
        returncode = None

        def __init__(self, command, **_kwargs):
            self.command = command
            processes.append(self)

        def communicate(self, input=None, timeout=None):
            if self.returncode is None:
                if reason == "timeout":
                    ticks[0] += 31
                else:
                    active[0] = False
                raise subprocess.TimeoutExpired(self.command, timeout, output=b"", stderr=b"")
            if drain_timeout:
                raise subprocess.TimeoutExpired(
                    self.command, timeout,
                    output=final_stdout.encode(), stderr=final_stderr.encode(),
                )
            return final_stdout, final_stderr

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

    request = SchedulerExecutionRequest(
        profile=ResolvedSchedulerProfile(
            host=host, model="test-model", reasoning_effort="low", timeout_seconds=30,
        ),
        instructions="Compute the result.", prompt="Settled input.",
        output_schema={"type": "object"}, identity={"run_id": "run_one"},
        monitor_path=tmp_path / "monitor.json", is_active=lambda: active[0],
    )
    executor = CodexSchedulerExecutor if host == "codex" else PiSchedulerExecutor
    error = TransientSchedulerExecutionError if reason == "timeout" else PermanentSchedulerExecutionError
    with pytest.raises(error):
        executor(popen=Process).execute(request)
    monitor = json.loads(Path(request.monitor_path).read_text())
    assert monitor["state"] == ("timed_out" if reason == "timeout" else "terminated")
    assert monitor["stdout_tail"] == final_stdout
    assert monitor["stderr_tail"] == final_stderr
    assert monitor["json_lines"] == 1
    assert processes[0].returncode == -15
