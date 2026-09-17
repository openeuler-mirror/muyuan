"""Main's freeze input; runtime-owned fields remain in the persisted models."""

from typing import Literal

from pydantic import Field

from goal_plus.domain.adaptive.deterministic import PriorityReplaceInput
from goal_plus.domain.agents import ModelRequest, WorkerBudgetInput, WorkerLaunchDefaults
from goal_plus.domain.search import BudgetInput, SearchSpec, StrategyOptions, VerifierInput
from goal_plus.domain.shared_tools import SharedDirInput


class StrategyInput(StrategyOptions):
    name: Literal["agent_guided", "random"] = Field(
        default="agent_guided", description="agent_guided requires candidate proposals; random does not.",
    )
    inner_agent: Literal["autoresearch", "common"] | None = None
    models: list[ModelRequest] = Field(
        default_factory=list,
        description="At most max_parallel entries. Set count on all entries (sum=max_parallel), or omit all for even allocation.",
    )
    worker_budget: WorkerBudgetInput | None = Field(
        default=None, description="Omit for no limit; otherwise set at least one positive limit per worker.",
    )
    worker_launch: WorkerLaunchDefaults | None = None
    allocation: PriorityReplaceInput | None = Field(
        default=None, description="Required only for adaptive_search; requires git_worktree.",
    )
    global_evidence_mode: Literal["manual", "auto", "independent"] | None = None


class SearchSpecInput(SearchSpec):
    budget: BudgetInput
    process_verifiers: list[VerifierInput] = Field(min_length=1)
    promotion_verifiers: list[VerifierInput] = Field(default_factory=list)
    strategy: StrategyInput = Field(default_factory=StrategyInput)
    shared_dir: SharedDirInput = Field(default_factory=SharedDirInput)

    def to_runtime_spec(self) -> SearchSpec:
        payload = self.model_dump(mode="json")
        mode = payload["strategy"].pop("global_evidence_mode")
        if mode is not None:
            payload["strategy"]["config"] = {"global_evidence_mode": mode}
        return SearchSpec.model_validate(payload)
