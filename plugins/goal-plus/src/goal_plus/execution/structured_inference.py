"""One-shot native CLI invocation; role adapters retain acceptance and retry policy."""

import json
import os
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from goal_plus.execution.inference_output import pi_assistant_message, read_inference_output, strict_output_schema
from goal_plus.execution.managed_process import ManagedProcess
from goal_plus.execution.launch_gate import ProcessLaunchGate
from goal_plus.execution.process_monitor import ProcessMonitor
from goal_plus.execution.sandbox import resolve_sandbox_command
from goal_plus.ports.clock import Clock
from goal_plus.ports.process import ProcessRegistration
from goal_plus.ports.inference import (
    InferenceInvocation,
    StructuredInferenceRequest,
    StructuredInferenceResult,
)


class StructuredInference:
    def __init__(
        self, *, clock: Clock, popen: Callable[..., subprocess.Popen[str]] | None = None,
    ) -> None:
        self.clock = clock
        self.popen = popen or subprocess.Popen
        self._active: ManagedProcess | None = None
        self._pending_cleanup: tempfile.TemporaryDirectory | None = None
        self._registration: ProcessRegistration | None = None
        self._registration_token: str | None = None

    def _finish_registration(self) -> None:
        if self._registration is not None and self._registration_token is not None:
            self._registration.finish(self._registration_token)
        self._registration_token = None
        self._registration = None

    def terminate(self) -> None:
        if self._active is not None:
            self._active.terminate()
            self._finish_registration()
            if self._pending_cleanup is not None:
                self._active.close()
                self._pending_cleanup.cleanup()
                self._pending_cleanup = None
                self._active = None

    def execute(
        self,
        request: StructuredInferenceRequest,
        *,
        monitor: ProcessMonitor,
        is_active: Callable[[], bool] | None,
        usage: Callable[[str, dict[str, Any] | None], dict[str, int | float]],
        registration: ProcessRegistration | None = None,
    ) -> StructuredInferenceResult:
        if self._active is not None:
            raise RuntimeError("inference process still active or cleanup pending")
        if request.agent_harness not in {"codex", "pi"} or request.runtime_provider != "direct":
            raise ValueError(f"unsupported one-shot inference host {request.agent_harness!r}")
        if (request.agent_harness == "pi") != (request.pi_tools is not None):
            raise ValueError("Pi inference requires its authorized tool bundle; Codex does not accept one")
        started_at = self.clock.timestamp()
        started = time.monotonic()
        temporary = tempfile.TemporaryDirectory(prefix="goal-plus-inference-")
        try:
            directory = Path(temporary.name)
            output_path = directory / "output.json"
            environment = os.environ.copy()
            options: dict[str, Any] = {}
            if request.agent_harness == "codex":
                (directory / "AGENTS.md").write_text(request.instructions, encoding="utf-8")
                schema_path = directory / "output.schema.json"
                schema_path.write_text(json.dumps(strict_output_schema(request.output_schema)), encoding="utf-8")
                command = [
                    "codex", "exec", "--json", "--ephemeral", "--sandbox", "read-only",
                    "--skip-git-repo-check", "--color", "never", "--output-schema", str(schema_path),
                    "--output-last-message", str(output_path), "-C", str(directory),
                ]
                if request.model:
                    command.extend(("--model", request.model))
                if request.reasoning_effort:
                    command.extend(("--config", "model_reasoning_effort=" + json.dumps(request.reasoning_effort)))
                command.append("-")
            else:
                tools = request.pi_tools
                assert tools is not None
                extension_path = directory / "structured-output.ts"
                extension_path.write_text(tools.extension, encoding="utf-8")
                command = [
                    "pi", "--mode", "json", "--print", "--no-session", "--no-builtin-tools",
                    "--tools", ",".join(tools.names), "--no-extensions", "--extension", str(extension_path),
                    "--no-skills", "--no-prompt-templates", "--no-context-files", "--no-approve",
                    "--system-prompt", request.instructions,
                ]
                if request.model:
                    provider, separator, model_id = request.model.partition("/")
                    if not separator:
                        provider, model_id = "", request.model
                    if provider:
                        command.extend(("--provider", provider))
                    command.extend(("--model", model_id))
                if request.reasoning_effort:
                    command.extend(("--thinking", request.reasoning_effort))
                environment[tools.output_env] = str(output_path)
                options["cwd"] = directory
            command = list(resolve_sandbox_command(
                command,
                cwd=options.get("cwd", directory),
                sandbox=request.sandbox,
            ))
            prompt_path = directory / "prompt.txt"
            prompt_path.write_text(request.prompt, encoding="utf-8")
            with ExitStack() as launch, prompt_path.open("r", encoding="utf-8") as prompt_input:
                gate = launch.enter_context(ProcessLaunchGate()) if registration is not None else None
                if gate is not None:
                    command = gate.command(command)
                    options.update(pass_fds=gate.pass_fds)
                process = self.popen(
                    command, stdin=prompt_input, text=True, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, env=environment, **options,
                )
                self._active = ManagedProcess(process)
                try:
                    if registration is not None:
                        self._registration = registration
                        self._registration_token = registration.register(process.pid)
                        gate.release()
                    outcome = self._active.wait(
                        request.timeout_seconds, monitor=monitor, is_active=is_active,
                        timeout_detail=request.timeout_detail, cancellation_detail=request.cancellation_detail,
                        active_check=request.active_check,
                    )
                finally:
                    if registration is not None:
                        self._active.close()
                        self._finish_registration()
                        self._active = None
                    elif process.returncode is not None:
                        self._active = None
            message = None
            parsed_usage: dict[str, int | float] = {}
            output_text = None
            payload = None
            output_error = None
            if outcome.state == "process_exited":
                message = pi_assistant_message(outcome.stdout) if request.agent_harness == "pi" else None
                if request.agent_harness == "codex" or process.returncode == 0:
                    parsed_usage = usage(outcome.stdout, message)
                if process.returncode == 0 and output_path.exists():
                    try:
                        output_text = read_inference_output(output_path)
                        payload = json.loads(output_text)
                    except (OSError, ValueError) as exc:
                        output_error = exc
        finally:
            if self._active is None:
                temporary.cleanup()
            else:
                self._pending_cleanup = temporary
        return StructuredInferenceResult(
            process=outcome, pid=getattr(process, "pid", None), returncode=process.returncode,
            stop_detail=(request.timeout_detail if outcome.state == "timed_out" else
                         request.cancellation_detail if outcome.state == "terminated" else outcome.detail),
            output_text=output_text, payload=payload, output_error=output_error,
            assistant_message=message, usage=parsed_usage,
            invocation=InferenceInvocation(
                invocation_id=f"invocation_{uuid.uuid4().hex}", agent_harness=request.agent_harness,
                model=request.model, reasoning_effort=request.reasoning_effort,
                started_at=started_at, completed_at=self.clock.timestamp(),
                elapsed_seconds=max(0.0, time.monotonic() - started),
            ),
        )
