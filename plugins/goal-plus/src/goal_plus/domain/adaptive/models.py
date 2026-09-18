"""Adaptive facts and durable execution identities; no evaluators or policy imports."""

from __future__ import annotations
import math
from typing import Literal
from pydantic import ConfigDict, Field, field_validator, model_validator
from goal_plus.domain.agents import WorkerBudget
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.base import SearchModel
from goal_plus.domain.serialization import canonical_json, sha256_text
from goal_plus.domain.verifier import VerifierSubject
from goal_plus.domain.time import timestamp_epoch


class SteeringPolicyIdentity(SearchModel):
    model_config = ConfigDict(frozen=True)
    policy: str = Field(min_length=1)
    parameters_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class SteeringDeliverySpec(SearchModel):
    model_config = ConfigDict(frozen=True)
    rpc_timeout_seconds: float = Field(default=2, gt=0, allow_inf_nan=False)
    max_source_age_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)
    mode: Literal["shadow", "apply"] = "apply"


class SteeringLimits(SearchModel):
    """Execution admission limits, independent of a diagnostic strategy's thresholds."""
    model_config = ConfigDict(frozen=True)
    cooldown_seconds: float = Field(ge=0, allow_inf_nan=False)
    max_steering_attempts_per_candidate: int = Field(gt=0, strict=True)
    max_steering_attempts_per_issue: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def issue_limit_is_bounded(self):
        if self.max_steering_attempts_per_issue > self.max_steering_attempts_per_candidate:
            raise ValueError("issue limit cannot exceed candidate limit")
        return self


class SteeringMessage(SearchModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["candidate_message"] = "candidate_message"
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def text_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("steering message must not be blank")
        return value


class SteeringDecision(SearchModel):
    """One soft suggestion, not a recoverable work item."""

    model_config = ConfigDict(frozen=True)
    source_record_id: str = Field(min_length=1)
    subject_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_id: str = Field(min_length=1)
    message: SteeringMessage
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    issue_key: str | None = None
    evidence_revision: str | None = None
    input_hash: str | None = None


class SteeringTarget(SearchModel):
    model_config = ConfigDict(frozen=True)
    agent_session_id: str = Field(min_length=1)
    execution_generation: int = Field(ge=0)
    native_session_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1)


class SteeringAttempt(SearchModel):
    """A spent sending opportunity; missing observation never authorizes resending."""

    schema_version: Literal[1] = 1
    attempt_key: str = Field(pattern=r"^steering_[0-9a-f]{24}$")
    source_record_id: str = Field(min_length=1)
    source: VerifierSubject
    policy: SteeringPolicyIdentity
    delivery: SteeringDeliverySpec
    target: SteeringTarget
    message: SteeringMessage
    message_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    renderer_version: str
    consumed_at: str
    observation: Literal["returned", "unknown", "not_sent"] | None = None
    issue_key: str | None = None
    evidence_revision: str | None = None
    input_hash: str | None = None
    action: str | None = None

    @model_validator(mode="after")
    def identity_matches_contents(self) -> "SteeringAttempt":
        expected = steering_attempt_key(self.source_record_id, self.source, self.policy,
                                        self.delivery, self.renderer_version)
        if self.attempt_key != expected or self.message_hash != sha256_text(self.message.text):
            raise ValueError("steering attempt identity or message hash mismatch")
        return self


def steering_attempt_key(record_id: str, subject: VerifierSubject,
                         policy: SteeringPolicyIdentity, delivery: SteeringDeliverySpec,
                         renderer_version: str) -> str:
    # Match the existing Allocation/Snapshot 96-bit filename identity convention.
    return "steering_" + sha256_text(canonical_json({
        "record_id": record_id, "subject": subject.model_dump(mode="json"),
        "policy": policy.model_dump(mode="json"), "delivery": delivery.model_dump(mode="json"),
        "renderer_version": renderer_version,
    }))[:24]


def steering_limit_reason(usage: tuple[SteeringAttempt, ...], *, issue_key: str,
                          evidence_revision: str, now: float,
                          parameters: SteeringLimits) -> str | None:
    # Full candidate history, not the bounded experiment window; unknown sends still count.
    if len(usage) >= parameters.max_steering_attempts_per_candidate:
        return "candidate_limit"
    timestamps = [timestamp_epoch(item.consumed_at) for item in usage]
    if any(value is None for value in timestamps):
        return "invalid_usage"
    if timestamps and now - max(timestamps) < parameters.cooldown_seconds:
        return "cooldown"
    issue_usage = [item for item in usage if item.issue_key == issue_key]
    if any(item.evidence_revision == evidence_revision for item in issue_usage):
        return "same_evidence_consumed"
    if len(issue_usage) >= parameters.max_steering_attempts_per_issue:
        return "issue_limit"
    return None

AllocationEligibility = Literal["eligible", "retired"]
AllocationDecisionStatus = Literal["pending", "interrupted", "applied", "superseded", "terminal_error"]

class ExpansionSource(SearchModel):
    """Exact scored attempt used to materialize a derived candidate."""

    candidate_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    settlement_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    verifier_record_id: str = Field(min_length=1)
    attempt_artifact_ref: ArtifactRef
    attempt_artifact_hash: str = Field(min_length=1)
    ledger_artifact_ref: ArtifactRef
    allocation_depth: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def identities_are_consistent(self) -> "ExpansionSource":
        expected_node_id = f"{self.candidate_id}/iteration-{self.iteration:04d}"
        if self.node_id != expected_node_id:
            raise ValueError("expansion source node identity does not match")
        if self.attempt_artifact_ref.provider != self.ledger_artifact_ref.provider:
            raise ValueError("expansion source artifact providers must match")
        return self


class SnapshotNode(SearchModel):
    candidate_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    node_id: str = Field(min_length=1)
    parent_node_id: str | None = Field(default=None, min_length=1)
    edge_kind: Literal["initial", "continuation", "allocation_expansion"]
    settlement_id: str = Field(min_length=1)
    verifier_record_id: str = Field(min_length=1)
    verifier_subject_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: ExpansionSource | None = None
    quality_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    allocation_scores: dict[str, float]

    @field_validator("allocation_scores")
    @classmethod
    def allocation_scores_are_finite(cls, value: dict[str, float]) -> dict[str, float]:
        if any(not math.isfinite(item) for item in value.values()):
            raise ValueError("snapshot allocation scores must be finite")
        return value


class SnapshotCandidate(SearchModel):
    candidate_id: str = Field(min_length=1)
    allocation_depth: int = Field(ge=0)
    nodes: list[SnapshotNode] = Field(default_factory=list)
    tip_node_id: str | None = Field(default=None, min_length=1)
    endpoint: ArtifactRef | None = None
    owner_known: bool = True
    root_parent_id: str | None = None
    last_iteration: int = Field(default=0, ge=0)


class SearchSnapshot(SearchModel):
    schema_version: Literal[6] = 6
    snapshot_revision: str = Field(pattern=r"^snapshot_[0-9a-f]{24}$")
    run_id: str = Field(min_length=1)
    frozen_spec_id: str = Field(min_length=1)
    policy_id: str = Field(min_length=1)
    max_parallel: int = Field(ge=1)
    verifier_cutoff_sequence: int = Field(ge=1)
    candidates: list[SnapshotCandidate]
    verifier_record_ids: list[str] = Field(default_factory=list)
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def contents_are_canonical_and_identified(self) -> "SearchSnapshot":
        candidate_ids = [item.candidate_id for item in self.candidates]
        if candidate_ids != sorted(candidate_ids) or len(candidate_ids) != len(
            set(candidate_ids)
        ):
            raise ValueError("snapshot candidates must be unique and canonical")

        all_nodes: dict[str, SnapshotNode] = {}
        for candidate in self.candidates:
            keys = [
                (node.iteration, node.settlement_id, node.verifier_record_id)
                for node in candidate.nodes
            ]
            if keys != sorted(keys):
                raise ValueError("snapshot nodes must be canonical")
            for node in candidate.nodes:
                if node.candidate_id != candidate.candidate_id:
                    raise ValueError("snapshot node candidate identity does not match")
                expected_id = f"{candidate.candidate_id}/iteration-{node.iteration:04d}"
                if node.node_id != expected_id or node.node_id in all_nodes:
                    raise ValueError("snapshot node identity is invalid or duplicated")
                all_nodes[node.node_id] = node

        for candidate in self.candidates:
            if candidate.tip_node_id is not None:
                tip = all_nodes.get(candidate.tip_node_id)
                if tip is None or tip.candidate_id != candidate.candidate_id:
                    raise ValueError("snapshot candidate tip identity is inconsistent")

        for node in all_nodes.values():
            if node.edge_kind == "initial":
                if node.parent_node_id is not None:
                    raise ValueError("initial snapshot node cannot have a parent")
                continue
            if node.parent_node_id is None:
                raise ValueError("non-initial snapshot node requires a parent")
            parent = all_nodes.get(node.parent_node_id)
            if parent is None:
                raise ValueError("snapshot node has a missing parent")
            if parent.node_id == node.node_id:
                raise ValueError("snapshot node cannot parent itself")
            if (
                node.edge_kind == "allocation_expansion"
                and parent.candidate_id == node.candidate_id
            ):
                raise ValueError("allocation expansion must cross candidate lineage")
            if (
                node.edge_kind == "continuation"
                and parent.candidate_id != node.candidate_id
            ):
                raise ValueError("continuation must stay within candidate lineage")

        for node in all_nodes.values():
            seen: set[str] = set()
            current: SnapshotNode | None = node
            while current is not None:
                if current.node_id in seen:
                    raise ValueError("snapshot lineage contains a cycle")
                seen.add(current.node_id)
                current = (
                    all_nodes[current.parent_node_id]
                    if current.parent_node_id is not None
                    else None
                )

        body = self.model_dump(
            mode="json", exclude={"snapshot_revision", "input_hash"}
        )
        revision, digest = snapshot_identity(body)
        if self.snapshot_revision != revision or self.input_hash != digest:
            raise ValueError("search snapshot identity does not match its contents")
        return self


class AllocationExpansionIntent(SearchModel):
    source_candidate_id: str = Field(min_length=1)
    source_iteration: int = Field(ge=1)
    source_settlement_id: str = Field(min_length=1)
    source_node_id: str = Field(min_length=1)
    source_verifier_record_id: str = Field(min_length=1)
    eligible_retirements: list[str] = Field(min_length=1)
    direction_key: str = Field(min_length=1, max_length=256)
    rationale: str = Field(min_length=1, max_length=2000)
    worker_budget: WorkerBudget | None = None


class AllocationPlan(SearchModel):
    schema_version: Literal[5] = 5
    based_on_snapshot_revision: str = Field(min_length=1)
    policy_id: str = Field(min_length=1)
    retire_candidate_ids: list[str] = Field(min_length=1)
    expansions: list[AllocationExpansionIntent] = Field(min_length=1)
    reason_codes: list[str] = Field(min_length=1)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def logical_targets_are_unique(self) -> "AllocationPlan":
        if len(self.retire_candidate_ids) != len(set(self.retire_candidate_ids)):
            raise ValueError("allocation plan cannot retire a candidate twice")
        for expansion in self.expansions:
            if not set(expansion.eligible_retirements) <= set(self.retire_candidate_ids):
                raise ValueError("expansion retirement conditions must belong to the plan")
        keys = [item.direction_key for item in self.expansions]
        if len(keys) != len(set(keys)):
            raise ValueError("allocation expansion direction keys must be unique")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("allocation reason codes must be unique")
        body = self.model_dump(mode="json", exclude={"plan_hash"})
        if self.plan_hash != sha256_text(canonical_json(body)):
            raise ValueError("allocation plan hash does not match its contents")
        return self


def snapshot_identity(payload: dict[str, object]) -> tuple[str, str]:
    input_hash = sha256_text(canonical_json(payload))
    return f"snapshot_{input_hash[:24]}", input_hash


class AllocationExpansion(SearchModel):
    expansion_id: str = Field(min_length=1)
    source: ExpansionSource
    eligible_retirements: list[str] = Field(min_length=1)
    direction_key: str = Field(min_length=1, max_length=256)
    rationale: str = Field(min_length=1, max_length=2000)
    worker_budget: WorkerBudget | None = None
    new_candidate_id: str | None = Field(default=None, min_length=1)
    status: Literal["pending", "materializing", "dispatching", "applied", "failed", "skipped"] = "pending"
    error: str | None = None


class AllocationDecision(SearchModel):
    """Durable execution intent emitted only by deterministic policy."""

    schema_version: Literal[5] = 5
    decision_id: str = Field(min_length=1, pattern=r"^allocation_[0-9a-f]{24}$")
    run_id: str = Field(min_length=1)
    based_on_snapshot_revision: str = Field(min_length=1)
    policy_id: str = Field(min_length=1)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    retire_candidate_ids: list[str] = Field(min_length=1)
    expansions: list[AllocationExpansion] = Field(min_length=1)
    reason_codes: list[str] = Field(min_length=1)
    status: AllocationDecisionStatus = "pending"
    error: str | None = None
    created_at: str
    applied_at: str | None = None
    verifier_sequence: int = Field(ge=1)
    selected_retirement: str | None = None
    retirement_completed: bool = False

    @model_validator(mode="after")
    def state_and_targets_are_consistent(self) -> "AllocationDecision":
        if len(self.retire_candidate_ids) != len(set(self.retire_candidate_ids)):
            raise ValueError("allocation decision cannot retire a candidate twice")
        if self.selected_retirement is not None and self.selected_retirement not in self.retire_candidate_ids:
            raise ValueError("selected retirement must belong to the decision")
        if self.retirement_completed and self.selected_retirement is None:
            raise ValueError("completed retirement requires an exact selected target")
        for expansion in self.expansions:
            if not set(expansion.eligible_retirements) <= set(self.retire_candidate_ids):
                raise ValueError("expansion retirement conditions must belong to the decision")
        expansion_ids = [item.expansion_id for item in self.expansions]
        if len(expansion_ids) != len(set(expansion_ids)):
            raise ValueError("allocation decision expansion ids must be unique")
        child_ids = [
            item.new_candidate_id
            for item in self.expansions
            if item.new_candidate_id is not None
        ]
        if len(child_ids) != len(set(child_ids)):
            raise ValueError("allocation decision candidate ids must be unique")
        if self.status in {"superseded", "terminal_error"} and not self.error:
            raise ValueError("terminal allocation decision requires an error")
        if self.status == "applied" and self.applied_at is None:
            raise ValueError("applied allocation decision requires materialized expansions")
        return self
