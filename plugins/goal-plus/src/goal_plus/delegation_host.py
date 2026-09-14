"""Ordinary native delegation; existing dispatch attempts own the work history."""

from dataclasses import asdict, dataclass
from pathlib import Path
import time
from uuid import uuid4

from goal_plus.domain.goal_commands import WORK_EVENT_SUMMARY_MAX_CHARACTERS
from goal_plus.domain.time import outer_deadline_epoch
from goal_plus.goal_session_host import NativeGoalSessionHost
from goal_plus.host_scope import execution_scope_snapshot
from goal_plus.infrastructure.native_session_inputs import native_work_item_input
from goal_plus.ports.session import NativeMainCaller, NativeSession
from goal_plus.ports.scope import SCOPE_PROCESS_TERMINAL_STATUSES
from goal_plus.projections.goal_tools import goal_receipt, work_item_summary
from goal_plus.projections.session_tools import invocation_receipt


@dataclass
class _HostedWork:
    native: NativeSession
    caller: NativeMainCaller
    goal_revision: int
    scope_id: str
    attempt_id: str
    generation: int
    busy: bool = False


class NativeDelegationHost(NativeGoalSessionHost):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._work: dict[tuple[str, str], _HostedWork] = {}

    def _require_reopenable(self, goal_plus_id, previous):
        scope_id = previous.metadata.get("scope_id")
        label = previous.metadata.get("native_resource_label")
        if not previous.agent_id or not scope_id or not label:
            raise RuntimeError("WorkSessionRecoveryUnavailable: native resource identity is missing")
        scope = execution_scope_snapshot(root_dir=self.root, scope_id=scope_id)
        resources = [item for item in scope.get("processes", [])
                     if item.get("label") == label and item.get("run_id") == goal_plus_id]
        if scope.get("agent_harness") != self.agent_harness or not resources:
            raise RuntimeError("WorkSessionRecoveryUnavailable: native resource identity is missing")
        if any(item.get("status") not in SCOPE_PROCESS_TERMINAL_STATUSES for item in resources):
            raise RuntimeError("WorkSessionStillRunning: close the previous native resource before resuming")

    def _admit_work(self, goal_plus_id: str, work_item_id: str, hosted: _HostedWork):
        record, _ = self.admit_goal(goal_plus_id, hosted.caller.session_id, hosted.scope_id)
        item = next((item for item in record.work_items
                     if item.work_item_id == work_item_id and item.goal_revision == record.goal_revision), None)
        if (record.control.version != hosted.caller.control_version or record.goal_revision != hosted.goal_revision
            or item is None or item.agent_id != hosted.native.native_session_id
            or item.attempt_id != hosted.attempt_id or item.generation != hosted.generation):
            raise PermissionError("WorkAttemptMismatch: native controller no longer owns the current attempt")
        return record, item

    def run(self, goal_plus_id: str, work_item_id: str, task: str, max_runtime_seconds: int = 600) -> dict:
        caller_id = self.caller()
        key = (goal_plus_id, work_item_id)
        with self._lock:
            if self._closing:
                raise RuntimeError("native delegation host is closing")
            record, scope = self.admit_goal(goal_plus_id, caller_id)
            timeout = float(max_runtime_seconds)
            if not 30 <= timeout <= 3600:
                raise ValueError("ordinary task runtime must be between 30 and 3600 seconds")
            if record.outer_deadline_at:
                timeout = min(timeout, outer_deadline_epoch(record.outer_deadline_at) - time.time())
            if timeout <= 0:
                raise PermissionError("ordinary task deadline has elapsed")
            deadline = time.monotonic() + timeout
            native_main = NativeMainCaller(self.agent_harness, caller_id, record.control.version)
            previous = next((item for item in record.work_items
                             if item.work_item_id == work_item_id and item.goal_revision == record.goal_revision), None)
            hosted = self._work.get(key)
            rework = previous is not None and previous.status == "result_ready"
            reopen = rework and hosted is None
            if rework:
                if reopen:
                    self._require_reopenable(goal_plus_id, previous)
                else:
                    self._admit_work(goal_plus_id, work_item_id, hosted)
            if hosted is not None and hosted.busy:
                raise RuntimeError("ordinary native session is already running")
            config = native_work_item_input(
                self.root, Path(record.source_path or ".").resolve(),
                previous.agent_id if reopen else uuid4().hex, scope, resume=reopen,
            )
            dispatched = self.runtime.goal_dispatch.record_work_event(
                goal_plus_id, work_item_id, "rework" if rework else "dispatch", task,
                host=self.agent_harness, task_name=work_item_id, native_main=native_main,
                metadata={"transport": "app-server" if self.agent_harness == "codex" else "native-rpc",
                          "scope_id": scope["scope_id"], "control_version": record.control.version,
                          "requested_reasoning_effort": config.reasoning_effort,
                          "orchestration_monitor": {"native_operation": "goal_plus_run_work_item", "direction": "main_to_subagent"}},
            )
            item = next(item for item in dispatched.work_items if item.work_item_id == work_item_id
                        and item.goal_revision == dispatched.goal_revision)
            try:
                if not rework or reopen:
                    if hosted is not None:
                        self.close_native(hosted.native)
                        self._work.pop(key)
                    label = f"work-item:{work_item_id}:{item.attempt_id}"
                    native = self.open_native(
                        config, scope, goal_plus_id=goal_plus_id, label=label,
                        admit=lambda: self.admit_goal(goal_plus_id, caller_id, scope["scope_id"]),
                    )
                    hosted = _HostedWork(native, native_main, dispatched.goal_revision, scope["scope_id"], item.attempt_id, item.generation)
                    self._work[key] = hosted
                    self.runtime.goal_dispatch.record_work_event(
                        goal_plus_id, work_item_id, "message" if reopen else "bind",
                        "Native host resumed the ordinary session." if reopen else "Native host bound the ordinary session.", host=self.agent_harness,
                        agent_id=native.native_session_id, attempt_id=item.attempt_id, generation=item.generation,
                        native_main=native_main,
                        metadata={"native_resource_label": label},
                    )
                hosted.attempt_id, hosted.generation, hosted.busy = item.attempt_id, item.generation, True
            except BaseException:
                try:
                    self.runtime.goal_dispatch.record_work_event(
                        goal_plus_id, work_item_id, "failed", "Native ordinary session could not start.", host=self.agent_harness,
                        attempt_id=item.attempt_id, generation=item.generation, native_main=native_main,
                    )
                finally:
                    if hosted is not None:
                        self.close_native(hosted.native)
                        self._work.pop(key, None)
                raise
        try:
            self._admit_work(goal_plus_id, work_item_id, hosted)
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise TimeoutError("ordinary task deadline elapsed during native startup")
            result = hosted.native.run(
                "You are an implementation subagent. Work only on this assigned task:\n\n" + task
                + "\n\nReturn a concise result and verification evidence.", timeout=timeout,
            )
            metadata = {
                "native_invocation": {key: value for key, value in asdict(result).items() if key != "text"},
                "orchestration_monitor": {"native_operation": "goal_plus_run_work_item", "direction": "subagent_to_main"},
            }
            if result.observation and result.observation.pi_metrics:
                metadata["native_reasoning_effort"] = result.observation.pi_metrics.get("thinking_level")
            summary = result.text.strip()[:WORK_EVENT_SUMMARY_MAX_CHARACTERS] or "Native task returned without a summary."
            updated = self.runtime.goal_dispatch.record_work_event(
                goal_plus_id, work_item_id, "result" if result.state == "completed" else "failed", summary,
                host=self.agent_harness, agent_id=hosted.native.native_session_id, attempt_id=hosted.attempt_id,
                generation=hosted.generation, native_main=native_main, metadata=metadata,
            )
            current = next(item for item in updated.work_items if item.work_item_id == work_item_id
                           and item.goal_revision == updated.goal_revision)
            if result.state != "completed":
                self.close_native(hosted.native)
                with self._lock:
                    self._work.pop(key, None)
            item = work_item_summary(current)
            item.pop("result_summary", None)
            return {**goal_receipt(updated), "work_item": item, "invocation": invocation_receipt(result)}
        except Exception:
            try:
                self.runtime.goal_dispatch.record_work_event(
                    goal_plus_id, work_item_id, "failed", "Native ordinary task execution failed.", host=self.agent_harness,
                    agent_id=hosted.native.native_session_id, attempt_id=hosted.attempt_id,
                    generation=hosted.generation, native_main=native_main,
                )
            finally:
                self.close_native(hosted.native)
                with self._lock:
                    self._work.pop(key, None)
            raise
        finally:
            with self._lock:
                hosted.busy = False
