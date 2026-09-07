"""Native inference role contracts using controlled process output, not agents."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import goal_plus.evidence_annotator as annotation
from goal_plus.adaptive_search.scheduler import (
    CodexSchedulerExecutor,
    PermanentSchedulerExecutionError,
    PiSchedulerExecutor,
    ResolvedSchedulerProfile,
    SchedulerExecutionOutputError,
    SchedulerExecutionRequest,
    SchedulerOutput,
    TransientSchedulerExecutionError,
)
from goal_plus.infrastructure.inference_bindings import build_pi_role_tools
from goal_plus.ports.inference import InferenceArtifactView, InferenceRoleCapabilities


def process_factory(host, mode, payload):
    class Process:
        pid = 123
        returncode = None

        def __init__(self, command, **kwargs):
            if host == "codex":
                output = command[command.index("--output-last-message") + 1]
            else:
                output = next(value for key, value in kwargs["env"].items() if key in {
                    "GOAL_PLUS_PI_ANNOTATION_OUTPUT", "GOAL_PLUS_PI_SCHEDULER_OUTPUT",
                })
            self.output = Path(output)

        def communicate(self, input=None, timeout=None):
            self.returncode = 1 if mode in {"process_error", "transient_process_error"} else 0
            if mode != "missing":
                self.output.write_bytes(
                    b"\xff" if mode == "encoding" else
                    ("invalid json" if mode == "invalid" else json.dumps(payload)).encode(),
                )
            message = {"role": "assistant", "stopReason": "stop", "usage": {"input": 7, "output": 3}}
            if mode == "assistant_error":
                message.update(stopReason="error", errorMessage="HTTP 503 unavailable")
            event = (
                {"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 3}}
                if host == "codex" else {"type": "message_end", "message": message}
            )
            stderr = "HTTP 503 unavailable" if mode == "transient_process_error" else "invalid host configuration"
            return json.dumps(event), stderr if self.returncode else ""

    return Process


@pytest.mark.parametrize("host", ["codex", "pi-rpc"])
@pytest.mark.parametrize("mode", ["valid", "missing", "invalid", "encoding", "process_error", "transient_process_error"])
def test_annotation_role_output_and_failure_contracts(tmp_path, monkeypatch, host, mode, scripted_process_output):
    payload = {"description": "Changed lookup.", "supplemental_evaluation": None, "tool_views": []}
    monkeypatch.setattr(subprocess, "Popen", process_factory(host, mode, payload))
    context = {
        "actual_diff": "+return lookup[key]", "comparison_basis": [],
        "annotator": {"host": host, "model": "test-model", "reasoning_effort": "low", "timeout_seconds": 30},
        "_annotation_monitor_path": str(tmp_path / "monitor.json"),
    }
    worker = annotation.CodexEvidenceAnnotator() if host == "codex" else annotation.PiEvidenceAnnotator()
    error = {
        "missing": annotation.AnnotationOutputError, "invalid": annotation.AnnotationOutputError,
        "encoding": annotation.AnnotationOutputError,
        "process_error": annotation.PermanentAnnotationError,
        "transient_process_error": annotation.TransientAnnotationError,
    }.get(mode)
    if error is None:
        result = worker.annotate(context)
        assert result.description == payload["description"]
        assert result.usage == {"input_tokens": 7, "output_tokens": 3}
    else:
        with pytest.raises(error) as raised:
            worker.annotate(context)
        expected = {} if host == "pi-rpc" and mode.endswith("process_error") else {"input_tokens": 7, "output_tokens": 3}
        assert raised.value.usage == expected
    monitor = json.loads((tmp_path / "monitor.json").read_text())
    assert monitor["state"] == ("completed" if mode == "valid" else "failed")


@pytest.mark.parametrize("host", ["codex", "pi-rpc"])
@pytest.mark.parametrize("mode", ["valid", "missing", "invalid", "encoding", "not_object", "process_error", "transient_process_error"])
def test_scheduler_role_output_and_failure_contracts(tmp_path, host, mode, scripted_process_output):
    payload = [] if mode == "not_object" else {"answer": "accepted"}
    request = SchedulerExecutionRequest(
        profile=ResolvedSchedulerProfile(host=host, model="test-model", reasoning_effort="low", timeout_seconds=30),
        instructions="Evaluate settled input.", prompt="Input.", output_schema={"type": "object"},
        identity={"run_id": "run_one"}, monitor_path=tmp_path / "monitor.json",
    )
    worker = CodexSchedulerExecutor if host == "codex" else PiSchedulerExecutor
    executor = worker(popen=process_factory(host, mode, payload))
    error = {
        "missing": SchedulerExecutionOutputError, "invalid": SchedulerExecutionOutputError,
        "encoding": UnicodeDecodeError,
        "not_object": SchedulerExecutionOutputError, "process_error": PermanentSchedulerExecutionError,
        "transient_process_error": TransientSchedulerExecutionError,
    }.get(mode)
    if error is None:
        result = executor.execute(request)
        assert result.payload == payload
        assert result.usage == {"input_tokens": 7, "output_tokens": 3}
        assert result.invocation.host == host
        assert result.invocation.task_kind == "search_scheduler"
        assert result.invocation.model == "test-model"
    else:
        with pytest.raises(error) as raised:
            executor.execute(request)
        if mode != "encoding":
            expected = {} if host == "pi-rpc" and mode.endswith("process_error") else {"input_tokens": 7, "output_tokens": 3}
            assert raised.value.usage == expected
    monitor = json.loads(request.monitor_path.read_text())
    assert monitor["state"] == ("completed" if mode == "valid" else "process_exited" if mode == "encoding" else "failed")


@pytest.mark.parametrize("role,with_views", [("annotation", True), ("annotation", False), ("scheduler", False)])
def test_pi_role_binding_executes_only_authorized_tools(tmp_path, role, with_views):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the generated Pi extension")
    version = subprocess.check_output([node, "--version"], text=True).strip()
    if int(version.removeprefix("v").split(".")[0]) < 22:
        pytest.skip("Node 22+ is required for native TypeScript stripping")
    view = tmp_path / "view"
    view.mkdir()
    (view / "code.txt").write_text("immutable")
    (tmp_path / "outside.txt").write_text("private")
    views = (InferenceArtifactView("target", view),) if with_views else ()
    binding = build_pi_role_tools(
        InferenceRoleCapabilities(role, views),
        output_schema=(SchedulerOutput.model_json_schema() if role == "scheduler" else
                       {"type": "object", "properties": {"answer": {"type": "string"}}}),
    )
    extension = tmp_path / "role.mts"
    extension.write_text(binding.extension)
    output = tmp_path / "output.json"
    executed = subprocess.run(
        [node, "--experimental-strip-types", "--input-type=module", "-e", '''
import { pathToFileURL } from "node:url";
const { default: register } = await import(pathToFileURL(process.argv[1]).href);
const tools = new Map();
register({ registerTool(tool) { tools.set(tool.name, tool); } });
const outputName = [...tools.keys()].find(name => name.startsWith("submit_"));
const submission = await tools.get(outputName).execute("output", { answer: "accepted" });
let read = null;
let denied = 0;
if (tools.has("artifact_view_read")) {
  const tool = tools.get("artifact_view_read");
  read = await tool.execute("read", { view_id: "target", path: "code.txt", offset: 0, length: 16 });
  for (const params of [
    { view_id: "target", path: "../outside.txt" },
    { view_id: "other", path: "code.txt" },
  ]) {
    try { await tool.execute("denied", { ...params, offset: 0, length: 16 }); }
    catch { denied++; }
  }
}
console.log(JSON.stringify({ tools: [...tools.keys()], submission, read, denied,
  parameters: tools.get(outputName).parameters }));
''', str(extension)],
        env={**os.environ, binding.output_env: str(output)},
        capture_output=True, text=True, timeout=10, check=True,
    )
    result = json.loads(executed.stdout)
    assert set(result["tools"]) == set(binding.names)
    assert len(binding.names) == (3 if with_views else 1)
    assert result["submission"]["terminate"] is True
    if role == "scheduler":
        from jsonschema import Draft202012Validator

        schema = result["parameters"]
        assert schema["properties"]["reward"]["type"] == "object"
        assert '"$ref"' not in json.dumps(schema)
        payload = {
            "reward": {"reward": 1, "rubric_score": 1, "exploitation": 1,
                       "exploration": 0, "uct": 1, "components": {"outcome": 1},
                       "rationale": "Valid public result."},
            "allocation_flag": False, "allocation_rationale": "Continue.",
            "allocation_confidence": 1, "decision": None,
        }
        validator = Draft202012Validator(schema)
        validator.validate(payload)
        assert not validator.is_valid({**payload, "reward": json.dumps(payload["reward"])})
        assert not validator.is_valid({**payload, "reward": {**payload["reward"], "extra": 1}})
    assert json.loads(output.read_text()) == {"answer": "accepted"}
    if with_views:
        assert result["read"]["content"][0]["text"] == "immutable"
        assert result["denied"] == 2
    assert (view / "code.txt").read_text() == "immutable"


def test_scheduler_binding_cannot_acquire_annotation_views(tmp_path):
    with pytest.raises(ValueError, match="scheduler cannot acquire"):
        build_pi_role_tools(
            InferenceRoleCapabilities("scheduler", (InferenceArtifactView("target", tmp_path),)),
            output_schema={"type": "object"},
        )
