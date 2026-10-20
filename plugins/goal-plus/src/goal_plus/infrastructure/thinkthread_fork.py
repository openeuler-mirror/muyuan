"""TT native checkpoint derivation and its scope-local resource receipts.

The Agent API forks the caller's latest complete native checkpoint. Its Fs
snapshot selector is independent, so both boundaries are checked explicitly.
"""

import hashlib
import json
import os
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from pydantic import TypeAdapter

from goal_plus.domain.artifacts import ThinkThreadWorkspaceAttachment
from goal_plus.domain.time import timestamp_epoch
from goal_plus.execution.fork import DispatchGroup
from goal_plus.execution.pi_checkpoint import completed_checkpoint_prefix
from goal_plus.execution.thinkthread_child import ChildDriver
from goal_plus.execution.thinkthread_rpc import candidate_capabilities_marker
from goal_plus.host_scope import execution_scope_admission, execution_scope_for_run
from goal_plus.host_services import SearchHostService
from goal_plus.infrastructure.clock import SystemClock
from goal_plus.infrastructure.state.files import load_json
from goal_plus.infrastructure.thinkthread_sessions import (
    _persist_resource,
    session_resource_path,
)
from goal_plus.ports.session import (
    ParentCheckpoint,
    ProviderDispatchReceipt,
    ProviderDispatchRequest,
)
from goal_plus.ports.thinkthread import AgentPosixBridgeError, capability_ids
from goal_plus.thinkthread_agent_posix import (
    AgentPosixSdkClient,
    validate_thinkthread_root,
)


@dataclass(frozen=True)
class _Boundary:
    checkpoint_id: str
    checkpoint_kind: str
    digest: str


def _boundary(
    path: Path, harness: str, caller_id: str, *, run_id: str, prepared_at: str,
) -> _Boundary:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows or not all(isinstance(row, dict) for row in rows):
        raise RuntimeError("ThinkThread native session history is invalid")
    header = rows[0]
    native_id = (header.get("payload") or {}).get("id") if harness == "codex" else header.get("id")
    if native_id != caller_id:
        raise PermissionError("ThinkThread native history belongs to another Main")
    if harness == "pi":
        leaf = rows[-1].get("id")
        if not isinstance(leaf, str):
            raise RuntimeError("ForkCheckpointUnavailable: Pi active leaf is missing")
        checkpoint, ancestry_prefix = completed_checkpoint_prefix(
            rows[1:], leaf, run_id=run_id, require_preparation_marker=True,
            include_tool_groups=False,
        )
        # Agent POSIX cannot select this marker's older Session boundary:
        # fsSnapshotId selects only the filesystem. Reject a completed Main
        # boundary after preparation instead of recording the old marker as
        # proof of the provider's latest-complete derivation. TT seals completed
        # native turns, so a tool group in the current turn does not advance it.
        latest_checkpoint, _ = completed_checkpoint_prefix(rows[1:], leaf, include_tool_groups=False)
        if latest_checkpoint != checkpoint:
            raise RuntimeError(
                "ForkCheckpointChanged: ThinkThread preparation is no longer "
                "the latest complete native checkpoint"
            )
        prefix = json.dumps(ancestry_prefix, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return _Boundary(checkpoint, "entry", hashlib.sha256(prefix.encode("utf-8")).hexdigest())
    for index in range(len(rows) - 1, 0, -1):
        row = rows[index]
        if harness == "codex":
            payload = row.get("payload")
            completed = row.get("type") == "event_msg" and isinstance(payload, dict) and payload.get("type") == "task_complete"
            checkpoint = payload.get("turn_id") if completed else None
        else:
            message = row.get("message")
            completed = (row.get("type") == "message" and isinstance(message, dict)
                         and message.get("role") == "assistant" and message.get("stopReason") == "stop")
            checkpoint = row.get("id") if completed else None
        if not isinstance(checkpoint, str) or not checkpoint:
            continue
        completed_at = row.get("timestamp")
        completed_epoch = timestamp_epoch(completed_at) if isinstance(completed_at, str) else None
        prepared_epoch = timestamp_epoch(prepared_at)
        if completed_epoch is None or prepared_epoch is None or completed_epoch <= prepared_epoch:
            raise RuntimeError(
                "ForkCheckpointUnavailable: the latest completed Codex turn "
                "must have a recorded completion after this Search preparation event"
            )
        prefix = json.dumps(rows[1:index + 1], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return _Boundary(checkpoint, "completed_turn" if harness == "codex" else "entry",
                         hashlib.sha256(prefix.encode("utf-8")).hexdigest())
    raise RuntimeError("ForkCheckpointUnavailable: ThinkThread Main must complete a native turn first")


def _native_model_from_history(path: Path) -> dict[str, str] | None:
    """Read the provider/model selected by the completed Main session.

    A normal Child spawn inherits the Root CLI model.  ``thinkthread.fork``
    derives a new provider session and therefore requires the exact model
    binding, even when SearchSpec has no worker override.
    """
    selected: dict[str, str] | None = None
    pending_provider: str | None = None

    def normalize_provider(value: object) -> str | None:
        if not isinstance(value, str) or not value:
            return None
        return "openai" if value == "thinkthread-managed" else value

    def consider(provider: object, model: object) -> None:
        nonlocal selected
        normalized = normalize_provider(provider) or pending_provider
        if normalized and isinstance(model, str) and model:
            selected = {"provider": normalized, "model": model}

    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if row.get("type") == "model_change":
            provider, model = row.get("provider"), row.get("modelId")
            pending_provider = normalize_provider(provider) or pending_provider
            consider(provider, model)
            continue
        payload = row.get("payload")
        if isinstance(payload, dict):
            provider = payload.get("model_provider")
            settings = payload.get("thread_settings")
            if isinstance(settings, dict):
                provider = settings.get("model_provider_id", provider)
            pending_provider = normalize_provider(provider) or pending_provider
            model = payload.get("model")
            if isinstance(settings, dict):
                model = settings.get("model", model)
            consider(provider, model)
        # Codex keeps the model in turn_context while session_meta carries the
        # provider.  Retain the provider across those records and combine the
        # exact pair before returning a fork request.
        pending_provider = normalize_provider(row.get("model_provider")) or pending_provider
        consider(row.get("model_provider"), row.get("model"))
    return selected


class ThinkThreadDispatchAdapter:
    def __init__(self, root: Path, runtime, agent_harness: str) -> None:
        self.root, self.runtime, self.agent_harness = root, runtime, agent_harness
        self.client = AgentPosixSdkClient()
        self.clock = SystemClock()
        self.driver = ChildDriver(self.client, self.clock, agent_harness=agent_harness)
        self.service = SearchHostService(root, runtime=runtime)

    def prepare_checkpoint(self, run_id: str, caller_id: str,
                           checkpoint_id: str = "latest_completed") -> ParentCheckpoint:
        preparation = self.runtime.goal_authorization.require_fork_checkpoint_preparation(
            run_id, batch_started_at=self.service.queries.dispatch_batch_started_at(run_id),
        )
        scope = execution_scope_for_run(root_dir=self.root, run_id=run_id)
        # Pi execution scopes deliberately use a host-local operational owner
        # (`<goal-id>:<cwd>`), while the provider-native caller is the actual
        # ThinkThread root id.  NativeCandidateHost applies the same rule:
        # Codex scopes bind directly to the native thread id; Pi scopes bind
        # through the exact session history file below.  Requiring equality on
        # both hosts would reject a valid Pi resume before the provider can
        # prove the history belongs to this Root.
        if scope is None or scope.get("agent_harness") != self.agent_harness:
            raise PermissionError("ThinkThread fork requires the owning Main execution scope")
        if self.agent_harness == "codex" and scope.get("owner_session_id") != caller_id:
            raise PermissionError("ThinkThread fork requires the owning Main execution scope")
        root = validate_thinkthread_root(self.client, agent_harness=self.agent_harness)
        profile = next(item for item in root["profiles"] if item.get("alias") == "self")
        if not any(item.get("session") == "fork_current_checkpoint" and item.get("fs") == "private_branch"
                   for item in profile.get("allowedDerivations", [])):
            raise RuntimeError("ThinkThread Profile self does not allow native checkpoint forks")
        run = self.runtime.search_run.state.read_run(run_id)
        if root.get("thinkthreadId") != run.source_provider_handle.get("root_thinkthread_id"):
            raise PermissionError("ThinkThread fork Root differs from the frozen source")
        baseline = run.source_artifact_ref
        if baseline is None or baseline.provider != "thinkthread":
            raise RuntimeError("ThinkThread fork requires the runtime-owned baseline snapshot")
        if self.agent_harness == "pi":
            source = scope.get("native_session_file")
            if not isinstance(source, str) or not Path(source).is_file():
                raise RuntimeError("ForkCheckpointSessionFileUnavailable")
            path = Path(source)
        else:
            native_home = os.environ.get("CODEX_HOME")
            if not native_home:
                raise RuntimeError("ForkCheckpointSessionFileUnavailable: CODEX_HOME is absent")
            matches = list((Path(native_home) / "sessions").rglob(f"*{caller_id}.jsonl"))
            if len(matches) != 1:
                raise RuntimeError("ForkCheckpointSessionFileUnavailable: Main rollout is not unique")
            path = matches[0]
        boundary = _boundary(
            path, self.agent_harness, caller_id, run_id=run_id, prepared_at=preparation.created_at,
        )
        if checkpoint_id not in {"latest_completed", boundary.checkpoint_id}:
            raise ValueError("ThinkThread can fork only the latest completed native checkpoint")
        self._source_path, self._boundary, self._caller_id = path, boundary, caller_id
        self._native_model = _native_model_from_history(path)
        self._prepared_at = preparation.created_at
        self._scope_id, self._run_id = scope["scope_id"], run_id
        self._checkpoint = ParentCheckpoint(
            parent_native_session_id=root["thinkthreadId"], checkpoint_id=boundary.checkpoint_id,
            checkpoint_kind=boundary.checkpoint_kind, frozen_spec_id=run.frozen_spec_id,
            source_artifact_ref=baseline, runtime_provider="thinkthread", main_session_id=caller_id,
        )
        return self._checkpoint

    def _check_boundary(self) -> None:
        if _boundary(self._source_path, self.agent_harness, self._caller_id,
                     run_id=self._run_id, prepared_at=self._prepared_at) != self._boundary:
            raise RuntimeError("ThinkThread fork source completed checkpoint changed during group creation")

    def start_group(self, requests: Sequence[ProviderDispatchRequest], *, timeout: float,
                    on_receipt: Callable[[ProviderDispatchRequest, ProviderDispatchReceipt], None] | None = None,
                    on_submitted: Callable[[ProviderDispatchRequest], None] | None = None,
                    on_unknown: Callable[[ProviderDispatchRequest], None] | None = None,
                    ) -> tuple[ProviderDispatchReceipt, ...]:
        with execution_scope_admission(root_dir=self.root, scope_id=self._scope_id, run_id=self._run_id):
            self._check_boundary()
            return DispatchGroup(
                self,
                cleanup=self._cleanup,
                on_receipt=on_receipt,
                on_submitted=on_submitted,
                on_unknown=on_unknown,
            ).start(requests, timeout=timeout)

    def fork(self, request: ProviderDispatchRequest, *, timeout: float) -> ProviderDispatchReceipt:
        if request.checkpoint != self._checkpoint:
            raise PermissionError("ThinkThread fork request belongs to another source boundary")
        self._check_boundary()
        identity = request.worker_identity
        prepared = self.service.prepare_thinkthread_session(identity["agent_session_id"])
        session = prepared.session
        if session.run_id != self._run_id or session.candidate_id != identity["candidate_id"]:
            raise PermissionError("ThinkThread fork worker identity mismatch")
        path = session_resource_path(self.root, self._scope_id, session.agent_session_id)
        if path.exists():
            raise RuntimeError("ThinkThread fork resource intent already exists; derive must not be replayed")
        nonce, operation_id = str(uuid.uuid4()), str(uuid.uuid4())
        record = {
            "run_id": session.run_id, "candidate_id": session.candidate_id,
            "agent_session_id": session.agent_session_id, "scope_id": self._scope_id,
            "root_thinkthread_id": prepared.root_thinkthread_id,
            "baseline_snapshot_id": prepared.baseline_snapshot_id,
            "agent_harness": self.agent_harness,
            "model": session.launch.get("model") or self._native_model,
            "status": "prepared",
            "registration_nonce": nonce,
            "dispatch_provenance": {"dispatch_group_id": request.dispatch_group_id, "dispatch_target_index": request.dispatch_target_index,
                "main_session_id": self._caller_id, "checkpoint_id": self._boundary.checkpoint_id,
                "checkpoint_kind": self._boundary.checkpoint_kind, "context_projection_digest": self._boundary.digest,
                # Agent POSIX does not return a provider request identity for
                # ``thinkthread.fork``.  This is Goal Plus' durable local
                # operation/request identity, used for replay and evidence;
                # it must not be presented as a provider-generated ID.
                "operation_id": operation_id, "request_id": operation_id,
                "profile": "self", "session_derivation": "fork_current_checkpoint"},
            "fork_intent": {"state": "started"},
            "requested_capabilities": ["thinkthread.message"],
        }
        _persist_resource(path, record)
        params = {"profile": "self", "fs": "private", "fsSnapshotId": prepared.baseline_snapshot_id,
                  "capabilities": ["thinkthread.message"]}
        if record["model"] is not None:
            params["model"] = record["model"]
        try:
            result = self.driver.fork(params, timeout=timeout)
        except AgentPosixBridgeError as exc:
            record["fork_intent"]["state"] = "unknown" if exc.completion_unknown else "rejected"
            _persist_resource(path, record)
            raise
        # Even a rejected grant/model receipt can name a real created resource.
        # Keep those identities before strict decoding so failure can release
        # the owned Child rather than losing it outside the cleanup boundary.
        raw_attachment = result.get("fs") if isinstance(result, dict) else None
        child_id = result.get("thinkthreadId") if isinstance(result, dict) else None
        branch_id = raw_attachment.get("fsBranchId") if isinstance(raw_attachment, dict) else None
        if (isinstance(child_id, str) and child_id.startswith("tt-")
                and isinstance(branch_id, str) and branch_id.startswith("fsbranch-")
                and raw_attachment.get("kind") == "private"):
            record.update(thinkthread_id=child_id, fs_branch_id=branch_id, active_message_id=None)
        else:
            record["fork_intent"]["state"] = "unknown"
        try:
            _persist_resource(path, record)
            binding = self.driver.binding(result, model=record["model"])
            record.update(effective_capabilities=sorted(capability_ids(result.get("capabilities"))),
                          effective_model=result.get("model"))
            record["fork_intent"]["state"] = "bound"
            _persist_resource(path, record)
            self.driver.observe(binding.child_id, root_id=prepared.root_thinkthread_id, branch_id=binding.branch_id)
            branch = self.client.invoke("fs.branch.stat", {"branchId": binding.branch_id})
            if branch.get("thinkthreadId") != binding.child_id or branch.get("baseSnapshotId") != prepared.baseline_snapshot_id:
                raise PermissionError("ThinkThread fork branch does not derive from the runtime baseline")
            # Initialize the Child MCP binding before its first wake. This is a
            # retained, non-waking identity message, not a model invocation.
            staged = self.client.invoke("message.send", {
                "recipientThinkthreadId": binding.child_id,
                "text": (f"ThinkThread Goal Plus dispatch: agent_session_id={session.agent_session_id}; registration_nonce={nonce}; dispatch_nonce={uuid.uuid4()}.\n"
                         + candidate_capabilities_marker(prepared.tool_capabilities)),
                "wake": False,
            })
            record["candidate_origin_message_id"] = staged.get("messageId")
            _persist_resource(path, record)
            self._check_boundary()
            receipt = ProviderDispatchReceipt(
                binding.child_id, self._checkpoint.parent_native_session_id, request.workspace, "started",
                started_at=self.clock.timestamp(), context_projection_digest=self._boundary.digest,
                runtime_provider="thinkthread", checkpoint_id=self._boundary.checkpoint_id,
                checkpoint_kind=self._boundary.checkpoint_kind,
                workspace_attachment=ThinkThreadWorkspaceAttachment(
                    provider="thinkthread", root_thinkthread_id=prepared.root_thinkthread_id,
                    fs_branch_id=binding.branch_id, baseline_snapshot_id=prepared.baseline_snapshot_id),
                provider_capabilities=tuple(sorted(capability_ids(result.get("capabilities")))),
                model=result.get("model"), profile="self", request_id=operation_id,
            )
            # Persist the exact successful outcome before any domain callback.
            # Recovery replays this receipt; it never forks a replacement Child.
            record["launch_receipt"] = TypeAdapter(ProviderDispatchReceipt).dump_python(receipt, mode="json")
            _persist_resource(path, record)
            return receipt
        except BaseException as error:
            try:
                if record.get("thinkthread_id") is not None:
                    self.driver.release(record["thinkthread_id"], root_id=prepared.root_thinkthread_id,
                                        branch_id=record["fs_branch_id"], timeout=5.0)
                    record.update(cleanup_state="closed", closed_at=self.clock.timestamp())
                    _persist_resource(path, record)
            except Exception as cleanup_error:  # noqa: BLE001 - preserve launch failure while retaining cleanup identity
                error.add_note(f"ThinkThread fork resource cleanup failed: {cleanup_error}")
            raise

    def _cleanup(self, receipt: ProviderDispatchReceipt) -> None:
        run = self.runtime.search_run.state.read_run(self._run_id)
        paths = [session_resource_path(self.root, self._scope_id, branch.agent_session_id)
                 for branch in run.dispatch_group.targets]
        matches = []
        for path in paths:
            if path.exists():
                record = load_json(path)
                if record.get("thinkthread_id") == receipt.provider_session_id:
                    matches.append((path, record))
        if len(matches) != 1:
            raise RuntimeError("ThinkThread fork cleanup has no unique durable resource identity")
        path, record = matches[0]
        attachment = receipt.workspace_attachment
        if (record.get("run_id") != self._run_id or record.get("scope_id") != self._scope_id
                or attachment is None or attachment.provider != "thinkthread"
                or record.get("root_thinkthread_id") != attachment.root_thinkthread_id
                or record.get("fs_branch_id") != attachment.fs_branch_id
                or record.get("baseline_snapshot_id") != attachment.baseline_snapshot_id):
            raise PermissionError("ThinkThread fork cleanup receipt differs from its resource journal")
        if record.get("cleanup_state") == "closed":
            return
        self.driver.release(receipt.provider_session_id, root_id=record["root_thinkthread_id"],
                            branch_id=record["fs_branch_id"], timeout=5.0)
        record.update(cleanup_state="closed", closed_at=self.clock.timestamp())
        _persist_resource(path, record)

    def recover_receipts(self, run_id: str, caller_id: str) -> tuple[ProviderDispatchReceipt, ...]:
        run = self.runtime.search_run.state.read_run(run_id)
        fork = run.dispatch_group
        scope = execution_scope_for_run(root_dir=self.root, run_id=run_id)
        if (fork is None or fork.main_session_id != caller_id or scope is None
                or scope.get("agent_harness") != self.agent_harness
                or (self.agent_harness == "codex" and scope.get("owner_session_id") != caller_id)):
            raise PermissionError("ThinkThread fork recovery requires the owning Main scope")
        self._run_id, self._scope_id = run_id, scope["scope_id"]
        root = validate_thinkthread_root(self.client, agent_harness=self.agent_harness)
        if root.get("thinkthreadId") != fork.parent_native_session_id:
            raise PermissionError("ThinkThread fork recovery Root changed")
        receipts = []
        with execution_scope_admission(root_dir=self.root, scope_id=self._scope_id, run_id=run_id):
            for branch in fork.targets:
                path = session_resource_path(self.root, self._scope_id, branch.agent_session_id)
                if not path.exists():
                    raise RuntimeError("ForkRecoveryIncomplete: branch has no durable provider outcome")
                record = load_json(path)
                provenance = record.get("dispatch_provenance")
                if (record.get("run_id") != run_id or record.get("scope_id") != self._scope_id
                        or record.get("agent_session_id") != branch.agent_session_id
                        or record.get("candidate_id") != branch.candidate_id
                        or not isinstance(provenance, dict)
                        or provenance.get("dispatch_group_id") != fork.dispatch_group_id
                        or provenance.get("dispatch_target_index") != branch.dispatch_target_index
                        or provenance.get("checkpoint_id") != fork.checkpoint_id
                        or provenance.get("checkpoint_kind") != fork.checkpoint_kind
                        or record.get("agent_harness") != self.agent_harness
                        or provenance.get("main_session_id") != caller_id):
                    raise PermissionError("ThinkThread fork recovery resource ownership mismatch")
                if record.get("launch_receipt") is None:
                    raise RuntimeError("ForkRecoveryIncomplete: provider derivation has no exact receipt")
                receipt = TypeAdapter(ProviderDispatchReceipt).validate_python(record["launch_receipt"])
                attachment = receipt.workspace_attachment
                if (receipt.runtime_provider != "thinkthread" or attachment is None
                        or attachment.provider != "thinkthread"
                        or receipt.provider_session_id != record.get("thinkthread_id")
                        or receipt.parent_native_session_id != fork.parent_native_session_id
                        or receipt.checkpoint_id != fork.checkpoint_id or receipt.checkpoint_kind != fork.checkpoint_kind
                        or receipt.context_projection_digest != provenance.get("context_projection_digest")
                        or receipt.request_id != provenance.get("operation_id")
                        or receipt.profile != provenance.get("profile")
                        or receipt.workspace != branch.binding.workspace
                        or attachment.root_thinkthread_id != record.get("root_thinkthread_id")
                        or attachment.fs_branch_id != record.get("fs_branch_id")
                        or attachment.baseline_snapshot_id != record.get("baseline_snapshot_id")
                        or list(receipt.provider_capabilities) != record.get("effective_capabilities")
                        or record.get("requested_capabilities") != ["thinkthread.message"]
                        or receipt.model != record.get("effective_model")):
                    raise PermissionError("ThinkThread fork recovery receipt differs from its journal")
                if record.get("cleanup_state") == "closed":
                    receipt = replace(receipt, launch_state="cleaned", closed_at=record.get("closed_at"))
                elif self.driver.observe(receipt.provider_session_id,
                                         root_id=attachment.root_thinkthread_id,
                                         branch_id=attachment.fs_branch_id) is None:
                    raise RuntimeError("ForkRecoveryIncomplete: fork Child is missing")
                receipts.append(receipt)
        return tuple(receipts)
