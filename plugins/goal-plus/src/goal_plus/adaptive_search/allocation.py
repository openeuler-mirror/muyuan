"""AllocationDecision contracts, persistence, validation, and execution."""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import Field, model_validator

from goal_plus.models import (
    AgentSessionRecord,
    CandidateProposal,
    FrozenSpec,
    RunRecord,
    SearchModel,
    WorkerBudget,
)
from goal_plus.runtime import load_json, utc_timestamp, write_json



AllocationEligibility = Literal["eligible", "retired"]

AllocationDecisionStatus = Literal["pending", "applied"]

class ExpansionSource(SearchModel):
    """Exact settled node selected by the scheduler as a new branch source."""

    candidate_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    node_id: str = Field(min_length=1)
    settled_commit: str = Field(min_length=1)
    score: float | None = Field(default=None, allow_inf_nan=False)
    artifact_hash: str = Field(min_length=1)
    allocation_depth: int = Field(default=0, ge=0)

class AllocationReplacementProposal(SearchModel):
    """Allocation portion emitted by one sessionless scheduler call."""

    retire_candidate_id: str = Field(min_length=1)
    new_candidate_id: str = Field(min_length=1)
    source_candidate_id: str = Field(min_length=1)
    source_iteration: int = Field(ge=1)
    source_node_id: str = Field(min_length=1)
    rationale: str = Field(min_length=1, max_length=2000)
    exploration_direction: str = Field(min_length=1, max_length=2000)
    worker_budget: WorkerBudget | None = None

class AllocationProposal(SearchModel):
    replacements: list[AllocationReplacementProposal] = Field(min_length=1)
    rationale: str = Field(min_length=1, max_length=3000)

    @model_validator(mode="after")
    def replacement_ids_are_unique(self) -> "AllocationProposal":
        retired = [item.retire_candidate_id for item in self.replacements]
        children = [item.new_candidate_id for item in self.replacements]
        if len(retired) != len(set(retired)):
            raise ValueError("scheduler decision cannot retire a candidate twice")
        if len(children) != len(set(children)):
            raise ValueError("scheduler decision candidate ids must be unique")
        return self

class AllocationReplacement(SearchModel):
    retire_candidate_id: str = Field(min_length=1)
    new_candidate_id: str = Field(min_length=1)
    source: ExpansionSource
    rationale: str = Field(min_length=1, max_length=2000)
    exploration_direction: str = Field(min_length=1, max_length=2000)
    worker_budget: WorkerBudget | None = None

class AllocationDecision(SearchModel):
    """Durable execution plan accepted from a confirmed scheduler flag."""

    schema_version: Literal[3] = 3
    decision_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    schedule_task_id: str = Field(min_length=1)
    trigger_candidate_id: str = Field(min_length=1)
    trigger_iteration: int = Field(ge=1)
    trigger_commit: str = Field(min_length=1)
    rationale: str = Field(min_length=1, max_length=3000)
    replacements: list[AllocationReplacement] = Field(min_length=1)
    status: AllocationDecisionStatus = "pending"
    created_at: str
    applied_at: str | None = None

    @model_validator(mode="after")
    def replacement_ids_are_unique(self) -> "AllocationDecision":
        retired = [item.retire_candidate_id for item in self.replacements]
        children = [item.new_candidate_id for item in self.replacements]
        if len(retired) != len(set(retired)):
            raise ValueError("allocation decision cannot retire a candidate twice")
        if len(children) != len(set(children)):
            raise ValueError("allocation decision candidate ids must be unique")
        return self


class AllocationTrigger(SearchModel):
    """Scheduler result identity required to validate and persist a Decision."""

    schedule_task_id: str = Field(min_length=1)
    settlement_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    commit: str = Field(min_length=1)
    reserved_candidate_ids: list[str] = Field(default_factory=list)


class RewardedSource(SearchModel):
    """Settled node identity proven to have a completed scheduler reward."""

    schedule_task_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    node_id: str = Field(min_length=1)
    settlement_id: str = Field(min_length=1)

class _AllocationRuntimePort(Protocol):
    root_dir: Path

    def _run_transaction(self, run_id: str) -> AbstractContextManager[None]: ...
    def _assert_worker_iteration_allowed(self, run: Any, action: str) -> None: ...
    def _load_run(self, run_id: str) -> Any: ...
    def _write_run(self, run: Any) -> None: ...
    def _load_frozen_spec(self, frozen_spec_id: str) -> Any: ...
    def _load_plan(self, run_id: str, plan_id: str) -> Any: ...
    def _load_candidate_record(self, run_id: str, candidate_id: str) -> Any: ...
    def _load_candidate_records(self, run_id: str) -> list[Any]: ...
    def _write_candidate_record(self, run_id: str, record: Any) -> None: ...
    def _materialize_candidate_record(self, *args: Any, **kwargs: Any) -> Any: ...
    def _source_ledger_at_revision(self, record: Any, revision: str) -> Any: ...
    def _load_agent_sessions(self, run_id: str) -> list[Any]: ...
    def start_agent_session(self, *args: Any, **kwargs: Any) -> Any: ...

def candidate_state_dir(run_dir: Path | str, candidate_id: str) -> Path:
    return Path(run_dir) / "candidates" / candidate_id

def allocation_decision_dir(run_dir: Path | str) -> Path:
    return Path(run_dir) / "allocation-decisions"

def allocation_decision_path(run_dir: Path | str, decision_id: str) -> Path:
    return allocation_decision_dir(run_dir) / f"{decision_id}.json"

def allocation_decision_paths(run_dir: Path | str) -> list[Path]:
    return sorted(allocation_decision_dir(run_dir).glob("allocation_*.json"))

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

class _AllocationStore:
    def __init__(self, root_dir: Path | str) -> None:
        self.root_dir = Path(root_dir).resolve()

    def run_dir(self, run_id: str) -> Path:
        return self.root_dir / "runs" / run_id

    def candidate_dir(self, run_id: str, candidate_id: str) -> Path:
        return candidate_state_dir(self.run_dir(run_id), candidate_id)

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

class AllocationExecutor:
    def __init__(
        self,
        runtime: _AllocationRuntimePort,
    ) -> None:
        self.runtime = runtime
        self.store = _AllocationStore(runtime.root_dir)

    @staticmethod
    def _advance_decision_index(
        run: RunRecord,
        decisions: list[AllocationDecision],
    ) -> None:
        indices: list[int] = []
        for decision in decisions:
            prefix, separator, suffix = decision.decision_id.rpartition("_")
            if separator and prefix == "allocation" and suffix.isdigit():
                indices.append(int(suffix))
        if indices:
            run.next_allocation_decision_index = max(
                run.next_allocation_decision_index,
                max(indices) + 1,
            )

    def _reconcile_decision_locked(
        self,
        run: RunRecord,
        decision: AllocationDecision,
    ) -> None:
        """Idempotently project one durable Decision onto mutable runtime records."""

        records = self.runtime._load_candidate_records(run.run_id)
        by_id = {item.candidate_id: item for item in records}
        trigger_record = by_id.get(decision.trigger_candidate_id)
        trigger_iteration = next(
            (
                item
                for item in trigger_record.iterations
                if item.iteration == decision.trigger_iteration
            ),
            None,
        ) if trigger_record is not None else None
        if (
            trigger_iteration is None
            or trigger_iteration.git_head != decision.trigger_commit
        ):
            raise ValueError("allocation Decision trigger no longer matches settlement")

        changed_ids = {decision.trigger_candidate_id}
        for replacement in decision.replacements:
            retired = by_id.get(replacement.retire_candidate_id)
            if retired is None:
                raise ValueError("allocation Decision retire candidate is unavailable")
            if retired.retired_by_decision_id not in {None, decision.decision_id}:
                raise ValueError("candidate was retired by another allocation Decision")
            retired.allocation_eligibility = "retired"
            retired.retired_by_decision_id = decision.decision_id
            changed_ids.add(retired.candidate_id)

        trigger_iteration.allocation_decision_id = decision.decision_id
        trigger_iteration.allocation_decision_error = None
        if trigger_record.score_report is not None:
            trigger_record.score_report = trigger_record.score_report.model_copy(
                update={"allocation_decision": decision}
            )
        for candidate_id in changed_ids:
            self.runtime._write_candidate_record(run.run_id, by_id[candidate_id])
        self.runtime._write_run(run)

    def _create_decision_locked(
        self,
        run: RunRecord,
        frozen: FrozenSpec,
        trigger: AllocationTrigger,
        draft: AllocationProposal,
        rewarded_sources: list[RewardedSource],
    ) -> AllocationDecision | None:
        decisions = self.store.load_allocation_decisions(run.run_id)
        self._advance_decision_index(run, decisions)
        existing = next(
            (
                decision
                for decision in decisions
                if decision.schedule_task_id == trigger.schedule_task_id
            ),
            None,
        )
        if existing is not None:
            if (
                existing.trigger_candidate_id != trigger.candidate_id
                or existing.trigger_iteration != trigger.iteration
                or existing.trigger_commit != trigger.commit
            ):
                raise ValueError("existing Decision does not match allocation trigger")
            self._reconcile_decision_locked(run, existing)
            return existing
        if frozen.spec.strategy.search_scheduler is None:
            return None

        records = self.runtime._load_candidate_records(run.run_id)
        by_id = {item.candidate_id: item for item in records}
        trigger_record = by_id.get(trigger.candidate_id)
        trigger_iteration = next(
            (
                item
                for item in trigger_record.iterations
                if item.iteration == trigger.iteration
            ),
            None,
        ) if trigger_record is not None else None
        if (
            trigger_iteration is None
            or trigger_iteration.settlement_id != trigger.settlement_id
            or trigger_iteration.git_head != trigger.commit
        ):
            raise ValueError("allocation trigger does not match settled iteration")
        reserved = set(trigger.reserved_candidate_ids)
        rewarded_by_node = {
            (source.candidate_id, source.iteration): source
            for source in rewarded_sources
        }
        replacements: list[AllocationReplacement] = []
        for item in draft.replacements:
            if item.new_candidate_id not in reserved:
                raise ValueError("scheduler used an unreserved candidate id")
            retired = by_id.get(item.retire_candidate_id)
            source_record = by_id.get(item.source_candidate_id)
            if retired is None or retired.allocation_eligibility != "eligible":
                raise ValueError("scheduler selected an ineligible retirement candidate")
            if source_record is None:
                raise ValueError("scheduler selected an unavailable source candidate")
            source_iteration = next(
                (
                    iteration
                    for iteration in source_record.iterations
                    if iteration.iteration == item.source_iteration
                ),
                None,
            )
            rewarded_source = rewarded_by_node.get(
                (item.source_candidate_id, item.source_iteration)
            )
            expected_node_id = (
                f"{source_record.candidate_id}/iteration-{item.source_iteration:04d}"
            )
            if (
                source_iteration is None
                or source_iteration.git_head is None
                or source_iteration.artifact_hash is None
                or item.source_node_id != expected_node_id
                or rewarded_source is None
                or rewarded_source.node_id != expected_node_id
                or rewarded_source.settlement_id != source_iteration.settlement_id
            ):
                raise ValueError(
                    "scheduler source node is not bound to a completed reward"
                )
            source_depth = source_record.task.allocation_depth
            replacements.append(
                AllocationReplacement(
                    retire_candidate_id=item.retire_candidate_id,
                    new_candidate_id=item.new_candidate_id,
                    source=ExpansionSource(
                        candidate_id=source_record.candidate_id,
                        iteration=source_iteration.iteration,
                        node_id=expected_node_id,
                        settled_commit=(
                            source_iteration.workspace_git_head_after_settlement
                            or source_iteration.ledger_git_head
                            or source_iteration.git_head
                        ),
                        score=source_iteration.score,
                        artifact_hash=source_iteration.artifact_hash,
                        allocation_depth=source_depth,
                    ),
                    rationale=item.rationale,
                    exploration_direction=item.exploration_direction,
                    worker_budget=item.worker_budget,
                )
            )

        eligible_count = sum(
            item.allocation_eligibility == "eligible" for item in records
        )
        projected_eligible = (
            eligible_count
            - len({item.retire_candidate_id for item in replacements})
            + len(replacements)
        )
        if projected_eligible > frozen.spec.budget.max_parallel:
            raise ValueError(
                "scheduler decision would exceed budget.max_parallel"
            )

        decision_id = f"allocation_{run.next_allocation_decision_index:04d}"
        run.next_allocation_decision_index += 1
        decision = AllocationDecision(
            decision_id=decision_id,
            run_id=run.run_id,
            schedule_task_id=trigger.schedule_task_id,
            trigger_candidate_id=trigger.candidate_id,
            trigger_iteration=trigger.iteration,
            trigger_commit=trigger.commit,
            rationale=draft.rationale,
            replacements=replacements,
            created_at=utc_timestamp(),
        )
        self.store.write_allocation_decision(decision)
        self._reconcile_decision_locked(run, decision)
        return decision

    def record(
        self,
        run_id: str,
        trigger: AllocationTrigger,
        draft: AllocationProposal,
        rewarded_sources: list[RewardedSource],
    ) -> AllocationDecision | None:
        with self.runtime._run_transaction(run_id):
            run = self.runtime._load_run(run_id)
            frozen = self.runtime._load_frozen_spec(run.frozen_spec_id)
            if not trigger.reserved_candidate_ids:
                return None
            try:
                return self._create_decision_locked(
                    run,
                    frozen,
                    trigger,
                    draft,
                    rewarded_sources,
                )
            except Exception as exc:
                record = self.runtime._load_candidate_record(
                    run_id,
                    trigger.candidate_id,
                )
                trigger_iteration = next(
                    item
                    for item in record.iterations
                    if item.iteration == trigger.iteration
                )
                trigger_iteration.allocation_decision_error = (
                    f"{type(exc).__name__}: {exc}"
                )
                self.runtime._write_candidate_record(run_id, record)
                return None

    def list(
        self,
        run_id: str,
        status: Literal["pending", "applied"] | None = None,
    ) -> list[AllocationDecision]:
        run = self.runtime._load_run(run_id)
        frozen = self.runtime._load_frozen_spec(run.frozen_spec_id)
        if frozen.spec.strategy.search_scheduler is None:
            raise RuntimeError("allocation decisions require strategy.search_scheduler")
        decisions = self.store.load_allocation_decisions(run_id)
        if status is not None:
            decisions = [item for item in decisions if item.status == status]
        return decisions

    def apply(
        self,
        run_id: str,
        decision_id: str,
    ) -> dict[str, Any]:
        created_candidate_ids: list[str] = []
        replacements_by_child: dict[str, AllocationReplacement] = {}
        with self.runtime._run_transaction(run_id):
            run = self.runtime._load_run(run_id)
            self.runtime._assert_worker_iteration_allowed(
                run,
                "apply allocation decision",
            )
            frozen = self.runtime._load_frozen_spec(run.frozen_spec_id)
            if frozen.spec.strategy.search_scheduler is None:
                raise RuntimeError(
                    "allocation decisions require strategy.search_scheduler"
                )
            decision = self.store.load_allocation_decision(run_id, decision_id)
            if decision.run_id != run_id:
                raise ValueError("allocation decision belongs to another run")

            records = self.runtime._load_candidate_records(run_id)
            records_by_id = {item.candidate_id: item for item in records}
            missing_child_ids: set[str] = set()
            for replacement in decision.replacements:
                retired = records_by_id.get(replacement.retire_candidate_id)
                if retired is None:
                    raise RuntimeError(
                        "allocation retire candidate is unavailable: "
                        f"{replacement.retire_candidate_id}"
                    )
                if retired.retired_by_decision_id not in {None, decision_id}:
                    raise RuntimeError(
                        f"candidate {retired.candidate_id} was retired by another decision"
                    )
                source_record = records_by_id.get(replacement.source.candidate_id)
                if source_record is None:
                    raise RuntimeError(
                        "allocation source candidate is unavailable: "
                        f"{replacement.source.candidate_id}"
                    )
                if source_record.task.plan_id is None:
                    raise RuntimeError("expansion source has no initial plan")
                source_iteration = next(
                    (
                        item
                        for item in source_record.iterations
                        if item.iteration == replacement.source.iteration
                    ),
                    None,
                )
                if (
                    source_iteration is None
                    or (
                        source_iteration.workspace_git_head_after_settlement
                        or source_iteration.ledger_git_head
                        or source_iteration.git_head
                    )
                    != replacement.source.settled_commit
                    or source_iteration.artifact_hash != replacement.source.artifact_hash
                ):
                    raise RuntimeError("allocation source no longer matches settled Evidence")
                self.runtime._load_plan(run_id, source_record.task.plan_id)
                self.runtime._source_ledger_at_revision(
                    source_record,
                    replacement.source.settled_commit,
                )
                child = records_by_id.get(replacement.new_candidate_id)
                if child is not None:
                    if child.task.expansion_source != replacement.source:
                        raise RuntimeError(
                            f"candidate {replacement.new_candidate_id} does not match "
                            "allocation decision"
                        )
                else:
                    missing_child_ids.add(replacement.new_candidate_id)

            max_candidates = frozen.spec.budget.max_candidates
            if (
                max_candidates is not None
                and len(records) + len(missing_child_ids) > max_candidates
            ):
                raise RuntimeError(
                    "allocation decision would exceed budget.max_candidates"
                )

            eligible_count = sum(
                item.allocation_eligibility == "eligible" for item in records
            )
            if (
                eligible_count + len(missing_child_ids)
                > frozen.spec.budget.max_parallel
            ):
                raise RuntimeError(
                    "allocation decision would exceed budget.max_parallel"
                )

            for replacement in decision.replacements:
                retired = self.runtime._load_candidate_record(
                    run_id,
                    replacement.retire_candidate_id,
                )
                retired.allocation_eligibility = "retired"
                retired.retired_by_decision_id = decision_id
                self.runtime._write_candidate_record(run_id, retired)

                child_id = replacement.new_candidate_id
                replacements_by_child[child_id] = replacement
                child_path = self.store.candidate_dir(run_id, child_id) / "candidate.json"
                if child_path.exists():
                    child = self.runtime._load_candidate_record(run_id, child_id)
                    if child.task.expansion_source != replacement.source:
                        raise RuntimeError(
                            f"candidate {child_id} does not match allocation decision"
                        )
                    created_candidate_ids.append(child_id)
                    continue

                source_record = self.runtime._load_candidate_record(
                    run_id,
                    replacement.source.candidate_id,
                )
                if source_record.task.plan_id is None:
                    raise RuntimeError("expansion source has no initial plan")
                plan = self.runtime._load_plan(run_id, source_record.task.plan_id)
                proposal = CandidateProposal(
                    intent=replacement.exploration_direction,
                    instructions=[
                        "这是 scheduler 从已验证父 Evidence 派生的新 candidate。",
                        "把 expansion_source 视为初始 incumbent，独立执行 scheduler 指定的探索方向，不依赖父 worker transcript。",
                    ],
                    metadata={
                        "allocation_decision_id": decision_id,
                        "expansion_source_candidate_id": replacement.source.candidate_id,
                        "expansion_source_iteration": replacement.source.iteration,
                        "exploration_direction": replacement.exploration_direction,
                    },
                )
                slot = int(source_record.task.strategy_metadata.get("slot") or 1)
                self.runtime._materialize_candidate_record(
                    run=run,
                    frozen=frozen,
                    candidate_id=child_id,
                    plan=plan,
                    proposal=proposal,
                    slot=slot,
                    expansion_source=replacement.source,
                    source_record=source_record,
                    results_ledger=self.runtime._source_ledger_at_revision(
                        source_record,
                        replacement.source.settled_commit,
                    ),
                )
                run.candidates_total += 1
                created_candidate_ids.append(child_id)

            if decision.status == "pending":
                decision = decision.model_copy(
                    update={"status": "applied", "applied_at": utc_timestamp()}
                )
                self.store.write_allocation_decision(decision)
            run.candidates_total = max(
                run.candidates_total,
                len(self.runtime._load_candidate_records(run_id)),
            )
            self.runtime._write_run(run)

        sessions: list[AgentSessionRecord] = []
        existing_sessions = {
            item.candidate_id: item
            for item in self.runtime._load_agent_sessions(run_id)
            if item.directive.get("allocation_decision_id") == decision_id
        }
        for candidate_id in created_candidate_ids:
            replacement = replacements_by_child[candidate_id]
            existing_session = existing_sessions.get(candidate_id)
            if existing_session is not None:
                sessions.append(existing_session)
                continue
            sessions.append(
                self.runtime.start_agent_session(
                    run_id,
                    candidate_id,
                    {
                        "search_scheduler_expansion": True,
                        "allocation_decision_id": decision_id,
                        "expansion_source": replacement.source.model_dump(mode="json"),
                        "exploration_direction": replacement.exploration_direction,
                        "resume_instruction": (
                            "先读取权威 candidate context 和当前 Global Evidence，再从已验证"
                            "初始 incumbent 执行 scheduler 给出的探索方向。"
                        ),
                    },
                    worker_budget=(
                        replacement.worker_budget.model_dump(mode="json")
                        if replacement.worker_budget is not None
                        else None
                    ),
                )
            )
        return {
            "decision": self.store.load_allocation_decision(
                run_id,
                decision_id,
            ).model_dump(mode="json"),
            "candidate_tasks": [
                self.runtime._load_candidate_record(run_id, candidate_id).task.model_dump(
                    mode="json"
                )
                for candidate_id in created_candidate_ids
            ],
            "agent_sessions": [item.model_dump(mode="json") for item in sessions],
        }

__all__ = [
    "AllocationDecision",
    "AllocationEligibility",
    "AllocationExecutor",
    "AllocationProposal",
    "AllocationReplacement",
    "AllocationReplacementProposal",
    "AllocationTrigger",
    "ExpansionSource",
    "RewardedSource",
    "load_allocation_decisions",
]
