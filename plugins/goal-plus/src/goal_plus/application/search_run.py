"""Initial Search run and plan transitions with explicit creation and resource ports."""

from typing import Any
from goal_plus.domain.search import VerifierInvalidationReason
from goal_plus.domain.agents import ModelSpec, SelectedModel
from goal_plus.domain.search import (
    BootstrapVerification,
    BootstrapVerifierResult,
    CandidateProposal,
    CandidateTask,
    CandidateWorkOrder,
    FrozenSpec,
    RunState,
    SearchPlan,
    VerifierRole,
)
from goal_plus.ports.state import CandidateRecord, RunRecord

from goal_plus.application.candidate_creation import CandidateCreation
from goal_plus.application.candidate_sessions import CandidateSessions
from goal_plus.application.search_research import SearchResearch
from goal_plus.application.annotation import AnnotationWorkflow
from goal_plus.application.goal_authorization import GoalAuthorization
from goal_plus.ports.search_run import (
    SearchRunState,
    BaselinePreparation,
    DeviceLeases,
    DEVICE_LEASE_RELEASE_STATES,
)
from goal_plus.ports.search_bootstrap import BootstrapVerifiers, SourceInspection
from goal_plus.ports.search_configuration import SearchRunBindings
from goal_plus.ports.clock import Clock


class SearchRun:
    def __init__(
        self,
        *,
        state: SearchRunState,
        source: SourceInspection,
        bindings: SearchRunBindings,
        baseline: BaselinePreparation,
        creation: CandidateCreation,
        sessions: CandidateSessions,
        research: SearchResearch,
        verifiers: BootstrapVerifiers,
        annotation: AnnotationWorkflow,
        leases: DeviceLeases,
        authorization: GoalAuthorization,
        clock: Clock,
    ) -> None:
        self.state, self.source, self.candidate_creation, self.candidate_sessions = (
            state,
            source,
            creation,
            sessions,
        )
        self.bindings, self.baseline = bindings, baseline
        self.candidate_ledger, self.research, self.verifiers = (
            creation.ledger,
            research,
            verifiers,
        )
        self.annotation, self.leases, self.authorization, self.clock = (
            annotation,
            leases,
            authorization,
            clock,
        )

    def persist(self, run: RunRecord) -> None:
        self.state.write_run(run)
        if run.state in DEVICE_LEASE_RELEASE_STATES:
            self.leases.release_device_pool_leases(run)

    def acquire_leases(self, run: RunRecord, frozen: FrozenSpec) -> None:
        with self.leases.reservation(run, frozen) as reservation:
            self.state.write_run(run)
            try:
                reservation.commit()
            except Exception:
                run.state = RunState.FAILED
                self.state.write_run(run)
                raise

    def create_run(
        self,
        frozen_spec_id: str,
        source_run_id: str | None = None,
        *,
        run_id: str | None = None,
    ) -> str:
        frozen = self.state.read_frozen_spec(frozen_spec_id)
        existing = None
        if run_id is not None:
            try:
                existing = self.state.read_run(run_id)
            except FileNotFoundError:
                pass
            if existing is not None:
                if (existing.frozen_spec_id, existing.source_run_id) != (frozen_spec_id, source_run_id):
                    raise RuntimeError("SearchCreateConflict: reserved run belongs to different inputs")
                if existing.bootstrap_complete:
                    return run_id
                if existing.state not in {RunState.RUNNING, RunState.FAILED}:
                    raise RuntimeError("SearchBootstrapFailed: reserved run cannot be resumed")
        self.bindings.validate_host_workspace_provider(frozen.spec, frozen.agent_harness, frozen.runtime_provider)
        self.source.validate_clean_source_repository(frozen.spec)
        if frozen.source_artifact_ref is not None:
            current_source_artifact = self.source.current_artifact(frozen.spec)
            if current_source_artifact != frozen.source_artifact_ref:
                raise RuntimeError(
                    "FrozenSourceChanged: current origin artifact does not match "
                    f"frozen source artifact {frozen.source_artifact_ref.id}"
                )
        capacity = frozen.workspace_capacity_estimate
        if capacity is not None and capacity.fits_available_space is False:
            raise RuntimeError(
                "workspace capacity preflight failed: conservative upper bound "
                f"{capacity.conservative_upper_bound_bytes} bytes exceeds "
                f"available space {capacity.available_bytes} bytes for "
                f"{capacity.worktree_nodes} nodes"
            )
        selected_models = self._expand_selected_models(
            frozen.spec.strategy.models,
            frozen.spec.budget.max_parallel,
        )
        if selected_models and len(selected_models) != frozen.spec.budget.max_parallel:
            raise ValueError(
                "frozen strategy.models must resolve to budget.max_parallel selected models"
            )
        baseline_source = (
            self.baseline.prepare_baseline(frozen.spec)
            if frozen.spec.workspace.provider == "thinkthread"
            else None
        )
        inherited_research = (
            self.research.build_inherited_research(
                source_run_id,
            )
            if source_run_id
            else {}
        )
        run_id = run_id or self.state.new_run_id(self.clock.now())
        run = existing or RunRecord(
            run_id=run_id,
            state=RunState.RUNNING,
            frozen_spec_id=frozen.frozen_spec_id,
            source_path=self.source.resolved_source_path(frozen.spec),
            source_artifact_ref=frozen.source_artifact_ref,
            created_at=self.clock.timestamp(),
            source_run_id=source_run_id,
            inherited_research=inherited_research,
            selected_models=selected_models,
            source_provider_handle=baseline_source.provider_handle
            if baseline_source is not None
            else {},
        )
        if existing is None:
            self.acquire_leases(run, frozen)
        elif run.state == RunState.FAILED:
            run.state = RunState.RUNNING
            self.acquire_leases(run, frozen)
        try:
            self.state.initialize_run_layout(run_id, frozen.spec.workspace.provider)
            if baseline_source is not None:
                baseline = run.source_artifact_ref or baseline_source.capture(run)
                with self.state.lock_run(run_id):
                    run = self.state.read_run(run_id)
                    run.source_artifact_ref = baseline
                    self.persist(run)
                baseline_report = baseline_source.verify(run, frozen, baseline)
                bootstrap = BootstrapVerification(
                    phase="origin",
                    workspace_provider="thinkthread",
                    passed=baseline_report.process_passed,
                    aggregate_score=baseline_report.aggregate_score,
                    verifier_results=[
                        BootstrapVerifierResult.model_validate(
                            item.model_dump(mode="json")
                        )
                        for item in baseline_report.verifier_results
                    ],
                    source_artifact_ref=baseline,
                    started_at=run.created_at,
                    completed_at=self.clock.timestamp(),
                )
                with self.state.lock_run(run_id):
                    run = self.state.read_run(run_id)
                    run.workspace_bootstrap_verification = bootstrap
                    if not bootstrap.evaluation_completed:
                        run.state = RunState.ABORTED
                    self.persist(run)
                if not bootstrap.evaluation_completed:
                    raise ValueError(
                        "OriginVerifierFailed: process verifier suite did not complete "
                        f"on ThinkThread baseline {baseline.id}.{bootstrap.failure_detail()}"
                    )
                bootstrap.require_numeric_metric(frozen.spec.metric_name)
            if source_run_id:
                with self.state.lock_run(source_run_id):
                    source_run = self.state.read_run(source_run_id)
                    if source_run.invalidated_at:
                        source_run.replacement_run_id = run_id
                        self.persist(source_run)
        except Exception:
            with self.state.lock_run(run_id):
                failed_run = self.state.read_run(run_id)
                if failed_run.state == RunState.RUNNING:
                    failed_run.state = RunState.FAILED
                    self.persist(failed_run)
            raise
        with self.state.lock_run(run_id):
            run = self.state.read_run(run_id)
            run.bootstrap_complete = True
            self.persist(run)
        return run_id

    def fail_creation(self, run_id: str) -> None:
        with self.state.lock_run(run_id):
            try:
                run = self.state.read_run(run_id)
            except FileNotFoundError:
                return
            if run.candidates_total:
                raise RuntimeError("cannot fail creation after candidate materialization")
            run.state = RunState.FAILED
            self.persist(run)

    def plan_next(self, run_id: str, requested_k: int = 4) -> SearchPlan:
        with self.state.lock_run(run_id):
            return self._plan_next_locked(run_id, requested_k)

    def _plan_next_locked(self, run_id: str, requested_k: int) -> SearchPlan:
        self.authorization.require_search_execution(run_id)
        if requested_k <= 0:
            raise ValueError("requested_k must be > 0")

        run = self.state.read_run(run_id)
        if run.state not in {
            RunState.RUNNING,
            RunState.WAITING_FOR_WORKERS,
            RunState.SELECTING,
            RunState.SELECTION_BLOCKED,
        }:
            raise RuntimeError(f"cannot plan next batch from state {run.state}")

        frozen = self.state.read_frozen_spec(run.frozen_spec_id)
        spec = frozen.spec
        self.candidate_sessions.constrain_budget(run_id, None)
        if self.state.read_plans(run_id):
            raise RuntimeError(
                "Search permits one initial SearchPlan; resume or "
                "redispatch the existing candidates instead of planning a new batch"
            )
        remaining = max(0, spec.budget.max_parallel - run.candidates_total)
        planned_k = min(requested_k, remaining)
        selected_models = list(run.selected_models)
        if selected_models:
            if requested_k != len(selected_models):
                raise ValueError(
                    "requested_k must equal the fixed selected_models size "
                    f"({len(selected_models)})"
                )
            if planned_k != len(selected_models):
                raise ValueError(
                    "selected_models size exceeds the remaining parallel budget"
                )
        strategy = spec.strategy
        self.bindings.validate_host_strategy(strategy, frozen.agent_harness, frozen.runtime_provider)
        mode = self.bindings.strategy_mode(strategy)

        if mode in {"agent", "agent_guided", "default"}:
            plan = self._plan_agent_guided(
                run, frozen, requested_k, planned_k, remaining
            )
        elif mode in {"random", "random_mode"}:
            plan = self._plan_independent(
                run, frozen, requested_k, planned_k, remaining
            )
        else:
            raise ValueError(
                f"unsupported initial strategy: {strategy.name}; "
                "use agent_guided or random"
            )

        plan.worker_policy = self.bindings.normalize_worker_policy(
            plan.strategy, plan.worker_policy, agent_harness=frozen.agent_harness, runtime_provider=frozen.runtime_provider
        )
        plan.selected_models = selected_models
        plan.strategy_trace.setdefault("worker_policy", plan.worker_policy)
        self.state.write_plan(plan)
        run.budget_used["last_plan_id"] = plan.plan_id
        self.persist(run)
        return plan

    def start_batch(
        self,
        run_id: str,
        plan_id: str,
        proposals: list[CandidateProposal] | None = None,
    ) -> list[CandidateTask]:
        with self.state.lock_run(run_id):
            return self._start_batch_locked(run_id, plan_id, proposals)

    def _start_batch_locked(
        self,
        run_id: str,
        plan_id: str,
        proposals: list[CandidateProposal] | None,
    ) -> list[CandidateTask]:
        self.authorization.require_search_execution(run_id)
        run = self.state.read_run(run_id)
        if run.state not in {
            RunState.RUNNING,
            RunState.WAITING_FOR_WORKERS,
            RunState.SELECTING,
            RunState.SELECTION_BLOCKED,
        }:
            raise RuntimeError(f"cannot create candidates from state {run.state}")

        frozen = self.state.read_frozen_spec(run.frozen_spec_id)
        self.acquire_leases(run, frozen)
        self.candidate_sessions.constrain_budget(run_id, None)
        plan = self.state.read_plan(run_id, plan_id)
        all_records = self.candidate_creation.state.read_candidates(run_id)
        plan_records = sorted(
            (record for record in all_records if record.task.plan_id == plan_id),
            key=lambda record: record.candidate_id,
        )
        if all_records:
            highest_index = max(
                int(record.candidate_id.removeprefix("c")) for record in all_records
            )
            run.candidates_total = max(run.candidates_total, len(all_records))
            run.next_candidate_index = max(run.next_candidate_index, highest_index + 1)

        if plan.status == "started":
            records_by_id = {record.candidate_id: record for record in plan_records}
            try:
                tasks = [
                    records_by_id[candidate_id].task
                    for candidate_id in plan.started_candidate_ids
                ]
            except KeyError as exc:
                raise RuntimeError(
                    f"started plan {plan_id} is missing candidate state for {exc.args[0]}"
                ) from exc
            if tasks:
                self._ensure_workspace_bootstrap_verification(
                    run=run,
                    frozen=frozen,
                    task=tasks[0],
                )
                for record in plan_records:
                    self.candidate_ledger.ensure(frozen, record)
                    self.candidate_creation.state.write_candidate(run_id, record)
                run.state = RunState.WAITING_FOR_WORKERS
                self.persist(run)
            return tasks
        if plan.status != "planned":
            raise RuntimeError(f"plan {plan_id} has already been started")

        remaining = max(
            0,
            frozen.spec.budget.max_parallel - run.candidates_total,
        )
        target_count = min(plan.planned_k, len(plan_records) + remaining)
        if target_count <= 0:
            return []

        if plan.requires_agent_proposals:
            if not proposals:
                raise ValueError("this strategy plan requires candidate proposals")
            self._validate_agent_proposals(plan, proposals)
            candidate_proposals = proposals[:target_count]
        else:
            if proposals:
                raise ValueError(
                    "this strategy plan already contains fixed work orders"
                )
            candidate_proposals = [
                self._proposal_from_work_order(work_order)
                for work_order in plan.work_orders
            ][:target_count]

        if len(plan_records) > len(candidate_proposals):
            raise RuntimeError(
                f"plan {plan_id} has more persisted candidates than candidate proposals"
            )
        for record, proposal in zip(plan_records, candidate_proposals, strict=False):
            if record.task.proposal != proposal:
                raise RuntimeError(
                    f"retry proposals do not match persisted candidate "
                    f"{record.candidate_id}"
                )

        tasks = [record.task for record in plan_records]
        if tasks:
            self._ensure_workspace_bootstrap_verification(
                run=run,
                frozen=frozen,
                task=tasks[0],
            )
            for record in plan_records:
                self.candidate_ledger.ensure(frozen, record)
                self.candidate_creation.state.write_candidate(run_id, record)
        for index, proposal in enumerate(
            candidate_proposals[len(plan_records) :], start=len(plan_records) + 1
        ):
            candidate_id = f"c{run.next_candidate_index:03d}"
            task = self.candidate_creation.prepare_task(
                run=run,
                frozen=frozen,
                candidate_id=candidate_id,
                plan=plan,
                proposal=proposal,
                slot=index,
            )
            record = CandidateRecord(
                candidate_id=candidate_id,
                status="created",
                task=task,
                results_ledger=self.candidate_ledger.inherited(run, task),
            )
            self.candidate_creation.state.write_candidate(run_id, record)
            tasks.append(task)
            run.next_candidate_index += 1
            run.candidates_total += 1
            self.persist(run)
            if len(tasks) == 1:
                self._ensure_workspace_bootstrap_verification(
                    run=run,
                    frozen=frozen,
                    task=task,
                )
            self.candidate_ledger.ensure(frozen, record)
            self.candidate_creation.state.write_candidate(run_id, record)

        if tasks:
            run.state = RunState.WAITING_FOR_WORKERS
            plan.status = "started"
            plan.started_candidate_ids = [task.candidate_id for task in tasks]
            self.state.write_plan(plan)
            self.persist(run)

        return tasks

    def _ensure_workspace_bootstrap_verification(
        self,
        *,
        run: RunRecord,
        frozen: FrozenSpec,
        task: CandidateTask,
    ) -> None:
        if (
            not self.candidate_creation.workspaces.requires_relocation(
                frozen.spec.workspace.provider
            )
            or frozen.origin_verification is None
        ):
            return
        if run.workspace_bootstrap_verification is not None:
            if not run.workspace_bootstrap_verification.evaluation_completed:
                raise RuntimeError(
                    "WorkspaceRelocationVerifierFailed: the persisted candidate "
                    "workspace bootstrap verification did not pass"
                )
            return

        if run.source_artifact_ref is None and task.base_artifact_ref is not None:
            # A plain-directory Git source gets a provider-created baseline during
            # c001 materialization. Recover that persisted task anchor when an
            # interrupted candidate-record write precedes the run-record write.
            run.source_artifact_ref = task.base_artifact_ref
            self.persist(run)

        candidate_artifact = self.candidate_creation.workspaces.current_artifact(task)
        if (
            run.source_artifact_ref is not None
            and candidate_artifact != run.source_artifact_ref
        ):
            verification = BootstrapVerification(
                phase="workspace_relocation",
                workspace_provider=frozen.spec.workspace.provider,
                passed=False,
                aggregate_score=0.0,
                verifier_results=[
                    BootstrapVerifierResult(
                        name="source_artifact_check",
                        role=VerifierRole.ANTI_CHEAT_GATE,
                        passed=False,
                        score=0.0,
                        metrics={
                            "expected": (
                                run.source_artifact_ref.model_dump(mode="json")
                                if run.source_artifact_ref is not None
                                else None
                            ),
                            "actual": (
                                candidate_artifact.model_dump(mode="json")
                                if candidate_artifact is not None
                                else None
                            ),
                        },
                        failure_class="FrozenSourceChanged",
                    )
                ],
                source_artifact_ref=candidate_artifact,
                candidate_id=task.candidate_id,
                started_at=self.clock.timestamp(),
                completed_at=self.clock.timestamp(),
            )
        else:
            verification = self.verifiers.run_bootstrap_verifiers(
                spec=frozen.spec,
                workspace=task.workspace,
                workspace_provider=frozen.spec.workspace.provider,
                phase="workspace_relocation",
                verifier_hashes=frozen.verifier_hashes,
                log_dir=self.state.relocation_log_dir(run.run_id),
                source_artifact_ref=candidate_artifact,
                device_inventory=frozen.device_inventory,
                candidate_id=task.candidate_id,
            )
            candidate_artifact_after = (
                self.candidate_creation.workspaces.current_artifact(task)
            )
            candidate_status = self.candidate_creation.workspaces.status(task)
            postcheck_results = list(verification.verifier_results)
            if candidate_artifact_after != run.source_artifact_ref:
                postcheck_results.append(
                    BootstrapVerifierResult(
                        name="source_artifact_check_after_verifier",
                        role=VerifierRole.ANTI_CHEAT_GATE,
                        passed=False,
                        score=0.0,
                        metrics={
                            "expected": (
                                run.source_artifact_ref.model_dump(mode="json")
                                if run.source_artifact_ref is not None
                                else None
                            ),
                            "actual": (
                                candidate_artifact_after.model_dump(mode="json")
                                if candidate_artifact_after is not None
                                else None
                            ),
                        },
                        failure_class="FrozenSourceChanged",
                    )
                )
            if candidate_status:
                postcheck_results.append(
                    BootstrapVerifierResult(
                        name="workspace_clean_check_after_verifier",
                        role=VerifierRole.ANTI_CHEAT_GATE,
                        passed=False,
                        score=0.0,
                        metrics={"git_status": candidate_status},
                        failure_class="VerifierWorkspaceSideEffect",
                    )
                )
            if len(postcheck_results) != len(verification.verifier_results):
                verification = verification.model_copy(
                    update={
                        "passed": False,
                        "aggregate_score": 0.0,
                        "verifier_results": postcheck_results,
                        "source_artifact_ref": candidate_artifact_after,
                    }
                )

        run.workspace_bootstrap_verification = verification
        if verification.evaluation_completed:
            self.persist(run)
            return

        run.state = RunState.ABORTED
        self.annotation.close_queue(
            run.run_id,
            reason="workspace_relocation_failed",
        )
        run.invalidated_at = self.clock.timestamp()
        run.invalidation_reason = "verifier_target_mismatch"
        run.invalidation_summary = (
            "The evaluation completed in origin but failed in the first materialized "
            "candidate workspace."
        )
        run.invalidation_evidence = [verification.model_dump(mode="json")]
        self.persist(run)
        raise RuntimeError(
            "WorkspaceRelocationVerifierFailed: verifier behavior in candidate "
            f"{task.candidate_id} did not match origin.{verification.failure_detail()}"
        )

    def _next_plan_id(self, run: RunRecord) -> str:
        plan_id = f"plan_{run.next_plan_index:03d}"
        run.next_plan_index += 1
        return plan_id

    def _plan_independent(
        self,
        run: RunRecord,
        frozen: FrozenSpec,
        requested_k: int,
        planned_k: int,
        remaining: int,
    ) -> SearchPlan:
        work_orders = []
        for slot in range(1, planned_k + 1):
            hypothesis_index = run.candidates_total + slot - 1
            planned_candidate_id = f"c{run.next_candidate_index + slot - 1:03d}"
            hypothesis = (
                frozen.spec.root_hypotheses[hypothesis_index]
                if hypothesis_index < len(frozen.spec.root_hypotheses)
                else f"独立候选 {planned_candidate_id}"
            )
            work_orders.append(
                CandidateWorkOrder(
                    slot=slot,
                    intent=hypothesis,
                    hypothesis=hypothesis,
                    metadata={"strategy": frozen.spec.strategy.orchestration_mode},
                )
            )

        return SearchPlan(
            run_id=run.run_id,
            plan_id=self._next_plan_id(run),
            strategy=frozen.spec.strategy,
            requested_k=requested_k,
            planned_k=planned_k,
            remaining_budget=remaining,
            requires_agent_proposals=False,
            work_orders=work_orders,
            strategy_trace={
                "selection_rule": "独立源码分支",
                "reason": "每个候选都从冻结的源码工作区开始。",
            },
            created_at=self.clock.timestamp(),
        )

    def _plan_agent_guided(
        self,
        run: RunRecord,
        frozen: FrozenSpec,
        requested_k: int,
        planned_k: int,
        remaining: int,
    ) -> SearchPlan:
        return SearchPlan(
            run_id=run.run_id,
            plan_id=self._next_plan_id(run),
            strategy=frozen.spec.strategy,
            requested_k=requested_k,
            planned_k=planned_k,
            remaining_budget=remaining,
            requires_agent_proposals=True,
            strategy_trace={
                "selection_rule": "agent 引导的初始候选",
                "reason": "主 agent 只定义一次初始候选集合。",
            },
            created_at=self.clock.timestamp(),
        )

    def _proposal_from_work_order(
        self, work_order: CandidateWorkOrder
    ) -> CandidateProposal:
        return CandidateProposal(
            hypothesis=work_order.hypothesis,
            intent=work_order.intent,
            instructions=work_order.instructions,
            metadata={
                **work_order.metadata,
                "slot": work_order.slot,
            },
        )

    def _validate_agent_proposals(
        self,
        plan: SearchPlan,
        proposals: list[CandidateProposal],
    ) -> None:
        if len(proposals) > plan.planned_k:
            raise ValueError("too many proposals for this plan")
        return

    @staticmethod
    def _expand_selected_models(
        models: list[ModelSpec], max_parallel: int
    ) -> list[SelectedModel]:
        if not models:
            return []
        explicit_counts = [model.count is not None for model in models]
        if any(explicit_counts) and not all(explicit_counts):
            raise ValueError(
                "frozen strategy.models must either specify count for every model or for none"
            )
        expanded = (
            [model for model in models for _ in range(int(model.count or 0))]
            if all(explicit_counts)
            else [models[index % len(models)] for index in range(max_parallel)]
        )
        selected: list[SelectedModel] = []
        for model in expanded:
            selected.append(
                SelectedModel(
                    slot=len(selected) + 1,
                    model=model.model,
                    provider=model.provider,
                    adapter_version=model.adapter_version,
                    reasoning_effort=model.reasoning_effort,
                    service_tier=model.service_tier,
                    context_policy=model.context_policy,
                )
            )
        return selected

    def invalidate_run(
        self,
        run_id: str,
        *,
        reason: VerifierInvalidationReason,
        summary: str,
        evidence: list[dict[str, Any]],
    ) -> RunRecord:
        """Atomically fence a verifier-invalid run before host workers stop."""

        if not summary.strip():
            raise ValueError("summary must be non-empty")
        if not evidence:
            raise ValueError("evidence must contain at least one concrete item")

        with self.state.lock_run(run_id):
            run = self.state.read_run(run_id)
            if run.state == RunState.PROMOTED:
                raise RuntimeError("cannot invalidate an already promoted run")
            if run.invalidated_at:
                if run.invalidation_reason != reason:
                    raise RuntimeError(
                        "run is already invalidated for a different verifier reason"
                    )
                return run
            self.annotation.close_queue(
                run_id,
                reason=f"run_invalidated:{reason}",
            )
            updated = RunRecord.model_validate(
                {
                    **run.model_dump(mode="json"),
                    "state": RunState.ABORTED,
                    "invalidated_at": self.clock.timestamp(),
                    "invalidation_reason": reason,
                    "invalidation_summary": summary.strip(),
                    "invalidation_evidence": evidence,
                }
            )
            self.persist(updated)
            return updated
