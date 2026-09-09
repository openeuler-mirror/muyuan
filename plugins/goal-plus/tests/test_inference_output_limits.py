"""Output budgets are enforced without running a native model or a full Search."""

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from goal_plus.bootstrap import build_process_monitor, build_structured_inference
from goal_plus.execution.managed_process import ManagedProcess
from goal_plus.execution.process_output import ProcessOutput
from goal_plus.ports.process_monitor import ProcessMonitorIdentity
from tests.test_structured_inference import request_for


def monitor():
    return build_process_monitor(None, ProcessMonitorIdentity())


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_managed_output_limit_stops_live_writer_and_bounds_capture(stream):
    process = subprocess.Popen(
        [sys.executable, "-c", "import os,sys,time; os.write(int(sys.argv[1]),b'x'*4096); time.sleep(60)",
         "1" if stream == "stdout" else "2"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        outcome = ManagedProcess(process, output_limit_bytes=128).wait(
            5, monitor=monitor(), is_active=None, timeout_detail="timeout", cancellation_detail="closed",
        )
        assert outcome.state == "output_limit_exceeded"
        assert len(outcome.stdout.encode()) <= 128
        assert len(outcome.stderr.encode()) <= 128
        assert stream in outcome.detail
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()


@pytest.mark.parametrize("size", [127, 128, 129])
def test_managed_limit_counts_bytes_and_includes_output_after_fast_exit(size):
    process = subprocess.Popen(
        [sys.executable, "-c", "import os,sys; os.write(1,b'x'*int(sys.argv[1]))", str(size)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        process.wait(timeout=5)
        outcome = ManagedProcess(process, output_limit_bytes=128).wait(
            5, monitor=monitor(), is_active=None, timeout_detail="timeout", cancellation_detail="closed",
        )
        assert outcome.state == ("output_limit_exceeded" if size > 128 else "process_exited")
        assert outcome.stdout == "x" * min(size, 128)
    finally:
        process.stdout.close()
        process.stderr.close()


@pytest.mark.parametrize("host", ["codex", "pi-rpc"])
def test_inference_rejects_oversized_result_before_json_decode(tmp_path, monkeypatch, host):
    import goal_plus.execution.inference_output as output_module

    monkeypatch.setattr(output_module, "INFERENCE_RESULT_LIMIT_BYTES", 128)
    native_popen = subprocess.Popen
    directories = []

    def popen(command, **options):
        output = (command[command.index("--output-last-message") + 1] if host == "codex"
                  else options["env"]["TEST_INFERENCE_OUTPUT"])
        directories.append(Path(output).parent)
        script = (
            "import json,sys; from pathlib import Path; "
            "Path(sys.argv[1]).write_text(json.dumps({'answer':'x'*1024})); "
            "print(json.dumps({'type':'message_end','message':{'role':'assistant','stopReason':'stop'}}))"
        )
        return native_popen([sys.executable, "-c", script, output], **options)

    result = build_structured_inference(popen=popen).execute(
        request_for(host), monitor=monitor(), is_active=None, usage=lambda *_: {"output_tokens": 7},
    )
    assert result.process.state == "process_exited"
    assert result.payload is None and result.output_text is None
    assert isinstance(result.output_error, ValueError)
    assert "InferenceOutputLimitExceeded" in str(result.output_error)
    assert result.usage == {"output_tokens": 7}
    assert not directories[0].exists()


@pytest.mark.parametrize("host", ["codex", "pi-rpc"])
def test_inference_does_not_parse_partial_usage_or_accept_output_after_stream_overflow(monkeypatch, host):
    import goal_plus.execution.process_output as output_module

    monkeypatch.setattr(output_module, "PROCESS_OUTPUT_LIMIT_BYTES", 128)
    native_popen = subprocess.Popen

    def popen(command, **options):
        output = (command[command.index("--output-last-message") + 1] if host == "codex"
                  else options["env"]["TEST_INFERENCE_OUTPUT"])
        script = (
            "import os,sys; from pathlib import Path; "
            "Path(sys.argv[1]).write_text('{}'); os.write(1,b'x'*4096)"
        )
        return native_popen([sys.executable, "-c", script, output], **options)

    def no_partial_usage(*_args):
        pytest.fail("overflowed transcript was treated as complete usage")

    result = build_structured_inference(popen=popen).execute(
        replace(request_for(host), timeout_seconds=5), monitor=monitor(), is_active=None, usage=no_partial_usage,
    )
    assert result.process.state == "output_limit_exceeded"
    assert result.payload is None and result.output_text is None
    assert result.usage == {}
    assert result.stop_detail.startswith("ProcessOutputLimitExceeded")


def test_pipe_collector_preserves_split_utf8_and_newlines_without_native_communicate(tmp_path):
    release = tmp_path / "release"
    script = (
        "import os,sys,time; from pathlib import Path\n"
        "os.write(1,b'\\xe4'); os.write(2,b'diagnostic\\r\\n')\n"
        "while not Path(sys.argv[1]).exists(): time.sleep(.01)\n"
        "os.write(1,b'\\xb8\\xad\\r\\nend\\r')\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(release)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    collector = ProcessOutput(process, limit_bytes=128)

    def unexpected(*_args, **_kwargs):
        pytest.fail("native communicate would accumulate unbounded output")

    process.communicate = unexpected
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            collector.communicate(timeout=0.2)
        release.touch()
        stdout, stderr = collector.communicate(timeout=5)
        assert stdout == "\u4e2d\nend\n"
        assert stderr == "diagnostic\n"
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        collector.close()
    assert process.stdout.closed and process.stderr.closed


@pytest.mark.parametrize("kind", ["fifo", "directory", "symlink", "growing", "utf8_boundary"])
def test_result_reader_rejects_special_files_and_enforces_bound_on_read(tmp_path, monkeypatch, kind):
    import goal_plus.execution.inference_output as output_module

    monkeypatch.setattr(output_module, "INFERENCE_RESULT_LIMIT_BYTES", 128)
    output = tmp_path / "output.json"
    if kind == "fifo":
        os.mkfifo(output)
    elif kind == "directory":
        output.mkdir()
    elif kind == "symlink":
        target = tmp_path / "other.txt"
        target.write_text("not an authorized result")
        output.symlink_to(target)
    else:
        output.write_bytes(b"x" * (127 if kind == "utf8_boundary" else 128) + b"\xc3\xa9")
        if kind == "growing":
            from types import SimpleNamespace

            fstat = os.fstat
            monkeypatch.setattr(output_module.os, "fstat", lambda fd: SimpleNamespace(
                st_mode=fstat(fd).st_mode, st_size=128,
            ))
    with pytest.raises((ValueError, OSError)):
        output_module.read_inference_output(output)


@pytest.mark.parametrize("size", [127, 128])
def test_result_reader_accepts_exact_byte_budget(tmp_path, monkeypatch, size):
    import goal_plus.execution.inference_output as output_module

    monkeypatch.setattr(output_module, "INFERENCE_RESULT_LIMIT_BYTES", 128)
    output = tmp_path / "output.json"
    content = b"x" * (size - 2) + b"\xc3\xa9"
    output.write_bytes(content)
    assert output_module.read_inference_output(output).encode() == content


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_capture_budget_still_reaps_owned_process(limit):
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        with pytest.raises(ValueError, match="positive integer"):
            ManagedProcess(process, output_limit_bytes=limit).wait(
                5, monitor=monitor(), is_active=None, timeout_detail="timeout", cancellation_detail="closed",
            )
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@pytest.mark.parametrize("role", ["annotation", "scheduler"])
@pytest.mark.parametrize("host", ["codex", "pi-rpc"])
@pytest.mark.parametrize("source", ["transcript", "result"])
def test_roles_reject_overflow_through_their_existing_output_error_contract(
    tmp_path, monkeypatch, role, host, source,
):
    import goal_plus.evidence_annotator as annotation
    import goal_plus.adaptive_search.scheduler as scheduler
    import goal_plus.execution.inference_output as result_module
    import goal_plus.execution.process_output as output_module

    if source == "transcript":
        monkeypatch.setattr(output_module, "PROCESS_OUTPUT_LIMIT_BYTES", 128)
    else:
        monkeypatch.setattr(result_module, "INFERENCE_RESULT_LIMIT_BYTES", 128)
    native_popen = subprocess.Popen
    processes = []

    def popen(command, **options):
        output = (command[command.index("--output-last-message") + 1] if host == "codex" else
                  options["env"]["GOAL_PLUS_PI_ANNOTATION_OUTPUT" if role == "annotation"
                                 else "GOAL_PLUS_PI_SCHEDULER_OUTPUT"])
        event = {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}}
        script = (
            "import os,sys; from pathlib import Path; "
            "Path(sys.argv[1]).write_text(sys.argv[2]); "
            "os.write(1,sys.argv[3].encode())"
        )
        payload = json.dumps({"description": "x" * 256, "supplemental_evaluation": None, "tool_views": []})
        process = native_popen(
            [sys.executable, "-c", script, output, payload,
             "x" * 4096 if source == "transcript" else json.dumps(event)], **options,
        )
        processes.append(process)
        return process

    monitor_path = tmp_path / "monitor.json"
    try:
        error = annotation.AnnotationOutputError if role == "annotation" else scheduler.SchedulerExecutionOutputError
        with pytest.raises(error, match="OutputLimitExceeded"):
            if role == "annotation":
                monkeypatch.setattr(subprocess, "Popen", popen)
                worker = annotation.CodexEvidenceAnnotator() if host == "codex" else annotation.PiEvidenceAnnotator()
                worker.annotate({
                    "actual_diff": "+return lookup[key]", "comparison_basis": [],
                    "annotator": {"host": host, "model": "test-model", "reasoning_effort": "low", "timeout_seconds": 5},
                    "_annotation_monitor_path": str(monitor_path),
                })
            else:
                worker = scheduler.CodexSchedulerExecutor if host == "codex" else scheduler.PiSchedulerExecutor
                worker(popen=popen).execute(scheduler.SchedulerExecutionRequest(
                    profile=scheduler.ResolvedSchedulerProfile(
                        host=host, model="test-model", reasoning_effort="low", timeout_seconds=5,
                    ),
                    instructions="Evaluate.", prompt="Input.", output_schema={"type": "object"},
                    identity={"run_id": "run_one"}, monitor_path=monitor_path,
                ))
        assert processes[0].poll() is not None
        assert json.loads(monitor_path.read_text())["state"] == "failed"
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
