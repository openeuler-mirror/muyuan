"""Local, independent records for direct no-session model invocations."""

from dataclasses import asdict
import json
import logging
import os
from pathlib import Path
import shutil
from typing import Any

from goal_plus.infrastructure.state.files import write_json
from goal_plus.ports.inference import (
    InferenceTranscriptIdentity, StructuredInferenceRequest, StructuredInferenceResult,
)

logger = logging.getLogger(__name__)


def inference_capture_enabled() -> bool:
    return os.environ.get("GOAL_PLUS_DEBUG") == "1"


class FileInferenceTranscript:
    def __init__(self, root: Path, identity: InferenceTranscriptIdentity) -> None:
        self.root = root
        self.identity = identity
        self.directory: Path | None = None
        self.provider_path: Path | None = None
        self.record: dict[str, Any] = {}
        self.offsets = {"stdout.jsonl": 0, "stderr.txt": 0}

    def _warning(self, exc: Exception) -> None:
        warning = f"capture error: {type(exc).__name__}: {exc}"
        self.record.setdefault("warnings", []).append(warning)
        self.record["capture_status"] = "partial"
        logger.warning("inference transcript %s", warning)

    def _write(self, name: str, value: Any) -> bool:
        if self.directory is None:
            return False
        try:
            write_json(self.directory / name, value)
            return True
        except (OSError, ValueError, TypeError) as exc:
            self._warning(exc)
            return False

    def start(self, request: StructuredInferenceRequest, invocation_id: str,
              started_at: str, schema: dict[str, Any], *,
              observer_output_path: Path | None = None) -> str | None:
        # Only the execution-generated UUID is a path component. Task identities
        # remain data, so malformed external identifiers cannot escape this root.
        self.directory = self.root / "host-logs" / "inference-transcripts" / invocation_id
        self.record = {
            "schema_version": 1, "kind": "no_session_inference", "invocation_id": invocation_id,
            "identity": asdict(self.identity), "agent_harness": request.agent_harness,
            "runtime_provider": request.runtime_provider,
            "started_at": started_at, "model": request.model,
            "reasoning_effort": request.reasoning_effort, "capture_status": "recording",
            "process_state": "starting", "acceptance": "pending", "warnings": [],
            "provider_visibility": ("native_session_archive" if request.runtime_provider == "thinkthread"
                                    else "provider_request_hook" if request.agent_harness == "pi" else "unavailable"),
        }
        if not self._write("record.json", self.record):
            return None
        payload = {**asdict(request), "output_schema": schema}
        if request.sandbox is not None:
            payload["sandbox"].update(
                workspace=str(request.sandbox.workspace),
                writable_roots=[str(path) for path in request.sandbox.writable_roots],
            )
        self._write("input.json", payload)
        self._write("record.json", self.record)
        if request.agent_harness == "pi" and request.runtime_provider == "direct":
            self.provider_path = observer_output_path or self.directory / "provider-requests.jsonl"
            return _PI_OBSERVER.replace("__PATH__", json.dumps(str(self.provider_path)))
        return None

    def observe(self, stdout: str, stderr: str) -> None:
        if self.directory is None:
            return
        self._archive_provider_requests()
        for name, content in (("stdout.jsonl", stdout), ("stderr.txt", stderr)):
            offset = self.offsets[name]
            if len(content) <= offset:
                continue
            try:
                with (self.directory / name).open("a", encoding="utf-8", newline="") as stream:
                    stream.write(content[offset:])
                self.offsets[name] = len(content)
            except (OSError, ValueError) as exc:
                self._warning(exc)
                self._write("record.json", self.record)

    def _archive_provider_requests(self) -> None:
        if self.directory is None or self.provider_path is None:
            return
        target = self.directory / "provider-requests.jsonl"
        try:
            if self.provider_path != target and self.provider_path.is_file():
                shutil.copyfile(self.provider_path, target)
        except (OSError, ValueError) as exc:
            self._warning(exc)

    def launch(self, command: list[str]) -> None:
        self._write("command.json", {"argv": command})

    def finish(self, result: StructuredInferenceResult) -> None:
        self.observe(result.process.stdout, result.process.stderr)
        if result.invocation.runtime_provider == "thinkthread":
            self.record["native_session_id"] = result.invocation.native_session_id
            self.record["native_invocation_id"] = result.invocation.native_invocation_id
            self.record.setdefault("warnings", []).append(
                "prepared input and result only; actual Child context requires the native session archive"
            )
        elif result.invocation.agent_harness == "pi":
            provider = self.directory / "provider-requests.jsonl" if self.directory else None
            observed = False
            if provider is not None and provider.is_file():
                try:
                    with provider.open("r", encoding="utf-8") as stream:
                        for line in stream:
                            if line.strip():
                                entry = json.loads(line)
                                observed = observed or (
                                    isinstance(entry, dict)
                                    and entry.get("type") == "provider_request"
                                )
                except (OSError, ValueError, TypeError) as exc:
                    self._warning(exc)
            if not observed:
                self.record.setdefault("warnings", []).append("provider request hook data unavailable")
        self._write("result.json", {
            "invocation": asdict(result.invocation), "process_state": result.process.state,
            "returncode": result.returncode, "stop_detail": result.stop_detail,
            "output_text": result.output_text, "payload": result.payload,
            "output_error": str(result.output_error) if result.output_error else None,
            "usage": result.usage,
        })
        self.record.update(
            process_state=result.process.state, returncode=result.returncode,
            completed_at=result.invocation.completed_at,
            capture_status=("complete" if result.process.state == "process_exited"
                            and not self.record.get("warnings") else "partial"),
        )
        self._write("record.json", self.record)

    def failed(self, error: str) -> None:
        self._archive_provider_requests()
        self.record.update(process_state="failed", capture_status="partial", execution_error=error)
        self._write("record.json", self.record)

    def accept(self, error: str | None = None) -> None:
        self.record.update(acceptance="rejected" if error else "accepted", acceptance_error=error)
        self._write("record.json", self.record)


_PI_OBSERVER = '''import { appendFileSync } from "node:fs";

export default function (pi: any) {
  const path = __PATH__;
  let sequence = 0;
  function record(data: any) {
    try {
      appendFileSync(path, JSON.stringify({ ...data, timestamp: new Date().toISOString() }) + "\\n", "utf8");
    } catch (error) {
      console.error("goal-plus inference capture failed:", String(error));
    }
  }
  record({ type: "capture_started" });
  pi.on("before_provider_request", (event: any, ctx: any) => {
    record({ type: "provider_request", sequence: ++sequence, payload: event.payload,
             model: ctx.model?.id, provider: ctx.model?.provider });
  });
  pi.on("agent_end", () => record({ type: "capture_finished", requests: sequence }));
}
'''
