"""Versioned allocation policy specifications and deterministic dispatch."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, ClassVar, Literal

from pydantic import Field

from goal_plus.domain.base import SearchModel

if TYPE_CHECKING:
    from goal_plus.domain.adaptive.models import AllocationPlan, SearchSnapshot


class SweSearchUctPolicyParameters(SearchModel):
    exploration_weight: float = Field(default=0.01, ge=0, allow_inf_nan=False)
    early_depth_bonus_weight: float = Field(default=2.0, ge=0, allow_inf_nan=False)
    early_depth_bonus_decay: float = Field(default=0.8, ge=0, allow_inf_nan=False)
    late_depth_penalty_weight: float = Field(default=0.008, ge=0, allow_inf_nan=False)
    low_reward_threshold: float = Field(default=0.0, ge=-1, le=1, allow_inf_nan=False)
    low_reward_streak: int = Field(default=2, ge=1)
    max_children_per_node: int = Field(default=3, ge=1)
    max_depth: int = Field(default=20, ge=1)


class SweSearchUctPolicySpec(SearchModel):
    required_score_keys: ClassVar[tuple[str, ...]] = ("progress_reward", "allocation_utility")
    policy: Literal["swe_search_uct/v1"]
    parameters: SweSearchUctPolicyParameters = Field(
        default_factory=SweSearchUctPolicyParameters
    )

AllocationPolicySpec = Annotated[
    SweSearchUctPolicySpec,
    Field(discriminator="policy", json_schema_extra={"required": ["policy"]}),
]


def evaluate_allocation_policy(
    snapshot: SearchSnapshot,
    *,
    policy: AllocationPolicySpec,
) -> AllocationPlan | None:
    """Dispatch one frozen policy spec to its deterministic implementation."""

    from goal_plus.domain.adaptive.swe_search_uct import evaluate_swe_search_uct_v1

    return evaluate_swe_search_uct_v1(snapshot, policy=policy)

__all__ = [
    "AllocationPolicySpec",
    "SweSearchUctPolicyParameters",
    "SweSearchUctPolicySpec",
    "evaluate_allocation_policy",
]
