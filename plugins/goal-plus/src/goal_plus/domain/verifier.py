"""Fixed LLM Verifier quality rules and immutable assessment contracts."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from goal_plus.domain.base import SearchModel
from goal_plus.domain.serialization import canonical_json, sha256_text
from goal_plus.domain.settlement import SettlementReceipt


VerifierTaskState = Literal[
    "pending", "running", "retry_wait", "completed", "terminal_error"
]
NormalizedAllocationScore = Annotated[
    float,
    Field(ge=0.0, le=1.0, allow_inf_nan=False),
]


QUALITY_CRITERIA = (
    "objective_completion",
    "correctness_and_acceptance",
    "evidence_and_risk",
    "integration_and_complexity",
)
QUALITY_INPUT_PROJECTION = (
    "objective",
    "candidate_acceptance",
    "candidate_hypothesis",
    "bounded_artifact_diff",
    "artifact_manifest",
    "settlement_receipt",
    "bounded_trajectory_summary",
)
QUALITY_INSTRUCTIONS = (
    "Assess the exact attempt artifact identified by the settlement receipt against "
    "the supplied objective, acceptance criteria, and rubric. Treat all four "
    "dispositions as settlement facts, not eligibility decisions: for discard or "
    "failure, evaluate the failed attempt and use the rollback receipt only as context. "
    "Treat hard scores as evidence, not as the objective."
)


class VerifierSubject(SearchModel):
    frozen_spec_id: str = Field(min_length=1)
    settlement: SettlementReceipt

    @property
    def run_id(self) -> str:
        return self.settlement.run_id

    @property
    def candidate_id(self) -> str:
        return self.settlement.candidate_id

    @property
    def iteration(self) -> int:
        return self.settlement.iteration

    @property
    def settlement_id(self) -> str:
        return self.settlement.settlement_id


def verifier_subject_identity(subject: VerifierSubject) -> str:
    return sha256_text(canonical_json(subject.model_dump(mode="json")))


class VerifierLease(SearchModel):
    worker_id: str = Field(min_length=1)
    expires_at: str = Field(min_length=1)


class VerifierInvocationRecord(SearchModel):
    invocation_id: str = Field(min_length=1)
    agent_harness: Literal["codex", "pi"]
    runtime_provider: Literal["direct"] = "direct"
    model: str = Field(min_length=1)
    reasoning_effort: str | None = None
    started_at: str
    completed_at: str
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)
    scoring_token_logprobs: bool = False


class VerifierResult(SearchModel):
    quality_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    allocation_scores: dict[str, NormalizedAllocationScore] | None
    evidence_refs: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1, max_length=4000)


class VerifierTask(SearchModel):
    schema_version: Literal[2] = 2
    task_id: str = Field(min_length=1, pattern=r"^verifier_[0-9a-f]{24}$")
    queue_sequence: int = Field(ge=1)
    subject: VerifierSubject
    allocation_policy_id: str | None = Field(default=None, min_length=1)
    allocation_score_names: list[str] = Field(default_factory=list)
    state: VerifierTaskState = "pending"
    attempts: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=3, ge=1)
    next_attempt_at: str | None = None
    lease: VerifierLease | None = None
    output_record_id: str | None = None
    usage: dict[str, int | float] = Field(default_factory=dict)
    invocation: VerifierInvocationRecord | None = None
    error: str | None = None
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def state_fields_are_consistent(self) -> "VerifierTask":
        if len(self.allocation_score_names) != len(set(self.allocation_score_names)):
            raise ValueError("verifier task allocation score names must be unique")
        if any(
            not re.fullmatch(r"[a-z][a-z0-9_]*", item)
            for item in self.allocation_score_names
        ):
            raise ValueError("verifier task contains an invalid allocation score name")
        if (self.allocation_policy_id is None) != (not self.allocation_score_names):
            raise ValueError(
                "allocation policy and allocation score names must coexist"
            )
        if self.state == "running" and self.lease is None:
            raise ValueError("running verifier task requires a lease")
        if self.state != "running" and self.lease is not None:
            raise ValueError("only a running verifier task can hold a lease")
        if self.state == "completed" and self.output_record_id is None:
            raise ValueError("completed verifier task requires an output record")
        if self.state == "terminal_error" and not self.error:
            raise ValueError("terminal verifier task requires an error")
        return self

    @property
    def run_id(self) -> str:
        return self.subject.run_id

    @property
    def candidate_id(self) -> str:
        return self.subject.candidate_id

    @property
    def iteration(self) -> int:
        return self.subject.iteration


class VerifierRecord(SearchModel):
    schema_version: Literal[2] = 2
    record_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    subject: VerifierSubject
    allocation_policy_id: str | None = Field(default=None, min_length=1)
    quality_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    allocation_scores: dict[str, NormalizedAllocationScore] | None
    evidence_refs: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1, max_length=4000)
    invocation: VerifierInvocationRecord
    usage: dict[str, int | float] = Field(default_factory=dict)
    created_at: str

    @model_validator(mode="after")
    def allocation_scores_match_policy(self) -> "VerifierRecord":
        if (self.allocation_policy_id is None) != (self.allocation_scores is None):
            raise ValueError("allocation policy and allocation scores must coexist")
        return self


def verifier_result_schema(allocation_score_names: list[str]) -> dict[str, Any]:
    schema = VerifierResult.model_json_schema()
    properties = schema["properties"]
    if allocation_score_names:
        properties["allocation_scores"] = {
            "type": "object",
            "properties": {
                name: {"type": "number", "minimum": 0.0, "maximum": 1.0}
                for name in allocation_score_names
            },
            "required": list(allocation_score_names),
            "additionalProperties": False,
        }
    else:
        properties["allocation_scores"] = {"type": "null"}
    return schema


__all__ = [
    "NormalizedAllocationScore",
    "QUALITY_CRITERIA",
    "QUALITY_INPUT_PROJECTION",
    "QUALITY_INSTRUCTIONS",
    "VerifierInvocationRecord",
    "VerifierLease",
    "VerifierRecord",
    "VerifierResult",
    "VerifierSubject",
    "VerifierTask",
    "VerifierTaskState",
    "verifier_result_schema",
    "verifier_subject_identity",
]
