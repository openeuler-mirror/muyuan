"""Direct Codex/Pi checkpoint and native branch derivation.

The shared host coordinates persisted fork groups. This adapter owns only the
native Main session attachment, complete checkpoint selection, and cleanup of
provider-created branches after a partial group failure.
"""

from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

from pydantic import TypeAdapter

from goal_plus.bootstrap import build_scope_process_registry
from goal_plus.candidate_host import candidate_host_queries
from goal_plus.domain.agents import AgentHarnessKind
from goal_plus.domain.time import timestamp_epoch
from goal_plus.execution.fork import DispatchGroup
from goal_plus.execution.native_session import CodexSession, PiSession
from goal_plus.execution.pi_checkpoint import select_completed_checkpoint
from goal_plus.execution.pi_config import prepare_pi_home
from goal_plus.execution.scope import normalize_native_agent_config
from goal_plus.execution.session_rpc import SessionRequestRejected
from goal_plus.host_scope import execution_scope_for_run
from goal_plus.infrastructure.native_session_inputs import (
    _fork_pi_session_file,
    _native_environment,
    _sandbox,
)
from goal_plus.paths import host_assets_root
from goal_plus.ports.goal_authorization import DispatchPreparation
from goal_plus.ports.process import ProcessRegistration
from goal_plus.ports.scope import ScopeAttachment
from goal_plus.ports.session import (
    ParentCheckpoint,
    ProviderDispatchReceipt,
    ProviderDispatchRequest,
    NativeSessionInput,
    SessionProcessInput,
)


class DirectDispatchAdapter:
    def __init__(
        self, root: Path, runtime, agent_harness: AgentHarnessKind, *, checkpoint_timeout: float,
    ) -> None:
        self.root, self.runtime, self.agent_harness = root, runtime, agent_harness
        self.checkpoint_timeout = checkpoint_timeout

    def prepare_checkpoint(
        self, run_id: str, caller_id: str, checkpoint_id: str = "latest_completed",
    ) -> ParentCheckpoint:
        # Explicit selectors need the same preparation proof as the default;
        # an older turn must not bypass preparation of this run and batch.
        preparation = self.runtime.goal_authorization.require_fork_checkpoint_preparation(
            run_id, batch_started_at=candidate_host_queries(self.root).dispatch_batch_started_at(run_id),
        )
        native = self._open_parent(run_id, caller_id, label="checkpoint", timeout=self.checkpoint_timeout)
        try:
            completed = self._completed_checkpoint(native, run_id, caller_id, preparation)
        finally:
            native.close()
        if checkpoint_id not in {"latest_completed", completed}:
            raise ValueError("fork can use only the current prepared completed checkpoint")
        run = self.runtime.search_run.state.read_run(run_id)
        self._run_id, self._caller_id = run_id, caller_id
        self._checkpoint = ParentCheckpoint(
            parent_native_session_id=caller_id, checkpoint_id=completed,
            checkpoint_kind="completed_turn" if self.agent_harness == "codex" else "entry",
            frozen_spec_id=run.frozen_spec_id, source_artifact_ref=run.source_artifact_ref,
            runtime_provider="direct", main_session_id=caller_id,
        )
        return self._checkpoint

    def start_group(
        self, requests: Sequence[ProviderDispatchRequest], *, timeout: float,
        on_receipt: Callable[[ProviderDispatchRequest, ProviderDispatchReceipt], None] | None = None,
        on_submitted: Callable[[ProviderDispatchRequest], None] | None = None,
        on_unknown: Callable[[ProviderDispatchRequest], None] | None = None,
    ) -> tuple[ProviderDispatchReceipt, ...]:
        if any(request.checkpoint != self._checkpoint for request in requests):
            raise PermissionError("fork request belongs to another source boundary")
        native = self._open_parent(self._run_id, self._caller_id, label="parent", timeout=timeout)
        try:
            return DispatchGroup(
                native, cleanup=lambda receipt: self._cleanup(native, receipt), on_receipt=on_receipt,
                on_submitted=on_submitted, on_unknown=on_unknown,
            ).start(requests, timeout=timeout)
        finally:
            native.close()

    def recover_receipts(self, run_id: str, caller_id: str) -> tuple[ProviderDispatchReceipt, ...]:
        fork = self.runtime.search_run.state.read_run(run_id).dispatch_group
        if fork is None or fork.main_session_id != caller_id or fork.runtime_provider != "direct":
            raise PermissionError("direct fork recovery belongs to another Main")
        receipts: list[ProviderDispatchReceipt] = []
        for branch in fork.targets:
            if branch.binding.launch_receipt:
                receipt = TypeAdapter(ProviderDispatchReceipt).validate_python(branch.binding.launch_receipt)
                if branch.launch_state == "cleaned":
                    receipt = replace(receipt, launch_state="cleaned", closed_at=branch.closed_at)
                receipts.append(receipt)
                continue

            # Pi's direct fork helper receives the durable candidate session
            # identity as its provider session id.  If the host dies after
            # SessionManager.forkFrom() has written the JSONL file but before
            # record_dispatch_receipt() copies the response into Goal Plus state,
            # the file itself is an exact, provider-owned receipt.  Recover it
            # without invoking the provider again.
            if self.agent_harness == "pi":
                session = self.runtime.candidate_sessions.state.read_agent_session(
                    branch.agent_session_id,
                    run_id,
                )
                if session.session_handle.external_id != branch.agent_session_id:
                    raise RuntimeError("ForkRecoveryIncomplete: Pi session identity is not deterministic")
                session_file = _fork_pi_session_file(self.root, session)
                if session_file is not None:
                    receipts.append(ProviderDispatchReceipt(
                        provider_session_id=branch.agent_session_id,
                        parent_native_session_id=fork.parent_native_session_id,
                        workspace=branch.binding.workspace,
                        launch_state="started",
                        native_session_file=str(session_file),
                        runtime_provider="direct",
                        checkpoint_id=fork.checkpoint_id,
                        checkpoint_kind=fork.checkpoint_kind,
                    ))
                    continue

            # Codex provider ids are generated remotely and the direct app
            # server exposes no journal/query operation.  Without an exact
            # persisted receipt, retrying would risk creating an untracked
            # duplicate branch, so recovery must fail closed.
            raise RuntimeError("ForkRecoveryIncomplete: direct fork has no complete persisted receipts")
        return tuple(receipts)

    def _open_parent(
        self, run_id: str, caller_id: str, *, label: str, timeout: float,
    ) -> CodexSession | PiSession:
        scope = execution_scope_for_run(root_dir=self.root, run_id=run_id)
        if (scope is None or scope.get("agent_harness") != self.agent_harness
                or (self.agent_harness == "codex" and scope.get("owner_session_id") != caller_id)):
            raise PermissionError("fork requires the owning Main execution scope")
        config = normalize_native_agent_config(self.agent_harness, scope.get("native_agent_config"))
        if config is None:
            raise RuntimeError("fork requires the Main model snapshot")
        workspace = Path(self.runtime.search_run.state.read_run(run_id).source_path).resolve()
        env = _native_environment(self.root)
        registry = build_scope_process_registry(self.root)
        scope_id = scope["scope_id"]
        registration = ProcessRegistration(
            register=lambda pid: registry.register(
                scope_id=scope_id, pid=pid, label=f"fork-{label}:{run_id}",
                attachment=ScopeAttachment("search_run", run_id),
            ),
            finish=lambda token: registry.finish(scope_id=scope_id, token=token),
        )
        if self.agent_harness == "codex":
            return CodexSession(
                NativeSessionInput(
                    agent_harness="codex",
                    process=SessionProcessInput(
                        ("codex", "app-server", "--stdio"), workspace, env,
                        _sandbox("codex", mode="read-only", workspace=workspace,
                                 runtime_root=self.root, session_dir=self.root / "host-sessions" / "codex"),
                    ),
                    model=config["model"], reasoning_effort=config.get("reasoning_effort"),
                    native_session_id=caller_id,
                    configuration={"sandbox_mode": "read-only"},
                ), registration, timeout=timeout, attach_existing=True,
            )
        session_dir = self.root / "host-sessions" / "pi"
        session_dir.mkdir(parents=True, exist_ok=True)
        prepare_pi_home(session_dir / "pi-home", env)
        env.update({"GOAL_PLUS_PI_ROLE": "main"})
        source_session_file = scope.get("native_session_file")
        if not isinstance(source_session_file, str) or not Path(source_session_file).is_file():
            raise RuntimeError("ForkCheckpointSessionFileUnavailable")
        source_session_path = Path(source_session_file).resolve()
        return PiSession(
            NativeSessionInput(
                agent_harness="pi",
                process=SessionProcessInput(
                    ("pi", "--mode", "rpc", "--approve", "--no-context-files",
                     "--session-dir", str(session_dir), "--session", str(source_session_path),
                     "--no-extensions"), workspace, env,
                    _sandbox("pi", mode="read-only", workspace=workspace,
                             runtime_root=self.root, session_dir=session_dir,
                             additional_writable_roots=(source_session_path.parent,)),
                ),
                native_session_id=caller_id,
                configuration={"fork_helper": str(host_assets_root() / "assets/pi/extensions/lib/fork.mjs")},
            ), registration, timeout=timeout,
        )

    def _completed_checkpoint(
        self, native: CodexSession | PiSession, run_id: str, caller_id: str,
        preparation: DispatchPreparation,
    ) -> str:
        if self.agent_harness == "pi":
            # get_state gives identity/streaming state, while get_entries owns
            # the active tree leaf needed by the shared Pi checkpoint selector.
            data = native._command("get_entries", timeout=self.checkpoint_timeout).get("data") or {}
            if not isinstance(data, dict):
                raise RuntimeError("ForkCheckpointUnavailable: Pi entries payload is invalid")
            return select_completed_checkpoint(
                data.get("entries"), data.get("leafId"), run_id=run_id, require_preparation_marker=True,
            )
        # Codex pages turns separately in newer app-server protocols. The
        # request rejection fallback is confined to the native wire adapter.
        try:
            response = native._request("thread/read", {
                "threadId": caller_id, "excludeTurns": True,
            }, timeout=self.checkpoint_timeout)
        except SessionRequestRejected:
            response = native._request("thread/read", {
                "threadId": caller_id, "includeTurns": True,
            }, timeout=self.checkpoint_timeout)
        turns = (response.get("thread") or {}).get("turns") or []
        paged_turns = not turns
        if paged_turns:
            cursor: str | None = None
            while True:
                params: dict[str, object] = {
                    "threadId": caller_id, "sortDirection": "desc", "limit": 100, "itemsView": "summary",
                }
                if cursor is not None:
                    params["cursor"] = cursor
                page = native._request("thread/turns/list", params, timeout=self.checkpoint_timeout)
                page_turns = page.get("data") or []
                if isinstance(page_turns, list):
                    turns.extend(page_turns)
                next_cursor = page.get("nextCursor")
                if not isinstance(next_cursor, str) or not next_cursor:
                    break
                cursor = next_cursor
        ordered_turns = turns if paged_turns else reversed(turns)
        for turn in ordered_turns:
            if isinstance(turn, dict) and turn.get("status") == "completed" and turn.get("id"):
                completed_at = turn.get("completedAt")
                prepared_at = timestamp_epoch(preparation.created_at)
                if (not isinstance(completed_at, int) or isinstance(completed_at, bool)
                        or prepared_at is None or completed_at <= prepared_at):
                    raise RuntimeError(
                        "ForkCheckpointUnavailable: the latest completed Codex turn "
                        "must have a recorded completion after this Search preparation event"
                    )
                return str(turn["id"])
        raise RuntimeError(
            "ForkCheckpointUnavailable: no completed Codex turn. The parent "
            "Main must finish its current turn first (for example by calling "
            "goal_plus_set_status with status=blocked and resuming the same session "
            "next turn); fork-bound candidates cannot run through "
            "goal_plus_session_run, so an unusable checkpoint leaves "
            "goal_plus_search_invalidate_run plus a re-frozen fresh-context "
            "replacement as the only exit."
        )

    def _cleanup(self, native: CodexSession | PiSession, receipt: ProviderDispatchReceipt) -> None:
        branch_id = receipt.provider_session_id
        if not branch_id:
            return
        if self.agent_harness == "codex":
            # Archive is the narrow lifecycle operation available to the
            # trusted app server for a persisted direct Codex branch.
            native._request("thread/archive", {"threadId": branch_id}, timeout=self.checkpoint_timeout)
            return
        session_dir = self.root / "host-sessions" / "pi"
        for path in session_dir.rglob(f"*_{branch_id}.jsonl"):
            path.unlink()
