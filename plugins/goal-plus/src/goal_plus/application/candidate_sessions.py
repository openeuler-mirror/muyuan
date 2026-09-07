"""Candidate session identity, provenance and launch admission."""

import math
from typing import Any
from goal_plus.application.adaptive_admission import AdaptiveAdmission
from goal_plus.application.candidate_execution import (
    require_candidate_not_recovering,
    require_current_session,
    require_worker_iteration_state,
)
from goal_plus.application.goal_authorization import GoalAuthorization
from goal_plus.application.search_context import SearchContext
from goal_plus.domain.agents import AgentHostHandle
from goal_plus.domain.search import AgentSessionRecord, FrozenSpec, RunState
from goal_plus.domain.time import outer_deadline_epoch
from goal_plus.domain.worker_policy import (
    candidate_worker_budget,
    normalize_worker_budget_override,
)
from goal_plus.ports.candidate_sessions import (
    CandidateSessionState,
    CandidateLaunch,
    MODEL_HANDOFF_RELATIVE_PATH,
)
from goal_plus.ports.clock import Clock
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

    @staticmethod
    def _adaptive_search_is_enabled(frozen: FrozenSpec) -> bool:
        return frozen.spec.strategy.orchestration_mode == "adaptive_search"

    def session(
        self,
        agent_session_id: str,
        run_id: str | None = None,
        *,
        allow_stale: bool = False,
    ) -> AgentSessionRecord:
        session = self.state.read_agent_session(agent_session_id, run_id)
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
            frozen,
            worker_budget_override
            if worker_budget_override is not None
            else candidate_worker_budget(frozen, candidate_record.task),
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
            frozen,
            worker_budget_override
            if worker_budget_override is not None
            else candidate_worker_budget(frozen, candidate_record.task),
        )
        return self.launch.continue_session(frozen, session, candidate_record, budget)

    def start_agent_session(
        self,
        run_id: str,
        candidate_id: str,
        directive: dict[str, Any] | str | None = None,
        *,
        worker_budget: dict[str, Any] | None = None,
    ) -> AgentSessionRecord:
        """Create a context/provenance handle and host-native launch payload.

        Does not start a worker or track lifecycle state. ``worker_budget`` is
        an optional one-dispatch override; it does not mutate the frozen spec or
        the candidate policy.
        """
        return self._create_agent_session(
            run_id=run_id,
            candidate_id=candidate_id,
            directive=directive,
            worker_budget_override=worker_budget,
            reuse_initial=True,
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
        behavior and any legacy native role come from the frozen binding. A
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
            self.admission.reconcile_candidate_execution_allowed(
                run_id,
                candidate_id,
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
                "这是现有候选的新 worker session。首先调用 search_get_agent_context，"
                "使用其中本 candidate 的紧凑历史和可选 ledger projection 恢复权威上下文；"
                "仅在必须"
                "核对旧轮次详情时调用 search_list_iterations，再读取 "
                "search_get_global_evidence。"
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
    ) -> AgentSessionRecord:
        run = self.state.read_run(run_id)
        frozen = self.state.read_frozen_spec(run.frozen_spec_id)
        if self._adaptive_search_is_enabled(frozen):
            self.admission.reconcile_candidate_execution_allowed(
                run_id,
                candidate_id,
                "start_session",
            )
        with self.state.lock_run(run_id):
            run = self.state.read_run(run_id)
            if run.state not in {
                RunState.RUNNING,
                RunState.WAITING_FOR_WORKERS,
                RunState.SELECTING,
                RunState.SELECTION_BLOCKED,
            }:
                raise RuntimeError(f"cannot start agent session from state {run.state}")
            self.authorization.require_search_execution(run_id)
            frozen = self.state.read_frozen_spec(run.frozen_spec_id)

            candidate_record = self.state.read_candidate(run_id, candidate_id)
            require_candidate_not_recovering(candidate_record)
            workspace = candidate_record.task.workspace
            if self._adaptive_search_is_enabled(frozen):
                self.admission.assert_candidate_execution_allowed(
                    candidate_record,
                    "start_session",
                )

            if worker_budget_override is not None:
                worker_budget_override = normalize_worker_budget_override(
                    worker_host=frozen.worker_host,
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
                    if (
                        session.directive == normalized_directive
                        and worker_budget_override is None
                    ):
                        return session
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
                        or session.launch != expected_launch
                    ):
                        raise RuntimeError(
                            f"candidate {candidate_id} initial agent session already exists "
                            "with different launch options"
                        )
                    return session

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
            host = frozen.worker_host
            if host == "pi-rpc":
                launch["run_id"] = run_id
            host_handle = AgentHostHandle(host=host)
            if host == "codex":
                host_handle = host_handle.model_copy(
                    update={"task_name": launch.get("task_name")}
                )
            elif host == "pi-rpc":
                host_handle = host_handle.model_copy(
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
                host=host,
                host_handle=host_handle,
                created_at=now,
                updated_at=now,
                directive=normalized_directive,
                workspace=workspace,
                launch=launch,
                counters={},
            )
            self.state.write_session_index(run)
            self.state.write_agent_session(session)
            return session

    def bind_agent_handle(
        self,
        agent_session_id: str,
        handle: dict[str, Any],
    ) -> AgentSessionRecord:
        """Bind a runtime session to the host-specific worker handle."""
        session = self.session(agent_session_id)
        host = handle.get("host", session.host)
        if host != session.host:
            raise ValueError(
                f"agent session host is {session.host}, got handle for {host}"
            )

        metadata = {
            **session.host_handle.metadata,
            **dict(handle.get("metadata") or {}),
        }
        if session.host == "pi-rpc":
            prior_dispatches = session.host_handle.metadata.get("dispatches")
            dispatches = (
                list(prior_dispatches) if isinstance(prior_dispatches, list) else []
            )
            handle_metadata = dict(handle.get("metadata") or {})
            metrics = handle_metadata.get("pi_metrics")
            metrics = metrics if isinstance(metrics, dict) else {}
            dispatches.append(
                {
                    "process_pid": handle_metadata.get("process_pid"),
                    "started_at": metrics.get("dispatch_started_at")
                    or metrics.get("started_at"),
                    "ended_at": metrics.get("dispatch_ended_at")
                    or metrics.get("ended_at"),
                    "duration_seconds": metrics.get("dispatch_duration_seconds")
                    or metrics.get("duration_seconds"),
                    "usage": metrics.get("usage_delta"),
                    "baseline_last_entry_id": metrics.get("baseline_last_entry_id"),
                    "final_last_entry_id": metrics.get("final_last_entry_id"),
                    "timed_out": bool(handle_metadata.get("timed_out")),
                    "runner_failed": bool(handle_metadata.get("runner_failed")),
                }
            )
            metadata["dispatches"] = dispatches
            metadata["dispatch_count"] = len(dispatches)
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
        updated_handle = session.host_handle.model_copy(
            update={
                "host": session.host,
                "external_id": handle.get(
                    "external_id", session.host_handle.external_id
                ),
                "task_name": handle.get("task_name", session.host_handle.task_name),
                "nickname": handle.get("nickname", session.host_handle.nickname),
                "metadata": metadata,
            }
        )
        updated = session.model_copy(
            update={
                "host_handle": updated_handle,
                "updated_at": self.clock.timestamp(),
            }
        )
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
        if run.state not in {
            RunState.RUNNING,
            RunState.WAITING_FOR_WORKERS,
            RunState.SELECTING,
            RunState.SELECTION_BLOCKED,
        }:
            raise RuntimeError(f"cannot continue agent session from state {run.state}")
        frozen = self.state.read_frozen_spec(run.frozen_spec_id)
        candidate_record = self.state.read_candidate(
            session.run_id,
            session.candidate_id,
        )
        if candidate_record.status not in {"created", "evaluated"}:
            raise RuntimeError(
                f"cannot continue candidate in status {candidate_record.status}"
            )
        if self._adaptive_search_is_enabled(frozen):
            self.admission.reconcile_candidate_execution_allowed(
                session.run_id,
                session.candidate_id,
                "continue_session",
            )

        worker_budget_override = normalize_worker_budget_override(
            worker_host=session.host,
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
            worker_host=frozen.worker_host,
            worker_budget=worker_budget,
        )

    def constrain_budget(
        self,
        run_id: str,
        frozen: FrozenSpec,
        worker_budget: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        deadline_value = self.context.outer_deadline(run_id)
        if deadline_value is None:
            return worker_budget
        deadline = outer_deadline_epoch(deadline_value)
        if deadline is None:
            raise RuntimeError("invalid GOAL_PLUS_OUTER_DEADLINE_AT value")
        reserve = float(
            frozen.spec.strategy.config.get("closeout_reserve_seconds")
            or frozen.spec.strategy.config.get("reserve_closeout_seconds")
            or 0
        )
        available = deadline - self.clock.now() - reserve
        if available < 1:
            raise RuntimeError(
                "OuterDeadlineExceeded: no candidate worker can start after the "
                "Goal Plus closeout boundary"
            )
        if worker_budget is None or worker_budget.get("max_runtime_seconds") is None:
            return worker_budget
        effective = dict(worker_budget)
        effective["max_runtime_seconds"] = min(
            int(worker_budget["max_runtime_seconds"]),
            max(1, math.floor(available)),
        )
        return effective

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
            if record.task.workspace_backend != "thinkthread":
                raise ValueError("candidate does not use the ThinkThread backend")
            session = self.session(agent_session_id, run_id=run_id)
            if session.candidate_id != candidate_id or session.host != "thinkthread":
                raise PermissionError(
                    "ThinkThread Child binding does not match the candidate session"
                )
            prior = record.task.workspace_provider_handle
            for key, value in (
                ("thinkthread_id", thinkthread_id),
                ("branch_id", branch_id),
                ("agent_session_id", agent_session_id),
            ):
                if prior.get(key) not in {None, value}:
                    raise RuntimeError(
                        f"ThinkThread candidate {candidate_id} changed {key} binding"
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
                "host": "thinkthread",
                "external_id": thinkthread_id,
                "metadata": {
                    "fs_branch_id": branch_id,
                    "continuation": "native_session",
                },
            },
        )
        return self.state.read_candidate(run_id, candidate_id)
