"""Main's freeze input; runtime-owned fields remain in the persisted models."""

from typing import Literal

from pydantic import Field

from goal_plus.domain.adaptive.policy import AllocationPolicySpec
from goal_plus.domain.agents import ModelRequest, WorkerBudgetInput
from goal_plus.domain.search import SearchSpec, StrategyOptions
from goal_plus.domain.shared_tools import SharedDirInput
from goal_plus.domain.shared_cache import SharedCacheSpec


class StrategyInput(StrategyOptions):
    models: list[ModelRequest] = Field(
        default_factory=list,
        description="At most max_parallel entries. Set count on all entries (sum=max_parallel), or omit all for even allocation.",
    )
    worker_budget: WorkerBudgetInput | None = Field(
        default=None, description="Omit for no limit; otherwise set at least one positive limit per worker.",
    )
    allocation: AllocationPolicySpec | None = Field(
        default=None, description=(
            "Select a policy to enable adaptive_search; requires git_worktree or thinkthread. "
            "Required verifier scores and policy parameter defaults are filled automatically."
        ),
    )
    global_evidence_mode: Literal["manual", "auto", "independent"] | None = None


class SearchSpecInput(SearchSpec):
    strategy: StrategyInput = Field(default_factory=StrategyInput)
    shared_dir: SharedDirInput = Field(default_factory=SharedDirInput)
    shared_cache: SharedCacheSpec = Field(default_factory=SharedCacheSpec)

    def to_runtime_spec(self) -> SearchSpec:
        payload = self.model_dump(mode="json")
        mode = payload["strategy"].pop("global_evidence_mode")
        if mode is not None:
            payload["strategy"]["config"] = {"global_evidence_mode": mode}
        return SearchSpec.model_validate(payload)
