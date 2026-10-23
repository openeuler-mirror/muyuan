"""Native inference role contracts using controlled process output, not agents."""

import json
import os
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest

import goal_plus.evidence_annotator as annotation
import goal_plus.bootstrap as bootstrap
from goal_plus.domain.verifier import verifier_result_schema
from goal_plus.infrastructure.inference_bindings import build_pi_role_tools
from goal_plus.ports.inference import InferenceArtifactView, InferenceRoleCapabilities


def process_factory(agent_harness, mode, payload):
    class Process:
        pid = 123
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

        def __init__(self, command, **kwargs):
            if agent_harness == "codex":
                output = command[command.index("--output-last-message") + 1]
            else:
                output = next(value for key, value in kwargs["env"].items() if key in {
                    "GOAL_PLUS_PI_ANNOTATION_OUTPUT", "GOAL_PLUS_PI_VERIFIER_OUTPUT",
                })
            self.output = Path(output)

        def communicate(self, input=None, timeout=None):
            if mode == "timeout":
                if self.returncode is not None:
                    return "", ""
                time.sleep(min(timeout, 0.01))
                raise subprocess.TimeoutExpired("controlled-inference", timeout)
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
                if agent_harness == "codex" else {"type": "message_end", "message": message}
            )
            stderr = "HTTP 503 unavailable" if mode == "transient_process_error" else "invalid host configuration"
            return json.dumps(event), stderr if self.returncode else ""

    return Process


@pytest.mark.parametrize("agent_harness", ["codex", "pi"])
@pytest.mark.parametrize("mode", ["valid", "missing", "invalid", "encoding", "process_error", "transient_process_error"])
def test_annotation_role_output_and_failure_contracts(tmp_path, monkeypatch, agent_harness, mode, scripted_process_output):
    payload = {"description": "Changed lookup.", "supplemental_evaluation": None, "tool_views": []}
    build_inference = bootstrap.build_structured_inference
    monkeypatch.setattr(bootstrap, "build_structured_inference", lambda: build_inference(
        popen=process_factory(agent_harness, mode, payload),
    ))
    worker = annotation.CodexEvidenceAnnotator() if agent_harness == "codex" else annotation.PiEvidenceAnnotator()
    context = {
        "actual_diff": "+return lookup[key]", "comparison_basis": [],
        "annotator": {"agent_harness": agent_harness, "model": "test-model", "reasoning_effort": "low", "timeout_seconds": 30},
        "_annotation_monitor_path": str(tmp_path / "monitor.json"),
    }
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
        expected = {} if agent_harness == "pi" and mode.endswith("process_error") else {"input_tokens": 7, "output_tokens": 3}
        assert raised.value.usage == expected
    monitor = json.loads((tmp_path / "monitor.json").read_text())
    assert monitor["state"] == ("completed" if mode == "valid" else "failed")


@pytest.mark.parametrize("agent_harness", ["codex", "pi"])
@pytest.mark.parametrize(
    "mode",
    [
        "valid",
        "missing",
        "invalid",
        "encoding",
        "process_error",
        "timeout",
    ],
)
def test_verifier_uses_shared_inference_and_registration(
    tmp_path, monkeypatch, agent_harness, mode, scripted_process_output,
):
    from types import SimpleNamespace
    from goal_plus.application.verifier import VerifierWorkflow
    from goal_plus.infrastructure.clock import SystemClock
    from goal_plus.infrastructure.state.verifier import FileVerifierState
    from goal_plus.llm_verifier import LlmVerifier
    from goal_plus.ports.process import ProcessRegistration
    from tests.test_llm_verifier import subject, verifier_input
    from goal_plus.domain.verifier import verifier_score_contract

    workflow = VerifierWorkflow(FileVerifierState(tmp_path), SystemClock())
    workflow.ensure_assessment(subject(), verifier_score_contract(),
        verifier_input().model_copy(update={"agent_harness": agent_harness}))
    task = workflow.claim_task("role-test", 60, run_id="run_1")
    payload = {"quality_score": 0.8, "scores": None, "evidence_refs": [], "score_rationales": {"quality_score": "Valid attempt"}}
    build_inference = bootstrap.build_structured_inference
    monkeypatch.setattr(bootstrap, "build_structured_inference", lambda: build_inference(
        popen=process_factory(agent_harness, mode, payload),
    ))
    worker = LlmVerifier(tmp_path)
    worker.source = SimpleNamespace(
        read_run=lambda _: SimpleNamespace(frozen_spec_id="frozen_1", source_artifact_ref=None),
        read_frozen_spec=lambda _: SimpleNamespace(
            frozen_spec_id="frozen_1", agent_harness=agent_harness, runtime_provider="direct"),
    )
    if mode == "timeout":
        execute = worker.inference.execute
        monkeypatch.setattr(worker.inference, "execute", lambda request, **kwargs: execute(
            replace(request, timeout_seconds=0.01), **kwargs,
        ))
    monkeypatch.setenv("GOAL_PLUS_INTERNAL_AGENT_HARNESS", agent_harness)
    monkeypatch.setenv("GOAL_PLUS_CODEX_MODEL", "test-model")
    monkeypatch.setenv("GOAL_PLUS_CODEX_REASONING_EFFORT", "low")
    monkeypatch.setattr("goal_plus.host_scope.pi_native_agent_config_for_run",
                        lambda **_: {"model_pattern": "test-model", "thinking_level": "low"})
    resources = []
    monkeypatch.setattr("goal_plus.bootstrap.build_inference_registration", lambda *a, **kw: ProcessRegistration(
        register=lambda pid: resources.append(("registered", pid)) or "token",
        finish=lambda token: resources.append(("finished", token)),
    ))
    monkeypatch.setattr(worker, "_prompt", lambda _: ("Exact test attempt", {}))
    if mode == "valid":
        result, invocation, usage = worker.execute(task)
        assert result.model_dump(
            exclude={"steering_findings", "knowledge_updates"}
        ) == payload
        assert invocation.agent_harness == agent_harness
        assert invocation.runtime_provider == "direct"
        assert invocation.model == "test-model" and invocation.reasoning_effort == "low"
        assert usage == {"input_tokens": 7, "output_tokens": 3}
    else:
        error = TimeoutError if mode == "timeout" else RuntimeError if mode == "process_error" else ValueError
        with pytest.raises(error):
            worker.execute(task)
    assert resources == [("registered", 123), ("finished", "token")]
    assert workflow.get_task(task.task_id).state == "running"
    assert workflow.list_records("run_1") == []


@pytest.mark.parametrize(
    "role,with_views",
    [
        ("annotation", True),
        ("annotation", False),
        ("verifier", False),
    ],
)
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
        output_schema=(verifier_result_schema([]) if role == "verifier" else
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
const hooks = new Map();
register({ registerTool(tool) { tools.set(tool.name, tool); }, on(name, hook) { hooks.set(name, hook); } });
const outputName = [...tools.keys()].find(name => name.startsWith("submit_"));
const submission = await tools.get(outputName).execute("output", { answer: "accepted" });
let feedback = null;
if (hooks.has("context")) {
  const hook = hooks.get("context");
  const message = { role: "toolResult", toolName: outputName, isError: true, content: [{ type: "text",
    text: 'Validation failed for tool "submit_verifier_output":\\n  - score_rationales.quality_score: must not have more than 1000 characters\\n\\nReceived arguments:\\n' + 'private payload'.repeat(1000) }] };
  const original = JSON.stringify(message);
  feedback = hook({ messages: [message] }).messages[0];
  if (JSON.stringify(message) !== original) throw new Error("stored message changed");
  if (JSON.stringify(hook({ messages: [feedback] }).messages[0]) !== JSON.stringify(feedback)) {
    throw new Error("feedback changed on repeated projection");
  }
  for (const untouched of [
    { ...message, isError: false }, { ...message, toolName: "another_tool" },
    { ...message, role: "user" }, { ...message, content: [{ type: "text", text: "execution failed" }] },
  ]) {
    if (JSON.stringify(hook({ messages: [untouched] }).messages[0]) !== JSON.stringify(untouched)) {
      throw new Error("unrelated message changed");
    }
  }
}
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
console.log(JSON.stringify({ tools: [...tools.keys()], submission, read, denied, feedback,
  parameters: tools.get(outputName).parameters }));
''', str(extension)],
        env={**os.environ, binding.output_env: str(output)},
        capture_output=True, text=True, timeout=10, check=True,
    )
    result = json.loads(executed.stdout)
    assert set(result["tools"]) == set(binding.names)
    assert len(binding.names) == (3 if with_views else 1)
    assert result["submission"]["terminate"] is True
    if role == "verifier":
        from jsonschema import Draft202012Validator

        schema = result["parameters"]
        assert '"$ref"' not in json.dumps(schema)
        payload = {
            "quality_score": 0.8,
            "scores": None,
            "evidence_refs": [],
            "score_rationales": {"quality_score": "Valid local assessment."},
            "steering_findings": None,
            "knowledge_updates": None,
        }
        validator = Draft202012Validator(schema)
        validator.validate(payload)
        assert not validator.is_valid({**payload, "quality_score": "0.8"})
        assert not validator.is_valid({**payload, "extra": 1})
        assert not validator.is_valid({**payload, "score_rationales": {"quality_score": "x" * 1001}})
        feedback = result["feedback"]
        assert feedback["isError"] is True
        message = feedback["content"][0]["text"]
        assert "score_rationales.quality_score" in message and "1000 characters" in message
        assert "Received arguments" not in message and "private payload" not in message
        assert len(message) < 1300
    assert json.loads(output.read_text()) == {"answer": "accepted"}
    if with_views:
        assert result["read"]["content"][0]["text"] == "immutable"
        assert result["denied"] == 2
    assert (view / "code.txt").read_text() == "immutable"


def test_verifier_binding_cannot_acquire_annotation_views(tmp_path):
    with pytest.raises(ValueError, match="verifier cannot acquire"):
        build_pi_role_tools(
            InferenceRoleCapabilities("verifier", (InferenceArtifactView("target", tmp_path),)),
            output_schema={"type": "object"},
        )
