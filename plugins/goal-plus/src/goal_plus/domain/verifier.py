"""Fixed LLM Verifier quality rules and immutable assessment contracts."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import ConfigDict, Field, model_validator

from goal_plus.domain.base import SearchModel
from goal_plus.domain.serialization import canonical_json, sha256_text
from goal_plus.domain.settlement import SettlementReceipt
from goal_plus.domain.artifacts import ArtifactRef, WorkspaceAttachment
from goal_plus.domain.agents import AgentRuntimeProvider


VerifierTaskState = Literal[
    "pending", "running", "retry_wait", "completed", "abandoned"
]
QUALITY_CRITERIA = (
    "objective_completion",
    "correctness_and_acceptance",
    "evidence_and_risk",
    "integration_and_complexity",
)
QUALITY_INSTRUCTIONS = (
    "Assess the exact attempt artifact identified by attempt_identity against the "
    "supplied objective, acceptance criteria, and rubric. Treat disposition in "
    "hard_evidence as a fact, not an eligibility decision: for discard or failure, "
    "evaluate the failed attempt rather than the restored state. Treat hard_score as "
    "evidence, not as the objective. Distinguish meeting the supplied success_threshold "
    "from further optimization; exceeding it does not by itself imply perfect quality. "
    "Use only supplied evidence for benchmarks, baselines and acceptance requirements; "
    "do not introduce remembered best-known results or infer measured improvement from a diff alone. "
    "Missing or omitted evidence is unknown, not proof that validation was not run or failed. "
    "Assess risk only within the demonstrated coverage of the hard verifier. "
    "Calibrate quality_score: 0 means demonstrated failure of the core objective; "
    "0.5 means partial completion or material unresolved gaps; 1 means the supplied objective "
    "and acceptance are fully supported without material correctness or integration concerns. "
    "Explain concrete gaps for intermediate scores without inventing requirements. "
    "Keep each score rationale concise, preferably under 800 characters and always at most "
    "1000 characters. Cover the four criteria without repeating their names or artifact IDs."
)


class VerifierScoreRule(SearchModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    minimum: float = Field(allow_inf_nan=False)
    maximum: float = Field(allow_inf_nan=False)
    instructions: str = Field(min_length=1)
    requires_comparison_base: bool = False

    @model_validator(mode="after")
    def range_is_ordered(self) -> "VerifierScoreRule":
        if self.name == "quality_score":
            raise ValueError("quality_score is reserved for the common quality rubric")
        if self.minimum > self.maximum:
            raise ValueError("verifier score rule minimum cannot exceed maximum")
        if not self.instructions.strip():
            raise ValueError("verifier score rule instructions must be non-empty")
        return self


DIAGNOSTIC_SCORE_KEYS = (
    "correctness_gap", "validation_gap", "local_confounder_evidence",
    "discriminating_test_gap", "boundary_evidence_strength",
)
# Bounded single-claim diagnostics, not an executable proposal language.
MAX_FINDING_TEXT = 1000
MAX_FINDING_REFS = 16


class DiagnosticClaim(SearchModel):
    model_config = ConfigDict(frozen=True)
    statement: str = Field(min_length=1, max_length=MAX_FINDING_TEXT)
    scope_ref: str = Field(min_length=1, max_length=128)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=MAX_FINDING_REFS)


class DiagnosticQuestion(DiagnosticClaim):
    falsifying_observation: str = Field(min_length=1, max_length=MAX_FINDING_TEXT)


class DiagnosticBoundary(DiagnosticClaim):
    condition: str = Field(min_length=1, max_length=MAX_FINDING_TEXT)


class SteeringFindings(SearchModel):
    model_config = ConfigDict(frozen=True)
    hypothesis: DiagnosticClaim | None
    confounder: DiagnosticClaim | None
    open_question: DiagnosticQuestion | None
    boundary: DiagnosticBoundary | None


class DiagnosticContract(SearchModel):
    version: Literal["search_diagnostics/v1"] = "search_diagnostics/v1"
    findings_schema: dict[str, Any]
    instructions: str


DIAGNOSTIC_INSTRUCTIONS = (
    "Diagnose only the current exact attempt using diagnostic_references. Each finding is "
    "at most one evidence-backed assessment, never a command, patch or proposed acceptance rule. "
    "Use hypothesis or an acceptance:N reference for scope_ref; bind every finding to the "
    "same principal claim/scope. Cite only supplied reference IDs. Omit unsupported findings. "
    "A confounder is a local implementation/measurement defect invalidating the experiment, "
    "not a failed hypothesis. An open question needs a concrete falsifying observation. "
    "A boundary needs a limited condition and evidence from valid experiments; confounded "
    "failures cannot establish that boundary. Missing confounder/open_question/boundary requires "
    "zero local_confounder_evidence/discriminating_test_gap/boundary_evidence_strength respectively. "
    "Unknown correctness is a validation gap, not proof of a correctness error. Do not predict "
    "future scores, award survival, invent evidence, or issue retirement decisions."
)


# Registration belongs to the evaluator, independently of any consumer policy.
VERIFIER_SCORE_RULES = {
    rule.name: rule
    for rule in (
        VerifierScoreRule(
            name="progress_reward", minimum=-1.0, maximum=1.0,
            requires_comparison_base=True,
            instructions="Compare only the exact attempt with its lineage-defined parent: -1 is "
            "a clear regression, 0 is no material change, and 1 is decisive "
            "progress. Do not predict future value.",
        ),
        VerifierScoreRule(
            name="allocation_utility", minimum=0.0, maximum=1.0,
            instructions="Estimate the value of one more standard-budget expansion from this exact "
            "checkpoint: 0 means no worthwhile actionable next step and 1 means a "
            "clear high-value opportunity.",
        ),
        VerifierScoreRule(name="correctness_gap", minimum=0, maximum=1,
            instructions="0: no demonstrated unresolved correctness defect in supplied evidence; "
            "1: clear violation of core acceptance. Intermediate values measure extent/severity, "
            "not repairability. Missing evidence alone is not proof of error."),
        VerifierScoreRule(name="validation_gap", minimum=0, maximum=1,
            instructions="0: supplied validation covers stated acceptance; 1: core claims lack "
            "relevant validation. Do not assume unprovided tests passed or promise future value."),
        VerifierScoreRule(name="local_confounder_evidence", minimum=0, maximum=1,
            instructions="0: no located local confounder; 1: direct evidence of a local implementation "
            "or measurement defect preventing valid judgment of the principal hypothesis. "
            "A disproved hypothesis is not a confounder. Requires confounder finding."),
        VerifierScoreRule(name="discriminating_test_gap", minimum=0, maximum=1,
            instructions="0: no concrete unanswered discriminating question; 1: an explicit "
            "falsifiable question remains untested. Not predicted benefit or allocation utility. "
            "Requires open_question finding."),
        VerifierScoreRule(name="boundary_evidence_strength", minimum=0, maximum=1,
            instructions="0: no supported boundary; 1: valid experiments or counterexamples "
            "strongly establish a limitation under explicit conditions. Confounded failures "
            "cannot establish a boundary. Requires boundary finding."),
    )
}


class VerifierSpec(SearchModel):
    """Presence enables assessment; scores select registered extra metric keys."""

    scores: list[str] = Field(
        default_factory=list,
        description="Unique keys of registered extra metrics; quality_score is always included.",
        json_schema_extra={"items": {"type": "string", "enum": sorted(VERIFIER_SCORE_RULES)}},
    )

    @model_validator(mode="after")
    def scores_are_registered(self) -> "VerifierSpec":
        verifier_score_contract(self.scores)
        return self


class VerifierScoreContract(SearchModel):
    schema_version: Literal[2] = 2
    score_rules: list[VerifierScoreRule] = Field(default_factory=list)
    diagnostics: DiagnosticContract | None = Field(default=None, exclude_if=lambda value: value is None)
    contract_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_rules(
        cls,
        *,
        score_rules: Sequence[VerifierScoreRule] = (),
    ) -> "VerifierScoreContract":
        body = {
            "schema_version": 2,
            "score_rules": [item.model_dump(mode="json") for item in score_rules],
        }
        if set(DIAGNOSTIC_SCORE_KEYS).intersection(item.name for item in score_rules):
            body["diagnostics"] = DiagnosticContract(
                findings_schema=SteeringFindings.model_json_schema(),
                instructions=DIAGNOSTIC_INSTRUCTIONS,
            ).model_dump(mode="json")
        return cls(
            **body,
            contract_hash=sha256_text(canonical_json(body)),
        )

    @model_validator(mode="after")
    def identity_and_rules_are_consistent(self) -> "VerifierScoreContract":
        names = [item.name for item in self.score_rules]
        if len(names) != len(set(names)):
            raise ValueError("verifier score contract rules must be unique")
        if bool(set(DIAGNOSTIC_SCORE_KEYS).intersection(names)) != (self.diagnostics is not None):
            raise ValueError("diagnostic metrics require their frozen findings contract")
        body = self.model_dump(mode="json", exclude={"contract_hash"})
        if self.contract_hash != sha256_text(canonical_json(body)):
            raise ValueError("verifier score contract hash does not match its contents")
        return self


def verifier_score_contract(score_keys: Sequence[str] = ()) -> VerifierScoreContract:
    """Resolve unique metric keys into a canonical, immutable task contract."""

    if isinstance(score_keys, str):
        raise ValueError("verifier scores must be a sequence of metric keys")
    try:
        rules = [VERIFIER_SCORE_RULES[key].model_copy(deep=True)
                 for key in sorted(score_keys)]
    except KeyError as exc:
        raise ValueError(f"unregistered verifier score: {exc.args[0]}") from exc
    return VerifierScoreContract.from_rules(score_rules=rules)


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
    runtime_provider: Literal["direct", "thinkthread"] = "direct"
    native_session_id: str | None = None
    native_invocation_id: str | None = None
    model: str = Field(min_length=1)
    reasoning_effort: str | None = None
    started_at: str
    completed_at: str
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)
    scoring_token_logprobs: bool = False


# Shared-cache proposals: bounded objective facts, never free-form essays.
MAX_CACHE_UPDATES = 8
MAX_CACHE_UPDATE_CHARS = 2000


class VerifierCacheUpdate(SearchModel):
    """One verifier-proposed shared cache entry (proposal-ledger pattern).

    The trusted runtime, not the model, validates and applies the update:
    section must belong to the frozen closed key set, content is bounded,
    supersedes must reference an existing record, and requires_evidence
    sections must carry an evidence_ref. Invalid proposals are rejected
    individually (or invalidate the whole output at the adapter layer).
    """

    section: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    content: str = Field(min_length=1, max_length=MAX_CACHE_UPDATE_CHARS)
    supersedes: str | None = Field(default=None, max_length=128)
    evidence_ref: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def content_is_collapsed(self) -> "VerifierCacheUpdate":
        if not " ".join(self.content.split()):
            raise ValueError("cache update content must be non-empty")
        return self


class VerifierCacheContract(SearchModel):
    """Cache sections the verifier may read and propose updates for."""

    sections: tuple[tuple[str, str, str], ...] = ()
    # (key, title, requirement) triples from the frozen SharedCacheSpec.

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(key for key, _title, _requirement in self.sections)


def verifier_cache_contract(snapshot: dict[str, Any] | None) -> VerifierCacheContract | None:
    """Derive the update contract from the frozen input's cache snapshot."""

    if not isinstance(snapshot, dict) or not snapshot.get("updates_allowed"):
        return None
    sections = snapshot.get("sections")
    if not isinstance(sections, list):
        return None
    triples = tuple(
        (
            str(item.get("key", "")),
            str(item.get("title", "")),
            str(item.get("requirement", "")),
        )
        for item in sections
        if isinstance(item, dict)
    )
    if not triples:
        return None
    return VerifierCacheContract(sections=triples)


def validate_cache_updates(
    updates: list[VerifierCacheUpdate] | None,
    contract: VerifierCacheContract | None,
) -> None:
    """Adapter-level validation: illegal proposals invalidate the whole output."""

    if contract is None or not contract.keys:
        if updates:
            raise ValueError("cache updates are not enabled by the frozen contract")
        return
    if updates is None:
        raise ValueError("cache updates require an explicit list, possibly empty")
    if len(updates) > MAX_CACHE_UPDATES:
        raise ValueError(f"at most {MAX_CACHE_UPDATES} cache updates per verdict")
    for update in updates:
        if update.section not in contract.keys:
            raise ValueError(f"cache update section {update.section!r} is outside the frozen key set")


class VerifierResult(SearchModel):
    quality_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    scores: dict[str, float] | None
    evidence_refs: list[str] = Field(default_factory=list)
    score_rationales: dict[str, str]
    steering_findings: SteeringFindings | None = None
    cache_updates: list[VerifierCacheUpdate] | None = None

    @model_validator(mode="after")
    def scores_and_rationales_are_atomic(self) -> "VerifierResult":
        _validate_finite_scores(self.scores)
        _validate_score_rationales(self.scores, self.score_rationales)
        return self


class DispatchAuditContext(SearchModel):
    """Frozen host facts and exact settlement references, without execution authority."""

    model_config = ConfigDict(frozen=True)
    runtime_provider: AgentRuntimeProvider = "direct"
    main_session_id: str | None = None
    checkpoint_id: str = Field(min_length=1)
    checkpoint_kind: str = Field(min_length=1)
    parent_native_session_id: str = Field(min_length=1)
    provider_session_id: str = Field(min_length=1)

    @property
    def branch_native_session_id(self) -> str:
        return self.provider_session_id
    workspace: Path
    workspace_hash: str = Field(min_length=1)
    workspace_attachment: WorkspaceAttachment | None = None
    frozen_spec_id: str = Field(min_length=1)
    spec_hash: str = Field(min_length=1)
    initial_frozen_spec_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    initial_spec_hash: str | None = Field(default=None, exclude_if=lambda value: value is None)
    parent_frozen_spec_id: str | None = None
    artifact_refs: tuple[ArtifactRef, ...]
    iteration: int = Field(ge=1)
    settlement_id: str = Field(min_length=1)
    launch_receipt: dict[str, Any]
    close_receipt: dict[str, Any] = Field(default_factory=dict)
    context_projection_digest: str | None = None


class VerifierInput(SearchModel):
    """One immutable settlement submission; never a live search-state reader."""

    objective: str
    candidate_acceptance: list[str]
    hypothesis: str
    artifact_diff: str
    agent_harness: Literal["codex", "pi"]
    metric_direction: Literal["minimize", "maximize"] | None = None
    success_threshold: float | None = Field(default=None, allow_inf_nan=False)
    initial_artifact_ref: ArtifactRef | None = None
    parent_node_id: str | None = None
    allocation_depth: int = Field(default=0, ge=0)
    input_error: str | None = None
    replaces_candidate_id: str | None = None
    # Frozen shared-cache projection (sections template + entries) captured at
    # settlement time; None when the run has no cache or verifier_input=false.
    cache_snapshot: dict[str, Any] | None = None
    # Present only for fork runs.  This is a bounded, read-only
    # projection; it never grants host control or changes hard Evidence.
    dispatch_audit_context: DispatchAuditContext | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_legacy_dispatch_field(cls, value: Any) -> Any:
        if isinstance(value, dict) and "fork_audit_context" in value:
            raise ValueError(
                "legacy fork_audit_context field is unsupported; use dispatch_audit_context"
            )
        return value


class VerifierTask(SearchModel):
    schema_version: Literal[7] = 7
    task_id: str = Field(min_length=1, pattern=r"^verifier_[0-9a-f]{24}$")
    queue_sequence: int = Field(ge=1)
    subject: VerifierSubject
    score_contract: VerifierScoreContract
    input: VerifierInput
    delivered: bool = False
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
        if self.state == "running" and self.lease is None:
            raise ValueError("running verifier task requires a lease")
        if self.state != "running" and self.lease is not None:
            raise ValueError("only a running verifier task can hold a lease")
        if self.state == "completed" and self.output_record_id is None:
            raise ValueError("completed verifier task requires an output record")
        if self.state == "retry_wait" and (
            self.next_attempt_at is None or not self.error
        ):
            raise ValueError("retrying verifier task requires retry time and error")
        if self.state != "retry_wait" and self.next_attempt_at is not None:
            raise ValueError("only a retrying verifier task can have a retry time")
        if self.state == "abandoned" and not self.error:
            raise ValueError("abandoned verifier task requires an error")
        if self.state == "abandoned" and self.output_record_id is None:
            raise ValueError("abandoned verifier task requires a terminal record")
        if self.delivered and self.state not in {"completed", "abandoned"}:
            raise ValueError("only terminal verifier tasks can be delivered")
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


class VerifierSubmission(SearchModel):
    subject: VerifierSubject
    score_contract: VerifierScoreContract
    input: VerifierInput


class VerifierRecord(SearchModel):
    schema_version: Literal[7] = 7
    record_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    subject: VerifierSubject
    queue_sequence: int = Field(ge=1)
    outcome: Literal["completed", "failed"] = "completed"
    error: str | None = None
    initial_artifact_ref: ArtifactRef | None = None
    parent_node_id: str | None = None
    allocation_depth: int = Field(default=0, ge=0)
    replaces_candidate_id: str | None = None
    score_contract_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    quality_score: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    scores: dict[str, float] | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    score_rationales: dict[str, str] = Field(default_factory=dict)
    steering_findings: SteeringFindings | None = None
    cache_updates: list[VerifierCacheUpdate] | None = None
    invocation: VerifierInvocationRecord | None = None
    usage: dict[str, int | float] = Field(default_factory=dict)
    created_at: str

    @model_validator(mode="after")
    def scores_are_valid(self) -> "VerifierRecord":
        if self.outcome == "failed":
            if (not self.error or self.quality_score is not None or self.scores is not None
                    or self.score_rationales or self.steering_findings is not None
                    or self.cache_updates is not None):
                raise ValueError("failed verifier record requires an error and no partial scores")
            return self
        if self.quality_score is None or self.invocation is None or self.error is not None:
            raise ValueError("completed verifier record requires a full assessment")
        if self.scores == {}:
            raise ValueError("scores must be null or non-empty")
        _validate_finite_scores(self.scores)
        _validate_score_rationales(self.scores, self.score_rationales)
        return self


def _validate_finite_scores(scores: dict[str, float] | None) -> None:
    if scores is None:
        return
    if any(
        not re.fullmatch(r"[a-z][a-z0-9_]*", name)
        or not math.isfinite(value)
        for name, value in scores.items()
    ):
        raise ValueError("scores require valid names and finite values")


def _validate_score_rationales(
    scores: dict[str, float] | None,
    rationales: dict[str, str],
) -> None:
    expected = {"quality_score", *(scores or {})}
    if set(rationales) != expected:
        raise ValueError("score rationales must exactly match numeric score fields")
    if any(not value.strip() or len(value) > 1000 for value in rationales.values()):
        raise ValueError("score rationales must be non-empty and at most 1000 characters")


def validate_scores(
    scores: dict[str, float] | None,
    rules: Sequence[VerifierScoreRule],
) -> None:
    if not rules and scores is not None:
        raise ValueError(
            "verifier scores must be null without registered rules"
        )
    rule_by_name = {item.name: item for item in rules}
    actual = scores or {}
    if set(actual) != set(rule_by_name):
        raise ValueError(
            "verifier scores must exactly match the task registration"
        )
    _validate_finite_scores(scores)
    for name, value in actual.items():
        rule = rule_by_name[name]
        if not rule.minimum <= value <= rule.maximum:
            raise ValueError(
                f"verifier score {name} is outside [{rule.minimum}, {rule.maximum}]"
            )


def verifier_result_schema(
    score_rules: Sequence[VerifierScoreRule],
    diagnostics: DiagnosticContract | None = None,
    cache_contract: VerifierCacheContract | None = None,
) -> dict[str, Any]:
    schema = VerifierResult.model_json_schema()
    properties = schema["properties"]
    if diagnostics is None:
        properties["steering_findings"] = {"type": "null"}
    else:
        frozen_schema = dict(diagnostics.findings_schema)
        schema.setdefault("$defs", {}).update(frozen_schema.pop("$defs", {}))
        properties["steering_findings"] = frozen_schema
        schema.setdefault("required", []).append("steering_findings")
    if cache_contract is None or not cache_contract.keys:
        properties["cache_updates"] = {"type": "null"}
    else:
        update_schema = VerifierCacheUpdate.model_json_schema()
        schema.setdefault("$defs", {})["VerifierCacheUpdate"] = update_schema
        properties["cache_updates"] = {
            "type": "array",
            "items": {"$ref": "#/$defs/VerifierCacheUpdate"},
            "maxItems": MAX_CACHE_UPDATES,
        }
        schema.setdefault("required", []).append("cache_updates")
    if score_rules:
        properties["scores"] = {
            "type": "object",
            "properties": {
                rule.name: {
                    "type": "number",
                    "minimum": rule.minimum,
                    "maximum": rule.maximum,
                }
                for rule in score_rules
            },
            "required": [item.name for item in score_rules],
            "additionalProperties": False,
        }
    else:
        properties["scores"] = {"type": "null"}
    rationale_names = [
        "quality_score",
        *(item.name for item in score_rules),
    ]
    properties["score_rationales"] = {
        "type": "object",
        "properties": {
            name: {"type": "string", "minLength": 1, "maxLength": 1000}
            for name in rationale_names
        },
        "required": rationale_names,
        "additionalProperties": False,
    }
    return schema


def diagnostic_references(submission: VerifierInput, subject: VerifierSubject) -> dict[str, str]:
    """Closed references to immutable local inputs; no paths, URLs or peer material."""
    receipt = subject.settlement
    return {
        "hypothesis": submission.hypothesis,
        **{f"acceptance:{index}": text for index, text in enumerate(submission.candidate_acceptance)},
        "diff": submission.artifact_diff,
        "hard_evidence": canonical_json({
            "process_passed": receipt.evidence.process_passed,
            "hard_score": receipt.evidence.hard_score,
            "disposition": receipt.disposition,
        }),
    }


def validate_diagnostics(findings: SteeringFindings | None, scores: dict[str, float] | None,
                         contract: VerifierScoreContract, references: dict[str, str]) -> None:
    if contract.diagnostics is None:
        if findings is not None:
            raise ValueError("diagnostics are not enabled by the frozen contract")
        return
    if findings is None:
        raise ValueError("diagnostics require complete findings, including explicit nulls")
    claims = (findings.hypothesis, findings.confounder, findings.open_question, findings.boundary)
    for claim in filter(None, claims):
        if (not claim.statement.strip() or claim.scope_ref not in references
                or not references[claim.scope_ref].strip()
                or not (claim.scope_ref == "hypothesis" or claim.scope_ref.startswith("acceptance:"))
                or any(ref not in references or not references[ref].strip() for ref in claim.evidence_refs)):
            raise ValueError("diagnostic reference is outside the immutable input")
        if findings.hypothesis is None or claim.scope_ref != findings.hypothesis.scope_ref:
            raise ValueError("diagnostics must bind the same principal hypothesis scope")
        if isinstance(claim, DiagnosticQuestion) and not claim.falsifying_observation.strip():
            raise ValueError("diagnostic question needs a falsifying observation")
        if isinstance(claim, DiagnosticBoundary) and not claim.condition.strip():
            raise ValueError("diagnostic boundary needs an explicit condition")
    for name, finding in (("local_confounder_evidence", findings.confounder),
                          ("discriminating_test_gap", findings.open_question),
                          ("boundary_evidence_strength", findings.boundary)):
        if finding is None and (scores or {}).get(name, 0) != 0:
            raise ValueError(f"{name} requires a supporting finding")


__all__ = [
    "DIAGNOSTIC_SCORE_KEYS",
    "DiagnosticContract",
    "DiagnosticClaim",
    "DiagnosticQuestion",
    "DiagnosticBoundary",
    "SteeringFindings",
    "VerifierCacheUpdate",
    "VerifierCacheContract",
    "validate_cache_updates",
    "verifier_cache_contract",
    "diagnostic_references",
    "validate_diagnostics",
    "QUALITY_CRITERIA",
    "QUALITY_INSTRUCTIONS",
    "VERIFIER_SCORE_RULES",
    "VerifierSpec",
    "VerifierScoreContract",
    "VerifierScoreRule",
    "VerifierInput",
    "VerifierSubmission",
    "VerifierInvocationRecord",
    "VerifierLease",
    "VerifierRecord",
    "VerifierResult",
    "VerifierSubject",
    "VerifierTask",
    "VerifierTaskState",
    "verifier_result_schema",
    "verifier_score_contract",
    "verifier_subject_identity",
    "validate_scores",
]
