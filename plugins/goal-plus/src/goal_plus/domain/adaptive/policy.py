"""Versioned Allocation/Steering specifications and deterministic dispatch."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, ClassVar, Literal

from pydantic import Field, model_validator

from goal_plus.domain.base import SearchModel
from goal_plus.domain.adaptive.models import SteeringDeliverySpec, SteeringLimits

if TYPE_CHECKING:
    from goal_plus.domain.adaptive.search_diagnostic_guidance import SearchDiagnosticInput, DiagnosticEvaluation
    from goal_plus.domain.adaptive.models import AllocationPlan, SearchSnapshot, SteeringPolicyIdentity


class SearchDiagnosticGuidanceParameters(SteeringLimits):
    # Initial v1 defaults follow the documented diagnostic example, not a calibrated benchmark.
    correctness_gap_threshold: float = Field(default=0.6, gt=0, le=1, allow_inf_nan=False)
    validation_gap_threshold: float = Field(default=0.6, gt=0, le=1, allow_inf_nan=False)
    confounder_threshold: float = Field(default=0.7, gt=0, le=1, allow_inf_nan=False)
    test_gap_threshold: float = Field(default=0.7, gt=0, le=1, allow_inf_nan=False)
    boundary_probe_threshold: float = Field(default=0.4, gt=0, le=1, allow_inf_nan=False)
    boundary_confirmed_threshold: float = Field(default=0.9, gt=0, le=1, allow_inf_nan=False)
    history_limit: int = Field(default=16, gt=0, strict=True)
    cooldown_seconds: float = Field(default=30, ge=0, allow_inf_nan=False)
    max_steering_attempts_per_candidate: int = Field(default=4, gt=0, strict=True)
    max_steering_attempts_per_issue: int = Field(default=2, gt=0, strict=True)

    @model_validator(mode="after")
    def ordered_limits(self):
        if self.boundary_probe_threshold >= self.boundary_confirmed_threshold:
            raise ValueError("boundary probe threshold must be below confirmed threshold")
        return self


class SearchDiagnosticGuidancePolicySpec(SearchModel):
    required_score_keys: ClassVar[tuple[str, ...]] = (
        "correctness_gap", "validation_gap", "local_confounder_evidence",
        "discriminating_test_gap", "boundary_evidence_strength",
    )
    policy: Literal["search_diagnostic_guidance/v1"]
    parameters: SearchDiagnosticGuidanceParameters = Field(default_factory=SearchDiagnosticGuidanceParameters)
    delivery: SteeringDeliverySpec = Field(default_factory=SteeringDeliverySpec)

    @property
    def identity(self) -> SteeringPolicyIdentity:
        from goal_plus.domain.adaptive.models import SteeringPolicyIdentity
        from goal_plus.domain.serialization import canonical_json, sha256_text
        return SteeringPolicyIdentity(policy=self.policy,
            parameters_hash=sha256_text(canonical_json(self.parameters.model_dump(mode="json"))))


SteeringPolicySpec = Annotated[
    SearchDiagnosticGuidancePolicySpec,
    Field(discriminator="policy", json_schema_extra={"required": ["policy"]}),
]


def evaluate_steering_policy(input: SearchDiagnosticInput, *, policy: SteeringPolicySpec) -> DiagnosticEvaluation:
    """Dispatch one frozen policy spec to its deterministic implementation."""

    from goal_plus.domain.adaptive.search_diagnostic_guidance import evaluate_search_diagnostic_guidance
    return evaluate_search_diagnostic_guidance(input, parameters=policy.parameters)


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
    "SteeringPolicySpec",
    "SearchDiagnosticGuidancePolicySpec",
    "SearchDiagnosticGuidanceParameters",
    "evaluate_steering_policy",
    "SweSearchUctPolicyParameters",
    "SweSearchUctPolicySpec",
    "evaluate_allocation_policy",
]
