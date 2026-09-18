"""Pure contracts and structural invariants."""

from __future__ import annotations

from enum import Enum
import math
from pathlib import Path
from typing import (
    Annotated,
    Any,
    Literal,
)

from pydantic import (
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from goal_plus.domain.adaptive.models import ExpansionSource
from goal_plus.domain.adaptive.policy import AllocationPolicySpec, SteeringPolicySpec
from goal_plus.domain.verifier import VerifierSpec
from goal_plus.domain.agents import (
    AgentSessionHandle,
    AgentHarnessKind,
    AgentRuntimeProvider,
    validate_search_execution,
    ModelSpec,
    SelectedModel,
    WorkerBudget,
    WorkerLaunchOptions,
)
from goal_plus.domain.annotation import (
    EvidenceAnnotatorSpec,
    EvidenceDetailReadRecord,
    GlobalEvidenceReadRecord,
)
from goal_plus.domain.artifacts import (
    ArtifactRef,
    ExecutionScope,
    WorkspaceAccess,
    WorkspaceProvider,
    WorkspaceCapacityEstimate,
    WorkspaceOperationTiming,
    WorkspaceSpec,
)
from goal_plus.domain.base import SearchModel
from goal_plus.domain.shared_tools import SharedDirSpec
from goal_plus.domain.selection import SelectionSpec

CandidateAcceptanceCriterion = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1000),
]

# Match the existing bounded preflight error excerpts; full output stays in logs.
VERIFIER_ERROR_EXCERPT_CHARS = 2_000


class RunState(str, Enum):
    FROZEN_SPEC = "frozen_spec"
    RUNNING = "running"
    WAITING_FOR_WORKERS = "waiting_for_workers"
    EVALUATING = "evaluating"
    SELECTING = "selecting"
    SELECTION_BLOCKED = "selection_blocked"
    READY_TO_PROMOTE = "ready_to_promote"
    PROMOTED = "promoted"
    ABORTED = "aborted"
    FAILED = "failed"


class VerifierRole(str, Enum):
    VALIDITY_GATE = "validity_gate"
    PROCESS_GATE = "process_gate"
    RANKING_SIGNAL = "ranking_signal"
    DIAGNOSTIC_SIGNAL = "diagnostic_signal"
    PROMOTION_GATE = "promotion_gate"
    ANTI_CHEAT_GATE = "anti_cheat_gate"


class FeedbackPolicy(str, Enum):
    VISIBLE_TO_WORKERS = "visible_to_workers"
    SUMMARY_ONLY = "summary_only"
    FINAL_ONLY = "final_only"


class Budget(SearchModel):
    max_parallel: int = Field(
        gt=0,
        description=(
            "一个 Search run 初始创建并实际并行工作的候选 Agent 数量。"
            "parallel_loops 只创建这一组长期候选，后续继续已有 candidate/session；"
            "adaptive_search 同时将其作为活跃 worker 上限。"
        ),
    )
    max_candidates: int | None = Field(
        default=None,
        gt=0,
        description=(
            "一个 run 累计可物化的唯一 candidate 上限；null 表示不限制。"
            "有界值必须大于等于 max_parallel。"
        ),
    )

    @model_validator(mode="after")
    def candidate_limit_covers_parallel_width(self) -> "Budget":
        if self.max_candidates is not None and self.max_candidates < self.max_parallel:
            raise ValueError("budget.max_candidates must be >= budget.max_parallel")
        return self


VerifierInvalidationReason = Literal[
    "verifier_contract_invalid",
    "verifier_coverage_inadequate",
    "verifier_nondeterministic",
    "verifier_target_mismatch",
    "verifier_infrastructure_failure",
]


class EditSurface(SearchModel):
    allow: list[str] = Field(min_length=1, description="Editable paths/globs relative to source_path.")
    deny: list[str] = Field(default_factory=list, description="Read-only paths/globs; overrides allow.")
    max_file_changes: int | None = Field(default=None, gt=0)


class StrategyOptions(SearchModel):
    name: str = Field(
        default="agent_guided",
        description="agent_guided (aliases agent/default) requires proposals; random (alias random_mode) does not. Not a custom label.",
    )
    orchestration_mode: Literal[
        "rolling_candidates",
        "parallel_loops",
        "adaptive_search",
    ] = Field(default="parallel_loops", description=(
        "Defaults to adaptive_search when allocation or steering is supplied, otherwise parallel_loops."
    ))
    inner_agent: str | None = Field(
        default=None,
        description=(
            "Host-neutral adapter: autoresearch or common. Omit for the host default."
        ),
    )
    worker_budget: WorkerBudget | None = None
    worker_launch: WorkerLaunchOptions | None = None
    evidence_annotator: EvidenceAnnotatorSpec = Field(
        default_factory=EvidenceAnnotatorSpec
    )
    models: list[ModelSpec] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
    )
    selection: SelectionSpec = Field(default_factory=SelectionSpec)
    verifier: VerifierSpec | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="Optional extra scores; configured policies automatically add their required score keys.",
    )
    allocation: AllocationPolicySpec | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    steering: SteeringPolicySpec | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("name")
    @classmethod
    def name_must_be_nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("strategy name must be non-empty")
        return value

    @field_validator("inner_agent")
    @classmethod
    def inner_agent_fields_must_be_nonempty(
        cls,
        value: str | None,
    ) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("inner-agent fields must be non-empty when provided")
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def search_mode_configuration_is_consistent(self) -> "StrategyOptions":
        policies = [policy for policy in (self.allocation, self.steering) if policy is not None]
        if policies and "orchestration_mode" not in self.model_fields_set:
            self.orchestration_mode = "adaptive_search"
        adaptive_search = self.orchestration_mode == "adaptive_search"
        if (
            "quality_score" in self.selection.ranking_keys
            and self.orchestration_mode == "rolling_candidates"
        ):
            raise ValueError(
                "quality_score selection requires parallel_loops or adaptive_search"
            )
        if adaptive_search and not policies:
            raise ValueError(
                "strategy.orchestration_mode=adaptive_search requires allocation or steering"
            )
        if self.allocation is not None and not adaptive_search:
            raise ValueError("strategy.allocation requires adaptive_search")
        if self.verifier is not None and self.orchestration_mode == "rolling_candidates":
            raise ValueError("strategy.verifier requires parallel_loops or adaptive_search")
        if self.steering is not None and not adaptive_search:
            raise ValueError("strategy.steering requires adaptive_search")
        if policies or "quality_score" in self.selection.ranking_keys:
            scores = list(self.verifier.scores) if self.verifier is not None else []
            for policy in policies:
                scores.extend(key for key in policy.required_score_keys if key not in scores)
            self.verifier = VerifierSpec(scores=scores)
        return self

    @property
    def requires_llm_verifier(self) -> bool:
        return (
            self.verifier is not None
            or "quality_score" in self.selection.ranking_keys
        )


class StrategySpec(StrategyOptions):
    config: dict[str, Any] = Field(default_factory=dict)


class VerifierCommand(SearchModel):
    name: str = Field(min_length=1)
    role: VerifierRole = Field(description="ranking_signal selects the score; diagnostic_signal is non-blocking. Other roles are gates, not JSON field predicates.")
    command: list[str] = Field(min_length=1)
    cwd: str = Field(default=".", description="Relative to the source/candidate workspace; normally '.'.")
    timeout_seconds: int = Field(default=300, gt=0)
    resource_lock: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Host-wide exclusive resource name. Each declared device pool requires a <pool_id>:0 lock."
        ),
    )
    feedback_policy: FeedbackPolicy = FeedbackPolicy.VISIBLE_TO_WORKERS

    @field_validator("resource_lock")
    @classmethod
    def resource_lock_must_be_nonempty(
        cls,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("resource_lock must be non-empty when provided")
        return normalized


class DevicePoolSpec(SearchModel):
    pool_id: str = Field(
        min_length=1,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )

    @field_validator("pool_id")
    @classmethod
    def pool_id_must_be_nonempty(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("device pool id must be non-empty")
        return normalized


class DevicePoolInventory(SearchModel):
    visibility_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    devices: list[str] = Field(min_length=1)

    @field_validator("devices")
    @classmethod
    def devices_must_be_unique_and_nonempty(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value or "," in value for value in normalized):
            raise ValueError("device ids must be non-empty single-device values")
        if len(normalized) != len(set(normalized)):
            raise ValueError("device ids must be unique")
        return normalized


class SearchSpec(SearchModel):
    objective: str = Field(min_length=1)
    metric_name: str = Field(min_length=1, description="Exact numeric stdout JSON key; no aliases. returncode/elapsed_seconds are reserved.")
    metric_direction: Literal["minimize", "maximize"]
    source_path: str = Field(min_length=1, description="Task source root to snapshot into candidate workspaces.")
    edit_surface: EditSurface
    budget: Budget
    process_verifiers: list[VerifierCommand] = Field(min_length=1)
    promotion_verifiers: list[VerifierCommand] = Field(default_factory=list)
    device_pools: list[DevicePoolSpec] = Field(default_factory=list)
    candidate_acceptance: list[CandidateAcceptanceCriterion] = Field(
        default_factory=list,
        max_length=32,
        description="Worker instructions, not executable gates. Enforce acceptance in verifier commands.",
    )
    constraints: dict[str, Any] = Field(default_factory=dict)
    root_hypotheses: list[str] = Field(default_factory=list)
    strategy: StrategySpec = Field(default_factory=StrategySpec)
    workspace: WorkspaceSpec = Field(default_factory=WorkspaceSpec)
    shared_dir: SharedDirSpec = Field(default_factory=SharedDirSpec)

    @field_validator("metric_name")
    @classmethod
    def metric_is_not_runtime_metadata(cls, value: str) -> str:
        if value in {"returncode", "elapsed_seconds"}:
            raise ValueError("metric_name must not use reserved runtime metadata: returncode, elapsed_seconds")
        return value

    @field_validator("process_verifiers", "promotion_verifiers")
    @classmethod
    def unambiguous_ranking_source(cls, commands: list[VerifierCommand]) -> list[VerifierCommand]:
        if sum(command.role == VerifierRole.RANKING_SIGNAL for command in commands) > 1:
            raise ValueError("each verifier suite permits at most one ranking_signal; combine metrics in that verifier")
        return commands

    @field_validator("source_path")
    @classmethod
    def source_path_must_be_nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source_path must be non-empty")
        return value

    @model_validator(mode="after")
    def device_pools_match_verifier_locks(self) -> "SearchSpec":
        pool_ids = [pool.pool_id for pool in self.device_pools]
        if len(pool_ids) != len(set(pool_ids)):
            raise ValueError("device pool ids must be unique")
        locks = [
            command.resource_lock
            for command in [*self.process_verifiers, *self.promotion_verifiers]
            if command.resource_lock
        ]
        for pool_id in pool_ids:
            matching = [lock for lock in locks if lock.rpartition(":")[0] == pool_id]
            if not matching:
                raise ValueError(
                    f"device pool {pool_id!r} is not referenced by a verifier resource_lock"
                )
            if any(lock.rpartition(":")[2] != "0" for lock in matching):
                raise ValueError(
                    f"device pool {pool_id!r} only supports single-device resource_lock {pool_id}:0"
                )
        return self

    @model_validator(mode="after")
    def strategy_extension_constraints_are_valid(self) -> "SearchSpec":
        if (
            (self.strategy.allocation is not None or self.strategy.steering is not None)
            and self.workspace.provider not in {"git_worktree", "thinkthread"}
        ):
            raise ValueError(
                "adaptive search requires workspace.provider=git_worktree or thinkthread"
            )
        return self


class SearchSpecDraft(SearchModel):
    """Partially discovered SearchSpec with the same nested field contracts."""

    objective: str | None = Field(default=None, min_length=1)
    metric_name: str | None = Field(default=None, min_length=1)
    metric_direction: Literal["minimize", "maximize"] | None = None
    source_path: str | None = Field(default=None, min_length=1)
    edit_surface: EditSurface | None = None
    budget: Budget | None = None
    process_verifiers: list[VerifierCommand] | None = Field(default=None, min_length=1)
    promotion_verifiers: list[VerifierCommand] | None = None
    device_pools: list[DevicePoolSpec] | None = None
    candidate_acceptance: list[CandidateAcceptanceCriterion] | None = Field(
        default=None,
        max_length=32,
    )
    constraints: dict[str, Any] | None = None
    root_hypotheses: list[str] | None = None
    strategy: StrategySpec | None = None
    workspace: WorkspaceSpec | None = None
    shared_dir: SharedDirSpec | None = None


class BootstrapVerifierResult(SearchModel):
    name: str
    role: VerifierRole
    passed: bool
    score: float | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    log_path: Path | None = None
    failure_class: str | None = None

    @property
    def blocks_bootstrap(self) -> bool:
        return (
            not self.passed and self.role != VerifierRole.DIAGNOSTIC_SIGNAL
            and (self.failure_class != "CandidateRejected" or self.role == VerifierRole.ANTI_CHEAT_GATE)
        )


class BootstrapVerification(SearchModel):
    phase: Literal["origin", "workspace_relocation"]
    workspace_provider: WorkspaceProvider
    passed: bool
    aggregate_score: float | None = None
    verifier_results: list[BootstrapVerifierResult] = Field(default_factory=list)
    source_artifact_ref: ArtifactRef | None = None
    candidate_id: str | None = None
    started_at: str
    completed_at: str

    @property
    def evaluation_completed(self) -> bool:
        return self.passed or bool(self.verifier_results) and all(
            not result.blocks_bootstrap for result in self.verifier_results
        )

    def failure_detail(self) -> str:
        failed = next(
            (result for result in self.verifier_results if result.blocks_bootstrap),
            None,
        )
        if failed is None:
            return ""
        details = [f"{failed.name}: {failed.failure_class}"]
        for key in ("returncode", "error", "timeout_seconds", "cwd"):
            value = failed.metrics.get(key)
            if value is not None:
                excerpt = str(value)
                if len(excerpt) > VERIFIER_ERROR_EXCERPT_CHARS:
                    excerpt = excerpt[:VERIFIER_ERROR_EXCERPT_CHARS] + "... [truncated]"
                details.append(f"{key}={excerpt}")
        if failed.log_path is not None:
            details.append(f"log_path={failed.log_path}")
        return " " + "; ".join(details)

    def require_numeric_metric(self, metric_name: str) -> None:
        if self.aggregate_score is None or not math.isfinite(self.aggregate_score):
            raise ValueError(
                "MissingNumericMetric: process verifier baseline must emit a finite "
                f"numeric metric for {metric_name!r} in a single-line JSON object "
                "on stdout before Search can start. Text scores and output files "
                "are not parsed as metrics."
            )


class FrozenSpec(SearchModel):
    frozen_spec_id: str
    spec_hash: str
    goal_plus_id: str | None = None
    goal_revision: int | None = Field(default=None, ge=1)
    agent_harness: AgentHarnessKind
    runtime_provider: AgentRuntimeProvider
    spec: SearchSpec
    supplemental_evaluation_enabled: bool = False
    supplemental_evaluation_required: bool = False
    verifier_hashes: dict[str, str]
    frozen_verifier_paths: dict[str, str]
    task_skill_paths: list[str] = Field(default_factory=list)
    workspace_capacity_estimate: WorkspaceCapacityEstimate | None = None
    source_artifact_ref: ArtifactRef | None = None
    origin_verification: BootstrapVerification | None = None
    device_inventory: dict[str, DevicePoolInventory] = Field(default_factory=dict)
    created_at: str

    @model_validator(mode="before")
    @classmethod
    def require_complete_frozen_protocol(cls, value: Any) -> Any:
        if isinstance(value, dict) and isinstance(value.get("spec"), dict):
            workspace = value["spec"].get("workspace")
            if not isinstance(workspace, dict) or "provider" not in workspace:
                raise ValueError("frozen spec requires explicit workspace.provider")
        return value

    @model_validator(mode="after")
    def agent_harness_supports_workspace(self) -> "FrozenSpec":
        validate_search_execution(self.agent_harness, self.runtime_provider, self.spec.workspace.provider)
        return self


class CandidateTask(SearchModel):
    run_id: str
    candidate_id: str
    parent_id: str | None = None
    parent_candidate_ids: list[str] = Field(default_factory=list)
    base_candidate_id: str | None = None
    plan_id: str | None = None
    hypothesis: str
    execution_scope: ExecutionScope = "native_root"
    workspace: Path
    workspace_provider: WorkspaceProvider = "copy"
    workspace_branch: str | None = None
    workspace_base_revision: str | None = None
    workspace_managed_root: Path | None = None
    workspace_source_subdir: str = "."
    workspace_provider_handle: dict[str, Any] = Field(default_factory=dict)
    workspace_creation_timing: WorkspaceOperationTiming | None = None
    task_skill_paths: list[Path] = Field(default_factory=list)
    allocation_depth: int = Field(default=0, ge=0)
    expansion_source: ExpansionSource | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    base_artifact_ref: ArtifactRef | None = None
    workspace_access: WorkspaceAccess = Field(default_factory=WorkspaceAccess)
    share_out_dir: Path | None = None
    allowed_files: list[str]
    denied_files: list[str]
    acceptance: list[CandidateAcceptanceCriterion] = Field(
        default_factory=list,
        max_length=32,
    )
    process_environment: dict[str, str] = Field(default_factory=dict)
    instructions: list[str] = Field(default_factory=list)
    expected_artifacts: list[str] = Field(default_factory=list)
    stop_conditions: dict[str, Any] = Field(default_factory=dict)
    proposal: "CandidateProposal | None" = None
    strategy_metadata: dict[str, Any] = Field(default_factory=dict)
    selected_model: SelectedModel | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    model_provenance: dict[str, Any] = Field(default_factory=dict)

    @field_validator("acceptance", mode="before")
    @classmethod
    def migrate_legacy_acceptance(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return value.get("criteria", [])
        return value

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        payload.pop("shared_dir", None)
        if "execution_scope" not in payload:
            payload["execution_scope"] = (
                "thinkthread_private"
                if payload.get("workspace_provider") == "thinkthread"
                else "native_root"
            )
        return payload

    @model_validator(mode="after")
    def execution_scope_matches_workspace(self) -> "CandidateTask":
        expected = (
            "thinkthread_private"
            if self.workspace_provider == "thinkthread"
            else "native_root"
        )
        if self.execution_scope != expected:
            raise ValueError(
                f"workspace provider {self.workspace_provider!r} requires "
                f"execution_scope={expected!r}"
            )
        return self


class CandidateProposal(SearchModel):
    hypothesis: str | None = None
    intent: str = Field(min_length=1)
    expected_tradeoff: str = ""
    instructions: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CandidateWorkOrder(SearchModel):
    slot: int = Field(gt=0)
    intent: str = Field(min_length=1)
    hypothesis: str | None = None
    instructions: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchPlan(SearchModel):
    run_id: str
    plan_id: str
    status: Literal["planned", "started"] = "planned"
    strategy: StrategySpec
    requested_k: int = Field(gt=0)
    planned_k: int = Field(ge=0)
    remaining_budget: int = Field(ge=0)
    requires_agent_proposals: bool = False
    worker_policy: dict[str, Any] = Field(default_factory=dict)
    work_orders: list[CandidateWorkOrder] = Field(default_factory=list)
    strategy_trace: dict[str, Any] = Field(default_factory=dict)
    started_candidate_ids: list[str] = Field(default_factory=list)
    selected_models: list[SelectedModel] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
    )
    created_at: str


class RunSummary(SearchModel):
    run_id: str
    state: RunState
    frozen_spec_id: str
    candidates_total: int
    candidates_evaluated: int
    best_candidate_id: str | None = None
    best_score: float | None = None
    budget_used: dict[str, Any] = Field(default_factory=dict)
    source_run_id: str | None = None
    invalidated_at: str | None = None
    invalidation_reason: VerifierInvalidationReason | None = None
    replacement_run_id: str | None = None


class CandidateRecoveryPoint(SearchModel):
    """A durable settlement boundary, never an arbitrary workspace HEAD."""

    artifact_ref: ArtifactRef
    iteration: int = Field(ge=0)
    settlement_id: str | None = None
    result_count: int = Field(ge=0)


class CandidateRecovery(SearchModel):
    recovery_id: str = Field(pattern=r"^recovery_[0-9a-f]{32}$")
    generation: int = Field(ge=1)
    state: Literal["prepared", "restoring", "restored"] = "prepared"
    point: CandidateRecoveryPoint
    fenced_session_ids: list[str]
    created_at: str
    restored_artifact_ref: ArtifactRef | None = None
    restored_at: str | None = None

    @model_validator(mode="after")
    def valid_restoration(self) -> "CandidateRecovery":
        if self.state == "restored" and (self.restored_artifact_ref is None or self.restored_at is None):
            raise ValueError("restored recovery requires artifact and completion time")
        if len(set(self.fenced_session_ids)) != len(self.fenced_session_ids):
            raise ValueError("recovery session identities must be unique")
        return self


class CandidateRunRequest(SearchModel):
    call_id: str = Field(min_length=1)
    fingerprint: str
    target_session_id: str | None = None


class AgentSessionRecord(SearchModel):
    agent_session_id: str
    run_id: str
    candidate_id: str
    execution_generation: int = Field(default=0, ge=0)
    run_requests: list[CandidateRunRequest] = Field(default_factory=list)
    agent_harness: AgentHarnessKind = "codex"
    runtime_provider: AgentRuntimeProvider = "direct"
    execution_scope: ExecutionScope = "native_root"
    session_handle: AgentSessionHandle = Field(default_factory=AgentSessionHandle)
    created_at: str
    updated_at: str
    directive: dict[str, Any] = Field(default_factory=dict)
    selected_model: SelectedModel | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    model_provenance: dict[str, Any] = Field(default_factory=dict)
    workspace: Path
    launch: dict[str, Any] = Field(default_factory=dict)
    counters: dict[str, int] = Field(default_factory=dict)
    global_evidence_reads: list[GlobalEvidenceReadRecord] = Field(
        default_factory=list
    )
    evidence_detail_reads: list[EvidenceDetailReadRecord] = Field(
        default_factory=list
    )

    @model_validator(mode="before")
    @classmethod
    def resolve_execution_scope(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "execution_scope" in value:
            return value
        payload = dict(value)
        payload["execution_scope"] = (
            "thinkthread_private"
            if payload.get("runtime_provider") == "thinkthread"
            else "native_root"
        )
        return payload

    @model_validator(mode="after")
    def execution_scope_matches_host(self) -> "AgentSessionRecord":
        expected = (
            "thinkthread_private" if self.runtime_provider == "thinkthread" else "native_root"
        )
        if self.execution_scope != expected:
            raise ValueError(
                f"agent host {self.agent_harness!r} requires execution_scope={expected!r}"
            )
        if (self.agent_harness, self.runtime_provider) != (
            self.session_handle.agent_harness, self.session_handle.runtime_provider
        ):
            raise ValueError("session handle must match agent harness and runtime provider")
        return self


# CandidateTask retains its public forward reference to CandidateProposal.
CandidateTask.model_rebuild()
