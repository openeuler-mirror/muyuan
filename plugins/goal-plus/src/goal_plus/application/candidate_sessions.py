"""Candidate session identity, provenance and launch admission."""

import math
import re
from contextlib import nullcontext
from dataclasses import asdict, replace
from typing import Any, Literal

from pydantic import TypeAdapter

from goal_plus.application.adaptive_admission import AdaptiveAdmission
from goal_plus.application.candidate_execution import (
    WORKER_ITERATION_RUN_STATES,
    require_candidate_not_recovering,
    require_current_session,
    is_recovery_replacement,
    require_fork_continuation,
    require_no_pending_verification,
    require_session_execution,
    require_worker_iteration_state,
)
from goal_plus.application.goal_authorization import GoalAuthorization
from goal_plus.application.search_context import SearchContext
from goal_plus.domain.agents import (
    AgentHarnessKind,
    AgentRuntimeProvider,
    AgentSessionHandle,
)
from goal_plus.domain.artifacts import LocalWorkspaceAttachment
from goal_plus.domain.search import (
    AgentSessionRecord,
    CandidateOrigin,
    DispatchTargetState,
    DispatchGroupState,
    FrozenSpec,
)
from goal_plus.domain.serialization import sha256_text
from goal_plus.domain.time import outer_deadline_epoch
from goal_plus.domain.tool_catalog import CandidateToolCapabilities
from goal_plus.domain.worker_policy import (
    candidate_worker_budget,
    normalize_worker_budget_override,
)
from goal_plus.ports.candidate_sessions import (
    MODEL_HANDOFF_RELATIVE_PATH,
    CandidateLaunch,
    CandidateSessionState,
)
from goal_plus.ports.clock import Clock
from goal_plus.ports.session import ProviderDispatchReceipt, SessionInvocationResult, SessionMode
from goal_plus.ports.state import CandidateRecord


class CandidateSessions:
    def __init__(
        self,
        *,
        state: CandidateSessionState,
        launch: CandidateLaunch,
        admission: AdaptiveAdmission,
        authorization: GoalAuthorization,
        context: SearchContext,
        clock: Clock,
    ) -> None:
        self.state, self.launch, self.admission = state, launch, admission
        self.authorization, self.context, self.clock = authorization, context, clock

    def _write_dispatch_group(self, run_id: str, group: DispatchGroupState) -> None:
        self.state.write_dispatch_group(run_id, group)

    @staticmethod
    def _adaptive_search_is_enabled(frozen: FrozenSpec) -> bool:
        return frozen.spec.strategy.adaptive_search_enabled

    def session(
        self,
        agent_session_id: str,
        run_id: str | None = None,
        *,
        allow_stale: bool = False,
    ) -> AgentSessionRecord:
        session = self.state.read_agent_session(agent_session_id, run_id)
        run = self.state.read_run(session.run_id)
        require_session_execution(session, self.state.read_frozen_spec(run.frozen_spec_id))
        if not allow_stale:
            require_current_session(
                session, self.state.read_candidate(session.run_id, session.candidate_id)
            )
        return session

    def _build_launch_payload(
        self,
        *,
        frozen: FrozenSpec,
        candidate_id: str,
        agent_session_id: str,
        directive: dict[str, Any],
        candidate_record: CandidateRecord,
        worker_budget_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        budget = self.constrain_budget(
            candidate_record.task.run_id,
            self._resolve_worker_budget_for_dispatch(
                frozen=frozen, candidate_record=candidate_record,
                worker_budget_override=worker_budget_override,
            ),
        )
        return self.launch.prepare(
            frozen, candidate_id, agent_session_id, directive, candidate_record, budget
        )

    def _build_continue_launch_payload(
        self,
        *,
        frozen: FrozenSpec,
        session: AgentSessionRecord,
        candidate_record: CandidateRecord,
        worker_budget_override: dict[str, Any] | None,
    ) -> dict[str, Any]:
        budget = self.constrain_budget(
            candidate_record.task.run_id,
            self._resolve_worker_budget_for_dispatch(
                frozen=frozen, candidate_record=candidate_record,
                worker_budget_override=worker_budget_override,
            ),
        )
        return self.launch.continue_session(frozen, session, candidate_record, budget)

    def start_agent_session(
        self,
        run_id: str,
        candidate_id: str,
        directive: dict[str, Any] | str | None = None,
        *,
        worker_budget: dict[str, Any] | None = None,
        run_locked: bool = False,
        session_mode: SessionMode = "fresh",
    ) -> AgentSessionRecord:
        """Create a context/provenance handle and host-native launch payload.

        Does not start a worker or track lifecycle state. ``worker_budget`` is
        an optional one-dispatch override; it does not mutate the frozen spec or
        the candidate policy.
        """
        if session_mode not in {"fresh", "fork"}:
            raise ValueError("unsupported session mode")
        return self._create_agent_session(
            run_id=run_id,
            candidate_id=candidate_id,
            directive=directive,
            worker_budget_override=worker_budget,
            reuse_initial=True,
            run_locked=run_locked,
            session_mode=session_mode,
        )

    def prepare_invocation(
        self, run_id: str, candidate_id: str, *, agent_harness: AgentHarnessKind,
        runtime_provider: AgentRuntimeProvider = "direct",
        mode: Literal["start", "continue", "refresh"] = "start",
        agent_session_id: str | None = None,
        worker_budget: dict[str, Any] | None = None,
    ) -> AgentSessionRecord:
        """Resolve provenance and prepare a launch; the host still executes it."""
        if mode not in {"start", "continue", "refresh"}:
            raise ValueError("unsupported candidate invocation mode")
        if agent_session_id is not None and mode != "continue":
            raise ValueError("agent_session_id requires continue mode")
        run = self.state.read_run(run_id)
        frozen = self.state.read_frozen_spec(run.frozen_spec_id)
        if (frozen.agent_harness, frozen.runtime_provider) != (agent_harness, runtime_provider):
            raise ValueError("candidate invocation host does not match the frozen worker host")
        if mode == "start":
            return self.start_agent_session(run_id, candidate_id, worker_budget=worker_budget)
        if mode == "refresh":
            return self.redispatch_candidate(run_id, candidate_id, worker_budget=worker_budget)
        sessions = [
            session for session in self.state.read_agent_sessions(run_id)
            if session.candidate_id == candidate_id and session.agent_harness == agent_harness
            and session.runtime_provider == runtime_provider
            and (agent_session_id is None or session.agent_session_id == agent_session_id)
        ]
        if not sessions:
            raise ValueError("no native session belongs to the requested candidate")
        return self.continue_agent_session(sessions[-1].agent_session_id, worker_budget=worker_budget)

    def record_run_request(self, agent_session_id: str, call_id: str, fingerprint: str, *, run_locked: bool = False) -> None:
        from goal_plus.domain.search import CandidateRunRequest

        session = self.state.read_agent_session(agent_session_id)
        with nullcontext() if run_locked else self.state.lock_run(session.run_id):
            session = self.state.read_agent_session(agent_session_id)
            candidate = self.state.read_candidate(session.run_id, session.candidate_id)
            require_current_session(session, candidate)
            previous = next((item for item in session.run_requests if item.call_id == call_id), None)
            if previous is not None:
                if previous.fingerprint != fingerprint:
                    raise ValueError("SessionRunConflict: call_id was used with different input")
                return
            session.run_requests.append(CandidateRunRequest(call_id=call_id, fingerprint=fingerprint))
            self.state.write_agent_session(session)

    def replace_session(self, agent_session_id: str, *, call_id: str, fingerprint: str) -> AgentSessionRecord:
        """Host calls only after confirming the idle controller's resources are closed."""
        session = self.state.read_agent_session(agent_session_id)
        return self._create_agent_session(
            run_id=session.run_id, candidate_id=session.candidate_id,
            directive={"state_level_resume": True}, replaced_agent_session_id=agent_session_id,
            run_request=(call_id, fingerprint),
        )

    def redispatch_candidate(
        self,
        run_id: str,
        candidate_id: str,
        *,
        worker_budget: dict[str, Any] | None = None,
    ) -> AgentSessionRecord:
        """Create a new worker launch for an existing candidate workspace.

        This is state-level resume, not same-worker continuation. It allocates
        a new agent_session_id for the same candidate/workspace. Inner-agent
        behavior comes from the frozen binding. A
        worker budget override applies only to this launch.
        """
        run = self.state.read_run(run_id)
        frozen = self.state.read_frozen_spec(run.frozen_spec_id)
        candidate_record = self.state.read_candidate(run_id, candidate_id)
        if candidate_record.status not in {"created", "evaluated"}:
            raise RuntimeError(
                f"cannot redispatch candidate in status {candidate_record.status}"
            )
        if self._adaptive_search_is_enabled(frozen):
            self.admission.assert_candidate_execution_allowed(
                candidate_record,
                "redispatch",
            )

        worker_budget_override = self._resolve_worker_budget_for_dispatch(
            frozen=frozen,
            candidate_record=candidate_record,
            worker_budget_override=worker_budget,
        )
        previous_session_ids = [
            session.agent_session_id
            for session in self.state.read_agent_sessions(run_id)
            if session.candidate_id == candidate_id
        ]
        resume_directive = {
            "state_level_resume": True,
            "resume_candidate_id": candidate_id,
            "previous_agent_session_ids": previous_session_ids,
            "resume_instruction": (
                "这是现有候选的新 worker session。首先调用 goal_plus_search_get_agent_context，"
                "使用其中本 candidate 的紧凑历史和可选 ledger projection 恢复权威上下文；"
                "仅在必须"
                "核对旧轮次详情时调用 goal_plus_search_list_iterations，再读取 "
                "goal_plus_search_get_global_evidence。"
            ),
        }
        return self._create_agent_session(
            run_id=run_id,
            candidate_id=candidate_id,
            directive=resume_directive,
            worker_budget_override=worker_budget_override,
        )

    def _create_agent_session(
        self,
        *,
        run_id: str,
        candidate_id: str,
        directive: dict[str, Any] | str | None,
        worker_budget_override: dict[str, Any] | None = None,
        reuse_initial: bool = False,
        run_locked: bool = False,
        replaced_agent_session_id: str | None = None,
        run_request: tuple[str, str] | None = None,
        session_mode: SessionMode = "fresh",
    ) -> AgentSessionRecord:
        run = self.state.read_run(run_id)
        frozen = self.state.read_frozen_spec(run.frozen_spec_id)
        if self._adaptive_search_is_enabled(frozen):
            self.admission.assert_candidate_execution_allowed(
                self.state.read_candidate(run_id, candidate_id),
                "start_session",
            )
        with nullcontext() if run_locked else self.state.lock_run(run_id):
            run = self.state.read_run(run_id)
            if run.state not in WORKER_ITERATION_RUN_STATES:
                raise RuntimeError(f"cannot start agent session from state {run.state}")
            self.authorization.require_search_execution(run_id)
            frozen = self.state.read_frozen_spec(run.frozen_spec_id)

            candidate_record = self.state.read_candidate(run_id, candidate_id)
            frozen = self.state.read_frozen_spec(candidate_record.active_frozen_spec_id or run.frozen_spec_id)
            require_candidate_not_recovering(candidate_record)
            require_no_pending_verification(candidate_record)
            recovery_replacement = (
                candidate_record.recovery is not None
                and candidate_record.recovery.state == "restored"
            )
            if not reuse_initial:
                require_fork_continuation(
                    candidate_record,
                    allow_recovery_replacement=recovery_replacement,
                )
            if replaced_agent_session_id is not None:
                require_current_session(self.state.read_agent_session(replaced_agent_session_id), candidate_record)
                if candidate_record.status not in {"created", "evaluated"}:
                    raise RuntimeError("candidate cannot be redispatched in its current state")
                # Validate the entire next state before any request/index writes.
                # A restored recovery retains its original checkpoint generation.
                candidate_record = CandidateRecord.model_validate({
                    **candidate_record.model_dump(mode="python"),
                    "execution_generation": candidate_record.execution_generation + 1,
                })
            workspace = candidate_record.task.workspace
            if self._adaptive_search_is_enabled(frozen):
                self.admission.assert_candidate_execution_allowed(
                    candidate_record,
                    "start_session",
                )

            if worker_budget_override is not None:
                worker_budget_override = normalize_worker_budget_override(
                    worker_budget=worker_budget_override,
                )

            normalized_directive = self._normalize_main_directive(directive)
            if candidate_record.recovery is not None:
                normalized_directive["recovery_checkpoint"] = (
                    candidate_record.recovery.point.model_dump(mode="json")
                )
                normalized_directive["recovery_id"] = (
                    candidate_record.recovery.recovery_id
                )
            if reuse_initial:
                session = next(
                    (
                        item
                        for item in self.state.read_agent_sessions(run_id)
                        if item.candidate_id == candidate_id
                        and item.execution_generation
                        == candidate_record.execution_generation
                    ),
                    None,
                )
                if session is not None:
                    require_session_execution(session, frozen)
                    require_current_session(session, candidate_record)
                    if (
                        session.directive == normalized_directive
                        and session.session_mode == session_mode
                        and worker_budget_override is None
                    ):
                        return session
                    # A process can die after the shared session identity is
                    # persisted but before the dispatch group is written.
                    # The retry is still the same initial session: promote
                    # its mode to ``fork`` while it has no bound provider
                    # handle, instead of allocating a second identity.
                    if (
                        session_mode == "fork"
                        and session.session_mode == "fresh"
                        and session.candidate_origin is None
                        and not session.session_handle.metadata.get("transport")
                        and session.session_handle.external_id in {None, session.agent_session_id}
                    ):
                        updated = AgentSessionRecord.model_validate({
                            **session.model_dump(mode="python"),
                            "session_mode": "fork",
                            "updated_at": self.clock.timestamp(),
                        })
                        self.state.write_agent_session(updated)
                        return updated
                    expected_launch = self._build_launch_payload(
                        frozen=frozen,
                        candidate_id=candidate_id,
                        agent_session_id=session.agent_session_id,
                        directive=normalized_directive,
                        candidate_record=candidate_record,
                        worker_budget_override=worker_budget_override,
                    )
                    if (
                        session.directive != normalized_directive
                        or session.session_mode != session_mode
                        or session.launch != expected_launch
                    ):
                        raise RuntimeError(
                            f"candidate {candidate_id} initial agent session already exists "
                            "with different launch options"
                        )
                    return session

            require_fork_continuation(
                candidate_record,
                allow_recovery_replacement=recovery_replacement,
            )
            agent_session_id = self._make_agent_session_id(
                run_id, run.next_agent_session_index
            )
            run.next_agent_session_index += 1
            now = self.clock.timestamp()
            launch = self._build_launch_payload(
                frozen=frozen,
                candidate_id=candidate_id,
                agent_session_id=agent_session_id,
                directive=normalized_directive,
                candidate_record=candidate_record,
                worker_budget_override=worker_budget_override,
            )
            agent_harness = frozen.agent_harness
            if agent_harness == "pi" and frozen.runtime_provider == "direct":
                launch["run_id"] = run_id
            session_handle = AgentSessionHandle(agent_harness=agent_harness, runtime_provider=frozen.runtime_provider)
            if agent_harness == "pi" and frozen.runtime_provider == "direct":
                session_handle = session_handle.model_copy(
                    update={
                        "external_id": launch.get("session_id", agent_session_id),
                        "metadata": {"continuation": "native_session"},
                    }
                )
            session = AgentSessionRecord(
                execution_generation=candidate_record.execution_generation,
                agent_session_id=agent_session_id,
                run_id=run_id,
                candidate_id=candidate_id,
                selected_model=candidate_record.task.selected_model,
                model_provenance=candidate_record.task.model_provenance,
                session_mode=session_mode,
                agent_harness=agent_harness,
                runtime_provider=frozen.runtime_provider,
                session_handle=session_handle,
                created_at=now,
                updated_at=now,
                directive=normalized_directive,
                workspace=workspace,
                launch=launch,
                counters={},
                candidate_origin=candidate_record.task.candidate_origin,
            )
            if run_request is not None:
                from goal_plus.domain.search import CandidateRunRequest

                request = CandidateRunRequest(call_id=run_request[0], fingerprint=run_request[1],
                                              target_session_id=agent_session_id)
                session.run_requests.append(request)
                previous = self.state.read_agent_session(replaced_agent_session_id)
                previous.run_requests.append(request)
                self.state.write_agent_session(previous)
            self.state.write_session_index(run)
            if replaced_agent_session_id is not None:
                self.state.write_execution_generation(candidate_record)
            self.state.write_agent_session(session)
            return session

    def bind_agent_handle(
        self,
        agent_session_id: str,
        handle: dict[str, Any],
    ) -> AgentSessionRecord:
        """Bind a runtime session to the host-specific worker handle."""
        session = self.session(agent_session_id)
        handle = AgentSessionHandle.model_validate({"agent_harness": session.agent_harness, "runtime_provider": session.runtime_provider, **handle}).model_dump(exclude_unset=True)
        agent_harness = handle.get("agent_harness", session.agent_harness)
        if agent_harness != session.agent_harness or handle["runtime_provider"] != session.runtime_provider:
            raise ValueError(
                f"agent session host is {session.agent_harness}, got handle for {agent_harness}"
            )

        metadata = {
            **session.session_handle.metadata,
            **dict(handle.get("metadata") or {}),
        }
        progress = metadata.get("progress_handoff")
        if isinstance(progress, dict) and not isinstance(
            progress.get("model_handoff"), dict
        ):
            model_keys = {
                "key_results",
                "what_was_tried",
                "pitfalls",
                "blockers",
                "next_steps",
                "verifier_assessment",
            }
            if model_keys.intersection(progress):
                progress = {
                    "model_handoff": dict(progress),
                    "source": "bound_metadata",
                }

        model_handoff, handoff_error = self.launch.read_handoff(session.workspace)
        if model_handoff is not None:
            progress_payload = dict(progress) if isinstance(progress, dict) else {}
            progress_payload.update(
                {
                    "model_handoff": model_handoff,
                    "source_path": MODEL_HANDOFF_RELATIVE_PATH,
                }
            )
            metadata["progress_handoff"] = progress_payload
            metadata.pop("progress_handoff_error", None)
        elif isinstance(progress, dict):
            metadata["progress_handoff"] = progress
        if handoff_error:
            metadata["progress_handoff_error"] = handoff_error
        metadata["bound_at"] = self.clock.timestamp()
        updated_handle = session.session_handle.model_copy(
            update={
                "agent_harness": session.agent_harness,
                "external_id": handle.get(
                    "external_id", session.session_handle.external_id
                ),
                "nickname": handle.get("nickname", session.session_handle.nickname),
                "metadata": metadata,
            }
        )
        updated = AgentSessionRecord.model_validate({
            **session.model_dump(mode="python"),
            "session_handle": updated_handle,
            "updated_at": self.clock.timestamp(),
        })
        self.state.write_agent_session(updated)
        return updated

    def prepare_dispatch_group(
        self, run_id: str, parent: str, checkpoint_id: str, checkpoint_kind: str, *,
        runtime_provider: AgentRuntimeProvider = "direct", main_session_id: str | None = None,
    ) -> DispatchGroupState:
        self.authorization.require_search_execution(run_id)
        with self.state.lock_run(run_id):
            run = self.state.read_run(run_id)
            frozen = self.state.read_frozen_spec(run.frozen_spec_id)
            require_worker_iteration_state(run, "prepare fork")
            if (not frozen.spec.strategy.fork_enabled
                    or frozen.runtime_provider != runtime_provider or not self.launch.supports_fork(frozen)):
                raise ValueError("fork requires the frozen host fork capability")
            if main_session_id is not None:
                self.authorization.require_main_search_session(
                    run_id, agent_harness=frozen.agent_harness, session_id=main_session_id, execution=True,
                )
            if runtime_provider == "thinkthread" and (
                main_session_id is None or parent != run.source_provider_handle.get("root_thinkthread_id")
                or run.source_artifact_ref is None or run.source_artifact_ref.provider != "thinkthread"
            ):
                raise ValueError("fork requires the bound ThinkThread Root and exact run baseline")
            if runtime_provider == "direct" and main_session_id is not None and parent != main_session_id:
                raise ValueError("direct fork parent does not match its Main session")
            fork = run.dispatch_group
            candidates = sorted(self.state.read_candidates(run_id), key=lambda r: r.candidate_id)
            existing_group_id = fork.dispatch_group_id if fork is not None else None
            if len(candidates) < 2 or any(
                c.iterations
                or (
                    c.task.candidate_origin is not None
                    and c.task.candidate_origin.dispatch_group_id != existing_group_id
                )
                for c in candidates
            ):
                raise RuntimeError("fork requires at least two fresh prepared candidates")
            candidate_ids = {candidate.candidate_id for candidate in candidates}
            existing_sessions = [session for session in self.state.read_agent_sessions(run_id)
                                 if session.candidate_id in candidate_ids]
            if fork is None and any(session.session_handle.metadata.get("transport") or (
                runtime_provider == "thinkthread" and session.session_handle.external_id is not None
            ) for session in existing_sessions):
                raise RuntimeError("fork cannot replace a previously launched session")
            group_id = f"fork_{run_id}_{sha256_text(checkpoint_id)[:12]}"
            now = self.clock.timestamp()
            if fork is None:
                fork = DispatchGroupState(
                    dispatch_group_id=group_id, parent_native_session_id=parent,
                    runtime_provider=runtime_provider, main_session_id=main_session_id,
                    checkpoint_id=checkpoint_id, checkpoint_kind=checkpoint_kind, frozen_spec_id=frozen.frozen_spec_id,
                    llm_validation=frozen.spec.strategy.llm_validation, created_at=now, updated_at=now,
                )
                # Establish the durable group envelope before any provider
                # operation.  A restart can then complete missing targets
                # without creating another dispatch group.
                self._write_dispatch_group(run_id, fork)
            else:
                if fork.status not in {"prepared", "partial"}:
                    raise RuntimeError("DispatchGroupAlreadyFinalized: inspect the existing group")
                if (
                    fork.dispatch_group_id != group_id
                    or fork.parent_native_session_id != parent
                    or fork.checkpoint_id != checkpoint_id
                    or fork.checkpoint_kind != checkpoint_kind
                    or fork.runtime_provider != runtime_provider
                    or fork.main_session_id != main_session_id
                ):
                    raise RuntimeError("DispatchGroupConflict: existing group has different identity")
            capabilities = CandidateToolCapabilities.from_frozen(frozen)
            branches = list(fork.targets)
            by_candidate = {branch.candidate_id: branch for branch in branches}
            for index, candidate in enumerate(candidates, start=1):
                branch = by_candidate.get(candidate.candidate_id)
                if branch is None:
                    session = self.start_agent_session(
                        run_id,
                        candidate.candidate_id,
                        run_locked=True,
                        session_mode="fork",
                    )
                    binding = CandidateOrigin(
                        dispatch_group_id=group_id, dispatch_target_index=index, checkpoint_id=checkpoint_id,
                        checkpoint_kind=checkpoint_kind, parent_native_session_id=parent,
                        runtime_provider=runtime_provider, main_session_id=main_session_id,
                        frozen_spec_id=frozen.frozen_spec_id, spec_hash=frozen.spec_hash,
                        source_artifact_ref=run.source_artifact_ref, workspace=candidate.task.workspace,
                        workspace_attachment=LocalWorkspaceAttachment(
                            provider=candidate.task.workspace_provider, path=candidate.task.workspace,
                        ) if runtime_provider == "direct" else None,
                        worker_identity={"agent_session_id": session.agent_session_id, "candidate_id": candidate.candidate_id},
                        budget=self.constrain_budget(run_id, candidate_worker_budget(frozen, candidate.task)) or {},
                        tool_capabilities=list(capabilities.tool_names()),
                    )
                    branch = DispatchTargetState(dispatch_target_index=index, candidate_id=candidate.candidate_id,
                        agent_session_id=session.agent_session_id, binding=binding,
                        audit_state="pending" if frozen.spec.strategy.llm_validation != "off" else "not_required")
                    branches.append(branch)
                    by_candidate[candidate.candidate_id] = branch
                    fork = fork.model_copy(update={"targets": list(branches), "updated_at": self.clock.timestamp()})
                    self._write_dispatch_group(run_id, fork)
                self.bind_dispatch_target(branch.agent_session_id, branch.binding, run_locked=True)
            fork = fork.model_copy(update={"targets": branches, "updated_at": self.clock.timestamp()})
            self._write_dispatch_group(run_id, fork)
            return fork

    def record_dispatch_receipt(self, run_id: str, branch_index: int, receipt: ProviderDispatchReceipt) -> None:
        with self.state.lock_run(run_id):
            fork = self.state.read_run(run_id).dispatch_group
            if fork is None or fork.status not in {"prepared", "submitted", "unknown", "partial"}:
                raise RuntimeError("fork is not accepting launch receipts")
            branch = next(b for b in fork.targets if b.dispatch_target_index == branch_index)
            if (receipt.parent_native_session_id != fork.parent_native_session_id
                    or receipt.runtime_provider != fork.runtime_provider
                    or (receipt.checkpoint_id is not None and receipt.checkpoint_id != fork.checkpoint_id)
                    or (receipt.checkpoint_kind is not None and receipt.checkpoint_kind != fork.checkpoint_kind)
                    or receipt.workspace != branch.binding.workspace
                    or receipt.provider_session_id == fork.parent_native_session_id
                    or any(b.dispatch_target_index != branch_index and b.provider_session_id == receipt.provider_session_id
                           for b in fork.targets if receipt.provider_session_id)):
                raise ValueError("fork receipt identity mismatch")
            if branch.provider_session_id not in {None, receipt.provider_session_id}:
                raise ValueError("fork branch cannot change native identity")
            if receipt.launch_state in {"started", "cleaned"}:
                attachment = receipt.workspace_attachment
                candidate = self.state.read_candidate(run_id, branch.candidate_id)
                if fork.runtime_provider == "thinkthread":
                    if (attachment is None or attachment.provider != "thinkthread"
                            or attachment.root_thinkthread_id != fork.parent_native_session_id
                            or branch.binding.source_artifact_ref is None
                            or attachment.baseline_snapshot_id != branch.binding.source_artifact_ref.id
                            or any(b.dispatch_target_index != branch_index and b.binding.workspace_attachment is not None
                                   and b.binding.workspace_attachment.provider == "thinkthread"
                                   and b.binding.workspace_attachment.fs_branch_id == attachment.fs_branch_id
                                   for b in fork.targets)):
                        raise ValueError("fork receipt private Fs attachment mismatch")
                elif attachment is not None and (
                    attachment.provider != candidate.task.workspace_provider or attachment.path != candidate.task.workspace
                ):
                    raise ValueError("fork receipt local workspace attachment mismatch")
                # Cleanup may follow an interrupted first receipt write. Bind
                # the original creation facts even when neither task nor
                # session persisted them; close facts remain on the branch.
                launch_receipt = (replace(receipt, launch_state="started", closed_at=None)
                                  if receipt.launch_state == "cleaned" else receipt)
                receipt_payload = TypeAdapter(ProviderDispatchReceipt).dump_python(launch_receipt, mode="json")
                binding = CandidateOrigin.model_validate({
                    **branch.binding.model_dump(mode="python"),
                    "provider_session_id": receipt.provider_session_id,
                    "workspace_attachment": attachment or branch.binding.workspace_attachment,
                    "launch_receipt": receipt_payload,
                    "context_projection_digest": receipt.context_projection_digest,
                })
                self.bind_dispatch_target(branch.agent_session_id, binding, run_locked=True)
                branch = branch.model_copy(update={"binding": binding})
            if receipt.launch_state == "cleaned":
                session = self.state.read_agent_session(branch.agent_session_id)
                updated_handle = session.session_handle.model_copy(update={"metadata": {
                    **session.session_handle.metadata, "fork_cleanup": "closed",
                }})
                updated = AgentSessionRecord.model_validate({
                    **session.model_dump(mode="python"), "session_handle": updated_handle,
                    "updated_at": self.clock.timestamp(),
                })
                candidate = self.state.read_candidate(run_id, branch.candidate_id)
                if candidate.task.workspace_provider == "thinkthread":
                    provider_handle = {**candidate.task.workspace_provider_handle, "cleanup_state": "closed"}
                    candidate.task.workspace_provider_handle = provider_handle
                    candidate.task.workspace_access.provider_handle = provider_handle
                    self.state.write_candidate_binding(candidate)
                self.state.write_agent_session(updated)
            branch = branch.model_copy(update={"provider_session_id": receipt.provider_session_id,
                "launch_state": receipt.launch_state, "failure": receipt.failure, "closed_at": receipt.closed_at})
            fork = fork.model_copy(update={"targets": [branch if b.dispatch_target_index == branch_index else b for b in fork.targets],
                                           "updated_at": self.clock.timestamp()})
            self._write_dispatch_group(run_id, fork)

    def mark_dispatch_submitted(self, run_id: str, branch_index: int) -> None:
        """Fence a provider dispatch before invoking native provider code."""
        with self.state.lock_run(run_id):
            fork = self.state.read_run(run_id).dispatch_group
            if fork is None or fork.status in {"started", "closed", "failed"}:
                raise RuntimeError("fork is not accepting a provider submission")
            branch = next((item for item in fork.targets if item.dispatch_target_index == branch_index), None)
            if branch is None:
                raise RuntimeError("fork branch is missing")
            if branch.launch_state in {"started", "cleaned"}:
                return
            if branch.launch_state in {"submitted", "unknown"}:
                return
            updated_branch = branch.model_copy(update={"launch_state": "submitted"})
            # Keep the group envelope in ``prepared`` until the provider
            # returns a durable outcome.  The target-level submitted state is
            # the recovery fence; retaining the group status lets a crash
            # between receipt writes and finalization re-enter recovery.
            status = fork.status
            updated = fork.model_copy(update={
                "targets": [updated_branch if item.dispatch_target_index == branch_index else item for item in fork.targets],
                "status": status,
                "updated_at": self.clock.timestamp(),
            })
            self._write_dispatch_group(run_id, updated)

    def mark_dispatch_unknown(self, run_id: str, branch_index: int) -> None:
        """Record that a fenced provider operation may have executed.

        The coordinator must fail closed until the provider journal or an
        exact receipt resolves this target; it must never treat the target as
        prepared and replay the operation blindly.
        """
        with self.state.lock_run(run_id):
            fork = self.state.read_run(run_id).dispatch_group
            if fork is None or fork.status in {"started", "closed", "failed"}:
                raise RuntimeError("fork is not accepting an unknown provider outcome")
            branch = next((item for item in fork.targets if item.dispatch_target_index == branch_index), None)
            if branch is None:
                raise RuntimeError("fork branch is missing")
            if branch.launch_state in {"started", "cleaned", "failed"}:
                return
            updated_branch = branch.model_copy(update={"launch_state": "unknown"})
            updated = fork.model_copy(update={
                "targets": [updated_branch if item.dispatch_target_index == branch_index else item for item in fork.targets],
                "status": "unknown",
                "updated_at": self.clock.timestamp(),
            })
            self._write_dispatch_group(run_id, updated)

    def finish_dispatch_group(self, run_id: str, *, failure: str | None = None) -> DispatchGroupState:
        with self.state.lock_run(run_id):
            fork = self.state.read_run(run_id).dispatch_group
            if fork is None:
                raise RuntimeError("fork group is missing")
            complete = all(b.launch_state == "started" and b.provider_session_id for b in fork.targets)
            if failure is None and not complete:
                raise RuntimeError("fork has incomplete receipts")
            submitted = any(b.launch_state in {"submitted", "unknown"} for b in fork.targets)
            unknown_failure = failure is not None and any(
                token in failure.lower() for token in ("unknown", "lost", "grant", "resource")
            )
            status = (
                "started"
                if failure is None
                else "failed"
                if unknown_failure
                else "partial"
                if any(b.provider_session_id for b in fork.targets) or submitted
                else "prepared"
            )
            fork = fork.model_copy(update={"status": status, "failure": failure, "updated_at": self.clock.timestamp()})
            self._write_dispatch_group(run_id, fork)
            return fork

    def record_dispatch_close(self, agent_session_id: str, native_session_id: str) -> None:
        initial = self.state.read_agent_session(agent_session_id)
        if initial.candidate_origin is None:
            return
        with self.state.lock_run(initial.run_id):
            session = self.state.read_agent_session(agent_session_id)
            binding = session.candidate_origin
            if (binding is None or not native_session_id
                    or binding.provider_session_id != native_session_id
                    or session.session_handle.external_id != native_session_id):
                raise ValueError("fork close receipt belongs to another session")
            fork = self.state.read_run(session.run_id).dispatch_group
            if fork is None:
                raise ValueError("fork close receipt has no group")
            matches = [branch for branch in fork.targets if branch.agent_session_id == agent_session_id]
            if (len(matches) != 1
                    or matches[0].candidate_id != session.candidate_id
                    or matches[0].provider_session_id != native_session_id
                    or matches[0].dispatch_target_index != binding.dispatch_target_index
                    or matches[0].binding != binding
                    or binding.worker_identity != {"agent_session_id": agent_session_id, "candidate_id": session.candidate_id}
                    or binding.dispatch_group_id != fork.dispatch_group_id
                    or binding.runtime_provider != fork.runtime_provider
                    or binding.parent_native_session_id != fork.parent_native_session_id
                    or binding.checkpoint_id != fork.checkpoint_id
                    or binding.checkpoint_kind != fork.checkpoint_kind
                    or binding.frozen_spec_id != fork.frozen_spec_id):
                raise ValueError("fork close receipt does not match its branch or group")
            branch = matches[0]
            closed_branch = branch if branch.closed_at is not None else branch.model_copy(
                update={"closed_at": self.clock.timestamp()},
            )
            branches = [closed_branch if item.agent_session_id == agent_session_id else item
                        for item in fork.targets]
            status = fork.status
            if status == "started" and all(
                item.launch_state == "started" and item.provider_session_id and item.closed_at
                for item in branches
            ):
                status = "closed"
            if branches == fork.targets and status == fork.status:
                return
            self._write_dispatch_group(session.run_id, fork.model_copy(update={
                "targets": branches, "status": status, "updated_at": self.clock.timestamp(),
            }))

    def bind_dispatch_target(
        self, agent_session_id: str, binding: CandidateOrigin, *, run_locked: bool = False,
    ) -> AgentSessionRecord:
        """Atomically bind one host fork receipt to task and session provenance."""
        initial = self.session(agent_session_id, allow_stale=True)
        if initial.workspace != binding.workspace:
            raise ValueError("fork binding workspace does not match session")
        with nullcontext() if run_locked else self.state.lock_run(initial.run_id):
            session = self.state.read_agent_session(agent_session_id)
            candidate = self.state.read_candidate(session.run_id, session.candidate_id)
            run = self.state.read_run(session.run_id)
            frozen = self.state.read_frozen_spec(run.frozen_spec_id)
            if (binding.frozen_spec_id != frozen.frozen_spec_id or binding.spec_hash != frozen.spec_hash
                    or binding.runtime_provider != frozen.runtime_provider
                    or binding.source_artifact_ref != run.source_artifact_ref
                    or binding.worker_identity != {"agent_session_id": session.agent_session_id, "candidate_id": session.candidate_id}):
                raise ValueError("fork binding does not match candidate or FrozenSpec")
            existing = candidate.task.candidate_origin
            if existing is not None and existing != binding:
                # Idempotent replay is allowed only for the exact same receipt.
                excluded = {"provider_session_id", "launch_receipt", "context_projection_digest", "workspace_attachment"}
                old = existing.model_dump(mode="json", exclude=excluded)
                new = binding.model_dump(mode="json", exclude=excluded)
                if old != new or (
                    existing.provider_session_id is not None
                    and existing.provider_session_id != binding.provider_session_id
                ):
                    raise ValueError("fork binding conflicts with persisted candidate")
                if existing.provider_session_id is not None and existing != binding:
                    raise ValueError("fork launch receipt is immutable")
            task_updates = {"candidate_origin": binding}
            metadata = {
                **session.session_handle.metadata,
                "dispatch": {
                    "dispatch_group_id": binding.dispatch_group_id,
                    "dispatch_target_index": binding.dispatch_target_index,
                    "checkpoint_id": binding.checkpoint_id,
                },
            }
            attachment = binding.workspace_attachment
            if binding.runtime_provider == "thinkthread" and binding.provider_session_id is not None:
                if attachment is None or attachment.provider != "thinkthread":
                    raise ValueError("fork binding requires its private Fs attachment")
                provider_handle = {
                    **candidate.task.workspace_provider_handle,
                    "thinkthread_id": binding.provider_session_id,
                    "branch_id": attachment.fs_branch_id,
                    "agent_session_id": agent_session_id,
                }
                access = candidate.task.workspace_access.model_copy(update={"provider_handle": provider_handle})
                task_updates.update(workspace_provider_handle=provider_handle,
                                    workspace_branch=attachment.fs_branch_id, workspace_access=access)
                metadata.update(fs_branch_id=attachment.fs_branch_id, continuation="native_session")
            task = type(candidate.task).model_validate({**candidate.task.model_dump(mode="python"), **task_updates})
            candidate = candidate.model_copy(update={"task": task})
            updated_handle = session.session_handle.model_copy(update={
                "external_id": binding.provider_session_id or session.session_handle.external_id,
                "metadata": metadata,
            })
            updated = AgentSessionRecord.model_validate({**session.model_dump(mode="python"),
                "candidate_origin": binding,
                "session_handle": updated_handle,
                "updated_at": self.clock.timestamp(),
            })
            self.state.write_candidate_binding(candidate)
            self.state.write_agent_session(updated)
            return updated

    def record_native_invocation(
        self, agent_session_id: str, result: SessionInvocationResult,
    ) -> AgentSessionRecord:
        """Persist exact host observations without granting execution or settling Evidence."""
        initial = self.session(agent_session_id)
        with self.state.lock_run(initial.run_id):
            session = self.session(agent_session_id)
            if (result.agent_harness, result.runtime_provider) != (session.agent_harness, session.runtime_provider):
                raise PermissionError("observation does not match session execution identity")
            if session.session_handle.external_id != result.native_session_id:
                raise PermissionError("observation does not belong to the native session handle")
            observation = result.observation
            if observation is None:
                return session
            metadata = dict(session.session_handle.metadata)
            dispatches = list(metadata.get("dispatches") or [])
            receipt = {
                "agent_harness": result.agent_harness,
                "runtime_provider": result.runtime_provider,
                "native_session_id": result.native_session_id,
                "invocation_id": result.invocation_id,
                "state": result.state,
                "started_at": observation.started_at,
                "ended_at": observation.ended_at,
                "duration_seconds": observation.duration_seconds,
                "usage": result.usage,
                "metrics_baseline": observation.metrics_baseline,
            }
            if result.error:
                receipt["error"] = (
                    result.failure.message if result.failure is not None
                    else "native model request failed"
                )
            if result.failure is not None:
                receipt["failure"] = asdict(result.failure)
            for previous in dispatches:
                if (
                    previous.get("native_session_id") == result.native_session_id
                    and previous.get("invocation_id") == result.invocation_id
                ):
                    if previous != receipt:
                        raise ValueError("native invocation observation already recorded with different facts")
                    return session
            dispatches.append(receipt)
            metadata.update(
                dispatches=dispatches, dispatch_count=len(dispatches),
                terminal_state=result.state, started_at=dispatches[0]["started_at"],
                ended_at=observation.ended_at,
                duration_seconds=sum(item["duration_seconds"] for item in dispatches),
            )
            if result.failure is not None:
                metadata["model_failure"] = asdict(result.failure)
            elif result.error:
                metadata["model_failure"] = {
                    "code": "provider_rejected",
                    "message": (
                        result.failure.message if result.failure is not None
                        else "native model request failed"
                    ),
                }
            if session.agent_harness == "pi" and observation.pi_metrics is not None:
                metadata["pi_metrics"] = observation.pi_metrics
                metadata["session_file"] = observation.pi_metrics.get("session_file")
            updated = session.model_copy(update={
                "session_handle": session.session_handle.model_copy(update={"metadata": metadata}),
                "updated_at": self.clock.timestamp(),
            })
            self.state.write_agent_session(updated)
            return updated

    def continue_agent_session(
        self,
        agent_session_id: str,
        worker_budget: dict[str, Any] | None = None,
    ) -> AgentSessionRecord:
        """Return host launch fields that continue a prior worker session.

        This does not create a new candidate workspace. Hosts with native
        continuation reuse the bound worker; state-redispatch hosts return
        their explicit redispatch payload. ``worker_budget`` applies only to
        this continuation dispatch and does not mutate the frozen spec.
        """
        session = self.session(agent_session_id)
        run = self.state.read_run(session.run_id)
        if run.state not in WORKER_ITERATION_RUN_STATES:
            raise RuntimeError(f"cannot continue agent session from state {run.state}")
        frozen = self.state.read_frozen_spec(run.frozen_spec_id)
        candidate_record = self.state.read_candidate(
            session.run_id,
            session.candidate_id,
        )
        frozen = self.state.read_frozen_spec(candidate_record.active_frozen_spec_id or run.frozen_spec_id)
        require_no_pending_verification(candidate_record)
        if candidate_record.status not in {"created", "evaluated"}:
            raise RuntimeError(
                f"cannot continue candidate in status {candidate_record.status}"
            )
        if self._adaptive_search_is_enabled(frozen):
            self.admission.assert_candidate_execution_allowed(
                candidate_record,
                "continue_session",
            )

        worker_budget_override = normalize_worker_budget_override(
            worker_budget=worker_budget,
        )
        launch = self._build_continue_launch_payload(
            frozen=frozen,
            session=session,
            candidate_record=candidate_record,
            worker_budget_override=worker_budget_override,
        )
        counters = dict(session.counters)
        counters["resume_dispatches"] = counters.get("resume_dispatches", 0) + 1
        updated = session.model_copy(
            update={
                "updated_at": self.clock.timestamp(),
                "workspace": candidate_record.task.workspace,
                "launch": launch,
                "counters": counters,
            }
        )
        self.state.write_agent_session(updated)
        return updated

    def _resolve_worker_budget_for_dispatch(
        self,
        *,
        frozen: FrozenSpec,
        candidate_record: CandidateRecord,
        worker_budget_override: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        worker_budget = (
            worker_budget_override
            if worker_budget_override is not None
            else candidate_worker_budget(frozen, candidate_record.task)
        )
        return normalize_worker_budget_override(
            worker_budget=worker_budget,
        )

    def constrain_budget(
        self,
        run_id: str,
        worker_budget: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        deadline_value = self.context.outer_deadline(run_id)
        if deadline_value is None:
            return worker_budget
        deadline = outer_deadline_epoch(deadline_value)
        if deadline is None:
            raise RuntimeError("invalid GOAL_PLUS_OUTER_DEADLINE_AT value")
        available = deadline - self.clock.now()
        if available < 1:
            raise RuntimeError(
                "OuterDeadlineExceeded: no candidate worker can start after the "
                "Goal Plus deadline"
            )
        if worker_budget is None or worker_budget.get("max_runtime_seconds") is None:
            return worker_budget
        effective = dict(worker_budget)
        effective["max_runtime_seconds"] = min(
            int(worker_budget["max_runtime_seconds"]),
            max(1, math.floor(available)),
        )
        return effective

    def invocation_timeout(self, agent_session_id: str, requested: float | None = None) -> float:
        """Bound a native invocation by its prepared budget and current Goal deadline."""
        session = self.session(agent_session_id)
        maximum = (session.launch.get("budget_control") or {}).get("max_runtime_seconds")
        if requested is None:
            deadline = outer_deadline_epoch(self.context.outer_deadline(session.run_id))
            requested = maximum if maximum is not None else (
                deadline - self.clock.now() if deadline is not None else None
            )
        if requested is None or not math.isfinite(requested) or requested <= 0:
            raise ValueError("invocation requires a finite positive budget or Goal deadline")
        effective = min(requested, maximum) if maximum is not None else requested
        budget = self.constrain_budget(
            session.run_id, {"max_runtime_seconds": math.ceil(effective)},
        )
        return min(effective, budget["max_runtime_seconds"])

    def _normalize_main_directive(
        self,
        main_directive: dict[str, Any] | str | None,
    ) -> dict[str, Any]:
        if main_directive is None:
            return {}
        if isinstance(main_directive, str):
            return {"goal": main_directive}
        if isinstance(main_directive, dict):
            return main_directive
        raise TypeError("main_directive must be a dict, string, or null")

    @staticmethod
    def _make_agent_session_id(run_id: str, index: int) -> str:
        match = re.fullmatch(r"run_(\d+)", run_id)
        if match:
            # agent_0007_02 rides along in every wait roster and session
            # receipt; keep it as short as the run id it derives from.
            return f"agent_{match.group(1)}_{index:02d}"
        run_suffix = run_id.removeprefix("run_")
        return f"agent_{run_suffix}_{index:03d}"

    def bind_thinkthread_candidate(
        self,
        *,
        run_id: str,
        candidate_id: str,
        agent_session_id: str,
        thinkthread_id: str,
        branch_id: str,
    ) -> CandidateRecord:
        with self.state.lock_run(run_id):
            run = self.state.read_run(run_id)
            self.authorization.require_search_execution(run_id)
            require_worker_iteration_state(run, "bind ThinkThread candidate")
            record = self.state.read_candidate(run_id, candidate_id)
            if record.task.workspace_provider != "thinkthread":
                raise ValueError("candidate does not use the ThinkThread workspace_provider")
            session = self.session(agent_session_id, run_id=run_id)
            if session.candidate_id != candidate_id or session.runtime_provider != "thinkthread":
                raise PermissionError(
                    "ThinkThread Child binding does not match the candidate session"
                )
            candidate_origin = record.task.candidate_origin
            # The prepared binding intentionally has no provider handle yet;
            # the first TT receipt is the point where Child/Fs identity is
            # attached.  Once either identity is present, every subsequent
            # bind must match it exactly.
            replacement = is_recovery_replacement(session, record)
            if (candidate_origin is not None
                    and (candidate_origin.provider_session_id is not None
                         or candidate_origin.workspace_attachment is not None)):
                attachment = candidate_origin.workspace_attachment
                if (candidate_origin.provider_session_id != thinkthread_id
                        or attachment is None or attachment.provider != "thinkthread"
                        or attachment.fs_branch_id != branch_id
                        or attachment.root_thinkthread_id != run.source_provider_handle.get("root_thinkthread_id")
                        or run.source_artifact_ref is None
                        or attachment.baseline_snapshot_id != run.source_artifact_ref.id) and not replacement:
                    raise ValueError("fork Child and private Fs identity cannot be changed")
            prior = record.task.workspace_provider_handle
            child_changed = prior.get("thinkthread_id") is not None and prior["thinkthread_id"] != thinkthread_id
            branch_changed = prior.get("branch_id") is not None and prior["branch_id"] != branch_id
            if child_changed or branch_changed:
                # A stopped Codex Child can never be rewoken, so its exact continuation
                # respawns a fresh Child and branch as an atomic pair; the run lock and
                # the live session handle together guard the replacement (CAS).
                if replacement:
                    if not (child_changed and branch_changed):
                        raise RuntimeError(
                            f"ThinkThread recovery replacement for {candidate_id} requires a new Child and branch"
                        )
                elif session.agent_harness != "codex" or not (child_changed and branch_changed):
                    raise RuntimeError(
                        f"ThinkThread candidate {candidate_id} changed its Child binding"
                    )
                if (
                    session.session_handle.external_id is not None
                    and session.session_handle.external_id != prior["thinkthread_id"]
                ):
                    raise RuntimeError(
                        "ThinkThread candidate replacement does not own its prior Child binding"
                    )
            prior_owner = prior.get("agent_session_id")
            if prior_owner not in {None, agent_session_id}:
                # A replacement session takes over the binding once the domain has
                # fenced its predecessor by execution generation; a same-generation
                # session can never seize another live session's Child.
                prior_session = self.state.read_agent_session(prior_owner)
                if prior_session.execution_generation == record.execution_generation:
                    raise RuntimeError(
                        f"ThinkThread candidate {candidate_id} changed agent_session_id binding"
                    )
            provider_handle = {
                **prior,
                "thinkthread_id": thinkthread_id,
                "branch_id": branch_id,
                "agent_session_id": agent_session_id,
            }
            record.task.workspace_provider_handle = provider_handle
            record.task.workspace_branch = branch_id
            record.task.workspace_access.provider_handle = provider_handle
            self.state.write_candidate_binding(record)
        self.bind_agent_handle(
            agent_session_id,
            {
                "agent_harness": session.agent_harness,
                "runtime_provider": "thinkthread",
                "external_id": thinkthread_id,
                "metadata": {
                    "fs_branch_id": branch_id,
                    "continuation": "native_session",
                },
            },
        )
        return self.state.read_candidate(run_id, candidate_id)
