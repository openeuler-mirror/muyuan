"""The single hard-settlement owner for local and provider-backed attempts."""

from typing import Callable

from goal_plus.application.candidate_ranking import best_artifact_iteration_record
from goal_plus.application.candidate_execution import (
    require_candidate_not_recovering,
    require_worker_iteration_state,
)
from goal_plus.domain.agents import (
    WORKSPACE_LEDGER_PROJECTION_NONE,
    WORKSPACE_LEDGER_PROJECTION_RESULTS_TSV,
)
from goal_plus.domain.artifacts import (
    ArtifactRef,
)
from goal_plus.domain.evidence import (
    IterationDisposition,
    IterationRecord,
    ResultLedgerEntry,
    ScoreReport,
)
from goal_plus.domain.search import (
    AgentSessionRecord,
    CandidateTask,
    FrozenSpec,
)
from goal_plus.domain.serialization import canonical_json, sha256_text
from goal_plus.domain.settlement import (
    RollbackReceipt,
    SettlementEvidence,
    SettlementReceipt,
)
from goal_plus.domain.shared_tools import (
    ToolAdoptionRecord,
    ToolizationDecision,
)
from goal_plus.ports.state import (
    CandidateRecord,
    RunRecord,
)
from goal_plus.application.settlement_policy import (
    candidate_attempt_id,
    iteration_hypothesis,
    iteration_disposition,
    update_best_seen,
    previous_best_score,
)
from goal_plus.application.candidate_checkpoints import CandidateCheckpoints
from goal_plus.application.candidate_sessions import CandidateSessions
from goal_plus.application.candidate_artifacts import CandidateArtifacts
from goal_plus.application.candidate_ledger import CandidateLedger
from goal_plus.application.shared_tools import SharedTools
from goal_plus.application.goal_authorization import GoalAuthorization
from goal_plus.ports.candidate_artifacts import CandidateArtifactState
from goal_plus.ports.candidate_settlement import CandidateSettlementState, InnerGitSettlement
from goal_plus.ports.clock import Clock


class CandidateSettlement:
    def __init__(
        self,
        *,
        state: CandidateSettlementState,
        clock: Clock,
        authorization: GoalAuthorization,
        candidate_sessions: CandidateSessions,
        artifacts: CandidateArtifacts,
        candidate_ledger: CandidateLedger,
        shared_tools: SharedTools,
        checkpoints: CandidateCheckpoints,
        persist_run: Callable[[RunRecord], None],
        verifier_submission,

    ) -> None:
        self.state, self.clock, self.authorization = state, clock, authorization
        self.candidate_sessions, self.artifacts, self.candidate_ledger = (
            candidate_sessions,
            artifacts,
            candidate_ledger,
        )
        self.shared_tools = shared_tools
        self.checkpoints, self.persist_run = (
            checkpoints,
            persist_run,
        )
        self.verifier_submission = verifier_submission

    def record(
        self,
        *,
        run_id: str,
        candidate_id: str,
        frozen: FrozenSpec,
        report: ScoreReport,
        attempt: CandidateArtifactState,
        pre_attempt_settled_head: str | None,
        pre_attempt_settled_artifact_ref: ArtifactRef | None,
        attempt_changed_files: list[str],
        hypothesis: str | None,
        agent_session_id: str | None,
        session: AgentSessionRecord | None,
        toolization_decision: ToolizationDecision | None,
        provider_request_ids: list[str] | None = None,
        attempt_id: str | None = None,
        expected_disposition: IterationDisposition | None = None,
        settled_artifact_ref: ArtifactRef | None = None,
        inner_git_settlement: InnerGitSettlement | None = None,
        local_task: CandidateTask | None = None,
        preserve_workspace: bool = False,
    ) -> ScoreReport:
        with self.state.lock_run(run_id):
            run = self.state.read_run(run_id)
            self.authorization.require_search_execution(run.run_id, settlement=True)
            require_worker_iteration_state(run, "record verifier result")
            record = self.state.read_candidate(run_id, candidate_id)
            require_candidate_not_recovering(record)
            if agent_session_id is not None:
                self.candidate_sessions.session(agent_session_id, run_id=run_id)
            workspace_ledger_projection = self.candidate_ledger.projection(
                frozen, record
            )
            if agent_session_id is None and record.pending_tool_copies:
                raise RuntimeError(
                    "parent process verifier cannot settle a candidate with pending tool copies"
                )
            prior_best = best_artifact_iteration_record(
                record,
                frozen.spec.metric_direction,
            )
            prior_score = (
                previous_best_score(
                    record,
                    candidate_best_score=(
                        prior_best.score if prior_best is not None else None
                    ),
                )
                if (frozen.spec.strategy.orchestration_mode == "adaptive_search")
                else (prior_best.score if prior_best is not None else None)
            )
            iteration_number = len(record.iterations) + 1
            exact_attempt_id = attempt_id or candidate_attempt_id(
                run_id=run_id,
                candidate_id=candidate_id,
                iteration=iteration_number,
                artifact_ref=attempt.artifact_ref,
            )
            failure_class = next(
                (
                    result.failure_class
                    for result in report.verifier_results
                    if result.failure_class
                ),
                None,
            )
            if failure_class is None and report.aggregate_score is None:
                failure_class = "MissingNumericMetric"
            hypothesis_text = iteration_hypothesis(
                hypothesis,
                record,
                iteration_number,
                scope="process",
                agent_session_id=agent_session_id,
            )
            created_at = self.clock.timestamp()
            attempt_artifact_ref = attempt.artifact_ref
            if attempt_artifact_ref is None and attempt.git_head is not None:
                attempt_artifact_ref = ArtifactRef(
                    kind="git_commit",
                    id=attempt.git_head,
                    provider=record.task.workspace_provider,
                )
            attempt_base_artifact_ref = pre_attempt_settled_artifact_ref
            if (
                attempt_base_artifact_ref is None
                and pre_attempt_settled_head is not None
            ):
                attempt_base_artifact_ref = ArtifactRef(
                    kind="git_commit",
                    id=pre_attempt_settled_head,
                    provider=record.task.workspace_provider,
                )
            if attempt.git_head is not None:
                settlement_artifact_identity = attempt.git_head
            elif attempt_artifact_ref is not None:
                settlement_artifact_identity = canonical_json(
                    attempt_artifact_ref.model_dump(mode="json")
                )
            else:
                raise RuntimeError("settlement requires an immutable artifact")
            settlement_id = (
                f"settlement_{candidate_id}_{iteration_number:04d}_"
                f"{sha256_text(settlement_artifact_identity)[:12]}"
            )
            pending_tool_copies = (
                list(record.verification_request.tool_copies)
                if local_task is not None and record.verification_request is not None
                else list(record.pending_tool_copies) if agent_session_id is not None else []
            )
            request = record.verification_request if local_task is not None else None
            staging_dir = request.shared_tool_staging_dir if request is not None else None
            for receipt in pending_tool_copies:
                if receipt.candidate_base_git_head != pre_attempt_settled_head:
                    raise RuntimeError(
                        "pending tool copy does not match settled candidate base"
                    )
            adopted_file_budget = sum(
                min(
                    len(
                        self.shared_tools.resolve_shared_tool(
                            run_id,
                            item.tool_id,
                            item.snapshot_hash,
                            require_discoverable=False,
                        ).files
                    ),
                    4,
                )
                for item in pending_tool_copies
            )
            adoption_confounded = bool(
                pending_tool_copies
                and (
                    len(pending_tool_copies) != 1
                    or not attempt_changed_files
                    or len(attempt_changed_files) > adopted_file_budget + 2
                )
            )
            if frozen.spec.shared_dir.enabled:
                # Family admission must see every already committed publication.
                self.shared_tools.reconcile_index(run_id)
            shared_tool_settlement = self.shared_tools.prepare_shared_tool_iteration(
                run_id=run_id,
                candidate_id=candidate_id,
                frozen=frozen,
                report=report,
                source_commit=attempt.git_head,
                iteration_number=iteration_number,
                agent_session_id=agent_session_id,
                toolization_decision=toolization_decision,
                staging_dir=staging_dir,
                capture_error=request.shared_tool_capture_error if request is not None else None,
            )
            iteration = IterationRecord(
                settlement_sequence=1 + sum(
                    len(candidate.iterations) for candidate in self.state.read_candidates(run_id)
                ),
                shared_tool_staging_dir=staging_dir,
                iteration=iteration_number,
                settlement_id=settlement_id,
                agent_session_id=agent_session_id,
                selected_model=(
                    session.selected_model.model
                    if session and session.selected_model
                    else None
                ),
                exact_model_ref=(
                    session.model_provenance.get("exact_model_ref") if session else None
                ),
                adapter_version=(
                    session.model_provenance.get("adapter_version") if session else None
                ),
                model_provenance=(session.model_provenance if session else {}),
                score=report.aggregate_score,
                process_passed=report.process_passed,
                git_head=attempt.git_head,
                attempt_base_git_head=pre_attempt_settled_head,
                artifact_ref=attempt_artifact_ref,
                attempt_base_artifact_ref=attempt_base_artifact_ref,
                provider_request_ids=list(provider_request_ids or []),
                attempt_changed_files=attempt_changed_files,
                artifact_clean=attempt.artifact_clean,
                artifact_status=attempt.artifact_status,
                git_artifact_clean=attempt.git_artifact_clean,
                git_status=attempt.git_status,
                failure_class=failure_class,
                summary=hypothesis_text,
                hypothesis=hypothesis_text,
                changed_files=list(attempt.changed_files),
                touched_denied_files=attempt.touched_denied_files,
                changed_outside_allowed=attempt.changed_outside_allowed,
                artifact_hash=attempt.artifact_hash,
                metrics={
                    **{
                        result.name: result.metrics
                        for result in report.verifier_results
                    },
                    "_candidate_attempt": {
                        "id": exact_attempt_id,
                        "artifact_ref": (
                            attempt_artifact_ref.model_dump(mode="json")
                            if attempt_artifact_ref is not None
                            else None
                        ),
                    },
                    **(
                        {
                            "_thinkthread_settlement": {
                                "key": exact_attempt_id,
                                "snapshot_id": (
                                    attempt_artifact_ref.id
                                    if attempt_artifact_ref is not None
                                    else None
                                ),
                            }
                        }
                        if record.task.workspace_provider == "thinkthread"
                        else {}
                    ),
                },
                log_paths=[
                    str(result.log_path)
                    for result in report.verifier_results
                    if result.log_path is not None
                ],
                shared_tools=shared_tool_settlement.tools,
                shared_tool_errors=shared_tool_settlement.errors,
                shared_tool_staged_entries=shared_tool_settlement.staged_entries,
                shared_tool_staged_file_count=(
                    shared_tool_settlement.staged_file_count
                ),
                shared_tool_staged_bytes=shared_tool_settlement.staged_bytes,
                shared_tool_consumed_entries=(shared_tool_settlement.consumed_entries),
                shared_tool_deduplicated_entries=(
                    shared_tool_settlement.deduplicated_entries
                ),
                shared_tool_publish_status=shared_tool_settlement.publish_status,
                adopted_tools=[
                    ToolAdoptionRecord(
                        tool_id=item.tool_id,
                        snapshot_hash=item.snapshot_hash,
                        receipt_id=item.receipt_id,
                    )
                    for item in pending_tool_copies
                ],
                adoption_confounded=(
                    None if not pending_tool_copies else adoption_confounded
                ),
                toolization_decision=toolization_decision,
                toolization_advisories=(shared_tool_settlement.toolization_advisories),
                created_at=created_at,
            )
            disposition = iteration_disposition(
                iteration,
                prior_score,
                frozen.spec.metric_direction,
            )
            if expected_disposition is not None and disposition != expected_disposition:
                raise RuntimeError(
                    "candidate disposition changed during provider settlement"
                )
            iteration.disposition = disposition

            worker_task = record.task
            if local_task is not None:
                record.task = local_task
            is_thinkthread = record.task.workspace_provider == "thinkthread"
            rollback_target_artifact_ref: ArtifactRef | None = None
            restored_artifact_ref: ArtifactRef | None = None
            if disposition in {"keep", "retain"}:
                best_iteration = iteration
            else:
                rollback_target_artifact_ref = (
                    prior_best.artifact_ref
                    if prior_best is not None
                    else attempt_base_artifact_ref
                )
                if (
                    rollback_target_artifact_ref is None
                    and prior_best is not None
                    and prior_best.git_head is not None
                ):
                    rollback_target_artifact_ref = ArtifactRef(
                        kind="git_commit",
                        id=prior_best.git_head,
                        provider=record.task.workspace_provider,
                    )
                if rollback_target_artifact_ref is None:
                    raise RuntimeError("candidate rollback has no restoration target")
                iteration.restored_to_iteration = (
                    prior_best.iteration if prior_best is not None else None
                )
                iteration.restored_to_git_head = (
                    rollback_target_artifact_ref.id
                    if rollback_target_artifact_ref.kind == "git_commit"
                    else None
                )
                restored_artifact_ref = (
                    (settled_artifact_ref or rollback_target_artifact_ref)
                    if is_thinkthread
                    else self.artifacts.restore_candidate_artifact(
                        record,
                        frozen.spec.metric_name,
                        workspace_ledger_projection,
                        rollback_target_artifact_ref,
                        (
                            f"goal-plus restore {candidate_id} best after "
                            f"iteration {iteration_number}"
                        ),
                    )
                )
                iteration.restored_to_artifact_ref = restored_artifact_ref
                best_iteration = prior_best

            if is_thinkthread:
                exact_settled_artifact_ref = settled_artifact_ref or (
                    iteration.artifact_ref
                    if disposition in {"keep", "retain"}
                    else iteration.restored_to_artifact_ref
                )
                if exact_settled_artifact_ref is None:
                    raise RuntimeError(
                        "ThinkThread settlement has no branch restoration target"
                    )
                settled_best = (
                    iteration if disposition in {"keep", "retain"} else prior_best
                )
                settled = CandidateArtifactState(
                    changed_files=(
                        list(settled_best.changed_files)
                        if settled_best is not None
                        else []
                    ),
                    touched_denied_files=False,
                    changed_outside_allowed=False,
                    artifact_hash=(
                        str(settled_best.artifact_hash)
                        if settled_best is not None
                        and settled_best.artifact_hash is not None
                        else sha256_text("[]")
                    ),
                    artifact_ref=exact_settled_artifact_ref,
                    artifact_status=[],
                    artifact_clean=True,
                    git_head=None,
                    git_status=[],
                    git_artifact_clean=False,
                )
            else:
                settled = self.artifacts.candidate_artifact_state(run, frozen, record)
            if not settled.artifact_clean:
                raise RuntimeError("candidate artifact is dirty after settlement")
            if (
                best_iteration is not None
                and settled.artifact_hash != best_iteration.artifact_hash
            ):
                raise RuntimeError(
                    "candidate artifact does not match its best iteration"
                )
            if (
                best_iteration is None
                and record.task.expansion_source is not None
                and settled.artifact_hash
                != record.task.expansion_source.attempt_artifact_hash
            ):
                raise RuntimeError(
                    "derived candidate artifact does not match its initial incumbent"
                )
            self.artifacts.apply_candidate_artifact_state(record, settled)

            ledger_entry = ResultLedgerEntry(
                source_run_id=run_id,
                source_candidate_id=candidate_id,
                iteration=iteration_number,
                git_head=attempt.git_head,
                artifact_ref=attempt_artifact_ref,
                metric_name=frozen.spec.metric_name,
                score=report.aggregate_score,
                status="pass" if report.process_passed else "fail",
                hypothesis=hypothesis_text,
                failure_class=failure_class,
                created_at=created_at,
            )
            if (
                workspace_ledger_projection == WORKSPACE_LEDGER_PROJECTION_NONE
                or inner_git_settlement is not None
            ):
                ledger_entry.ledger_artifact_ref = settled.artifact_ref
            ledger_git_head = self.candidate_ledger.append(
                record,
                ledger_entry,
                frozen.spec.metric_name,
                workspace_ledger_projection,
            )
            iteration.ledger_git_head = ledger_git_head
            ledger_artifact_ref = ledger_entry.ledger_artifact_ref
            if ledger_artifact_ref is not None:
                iteration.ledger_artifact_ref = ledger_artifact_ref
                iteration.workspace_artifact_after_settlement = ledger_artifact_ref
                iteration.workspace_git_head_after_settlement = (
                    ledger_artifact_ref.id
                    if ledger_artifact_ref.kind == "git_commit"
                    else None
                )
                record.results_ledger_artifact_ref = ledger_artifact_ref
            if (
                attempt_artifact_ref is None
                or settled.artifact_ref is None
                or ledger_artifact_ref is None
            ):
                raise RuntimeError("settlement receipt requires exact artifact references")
            rollback = None
            if rollback_target_artifact_ref is not None:
                if restored_artifact_ref is None:
                    raise RuntimeError("rollback settlement has no restoration receipt")
                rollback = RollbackReceipt(
                    target_artifact_ref=rollback_target_artifact_ref,
                    restored_artifact_ref=restored_artifact_ref,
                    restored_to_iteration=iteration.restored_to_iteration,
                    restored_artifact_hash=settled.artifact_hash,
                )
            receipt = SettlementReceipt(
                settlement_id=settlement_id,
                run_id=run_id,
                candidate_id=candidate_id,
                iteration=iteration_number,
                attempt_base_artifact_ref=attempt_base_artifact_ref,
                attempt_artifact_ref=attempt_artifact_ref,
                attempt_artifact_hash=attempt.artifact_hash,
                evidence=SettlementEvidence(
                    metric_name=frozen.spec.metric_name,
                    hard_score=report.aggregate_score,
                    process_passed=report.process_passed,
                    changed_files=list(attempt.changed_files),
                    attempt_changed_files=list(attempt_changed_files),
                    touched_denied_files=attempt.touched_denied_files,
                    changed_outside_allowed=attempt.changed_outside_allowed,
                    artifact_clean=attempt.artifact_clean,
                    artifact_status=list(attempt.artifact_status),
                    failure_class=failure_class,
                    metrics=dict(iteration.metrics),
                    log_paths=list(iteration.log_paths),
                ),
                disposition=disposition,
                rollback=rollback,
                post_settlement_artifact_ref=settled.artifact_ref,
                ledger_artifact_ref=ledger_artifact_ref,
                created_at=created_at,
            )
            iteration = IterationRecord.model_validate(
                {
                    **iteration.model_dump(mode="python"),
                    "settlement_receipt": receipt,
                }
            )
            if inner_git_settlement is not None:
                prior_state = dict(record.inner_agent_state)
                attempt_heads = dict(prior_state.get("attempt_git_heads") or {})
                attempt_heads[str(iteration_number)] = str(
                    inner_git_settlement.attempt_git_head
                )
                record.inner_agent_state = {
                    "protocol": "goal-plus.inner-git.v1",
                    "adapter": "autoresearch-git",
                    "baseline_git_head": inner_git_settlement.baseline_git_head,
                    "ledger_git_head": inner_git_settlement.ledger_git_head,
                    "result_count": inner_git_settlement.result_count,
                    "attempt_git_heads": attempt_heads,
                }
            record.iterations.append(iteration)
            report = report.model_copy(
                update={
                    "disposition": disposition,
                    "failure_class": failure_class,
                    "attempt_id": exact_attempt_id,
                    "attempt_iteration": iteration_number,
                    "attempt_artifact_ref": iteration.artifact_ref,
                    "best_iteration": (
                        best_iteration.iteration if best_iteration is not None else None
                    ),
                    "best_git_head": (
                        best_iteration.git_head if best_iteration is not None else None
                    ),
                    "workspace_git_head_after_settlement": (
                        iteration.workspace_git_head_after_settlement
                    ),
                    "best_artifact_ref": (
                        best_iteration.artifact_ref
                        if best_iteration is not None
                        else None
                    ),
                    "workspace_artifact_after_settlement": (
                        iteration.workspace_artifact_after_settlement
                    ),
                    "shared_tool_staged_entries": iteration.shared_tool_staged_entries,
                    "shared_tool_staged_file_count": iteration.shared_tool_staged_file_count,
                    "shared_tool_staged_bytes": iteration.shared_tool_staged_bytes,
                    "shared_tool_publish_status": iteration.shared_tool_publish_status,
                    "shared_tool_errors": iteration.shared_tool_errors,
                    "shared_tool_consumed_entries": iteration.shared_tool_consumed_entries,
                    "shared_tool_deduplicated_entries": iteration.shared_tool_deduplicated_entries,
                    "toolization_decision": iteration.toolization_decision,
                    "toolization_advisories": iteration.toolization_advisories,
                }
            )
            record.status = "evaluated"
            if local_task is not None:
                record.task = worker_task
                report = report.model_copy(update={"verification": record.verification})
            record.score_report = report
            if record.promotion_evidence and (
                record.promotion_evidence.artifact_ref != report.best_artifact_ref
                or record.promotion_evidence.artifact_hash != settled.artifact_hash
            ):
                record.promotion_report = None
                record.promotion_evidence = None
            submission = self.verifier_submission.prepare(frozen, record, record.iterations[-1])
            # Commit the complete settlement and immutable outbound input together.
            self.state.write_candidate(run_id, record)
            if frozen.spec.shared_dir.enabled:
                self.shared_tools.reconcile(run_id, record)
            records = self.state.read_candidates(run_id)
            best_records = []
            for candidate in records:
                best = best_artifact_iteration_record(
                    candidate, frozen.spec.metric_direction
                )
                if best is not None and candidate.score_report is not None:
                    best_records.append((candidate, best))
            winner = None
            for candidate, best in sorted(
                best_records, key=lambda item: item[1].settlement_sequence
            ):
                candidate_report = candidate.score_report.model_copy(
                    update={
                        "aggregate_score": best.score,
                        "disposition": best.disposition,
                    }
                )
                if update_best_seen(run, frozen.spec, candidate_report):
                    winner = candidate, best
            if winner is not None:
                self.checkpoints.write_best(run, frozen.spec, *winner)
            run.candidates_evaluated = sum(
                candidate.status == "evaluated" for candidate in records
            )
            self.persist_run(run)
            for candidate_session in self.candidate_sessions.state.read_agent_sessions(
                run_id
            ):
                if candidate_session.candidate_id != record.candidate_id:
                    continue
                count = sum(
                    item.agent_session_id == candidate_session.agent_session_id
                    for item in record.iterations
                )
                if candidate_session.counters.get("verifier_runs") != count:
                    candidate_session.counters["verifier_runs"] = count
                    candidate_session.updated_at = self.clock.timestamp()
                    self.candidate_sessions.state.write_agent_session(candidate_session)
            if record.verification is not None and record.score_report is not None:
                synchronized = self.artifacts.workspace.publish_settlement(
                    record.task,
                    record.score_report.attempt_artifact_ref,
                    record.results_ledger_artifact_ref,
                    preserve_workspace=preserve_workspace,
                    ledger_projection=(
                        self.candidate_ledger.projection(frozen, record)
                        == WORKSPACE_LEDGER_PROJECTION_RESULTS_TSV
                    ),
                )
                if synchronized:
                    record.verification = None
                else:
                    record.verification.state = "needs_recovery"
                    record.verification.error = (
                        "WorkerWorkspaceChanged: submitted artifact was settled separately"
                    )
                record.score_report.verification = record.verification
            self.state.write_candidate(run_id, record)
        self.verifier_submission.submit(submission)
        return record.score_report
