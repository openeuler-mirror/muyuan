"""Durable identities for an offline candidate Evidence replay."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.base import SearchModel


class ReplayTarget(SearchModel):
    candidate_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    artifact_ref: ArtifactRef
    detail_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReplayForkPoint(SearchModel):
    schema_version: Literal[1] = 1
    fork_point_id: str = Field(pattern=r"^fork_[0-9a-f]{32}$")
    state: Literal["preparing", "ready", "unavailable"] = "preparing"
    reason: str | None = None
    run_id: str = Field(min_length=1)
    source_root: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    agent_session_id: str = Field(min_length=1)
    execution_generation: int = Field(ge=0)
    source_iteration_count: int = Field(ge=0)
    frozen_spec_id: str = Field(min_length=1)
    native_thread_id: str = Field(min_length=1)
    native_turn_id: str = Field(min_length=1)
    native_tool_call_id: str = Field(min_length=1)
    source: Literal["explicit", "verifier_injected"]
    operation_id: str = Field(min_length=1)
    global_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_provider: Literal["git_worktree", "copy"]
    workspace_head: str = Field(min_length=1)
    workspace_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str = Field(min_length=1)
    targets: list[ReplayTarget] = Field(default_factory=list)
    launch: dict[str, Any] = Field(default_factory=dict)
    remaining_runtime_seconds: int = Field(gt=0)
    native_prefix_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    native_outer_call_id: str | None = None

    @model_validator(mode="after")
    def state_has_proof(self) -> "ReplayForkPoint":
        if self.state == "ready" and (
            self.native_prefix_digest is None or self.native_outer_call_id is None
        ):
            raise ValueError("ready ForkPoint requires a verified native prefix")
        if self.state == "unavailable" and not self.reason:
            raise ValueError("unavailable ForkPoint requires a reason")
        keys = [(item.candidate_id, item.iteration) for item in self.targets]
        if len(keys) != len(set(keys)):
            raise ValueError("ForkPoint target identities must be unique")
        return self


class ReplayBranch(SearchModel):
    branch_id: str = Field(pattern=r"^branch_[0-9a-f]{32}$")
    target: ReplayTarget
    state: Literal["created", "running", "completed", "failed"] = "created"
    native_thread_id: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class ReplayExperiment(SearchModel):
    schema_version: Literal[1] = 1
    experiment_id: str = Field(pattern=r"^replay_[0-9a-f]{32}$")
    fork_point_id: str = Field(pattern=r"^fork_[0-9a-f]{32}$")
    request_id: str = Field(min_length=1)
    created_at: str = Field(min_length=1)
    state: Literal["created", "running", "completed", "failed"] = "created"
    cleaned_at: str | None = None
    branches: list[ReplayBranch] = Field(min_length=1)

    @model_validator(mode="after")
    def selected_targets_are_unique(self) -> "ReplayExperiment":
        keys = [(item.target.candidate_id, item.target.iteration) for item in self.branches]
        if len(keys) != len(set(keys)):
            raise ValueError("ReplayExperiment cannot select one Evidence twice")
        return self
