"""Original Decision layout; Application owns validation and state transitions."""

from contextlib import AbstractContextManager
from pathlib import Path

from goal_plus.infrastructure.state.candidate_queries import FileCandidateQueries

from goal_plus.domain.adaptive.models import (
    AdaptiveReconciliationRecord,
    AllocationDecision,
    SearchSnapshot,
    adaptive_reconciliation_identity,
)
from goal_plus.domain.evidence import ResultLedgerEntry
from goal_plus.domain.search import SearchPlan
from goal_plus.infrastructure.state.files import (
    exclusive_file_lock,
    load_json,
    write_json,
)
from goal_plus.infrastructure.state.results_ledger import read_candidate_results_ledger, read_results_tsv
from goal_plus.ports.allocation_apply import AllocationMaterializationProjection
from goal_plus.ports.state import CandidateRecord


def allocation_decision_dir(run_dir: Path | str) -> Path:
    return Path(run_dir) / "allocation-decisions"


def allocation_decision_path(run_dir: Path | str, decision_id: str) -> Path:
    return allocation_decision_dir(run_dir) / f"{decision_id}.json"


def allocation_decision_paths(run_dir: Path | str) -> list[Path]:
    return sorted(allocation_decision_dir(run_dir).glob("allocation_*.json"))


def search_snapshot_path(run_dir: Path | str, snapshot_revision: str) -> Path:
    return Path(run_dir) / "adaptive-snapshots" / f"{snapshot_revision}.json"


def adaptive_reconciliation_path(
    run_dir: Path | str, verifier_record_id: str
) -> Path:
    return (
        Path(run_dir)
        / "adaptive-reconciliations"
        / f"{adaptive_reconciliation_identity(verifier_record_id)}.json"
    )


def load_allocation_decision(
    run_dir: Path | str,
    decision_id: str,
) -> AllocationDecision:
    path = allocation_decision_path(run_dir, decision_id)
    if not path.exists():
        raise FileNotFoundError(f"allocation decision not found: {decision_id}")
    return AllocationDecision.model_validate(load_json(path))


def load_allocation_decisions(run_dir: Path | str) -> list[AllocationDecision]:
    return [
        AllocationDecision.model_validate(load_json(path))
        for path in allocation_decision_paths(run_dir)
    ]


def load_search_snapshot(
    run_dir: Path | str, snapshot_revision: str
) -> SearchSnapshot:
    path = search_snapshot_path(run_dir, snapshot_revision)
    if not path.exists():
        raise FileNotFoundError(
            f"adaptive search snapshot not found: {snapshot_revision}"
        )
    return SearchSnapshot.model_validate(load_json(path))


class FileAllocationState(FileCandidateQueries):
    def lock_adaptive_reconciliation(
        self, run_id: str
    ) -> AbstractContextManager[None]:
        return exclusive_file_lock(
            self.run_dir(run_id) / "adaptive-reconciliations" / "reconcile.lock"
        )

    def load_adaptive_reconciliation(
        self, run_id: str, verifier_record_id: str
    ) -> AdaptiveReconciliationRecord | None:
        path = adaptive_reconciliation_path(
            self.run_dir(run_id), verifier_record_id
        )
        if not path.exists():
            return None
        return AdaptiveReconciliationRecord.model_validate(load_json(path))

    def write_adaptive_reconciliation(
        self, record: AdaptiveReconciliationRecord
    ) -> None:
        path = adaptive_reconciliation_path(
            self.run_dir(record.run_id), record.verifier_record_id
        )
        if path.exists():
            existing = AdaptiveReconciliationRecord.model_validate(load_json(path))
            immutable_fields = (
                "reconciliation_id",
                "run_id",
                "task_id",
                "verifier_record_id",
                "snapshot_revision",
                "plan_hash",
            )
            if any(
                getattr(existing, field) != getattr(record, field)
                for field in immutable_fields
            ):
                raise RuntimeError("adaptive reconciliation identity collision")
            if existing.state == "completed" and existing != record:
                raise RuntimeError("completed adaptive reconciliation is immutable")
            if existing.state == "completed":
                return
        elif record.state != "prepared":
            raise RuntimeError(
                "adaptive reconciliation must be prepared before completion"
            )
        write_json(path, record.model_dump(mode="json"))

    def load_allocation_decision(
        self,
        run_id: str,
        decision_id: str,
    ) -> AllocationDecision:
        try:
            return load_allocation_decision(self.run_dir(run_id), decision_id)
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                f"allocation decision not found: {decision_id} in run {run_id}"
            ) from exc

    def load_allocation_decisions(self, run_id: str) -> list[AllocationDecision]:
        return load_allocation_decisions(self.run_dir(run_id))

    def write_allocation_decision(self, decision: AllocationDecision) -> None:
        write_json(
            allocation_decision_path(
                self.run_dir(decision.run_id),
                decision.decision_id,
            ),
            decision.model_dump(mode="json"),
        )

    def write_search_snapshot(self, snapshot: SearchSnapshot) -> None:
        path = search_snapshot_path(
            self.run_dir(snapshot.run_id), snapshot.snapshot_revision
        )
        if path.exists():
            existing = SearchSnapshot.model_validate(load_json(path))
            if existing != snapshot:
                raise RuntimeError("immutable SearchSnapshot identity collision")
            return
        write_json(path, snapshot.model_dump(mode="json"))

    def load_search_snapshot(
        self, run_id: str, snapshot_revision: str
    ) -> SearchSnapshot:
        return load_search_snapshot(self.run_dir(run_id), snapshot_revision)

    def lock_run(self, run_id: str) -> AbstractContextManager[None]:
        return exclusive_file_lock(self.run_dir(run_id) / "run.lock")

    def read_plan(self, run_id: str, plan_id: str) -> SearchPlan:
        return SearchPlan.model_validate(
            load_json(self.run_dir(run_id) / "plans" / f"{plan_id}.json")
        )

    def find_candidate(self, run_id: str, candidate_id: str) -> CandidateRecord | None:
        if not (self.candidate_dir(run_id, candidate_id) / "candidate.json").exists():
            return None
        return self.read_candidate(run_id, candidate_id)

    def write_retirement(
        self, run_id: str, candidate_id: str, decision_id: str
    ) -> None:
        record = self.read_candidate(run_id, candidate_id)
        if (
            not record.results_ledger
            and record.results_ledger_artifact_ref is None
            and record.results_ledger_git_head is None
        ):
            record.results_ledger = read_results_tsv(record)
        record.allocation_eligibility = "retired"
        record.retired_by_decision_id = decision_id
        directory = self.candidate_dir(run_id, record.candidate_id)
        write_json(directory / "candidate.json", record.model_dump(mode="json"))
        write_json(directory / "task.json", record.task.model_dump(mode="json"))

    def write_materialization_projection(
        self, run_id: str, projection: AllocationMaterializationProjection
    ) -> None:
        run = self.read_run(run_id)
        run.candidates_total = projection.candidates_total
        run.source_artifact_ref = projection.source_artifact_ref
        run.source_subdir = projection.source_subdir
        run.source_repository = projection.source_repository
        write_json(self.run_dir(run_id) / "run.json", run.model_dump(mode="json"))

    def read_source_ledger(self, source: CandidateRecord) -> list[ResultLedgerEntry]:
        return read_candidate_results_ledger(source)

    def write_candidate_index(self, run_id: str, next_candidate_index: int) -> None:
        run = self.read_run(run_id)
        run.next_candidate_index = next_candidate_index
        write_json(self.run_dir(run_id) / "run.json", run.model_dump(mode="json"))
