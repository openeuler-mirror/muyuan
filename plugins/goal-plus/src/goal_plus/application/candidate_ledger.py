"""Candidate ledger state and projections with explicit workspace I/O ports."""

from pathlib import Path
from goal_plus.application.candidate_ranking import records_by_created, top_records
from goal_plus.domain.results_ledger import RESULTS_TSV_RELATIVE_PATH
from goal_plus.domain.search import CandidateTask, FrozenSpec
from goal_plus.domain.agents import (
    WORKSPACE_LEDGER_PROJECTION_NONE,
    WORKSPACE_LEDGER_PROJECTION_RESULTS_TSV,
)
from goal_plus.ports.candidate_creation import CandidateCreationState
from goal_plus.ports.candidate_ledger import CandidateLedgerWorkspace
from goal_plus.ports.state import RunRecord
from goal_plus.domain.evidence import ResultLedgerEntry
from goal_plus.domain.results_ledger import (
    normalize_result_text,
    render_results_tsv,
    valid_inner_git_head,
)
from goal_plus.ports.state import CandidateRecord


def backfill_results_ledger_from_iterations(
    record: CandidateRecord,
    metric_name: str,
) -> None:
    represented = {
        entry.iteration
        for entry in record.results_ledger
        if entry.source_run_id == record.task.run_id
        and entry.source_candidate_id == record.candidate_id
        and entry.iteration is not None
    }
    for iteration in record.iterations:
        if iteration.iteration in represented:
            continue
        hypothesis = normalize_result_text(iteration.hypothesis or iteration.summary)
        record.results_ledger.append(
            ResultLedgerEntry(
                source_run_id=record.task.run_id,
                source_candidate_id=record.candidate_id,
                frozen_spec_id=iteration.frozen_spec_id,
                spec_hash=iteration.spec_hash,
                iteration=iteration.iteration,
                git_head=iteration.git_head,
                ledger_git_head=iteration.ledger_git_head,
                artifact_ref=iteration.artifact_ref,
                ledger_artifact_ref=iteration.ledger_artifact_ref,
                metric_name=metric_name,
                score=iteration.score,
                status="pass" if iteration.process_passed else "fail",
                hypothesis=hypothesis or f"recovered iteration {iteration.iteration}",
                failure_class=iteration.failure_class,
                created_at=iteration.created_at,
            )
        )


def render_thinkthread_inner_git_results_tsv(
    record: CandidateRecord,
    metric_name: str,
) -> str:
    attempt_heads = record.inner_agent_state.get("attempt_git_heads")
    private_heads = attempt_heads if isinstance(attempt_heads, dict) else {}
    entries = [entry.model_copy(deep=True) for entry in record.results_ledger]
    for entry in entries:
        if entry.git_head is not None or entry.iteration is None:
            continue
        private_head = private_heads.get(str(entry.iteration))
        if valid_inner_git_head(private_head):
            entry.git_head = str(private_head)
    return render_results_tsv(entries, ledger_metric_name(record, metric_name),
                              candidate_identity=(record.task.run_id, record.candidate_id) if record.spec_revisions else None)


def ledger_metric_name(record: CandidateRecord, metric_name: str) -> str:
    """The legacy TSV header stays fixed; each persisted row retains its real metric."""
    return record.results_ledger_metric_name or metric_name


class CandidateLedger:
    def __init__(
        self, state: CandidateCreationState, workspace: CandidateLedgerWorkspace
    ) -> None:
        self.state, self.workspace = state, workspace

    def preferred_source(
        self,
        source_run_id: str,
    ) -> CandidateRecord | None:
        source_run = self.state.read_run(source_run_id)
        records = self.state.read_candidates(source_run_id)
        by_id = {record.candidate_id: record for record in records}
        source_frozen = self.state.read_frozen_spec(source_run.frozen_spec_id)
        for candidate_id in (
            source_run.selected_candidate_id,
            source_run.best_candidate_id,
        ):
            if candidate_id and candidate_id in by_id:
                record = by_id[candidate_id]
                backfill_results_ledger_from_iterations(
                    record,
                    source_frozen.spec.metric_name,
                )
                return record
        if not records:
            return None
        spec_ids = {record.active_frozen_spec_id or source_run.frozen_spec_id for record in records}
        if len(spec_ids) > 1:
            frontier = records_by_created(records)[:1]
        else:
            ranking_frozen = self.state.read_frozen_spec(next(iter(spec_ids)))
            frontier = top_records(records, ranking_frozen.spec, 1)
        if not frontier:
            return None
        record = frontier[0]
        backfill_results_ledger_from_iterations(
            record,
            source_frozen.spec.metric_name,
        )
        return record

    def inherited(
        self,
        run: RunRecord,
        task: CandidateTask,
    ) -> list[ResultLedgerEntry]:
        source_record: CandidateRecord | None = None
        if task.base_candidate_id:
            source_record = self.state.read_candidate(
                run.run_id,
                task.base_candidate_id,
            )
        elif run.source_run_id:
            source_record = self.preferred_source(run.source_run_id)
        if source_record is None:
            return []
        frozen = self.state.read_frozen_spec(run.frozen_spec_id)
        backfill_results_ledger_from_iterations(
            source_record,
            frozen.spec.metric_name,
        )
        return self.workspace.read(source_record)

    def ensure_results(
        self,
        record: CandidateRecord,
        metric_name: str,
    ) -> Path:
        if record.results_ledger_metric_name is None:
            record.results_ledger_metric_name = metric_name
        metric_name = ledger_metric_name(record, metric_name)
        if (
            record.results_ledger_artifact_ref is not None
            or record.results_ledger_git_head is not None
        ):
            return self.workspace.check(record, metric_name)
        if not record.results_ledger:
            record.results_ledger = self.workspace.read(record)
        if record.results_ledger_artifact_ref is None:
            backfill_results_ledger_from_iterations(record, metric_name)
        path = record.task.workspace / RESULTS_TSV_RELATIVE_PATH
        ledger_artifact_ref = self.workspace.initialize(
            record,
            render_results_tsv(record.results_ledger, metric_name,
                               candidate_identity=(record.task.run_id, record.candidate_id) if record.spec_revisions else None),
            f"goal-plus results ledger sync {record.candidate_id}",
        )
        ledger_git_head = (
            ledger_artifact_ref.id if ledger_artifact_ref.kind == "git_commit" else None
        )
        record.results_ledger_git_head = ledger_git_head
        record.results_ledger_artifact_ref = ledger_artifact_ref
        self.workspace.check(record, metric_name)
        return path

    def projection(
        self,
        frozen: FrozenSpec,
        candidate_record: CandidateRecord,
    ) -> str:
        worker_policy = candidate_record.task.strategy_metadata.get("worker_policy", {})
        persisted_binding = worker_policy.get("inner_agent_binding")
        if isinstance(persisted_binding, dict) and (
            "workspace_ledger_projection" in persisted_binding
        ):
            projection = persisted_binding["workspace_ledger_projection"]
            if projection not in {
                WORKSPACE_LEDGER_PROJECTION_NONE,
                WORKSPACE_LEDGER_PROJECTION_RESULTS_TSV,
            }:
                raise RuntimeError(
                    "candidate inner-agent binding has an unsupported workspace "
                    f"ledger projection: {projection!r}"
                )
            return str(projection)

        # Runs created before the capability was persisted used results.tsv for
        # every Git candidate. Keep those already-materialized projections
        # readable and append-compatible instead of changing their protocol on
        # resume.
        if self.workspace.has_legacy_projection(candidate_record):
            return WORKSPACE_LEDGER_PROJECTION_RESULTS_TSV
        return self.workspace.bound_projection(frozen, candidate_record)

    def ensure(
        self,
        frozen: FrozenSpec,
        record: CandidateRecord,
    ) -> Path | None:
        projection = self.projection(frozen, record)
        if projection == WORKSPACE_LEDGER_PROJECTION_NONE:
            if record.results_ledger_artifact_ref is None:
                record.results_ledger_artifact_ref = record.task.base_artifact_ref
            record.results_ledger_git_head = None
            return None
        if projection == WORKSPACE_LEDGER_PROJECTION_RESULTS_TSV:
            if record.task.workspace_provider == "thinkthread":
                backfill_results_ledger_from_iterations(
                    record,
                    frozen.spec.metric_name,
                )
                if record.results_ledger_artifact_ref is None:
                    record.results_ledger_artifact_ref = record.task.base_artifact_ref
                record.results_ledger_git_head = None
                return Path(RESULTS_TSV_RELATIVE_PATH)
            return self.ensure_results(record, frozen.spec.metric_name)
        raise RuntimeError(f"unsupported workspace ledger projection: {projection!r}")

    def append(
        self,
        record: CandidateRecord,
        entry: ResultLedgerEntry,
        metric_name: str,
        workspace_ledger_projection: str,
    ) -> str | None:
        if workspace_ledger_projection == WORKSPACE_LEDGER_PROJECTION_NONE:
            settled_ref = entry.ledger_artifact_ref or entry.artifact_ref
            entry.ledger_artifact_ref = settled_ref
            record.results_ledger.append(entry)
            record.results_ledger_artifact_ref = settled_ref
            record.results_ledger_git_head = None
            return None
        if (
            workspace_ledger_projection
            == WORKSPACE_LEDGER_PROJECTION_RESULTS_TSV
        ):
            if record.task.workspace_provider == "thinkthread":
                settled_ref = entry.ledger_artifact_ref
                if (
                    settled_ref is None
                    or settled_ref.kind != "fs_snapshot"
                    or settled_ref.provider != "thinkthread"
                ):
                    raise RuntimeError(
                        "ThinkThread AutoResearch-Git ledger append requires the "
                        "post-settlement snapshot"
                    )
                record.results_ledger.append(entry)
                record.results_ledger_artifact_ref = settled_ref
                record.results_ledger_git_head = None
                return None
            return self._append_results_tsv(record, entry, metric_name)
        raise RuntimeError(
            "unsupported workspace ledger projection: "
            f"{workspace_ledger_projection!r}"
        )


    def _append_results_tsv(
        self,
        record: CandidateRecord,
        entry: ResultLedgerEntry,
        metric_name: str,
    ) -> str | None:
        metric_name = ledger_metric_name(record, metric_name)
        self.workspace.check(record, metric_name)
        before = render_results_tsv(record.results_ledger, metric_name,
                                    candidate_identity=(record.task.run_id, record.candidate_id) if record.spec_revisions else None)
        after = render_results_tsv(
            [*record.results_ledger, entry],
            metric_name,
            candidate_identity=(record.task.run_id, record.candidate_id) if record.spec_revisions else None,
        )
        if not after.startswith(before) or len(after.splitlines()) != len(
            before.splitlines()
        ) + 1:
            raise RuntimeError(
                "ResultsLedgerAppendError: a verifier result must append exactly "
                "one row without changing the existing workspace/results.tsv"
            )
        self.workspace.write(record, after)
        ledger_artifact_ref = self.workspace.commit(
            record,
            (
                f"goal-plus results ledger {record.candidate_id}:"
                f"{entry.iteration or 'legacy'}"
            ),
        )
        if ledger_artifact_ref is None:
            raise RuntimeError(
                "ResultsLedgerCommitError: failed to commit the appended "
                "workspace/results.tsv row"
            )
        ledger_git_head = (
            ledger_artifact_ref.id
            if ledger_artifact_ref.kind == "git_commit"
            else None
        )
        entry.ledger_git_head = ledger_git_head
        entry.ledger_artifact_ref = ledger_artifact_ref
        record.results_ledger.append(entry)
        record.results_ledger_git_head = ledger_git_head
        record.results_ledger_artifact_ref = ledger_artifact_ref
        self.workspace.check(record, metric_name)
        return ledger_git_head
