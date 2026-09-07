"""Pure contracts and structural invariants."""

from __future__ import annotations

from enum import Enum
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

from goal_plus.domain.adaptive.models import (
    ExpansionSource,
    SearchSchedulerSpec,
)
from goal_plus.domain.adaptive.policies import ResolvedSearchSchedulerPolicies
from goal_plus.domain.agents import (
    AgentHostHandle,
    AgentHostKind,
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
    WorkspaceBackend,
    WorkspaceCapacityEstimate,
    WorkspaceOperationTiming,
    WorkspaceSpec,
)
from goal_plus.domain.base import SearchModel
from goal_plus.domain.shared_tools import SharedDirSpec

CandidateAcceptanceCriterion = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1000),
]


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
    max_tokens: int | None = Field(default=None, gt=0)

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
    allow: list[str] = Field(min_length=1)
    deny: list[str] = Field(default_factory=list)
    max_file_changes: int | None = Field(default=None, gt=0)


class StrategySpec(SearchModel):
    name: str = "agent_guided"
    orchestration_mode: Literal[
        "rolling_candidates",
        "parallel_loops",
        "adaptive_search",
    ] = "parallel_loops"
    worker_host: AgentHostKind = "codex"
    inner_agent: str | None = Field(
        default=None,
        description=(
            "Host-neutral inner-agent adapter id. New frozen specs normalize this "
            "to an explicit value."
        ),
    )
    worker_agent_type: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "Legacy Codex-native role override. New specs must use inner_agent."
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
    search_scheduler: SearchSchedulerSpec | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def name_must_be_nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("strategy name must be non-empty")
        return value

    @field_validator("inner_agent", "worker_agent_type")
    @classmethod
    def inner_agent_fields_must_be_nonempty(
        cls,
        value: str | None,
    ) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("inner-agent fields must be non-empty when provided")
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def adaptive_search_requires_scheduler_configuration(self) -> "StrategySpec":
        adaptive_search = self.orchestration_mode == "adaptive_search"
        scheduler_configured = self.search_scheduler is not None
        if adaptive_search and not scheduler_configured:
            raise ValueError(
                "strategy.orchestration_mode=adaptive_search requires "
                "strategy.search_scheduler"
            )
        if scheduler_configured and not adaptive_search:
            raise ValueError(
                "strategy.search_scheduler requires "
                "strategy.orchestration_mode=adaptive_search"
            )
        return self

    @model_validator(mode="after")
    def internal_agents_use_worker_host(self) -> "StrategySpec":
        annotator_host = self.evidence_annotator.host
        if annotator_host is not None and annotator_host != self.worker_host:
            raise ValueError(
                "strategy.evidence_annotator.host must match strategy.worker_host "
                "so internal agents inherit the native host configuration"
            )
        scheduler = self.search_scheduler
        if scheduler is not None and scheduler.host != self.worker_host:
            raise ValueError(
                "strategy.search_scheduler.host must match strategy.worker_host "
                "so internal agents inherit the native host configuration"
            )
        return self

    @model_validator(mode="after")
    def agent_profile_matches_worker_host(self) -> "StrategySpec":
        profile = (
            self.worker_launch.agent_profile
            if self.worker_launch is not None
            else None
        )
        if self.worker_host == "thinkthread":
            if profile != "pi":
                raise ValueError(
                    "thinkthread worker_host requires "
                    "worker_launch.agent_profile='pi'"
                )
            return self
        if profile is not None:
            raise ValueError(
                "worker_launch.agent_profile is only valid for thinkthread worker_host"
            )
        return self


class VerifierCommand(SearchModel):
    name: str = Field(min_length=1)
    role: VerifierRole
    command: list[str] = Field(min_length=1)
    cwd: str = "."
    timeout_seconds: int = Field(default=300, gt=0)
    resource_lock: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "可选的 host 级独占资源名。值相同的 verifier 命令会跨候选和 run 串行执行。"
        ),
    )
    feedback_policy: FeedbackPolicy = FeedbackPolicy.VISIBLE_TO_WORKERS
    expected_outputs: list[str] = Field(
        default_factory=list,
        description=(
            "候选工作区中预期的产物路径或 glob；此字段不解析 verifier stdout metric。"
        ),
    )

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
    metric_name: str = Field(min_length=1)
    metric_direction: Literal["minimize", "maximize"]
    source_path: str
    edit_surface: EditSurface
    budget: Budget
    process_verifiers: list[VerifierCommand] = Field(min_length=1)
    promotion_verifiers: list[VerifierCommand] = Field(default_factory=list)
    device_pools: list[DevicePoolSpec] = Field(default_factory=list)
    candidate_acceptance: list[CandidateAcceptanceCriterion] = Field(
        default_factory=list,
        max_length=32,
    )
    constraints: dict[str, Any] = Field(default_factory=dict)
    root_hypotheses: list[str] = Field(default_factory=list)
    strategy: StrategySpec = Field(default_factory=StrategySpec)
    workspace: WorkspaceSpec = Field(default_factory=WorkspaceSpec)
    shared_dir: SharedDirSpec = Field(default_factory=SharedDirSpec)

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
        scheduler = self.strategy.search_scheduler
        if scheduler is not None:
            scheduler.validate_integration_constraints(
                workspace_backend=self.workspace.backend,
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


class BootstrapVerification(SearchModel):
    phase: Literal["origin", "workspace_relocation"]
    backend: WorkspaceBackend
    passed: bool
    aggregate_score: float | None = None
    verifier_results: list[BootstrapVerifierResult] = Field(default_factory=list)
    source_artifact_ref: ArtifactRef | None = None
    candidate_id: str | None = None
    started_at: str
    completed_at: str


class FrozenSpec(SearchModel):
    frozen_spec_id: str
    spec_hash: str
    goal_plus_id: str | None = None
    spec: SearchSpec
    search_scheduler_policies: ResolvedSearchSchedulerPolicies | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
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

    @model_validator(mode="after")
    def scheduler_configuration_has_frozen_policies(self) -> "FrozenSpec":
        configured = self.spec.strategy.search_scheduler
        policies = self.search_scheduler_policies
        if (configured is None) != (policies is None):
            raise ValueError(
                "search_scheduler configuration and frozen policies must coexist"
            )
        if configured is not None and policies is not None:
            reward_ref, reward_marker, reward_prompt = configured.reward.partition(
                "@prompt"
            )
            allocation_ref, allocation_marker, allocation_prompt = (
                configured.allocation.partition("@prompt")
            )
            if (
                policies.reward.reference != reward_ref.strip()
                or policies.reward.supplemental_prompt
                != (reward_prompt.strip() if reward_marker else None)
                or policies.allocation.reference != allocation_ref.strip()
                or policies.allocation.supplemental_prompt
                != (allocation_prompt.strip() if allocation_marker else None)
            ):
                raise ValueError(
                    "frozen scheduler policies do not match scheduler configuration"
                )
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
    workspace_backend: WorkspaceBackend = "copy"
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
                if payload.get("workspace_backend") == "thinkthread"
                else "native_root"
            )
        return payload

    @model_validator(mode="after")
    def execution_scope_matches_workspace(self) -> "CandidateTask":
        expected = (
            "thinkthread_private"
            if self.workspace_backend == "thinkthread"
            else "native_root"
        )
        if self.execution_scope != expected:
            raise ValueError(
                f"workspace backend {self.workspace_backend!r} requires "
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


class AgentSessionRecord(SearchModel):
    agent_session_id: str
    run_id: str
    candidate_id: str
    execution_generation: int = Field(default=0, ge=0)
    host: AgentHostKind = "codex"
    execution_scope: ExecutionScope = "native_root"
    host_handle: AgentHostHandle = Field(default_factory=AgentHostHandle)
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
    def infer_legacy_execution_scope(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "execution_scope" in value:
            return value
        payload = dict(value)
        payload["execution_scope"] = (
            "thinkthread_private"
            if payload.get("host") == "thinkthread"
            else "native_root"
        )
        return payload

    @model_validator(mode="after")
    def execution_scope_matches_host(self) -> "AgentSessionRecord":
        expected = (
            "thinkthread_private" if self.host == "thinkthread" else "native_root"
        )
        if self.execution_scope != expected:
            raise ValueError(
                f"agent host {self.host!r} requires execution_scope={expected!r}"
            )
        return self


# CandidateTask retains its public forward reference to CandidateProposal.
CandidateTask.model_rebuild()
