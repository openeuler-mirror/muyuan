"""Exercise the native inference API with controlled output and local processes."""

import json
import shutil
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from goal_plus.bootstrap import build_inference_registration, build_process_monitor, build_structured_inference
from goal_plus.host_scope import ensure_execution_scope, execution_scope_snapshot
from goal_plus.execution.inference_output import pi_assistant_message, strict_output_schema
from goal_plus.ports.inference import PiInferenceTools, StructuredInferenceRequest
from goal_plus.ports.process_monitor import ProcessMonitorIdentity
from goal_plus.ports.sandbox import ProcessSandbox


def request_for(agent_harness):
    return StructuredInferenceRequest(
        agent_harness=agent_harness, instructions="Evaluate input.", prompt="Input data.",
        output_schema={"type": "object", "properties": {"answer": {"type": "string", "default": "none"}}},
        model="test-provider/test-model", reasoning_effort="low", timeout_seconds=5,
        timeout_detail="inference timeout", cancellation_detail="inference closed",
        pi_tools=PiInferenceTools(
            names=("submit_output", "read_view"), extension="export default function() {}",
            output_env="TEST_INFERENCE_OUTPUT",
        ) if agent_harness == "pi" else None,
    )


@pytest.mark.host_process
@pytest.mark.parametrize(("agent_harness", "protected"), [("codex", False), ("pi", False), ("pi", True)])
def test_native_request_executes_with_file_stdin_and_cleans_its_files(tmp_path, monkeypatch, agent_harness, protected):
    if protected:
        backend = "/usr/bin/sandbox-exec" if sys.platform == "darwin" else "bwrap"
        if (sys.platform != "darwin" and not sys.platform.startswith("linux")) or shutil.which(backend) is None:
            pytest.skip("Seatbelt or bubblewrap is required for the protected Pi launch")
    calls = []
    native_popen = subprocess.Popen
    monkeypatch.setenv("TEST_NATIVE_CONFIG", "host-only-value")
    request = request_for(agent_harness)
    root = tmp_path / ".gp"
    if protected:
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
        config = home / ".pi/agent"
        config.mkdir(parents=True)
        (config / "models.json").write_text('{"providers":{}}')
        request = replace(request, sandbox=ProcessSandbox(
            mode="workspace-write", backend="outer", workspace=root, writable_roots=(root,),
        ))
    scope = ensure_execution_scope(
        root_dir=root, agent_harness="codex" if agent_harness == "codex" else "pi",
        owner_session_id="inference", run_id="run_inference",
    )
    registration = build_inference_registration(root, "run_inference", label="test-inference")

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        assert kwargs["stdin"].read() == request.prompt
        kwargs["stdin"].seek(0)
        assert kwargs["env"]["TEST_NATIVE_CONFIG"] == "host-only-value"
        assert "start_new_session" not in kwargs
        if agent_harness == "codex":
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
            if protected:
                scratch = Path(kwargs["env"]["TMPDIR"])
                assert scratch.is_dir()
                pi_home = Path(kwargs["env"]["PI_CODING_AGENT_DIR"])
                assert pi_home.is_relative_to(kwargs["cwd"])
                assert (pi_home / "models.json").read_text() == '{"providers":{}}'
        event = {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}}
        script = (
            "import json,os,subprocess,sys,tempfile\nfrom pathlib import Path\n"
            "with tempfile.TemporaryFile() as scratch: scratch.write(b'tmp works')\n"
            "scope=json.loads(Path(sys.argv[3]).read_text())\n"
            "registered=[p for p in scope['processes'] if p['kind']=='internal' and p['status']=='registered']\n"
            "assert len(registered) == 1\n"
            f"if not {protected!r}: assert registered[0]['pid'] == os.getpid()\n"
            "text=sys.stdin.read()\n"
            "Path(sys.argv[1]).write_text(json.dumps({'answer':text}))\n"
            "event=json.loads(sys.argv[2]); event['test_event']=True\n"
            "print(json.dumps(event))\n"
        )
        scope_path = root / "host-scopes" / scope["scope_id"] / "scope.json"
        native_offset = command.index("codex" if agent_harness == "codex" else "pi")
        return native_popen([
            *command[:native_offset], sys.executable, "-c", script, output_path, json.dumps(event), str(scope_path),
        ], **kwargs)

    monitor = build_process_monitor(tmp_path / "monitor.json", ProcessMonitorIdentity(agent_harness=agent_harness))
    result = build_structured_inference(popen=popen).execute(
        request, monitor=monitor, is_active=None, usage=lambda _stdout, _message: {"input_tokens": 2},
        registration=registration,
    )
    assert result.process.state == "process_exited"
    assert result.returncode == 0, result.process.stderr
    assert result.payload == {"answer": request.prompt}
    assert execution_scope_snapshot(root_dir=root, scope_id=scope["scope_id"])["processes"][0]["status"] == "finished"
    assert result.output_error is None
    assert result.usage == {"input_tokens": 2}
    assert result.invocation.agent_harness == agent_harness
    assert result.invocation.model == request.model
    assert result.invocation.elapsed_seconds >= 0
    assert result.invocation.invocation_id.startswith("invocation_")
    assert (result.assistant_message is not None) == (agent_harness == "pi")
    command, kwargs = calls[0]
    directory = Path(command[command.index("-C") + 1]) if agent_harness == "codex" else kwargs["cwd"]
    assert not directory.exists()
    if protected:
        assert not Path(kwargs["env"]["TMPDIR"]).exists()
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


@pytest.mark.parametrize("agent_harness", ["codex", "pi", "thinkthread"])
def test_unsupported_host_or_mismatched_tool_bundle_cannot_launch(agent_harness):
    request = request_for("codex")
    request = replace(
        request, agent_harness=agent_harness,
        pi_tools=PiInferenceTools(names=("submit",), extension="", output_env="OUTPUT") if agent_harness == "codex" else None,
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


@pytest.mark.parametrize("protected", [False, True])
def test_transcript_capture_does_not_change_one_shot_pi_launch(tmp_path, scripted_process_output, monkeypatch, protected):
    from goal_plus.infrastructure.inference_transcript import FileInferenceTranscript
    from goal_plus.ports.inference import InferenceTranscriptIdentity

    commands = []
    policies = []
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "empty-config"))
    monkeypatch.setattr("goal_plus.execution.structured_inference.resolve_sandbox_command",
                        lambda command, **kwargs: policies.append(kwargs["sandbox"]) or command)

    class Process:
        returncode = 0
        pid = 123

        def __init__(self, command, **kwargs):
            commands.append(command)
            assert "--no-session" in command
            assert command.count("--extension") == 2
            observer = Path(command[command.index("--extension", command.index("--extension") + 1) + 1])
            assert "before_provider_request" in observer.read_text(encoding="utf-8")
            provider_path = kwargs["cwd"] / "provider-requests.jsonl"
            assert json.dumps(str(provider_path)) in observer.read_text(encoding="utf-8")
            provider_path.write_text(json.dumps({"type": "provider_request", "payload": {"input": "exact"}}) + "\n")
            Path(kwargs["env"]["TEST_INFERENCE_OUTPUT"]).write_text('{"answer":"ok"}')

        def communicate(self, **_kwargs):
            return "", ""

    transcript = FileInferenceTranscript(tmp_path, InferenceTranscriptIdentity(
        "verifier", "run_1", "task_1", "settlement_1", "c001", 2, 1,
    ))
    request = request_for("pi")
    if protected:
        request = replace(request, sandbox=ProcessSandbox(
            mode="workspace-write", backend="outer", workspace=tmp_path,
        ))
    result = build_structured_inference(popen=Process).execute(
        request, monitor=build_process_monitor(None, ProcessMonitorIdentity()),
        is_active=None, usage=lambda *_: {}, transcript=transcript,
    )
    assert result.payload == {"answer": "ok"}
    assert len(commands) == 1
    assert (transcript.directory / "input.json").is_file()
    assert json.loads((transcript.directory / "command.json").read_text())["argv"].count("--extension") == 2
    assert (transcript.directory / "result.json").is_file()
    assert json.loads((transcript.directory / "provider-requests.jsonl").read_text())["payload"] == {"input": "exact"}
    assert json.loads((transcript.directory / "record.json").read_text())["capture_status"] == "complete"
    assert not transcript.provider_path.exists()
    if protected:
        assert json.loads((transcript.directory / "input.json").read_text())["sandbox"]["workspace"] == str(tmp_path)
        assert policies[0].workspace != tmp_path
        assert all(path != tmp_path and path != transcript.directory for path in policies[0].writable_roots)
