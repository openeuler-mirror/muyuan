"""Exercise the native inference API with controlled output and local processes."""

import json
import os
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from goal_plus.bootstrap import build_inference_registration, build_process_monitor, build_structured_inference
from goal_plus.host_scope import ensure_execution_scope, execution_scope_snapshot
from goal_plus.execution.process_identity import process_alive
from goal_plus.execution.inference_output import pi_assistant_message, strict_output_schema
from goal_plus.ports.inference import PiInferenceTools, StructuredInferenceRequest
from goal_plus.ports.process_monitor import ProcessMonitorIdentity


def request_for(host):
    return StructuredInferenceRequest(
        host=host, instructions="Evaluate input.", prompt="Input data.",
        output_schema={"type": "object", "properties": {"answer": {"type": "string", "default": "none"}}},
        model="test-provider/test-model", reasoning_effort="low", timeout_seconds=5,
        timeout_detail="inference timeout", cancellation_detail="inference closed",
        pi_tools=PiInferenceTools(
            names=("submit_output", "read_view"), extension="export default function() {}",
            output_env="TEST_INFERENCE_OUTPUT",
        ) if host == "pi-rpc" else None,
    )


@pytest.mark.linux_procfs
@pytest.mark.parametrize("host", ["codex", "pi-rpc"])
def test_native_request_executes_with_file_stdin_and_cleans_its_files(tmp_path, monkeypatch, host):
    calls = []
    native_popen = subprocess.Popen
    monkeypatch.setenv("TEST_NATIVE_CONFIG", "host-only-value")
    request = request_for(host)
    root = tmp_path / ".gp"
    scope = ensure_execution_scope(
        root_dir=root, host="codex" if host == "codex" else "pi", owner_pid=os.getpid(),
        owner_session_id="inference", run_id="run_inference", start_guardian=False,
    )
    registration = build_inference_registration(root, "run_inference", label="test-inference")

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        assert kwargs["stdin"].read() == request.prompt
        kwargs["stdin"].seek(0)
        assert kwargs["env"]["TEST_NATIVE_CONFIG"] == "host-only-value"
        assert kwargs["start_new_session"] is True
        if host == "codex":
            output_path = command[command.index("--output-last-message") + 1]
            schema = json.loads(Path(command[command.index("--output-schema") + 1]).read_text())
            assert schema["required"] == ["answer"]
            assert schema["additionalProperties"] is False
            assert "default" not in schema["properties"]["answer"]
            assert "cwd" not in kwargs
            assert command[command.index("--sandbox") + 1] == "read-only"
            assert Path(command[command.index("-C") + 1], "AGENTS.md").read_text() == request.instructions
        else:
            output_path = kwargs["env"]["TEST_INFERENCE_OUTPUT"]
            assert command[command.index("--tools") + 1] == "submit_output,read_view"
            assert "--no-builtin-tools" in command
            assert "--no-context-files" in command
            assert "--no-extensions" in command
            assert "--no-skills" in command
            assert command[command.index("--provider") + 1] == "test-provider"
            assert command[command.index("--model") + 1] == "test-model"
            assert Path(command[command.index("--extension") + 1]).read_text() == request.pi_tools.extension
            assert kwargs["cwd"] == Path(output_path).parent
        event = {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}}
        script = (
            "import json,os,subprocess,sys\nfrom pathlib import Path\n"
            "scope=json.loads(Path(sys.argv[3]).read_text())\n"
            "assert any(p['pid']==os.getpid() and p['kind']=='internal' and p['status']=='running' for p in scope['processes'])\n"
            "text=sys.stdin.read()\n"
            "Path(sys.argv[1]).write_text(json.dumps({'answer':text}))\n"
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
            "event=json.loads(sys.argv[2]); event['test_child_pid']=child.pid\n"
            "print(json.dumps(event))\n"
        )
        scope_path = root / "host-scopes" / scope["scope_id"] / "scope.json"
        native_offset = command.index("codex" if host == "codex" else "pi")
        return native_popen([
            *command[:native_offset], sys.executable, "-c", script, output_path, json.dumps(event), str(scope_path),
        ], **kwargs)

    monitor = build_process_monitor(tmp_path / "monitor.json", ProcessMonitorIdentity(host=host))
    result = build_structured_inference(popen=popen).execute(
        request, monitor=monitor, is_active=None, usage=lambda _stdout, _message: {"input_tokens": 2},
        registration=registration,
    )
    assert result.process.state == "process_exited"
    assert result.returncode == 0
    assert result.payload == {"answer": request.prompt}
    assert not process_alive(json.loads(result.process.stdout)["test_child_pid"])
    assert execution_scope_snapshot(root_dir=root, scope_id=scope["scope_id"])["processes"][0]["status"] == "finished"
    assert result.output_error is None
    assert result.usage == {"input_tokens": 2}
    assert result.invocation.host == host
    assert result.invocation.model == request.model
    assert result.invocation.elapsed_seconds >= 0
    assert result.invocation.invocation_id.startswith("invocation_")
    assert (result.assistant_message is not None) == (host == "pi-rpc")
    command, kwargs = calls[0]
    directory = Path(command[command.index("-C") + 1]) if host == "codex" else kwargs["cwd"]
    assert not directory.exists()
    assert "host-only-value" not in json.dumps(asdict(result))
    assert request.output_schema["properties"]["answer"]["default"] == "none"
    assert json.loads((tmp_path / "monitor.json").read_text())["state"] == "process_exited"


@pytest.mark.parametrize("content", [None, "bad json", "[]", "null", '{"answer":"ok"}'])
def test_output_observation_distinguishes_missing_invalid_and_valid_json(content, scripted_process_output):
    class Process:
        returncode = 0
        pid = 123

        def __init__(self, command, **_kwargs):
            if content is not None:
                Path(command[command.index("--output-last-message") + 1]).write_text(content)

        def communicate(self, **_kwargs):
            return "", ""

    result = build_structured_inference(popen=Process).execute(
        request_for("codex"), monitor=build_process_monitor(None, ProcessMonitorIdentity()),
        is_active=None, usage=lambda *_: {},
    )
    assert result.output_text == content
    assert (result.output_error is not None) == (content == "bad json")
    if content not in {None, "bad json"}:
        assert result.payload == json.loads(content)


@pytest.mark.parametrize("host", ["codex", "pi-rpc", "thinkthread"])
def test_unsupported_host_or_mismatched_tool_bundle_cannot_launch(host):
    request = request_for("codex")
    request = replace(
        request, host=host,
        pi_tools=PiInferenceTools(names=("submit",), extension="", output_env="OUTPUT") if host == "codex" else None,
    )

    def unexpected_launch(*_args, **_kwargs):
        pytest.fail("invalid request launched a process")

    with pytest.raises(ValueError):
        build_structured_inference(popen=unexpected_launch).execute(
            request, monitor=build_process_monitor(None, ProcessMonitorIdentity()), is_active=None, usage=lambda *_: {},
        )


def test_shared_schema_normalization_and_event_decode_are_pure():
    schema = {"type": "array", "items": {"type": "object", "properties": {"value": {"type": "integer", "default": 1}}}}
    before = json.dumps(schema)
    assert strict_output_schema(schema)["items"]["required"] == ["value"]
    assert json.dumps(schema) == before
    latest = {"role": "assistant", "stopReason": "error", "errorMessage": "503"}
    stdout = "not-json\n[]\n" + "\n".join(json.dumps(item) for item in [
        {"type": "message_end", "message": {"role": "assistant", "content": []}},
        {"type": "message_end", "message": {"role": "user"}},
        {"type": "agent_end", "messages": [latest, {"role": "tool"}]},
        {"type": "agent_end", "messages": [{"role": "user"}]},
    ])
    assert pi_assistant_message(stdout) == latest
