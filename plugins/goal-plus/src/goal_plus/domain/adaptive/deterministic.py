"""Replayable Adaptive Search snapshots, features, and allocation policy."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import Field, field_validator, model_validator

from goal_plus.domain.agents import WorkerBudget
from goal_plus.domain.base import SearchModel
from goal_plus.domain.serialization import canonical_json, sha256_text
from goal_plus.domain.verifier import NormalizedAllocationScore


@dataclass(frozen=True)
class PolicyVerifierRules:
    policy_id: str
    instructions: str
    scores: tuple[tuple[str, str], ...]

    @property
    def score_names(self) -> list[str]:
        return [name for name, _instructions in self.scores]


class PriorityReplaceInput(SearchModel):
    features: list[Literal["latest_record_values/v1", "progress_trend/v1"]] = Field(
        default_factory=lambda: ["latest_record_values/v1", "progress_trend/v1"],
        min_length=1,
        json_schema_extra={"uniqueItems": True},
    )
    switch_margin: float = Field(default=0.15, ge=0, le=1, allow_inf_nan=False)

    @field_validator("features")
    @classmethod
    def features_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("adaptive allocation features must be unique")
        return value


class PriorityReplacePolicySpec(PriorityReplaceInput):
    policy: Literal["priority_replace/v1"] = "priority_replace/v1"

    def verifier_rules(self) -> PolicyVerifierRules:
        return PolicyVerifierRules(
            policy_id=self.policy,
            instructions=(
                "Produce every allocation score required by priority_replace/v1 as a "
                "normalized utility in the inclusive range [0, 1], where higher is "
                "always better. Do not replace or reinterpret quality_score."
            ),
            scores=(
                (
                    "progress_reward",
                    "Estimate the new local value present in the exact attempt artifact, "
                    "including useful partial progress that a binary or coarse hard score "
                    "may hide.",
                ),
                (
                    "allocation_utility",
                    "Estimate the value of continuing search from the exact attempt "
                    "artifact, regardless of whether settlement kept, retained, discarded, "
                    "or failed it.",
                ),
            ),
        )


class SnapshotNode(SearchModel):
    candidate_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    settlement_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    verifier_record_id: str = Field(min_length=1)
    verifier_subject_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    quality_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    allocation_scores: dict[str, NormalizedAllocationScore]


class SnapshotCandidate(SearchModel):
    candidate_id: str = Field(min_length=1)
    allocation_eligibility: Literal["eligible", "retired"]
    allocation_depth: int = Field(ge=0)
    nodes: list[SnapshotNode] = Field(default_factory=list)


class SearchSnapshot(SearchModel):
    schema_version: Literal[2] = 2
    snapshot_revision: str = Field(pattern=r"^snapshot_[0-9a-f]{24}$")
    run_id: str = Field(min_length=1)
    frozen_spec_id: str = Field(min_length=1)
    policy_id: str = Field(min_length=1)
    max_parallel: int = Field(ge=1)
    max_candidates: int | None = Field(default=None, ge=1)
    candidates_total: int = Field(ge=0)
    candidates: list[SnapshotCandidate]
    historical_decision_ids: list[str] = Field(default_factory=list)
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def identity_matches_contents(self) -> "SearchSnapshot":
        body = self.model_dump(
            mode="json", exclude={"snapshot_revision", "input_hash"}
        )
        revision, digest = snapshot_identity(body)
        if self.snapshot_revision != revision or self.input_hash != digest:
            raise ValueError("search snapshot identity does not match its contents")
        return self


class CandidateFeatures(SearchModel):
    candidate_id: str = Field(min_length=1)
    latest_quality: float | None = Field(
        default=None, ge=0, le=1, allow_inf_nan=False
    )
    latest_allocation_utility: NormalizedAllocationScore | None = None
    progress_trend: NormalizedAllocationScore | None = None


class FeatureView(SearchModel):
    schema_version: Literal[2] = 2
    snapshot_revision: str = Field(min_length=1)
    provider_ids: list[str]
    candidates: list[CandidateFeatures]
    feature_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def hash_matches_contents(self) -> "FeatureView":
        body = self.model_dump(mode="json", exclude={"feature_hash"})
        if self.feature_hash != sha256_text(canonical_json(body)):
            raise ValueError("feature view hash does not match its contents")
        return self


class AllocationExpansionIntent(SearchModel):
    source_candidate_id: str = Field(min_length=1)
    source_iteration: int = Field(ge=1)
    source_settlement_id: str = Field(min_length=1)
    source_node_id: str = Field(min_length=1)
    source_verifier_record_id: str = Field(min_length=1)
    direction_key: str = Field(min_length=1, max_length=256)
    rationale: str = Field(min_length=1, max_length=2000)
    worker_budget: WorkerBudget | None = None


class AllocationPlan(SearchModel):
    schema_version: Literal[2] = 2
    based_on_snapshot_revision: str = Field(min_length=1)
    feature_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_id: str = Field(min_length=1)
    retire_candidate_ids: list[str] = Field(min_length=1)
    expansions: list[AllocationExpansionIntent] = Field(min_length=1)
    reason_codes: list[str] = Field(min_length=1)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def logical_targets_are_unique(self) -> "AllocationPlan":
        if len(self.retire_candidate_ids) != len(set(self.retire_candidate_ids)):
            raise ValueError("allocation plan cannot retire a candidate twice")
        keys = [item.direction_key for item in self.expansions]
        if len(keys) != len(set(keys)):
            raise ValueError("allocation expansion direction keys must be unique")
        body = self.model_dump(mode="json", exclude={"plan_hash"})
        if self.plan_hash != sha256_text(canonical_json(body)):
            raise ValueError("allocation plan hash does not match its contents")
        return self


def snapshot_identity(payload: dict[str, object]) -> tuple[str, str]:
    input_hash = sha256_text(canonical_json(payload))
    return f"snapshot_{input_hash[:24]}", input_hash


def compute_feature_view(
    snapshot: SearchSnapshot, provider_ids: list[str]
) -> FeatureView:
    supported = {"latest_record_values/v1", "progress_trend/v1"}
    if len(provider_ids) != len(set(provider_ids)) or set(provider_ids) - supported:
        raise ValueError("feature pipeline contains unknown or duplicate providers")
    features: list[CandidateFeatures] = []
    latest_values_enabled = "latest_record_values/v1" in provider_ids
    progress_trend_enabled = "progress_trend/v1" in provider_ids
    for candidate in snapshot.candidates:
        latest = candidate.nodes[-1] if candidate.nodes else None
        quality = (
            latest.quality_score
            if latest_values_enabled
            and latest is not None
            else None
        )
        utility = (
            latest.allocation_scores.get("allocation_utility")
            if latest_values_enabled
            and latest is not None
            else None
        )
        progress_values = [
            value
            for node in candidate.nodes[-3:]
            if (value := node.allocation_scores.get("progress_reward")) is not None
        ]
        trend = (
            sum(progress_values) / len(progress_values)
            if progress_trend_enabled and progress_values
            else None
        )
        features.append(
            CandidateFeatures(
                candidate_id=candidate.candidate_id,
                latest_quality=quality,
                latest_allocation_utility=utility,
                progress_trend=trend,
            )
        )
    body = {
        "schema_version": 2,
        "snapshot_revision": snapshot.snapshot_revision,
        "provider_ids": provider_ids,
        "candidates": [item.model_dump(mode="json") for item in features],
    }
    return FeatureView(
        **body,
        feature_hash=sha256_text(canonical_json(body)),
    )


def compute_allocation_plan(
    snapshot: SearchSnapshot,
    features: FeatureView,
    *,
    policy: PriorityReplacePolicySpec,
) -> AllocationPlan | None:
    if snapshot.snapshot_revision != features.snapshot_revision:
        raise ValueError("feature view belongs to another snapshot")
    if snapshot.policy_id != policy.policy:
        raise ValueError("allocation policy does not match the snapshot")
    if features.provider_ids != policy.features:
        raise ValueError("feature view does not match the allocation policy")
    active_ids = {
        item.candidate_id
        for item in snapshot.candidates
        if item.allocation_eligibility == "eligible"
    }
    if len(active_ids) < snapshot.max_parallel:
        return None
    if (
        snapshot.max_candidates is not None
        and snapshot.candidates_total >= snapshot.max_candidates
    ):
        return None

    def lane_priority(item: CandidateFeatures) -> float | None:
        values = [
            value
            for value in (
                item.latest_allocation_utility,
                item.progress_trend,
            )
            if value is not None
        ]
        return sum(values) / len(values) if values else None

    comparable = [
        (priority, item)
        for item in features.candidates
        if item.candidate_id in active_ids
        and (priority := lane_priority(item)) is not None
    ]
    if len(comparable) != len(active_ids):
        return None
    retire_priority, retire = min(
        comparable, key=lambda pair: (pair[0], pair[1].candidate_id)
    )
    source_rows = [
        (node.allocation_scores["allocation_utility"], node)
        for candidate in snapshot.candidates
        for node in candidate.nodes
        if "allocation_utility" in node.allocation_scores
    ]
    if not source_rows:
        return None

    source_utility, source_node = max(
        source_rows,
        key=lambda pair: (
            pair[0],
            pair[1].candidate_id,
            pair[1].iteration,
            pair[1].settlement_id,
            pair[1].verifier_record_id,
        ),
    )
    if source_utility - retire_priority < policy.switch_margin:
        return None
    direction_key = re.sub(
        r"[^a-zA-Z0-9_.:-]", "_",
        f"priority_replace:v1:{source_node.node_id}:{retire.candidate_id}",
    )
    expansion = AllocationExpansionIntent(
        source_candidate_id=source_node.candidate_id,
        source_iteration=source_node.iteration,
        source_settlement_id=source_node.settlement_id,
        source_node_id=source_node.node_id,
        source_verifier_record_id=source_node.verifier_record_id,
        direction_key=direction_key,
        rationale="replace the lowest-priority lane from the strongest scored attempt",
    )
    body = {
        "schema_version": 2,
        "based_on_snapshot_revision": snapshot.snapshot_revision,
        "feature_hash": features.feature_hash,
        "policy_id": snapshot.policy_id,
        "retire_candidate_ids": [retire.candidate_id],
        "expansions": [expansion.model_dump(mode="json")],
        "reason_codes": ["priority_gap_exceeds_switch_margin"],
    }
    return AllocationPlan(
        **body,
        plan_hash=sha256_text(canonical_json(body)),
    )


__all__ = [
    "AllocationExpansionIntent",
    "AllocationPlan",
    "CandidateFeatures",
    "FeatureView",
    "PolicyVerifierRules",
    "PriorityReplacePolicySpec",
    "SearchSnapshot",
    "SnapshotCandidate",
    "SnapshotNode",
    "compute_allocation_plan",
    "compute_feature_view",
    "snapshot_identity",
]
