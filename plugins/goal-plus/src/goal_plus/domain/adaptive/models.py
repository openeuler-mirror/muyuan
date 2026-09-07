"""Pure contracts and structural invariants."""

from __future__ import annotations

import math
from typing import (
    Any,
    Literal,
)

from pydantic import (
    Field,
    field_validator,
    model_validator,
)

from goal_plus.domain.adaptive.policies import parse_scheduler_policy_reference
from goal_plus.domain.agents import (
    AgentHostKind,
    WorkerBudget,
)
from goal_plus.domain.base import SearchModel

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


ScheduleTaskState = Literal[
    "pending", "running", "retry_wait", "completed", "terminal_error"
]


class ResolvedSchedulerProfile(SearchModel):
    host: AgentHostKind
    model: str = Field(min_length=1)
    reasoning_effort: str = Field(min_length=1)
    timeout_seconds: int = Field(gt=0, le=1800)
    configuration_source: Literal["host_process"] = "host_process"


class SchedulerInvocationRecord(SearchModel):
    invocation_id: str = Field(min_length=1)
    task_kind: Literal["search_scheduler"] = "search_scheduler"
    host: AgentHostKind
    model: str = Field(min_length=1)
    reasoning_effort: str = Field(min_length=1)
    started_at: str
    completed_at: str
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)


class SchedulerReward(SearchModel):
    reward: float = Field(allow_inf_nan=False)
    rubric_score: float = Field(ge=-100, le=100, allow_inf_nan=False)
    exploitation: float = Field(allow_inf_nan=False)
    exploration: float = Field(ge=0, allow_inf_nan=False)
    uct: float = Field(allow_inf_nan=False)
    components: dict[str, float] = Field(default_factory=dict)
    rationale: str = Field(min_length=1, max_length=2000)

    @field_validator("components")
    @classmethod
    def components_are_finite(cls, value: dict[str, float]) -> dict[str, float]:
        if any(not math.isfinite(item) for item in value.values()):
            raise ValueError("scheduler reward components must be finite")
        return value


class SchedulerOutput(SearchModel):
    reward: SchedulerReward
    allocation_flag: bool
    allocation_rationale: str = Field(min_length=1, max_length=2000)
    allocation_confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    decision: AllocationProposal | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def decision_requires_reschedule_flag(self) -> "SchedulerOutput":
        if not self.allocation_flag and self.decision is not None:
            raise ValueError("allocation_flag=false output cannot include a Decision")
        return self


class SchedulerRewardRecord(SearchModel):
    schedule_task_id: str = Field(min_length=1)
    settlement_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    commit: str = Field(min_length=1)
    reward: SchedulerReward
    allocation_flag: bool
    allocation_rationale: str = Field(min_length=1, max_length=2000)
    allocation_confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    completed_at: str


class ScheduleTask(SearchModel):
    schema_version: Literal[4] = 4
    task_id: str = Field(min_length=1)
    frozen_spec_id: str = Field(min_length=1)
    settlement_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    annotation_task_id: str = Field(min_length=1)
    annotation_state: Literal["completed", "terminal_error"]
    evidence_view: str | None = Field(default=None, max_length=1000)
    annotation_error_fingerprint: str | None = None
    reward_policy_ref: str = Field(min_length=1)
    allocation_policy_ref: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    attempt_base_commit: str = Field(min_length=1)
    attempt_commit: str = Field(min_length=1)
    attempt_changed_files: list[str] = Field(default_factory=list)
    task_context_source: Literal["goal_plus_raw_goal", "frozen_objective"]
    task_context_ref: str | None = None
    task_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reserved_candidate_ids: list[str] = Field(default_factory=list)
    profile: ResolvedSchedulerProfile | None = None
    outer_deadline_at: str | None = None
    state: ScheduleTaskState = "pending"
    attempts: int = Field(default=0, ge=0)
    next_attempt_at: str | None = None
    error_fingerprint: str | None = None
    last_error: str | None = None
    attempt_history: list[dict[str, Any]] = Field(default_factory=list)
    output: SchedulerOutput | None = None
    decision_id: str | None = None
    usage: dict[str, int | float] = Field(default_factory=dict)
    invocation: SchedulerInvocationRecord | None = None
    consumed_at: str | None = None
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def state_matches_output(self) -> "ScheduleTask":
        if self.annotation_state == "completed" and self.evidence_view is None:
            raise ValueError("completed annotation requires an Evidence View")
        if self.annotation_state == "terminal_error" and self.evidence_view is not None:
            raise ValueError("failed annotation cannot provide an Evidence View")
        if self.state == "completed" and self.output is None:
            raise ValueError("completed schedule task requires scheduler output")
        if self.state == "terminal_error" and not self.last_error:
            raise ValueError("terminal schedule task requires an error")
        return self


def requests_allocation_confirmation(previous: ScheduleTask | None) -> bool:
    return bool(
        previous is not None
        and previous.state == "completed"
        and previous.output is not None
        and previous.output.allocation_flag
    )


def scheduler_reward_records(
    tasks: list[ScheduleTask],
    *,
    candidate_id: str | None = None,
) -> list[SchedulerRewardRecord]:
    """Project completed scheduler rewards directly from authoritative tasks."""

    records = [
        SchedulerRewardRecord(
            schedule_task_id=task.task_id,
            settlement_id=task.settlement_id,
            node_id=task.node_id,
            run_id=task.run_id,
            candidate_id=task.candidate_id,
            iteration=task.iteration,
            commit=task.attempt_commit,
            reward=task.output.reward,
            allocation_flag=task.output.allocation_flag,
            allocation_rationale=task.output.allocation_rationale,
            allocation_confidence=task.output.allocation_confidence,
            completed_at=task.updated_at,
        )
        for task in tasks
        if task.state == "completed"
        and task.output is not None
        and (candidate_id is None or task.candidate_id == candidate_id)
    ]
    return sorted(records, key=lambda item: (item.candidate_id, item.iteration))


class SearchSchedulerSpec(SearchModel):
    host: AgentHostKind
    model: str = Field(min_length=1)
    reasoning_effort: str = Field(min_length=1)
    timeout_seconds: int = Field(gt=0, le=1800)
    reward: str = Field(min_length=1)
    allocation: str = Field(min_length=1)

    @field_validator("reward", "allocation")
    @classmethod
    def policy_reference_is_valid(cls, value: str) -> str:
        parse_scheduler_policy_reference(value)
        return value.strip()

    def validate_integration_constraints(
        self,
        *,
        workspace_backend: str,
    ) -> None:
        if workspace_backend != "git_worktree":
            raise ValueError("search_scheduler requires workspace.backend=git_worktree")
