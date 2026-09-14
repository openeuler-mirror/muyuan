"""Pure contracts and structural invariants."""

from __future__ import annotations

from typing import (
    Any,
    Literal,
)

from pydantic import (
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from goal_plus.domain.agents import AgentHarnessKind
from goal_plus.domain.artifacts import WorkspaceProvider
from goal_plus.domain.base import SearchModel
from goal_plus.domain.search import SearchSpecDraft

GoalPlusStatus = Literal["active", "needs_user", "blocked", "complete", "abandoned"]


GoalPlusPhase = Literal[
    "intake",
    "goal",
    "spec_discovery",
    "search",
    "final_audit",
    "final_check",
]


GoalPlusConfidence = Literal["high", "medium", "low"]


GoalPlusRecommendedPhase = Literal["goal", "spec_discovery", "search"]


GoalPlusDiscoveryOrigin = Literal["initial", "in_progress"]


GoalPlusGateEvent = Literal["stop", "subagent_stop", "pre_tool_use", "user_prompt_submit"]


GoalPlusGateDecision = Literal["allow", "block"]


GoalPlusSessionState = Literal["attached", "paused", "stale", "detached"]


GoalPlusEntrypointStatus = Literal["host_verified", "legacy_unverified"]


GoalPlusEntrypointHost = Literal["codex", "pi"]


GoalPlusEntrypointAction = Literal["start", "edit", "summary", "pause", "resume", "clear"]


GoalPlusHostSessionEvent = Literal[
    "session_start",
    "user_turn",
    "interrupt",
    "execution_lost",
    "continue_confirmed",
]


GoalPlusHostSessionDelivery = Literal["auto", "next_user_turn"]


GoalPlusHostSessionInteraction = Literal["interactive", "headless"]


GoalPlusHostSessionDecisionKind = Literal[
    "continue_now",
    "continue_after_user",
    "ask_user",
    "wait_for_user",
    "stop",
]


GoalPlusWorkStatus = Literal[
    "launching",
    "active",
    "result_ready",
    "accepted",
    "failed",
    "cancelled",
    "superseded",
]


GoalPlusWorkEventKind = Literal[
    "dispatch",
    "bind",
    "message",
    "result",
    "rework",
    "accepted",
    "failed",
    "cancelled",
]


GoalPlusUltraOperation = Literal["spawn", "send", "wait", "interrupt", "observe"]


class GoalPlusUltraHost(SearchModel):
    protocol: Literal["goal-plus-ultra-v1"] = "goal-plus-ultra-v1"
    host_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._-]+$")
    native_reasoning_effort: str = Field(min_length=1, max_length=120)
    enforcement: Literal["host", "harness", "requested"]
    operations: list[GoalPlusUltraOperation] = Field(min_length=3, max_length=5)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("operations")
    @classmethod
    def require_complete_lifecycle(
        cls,
        value: list[GoalPlusUltraOperation],
    ) -> list[GoalPlusUltraOperation]:
        required = {"spawn", "wait", "observe"}
        if not required.issubset(value) or len(value) != len(set(value)):
            raise ValueError(
                "Ultra host operations require unique spawn, wait, and observe capabilities"
            )
        return value


class GoalPlusExecutionPolicy(SearchModel):
    mode: Literal["orchestrated"] = "orchestrated"
    main_reasoning_effort: Literal["max", "host_selected"] = "max"
    delegation: Literal["proactive"] = "proactive"
    search_routing: Literal["auto"] = "auto"
    completion: Literal["until_terminal"] = "until_terminal"
    host: GoalPlusUltraHost | None = None


class GoalPlusNextAction(SearchModel):
    kind: str = Field(min_length=1)
    description: str = Field(min_length=1)
    required: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


GoalPlusFinalCheckMode = Literal["disabled", "required"]


GoalPlusFinalCheckStatus = Literal[
    "pending",
    "passed",
    "failed",
    "interrupted",
    "superseded",
]


GoalPlusFinalCheckerHost = Literal["codex", "pi"]


class GoalPlusCommandConfig(SearchModel):
    """Explicit host-command settings, separate from the natural-language goal."""

    mode: Literal["autonomous", "probe"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    max_parallel: int | None = Field(
        default=None, gt=0, exclude_if=lambda value: value is None
    )
    max_duration_seconds: int | None = Field(
        default=None, gt=0, exclude_if=lambda value: value is None
    )
    workspace_provider: WorkspaceProvider | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    promotion_mode: Literal["apply", "artifact_only"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    strategy: str | None = Field(
        default=None, min_length=1, exclude_if=lambda value: value is None
    )
    inner_agent: str | None = Field(
        default=None, min_length=1, exclude_if=lambda value: value is None
    )
    # Read-only compatibility for records created before main= was retired.
    main: str | None = Field(
        default=None,
        min_length=1,
        description="Legacy persisted main-model assertion; rejected for new commands.",
        exclude_if=lambda value: value is None,
    )
    annotator: str | None = Field(
        default=None, min_length=1, exclude_if=lambda value: value is None
    )
    workers: str | None = Field(
        default=None, min_length=1, exclude_if=lambda value: value is None
    )
    worker_directive: Literal["workers", "models"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def worker_directive_matches_workers(self) -> "GoalPlusCommandConfig":
        if (self.workers is None) != (self.worker_directive is None):
            raise ValueError(
                "workers and worker_directive must either both be set or both be absent"
            )
        return self


class GoalPlusGoalRevision(SearchModel):
    revision: int = Field(ge=1)
    raw_goal: str = Field(min_length=1)
    command_config: GoalPlusCommandConfig = Field(
        default_factory=GoalPlusCommandConfig
    )
    reason: str | None = None
    created_at: str


class GoalPlusFinalCheckerIdentity(SearchModel):
    goal_plus_id: str = Field(min_length=1)
    check_id: str = Field(min_length=1)
    goal_revision: int = Field(ge=1)


class GoalPlusFinalCheck(SearchModel):
    check_id: str = Field(min_length=1)
    goal_revision: int = Field(ge=1)
    checker_harness: GoalPlusFinalCheckerHost
    status: GoalPlusFinalCheckStatus = "pending"
    requested_phase: GoalPlusPhase
    requested_at: str
    completed_at: str | None = None
    summary: str | None = None
    findings: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    checker_metadata: dict[str, Any] = Field(default_factory=dict)


class GoalPlusTriage(SearchModel):
    is_optimization: bool
    confidence: GoalPlusConfidence
    recommended_phase: GoalPlusRecommendedPhase
    identified_at: GoalPlusDiscoveryOrigin = "initial"
    scenario: str | None = None
    reasons: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class GoalPlusSpecDraft(SearchModel):
    baseline: dict[str, Any]
    metric: dict[str, Any]
    correctness_gate: dict[str, Any]
    edit_surface: dict[str, Any]
    verifier_artifacts: list[str] = Field(default_factory=list)
    search_spec: SearchSpecDraft | dict[str, Any] = Field(union_mode="left_to_right")
    promotion_rule: str = Field(min_length=1)
    confidence: GoalPlusConfidence
    origin: GoalPlusDiscoveryOrigin | None = None
    open_questions: list[str] = Field(
        default_factory=list,
        description="Unresolved evaluation-contract questions. Use an empty list once resolved; do not retain resolved answers here.",
    )

    @field_serializer("search_spec")
    def serialize_search_spec(
        self, value: SearchSpecDraft | dict[str, Any]
    ) -> dict[str, Any]:
        if isinstance(value, SearchSpecDraft):
            return value.model_dump(mode="json", exclude_none=True)
        return value


class GoalPlusSpecDraftInput(GoalPlusSpecDraft):
    """Strict tool-input shape; persisted legacy drafts remain backward-readable."""

    search_spec: SearchSpecDraft = Field(
        description=(
            "The executable SearchSpec being prepared. Put process_verifiers, promotion_verifiers, "
            "strategy, workspace, budget, constraints and root_hypotheses inside this object. "
            "The enclosing draft contains discovery notes and confidence, not these execution settings."
        ),
    )


class GoalPlusLinkedSearch(SearchModel):
    goal_revision: int = Field(default=1, ge=1)
    frozen_spec_id: str | None = None
    run_id: str | None = None
    linked_at: str | None = None
    selected_candidate_id: str | None = None
    report_path: str | None = None
    html_report_path: str | None = None
    promotion_artifact_path: str | None = None
    summary: str | None = None
    result_recorded_at: str | None = None


class GoalPlusWorkItem(SearchModel):
    work_item_id: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    objective: str = Field(min_length=1, max_length=8000)
    goal_revision: int = Field(ge=1)
    status: GoalPlusWorkStatus
    attempt_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    generation: int = Field(default=0, ge=0)
    launch_deadline: str | None = None
    bound_at: str | None = None
    host: str | None = Field(default=None, min_length=1, max_length=120)
    task_name: str | None = None
    agent_id: str | None = None
    transcript_path: str | None = None
    result_summary: str | None = None
    result_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class GoalPlusActiveSession(SearchModel):
    agent_harness: AgentHarnessKind
    session_id: str = Field(min_length=1)
    transcript_path: str | None = None
    tool_use_id: str | None = None
    state: GoalPlusSessionState = "attached"
    attached_at: str
    last_seen_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class GoalPlusControl(SearchModel):
    """Task continuation authority, independent of native session attachment."""

    state: Literal["active", "paused", "closed"] = "active"
    reason: str | None = None
    version: int = Field(default=0, ge=0)


class GoalPlusResumeExpectation(SearchModel):
    """Conditional automatic resume, supplied by the owning host controller."""

    goal_plus_id: str = Field(min_length=1)
    goal_revision: int = Field(ge=1)
    session_id: str = Field(min_length=1)
    control_version: int = Field(ge=0)


class GoalPlusHostSessionDecision(SearchModel):
    goal_plus_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    record_status: GoalPlusStatus
    session_state: GoalPlusSessionState | Literal["terminal", "unavailable"]
    decision: GoalPlusHostSessionDecisionKind
    reason: str = Field(min_length=1)
    control: GoalPlusControl = Field(default_factory=GoalPlusControl)


class GoalPlusHostCommandInvocation(SearchModel):
    """Immutable evidence that a supported host command authorized one action."""

    agent_harness: GoalPlusEntrypointHost
    transport: Literal["codex_user_prompt_submit", "pi_extension_command"]
    command: Literal[
        "$goal-plus",
        "$goal-plus-with-final-check",
        "/goal-plus",
        "/goal-plus-with-final-check",
    ]
    action: GoalPlusEntrypointAction
    session_id: str = Field(min_length=1, max_length=512)
    native_entry_id: str | None = Field(default=None, min_length=1, max_length=512)
    invocation_id: str = Field(
        min_length=1,
        max_length=512,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    raw_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    invoked_at: str = Field(min_length=1)
    resume_expectation: GoalPlusResumeExpectation | None = None

    @model_validator(mode="after")
    def match_host_transport_and_command(self) -> "GoalPlusHostCommandInvocation":
        expected_transport = {
            "codex": "codex_user_prompt_submit",
            "pi": "pi_extension_command",
        }[self.agent_harness]
        if self.transport != expected_transport:
            raise ValueError(
                f"Goal Plus host {self.agent_harness} requires transport {expected_transport}"
            )
        expected_prefix = "$" if self.agent_harness == "codex" else "/"
        if not self.command.startswith(expected_prefix):
            raise ValueError(
                f"Goal Plus host {self.agent_harness} requires the {expected_prefix} command prefix"
            )
        if self.action != "start" and self.command not in {
            "$goal-plus",
            "/goal-plus",
        }:
            raise ValueError("Goal Plus controls must use the host's base goal-plus command")
        if self.resume_expectation is not None and self.action != "resume":
            raise ValueError("resume_expectation is only valid for resume")
        return self


class GoalSearchCreateRequest(SearchModel):
    request_id: str = Field(min_length=1)
    frozen_spec_id: str
    source_run_id: str | None = None
    fingerprint: str
    run_id: str
    goal_revision: int = Field(ge=1)
    control_version: int = Field(ge=0)
    state: Literal["prepared", "creating", "created", "linked", "failed"] = "prepared"
    error: str | None = None


class GoalPlusRecord(SearchModel):
    goal_plus_id: str
    raw_goal: str = Field(min_length=1)
    command_config: GoalPlusCommandConfig = Field(
        default_factory=GoalPlusCommandConfig
    )
    source_path: str | None = None
    outer_deadline_at: str | None = None
    status: GoalPlusStatus = "active"
    control: GoalPlusControl = Field(default_factory=GoalPlusControl)
    phase: GoalPlusPhase = "goal"
    policy: dict[str, Any] = Field(default_factory=dict)
    goal_revision: int = Field(default=1, ge=1)
    goal_revisions: list[GoalPlusGoalRevision] = Field(default_factory=list)
    final_checks: list[GoalPlusFinalCheck] = Field(default_factory=list)
    triage: GoalPlusTriage | None = None
    spec_draft: GoalPlusSpecDraft | None = None
    work_items: list[GoalPlusWorkItem] = Field(default_factory=list)
    search_tasks: list[GoalPlusLinkedSearch] = Field(default_factory=list)
    search_create_requests: list[GoalSearchCreateRequest] = Field(default_factory=list)
    linked_search: GoalPlusLinkedSearch | None = None
    next_action: GoalPlusNextAction | None = None
    active_session: GoalPlusActiveSession | None = None
    entrypoint_status: GoalPlusEntrypointStatus = "legacy_unverified"
    host_command_invocations: list[GoalPlusHostCommandInvocation] = Field(
        default_factory=list
    )
    hook_counters: dict[str, int] = Field(default_factory=dict)
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def synchronize_search_task_compatibility_view(self) -> "GoalPlusRecord":
        if self.host_command_invocations:
            self.entrypoint_status = "host_verified"
        if not self.goal_revisions:
            self.goal_revisions = [
                GoalPlusGoalRevision(
                    revision=self.goal_revision,
                    raw_goal=self.raw_goal,
                    reason="legacy record imported",
                    created_at=self.created_at,
                )
            ]
        latest_revision = self.goal_revisions[-1]
        if (
            latest_revision.revision != self.goal_revision
            or latest_revision.raw_goal != self.raw_goal
            or latest_revision.command_config != self.command_config
        ):
            raise ValueError(
                "raw_goal, command_config, and goal_revision must match the latest "
                "goal revision"
            )
        if not self.search_tasks and self.linked_search is not None:
            self.search_tasks = [self.linked_search.model_copy(deep=True)]
        elif self.search_tasks:
            latest_task = self.search_tasks[-1]
            self.linked_search = (
                latest_task.model_copy(deep=True)
                if latest_task.goal_revision == self.goal_revision
                else None
            )
        return self


class GoalPlusGateResult(SearchModel):
    decision: GoalPlusGateDecision
    phase: GoalPlusPhase
    status: GoalPlusStatus
    reason: str | None = None
    continuation_prompt: str | None = None
    goal_plus_id: str | None = None
    goal_revision: int | None = None
    control_version: int | None = None
    session_id: str | None = None
