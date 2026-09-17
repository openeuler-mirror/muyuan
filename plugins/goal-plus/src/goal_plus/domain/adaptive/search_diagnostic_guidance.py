"""Pure evidence-led guidance; never optimizes survival or changes Allocation."""

from typing import Literal
from pydantic import ConfigDict, Field

from goal_plus.domain.base import SearchModel
from goal_plus.domain.adaptive.models import SteeringAttempt, steering_limit_reason
from goal_plus.domain.adaptive.policy import SearchDiagnosticGuidanceParameters
from goal_plus.domain.serialization import canonical_json, sha256_text
from goal_plus.domain.verifier import SteeringFindings


class ExperimentIdentity(SearchModel):
    """Optional hard-verifier attestation, not a model-generated experiment label."""
    artifact_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    test_scope_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    seed: str = Field(min_length=1)
    deterministic: bool = Field(strict=True)
    replication_required: bool = Field(strict=True)


class DiagnosticExperiment(SearchModel):
    settlement_id: str
    verifier_contract_hash: str
    identity: ExperimentIdentity | None


class SearchDiagnosticInput(SearchModel):
    model_config = ConfigDict(frozen=True)
    source_record_id: str
    subject_hash: str
    candidate_id: str
    score_contract_hash: str
    scores: dict[str, float]
    findings: SteeringFindings
    public_references: dict[str, str]
    experiment: DiagnosticExperiment
    experiment_history: tuple[DiagnosticExperiment, ...]
    history_coverage: Literal["complete", "truncated"]
    source_generation: int
    usage: tuple[SteeringAttempt, ...]
    observed_at: float
    issue_key: str
    evidence_revision: str

    @property
    def input_hash(self) -> str:
        return sha256_text(canonical_json(self.model_dump(mode="json")))


class DiagnosticEvaluation(SearchModel):
    action: Literal["minimal_repair", "hypothesis_probe", "boundary_probe", "deduplicate"] | None = None
    reason_code: str
    duplicate_settlement_id: str | None = None
    duplicate_status: Literal["duplicate", "not_duplicate", "unknown", "necessary_replication"] = "unknown"


def duplicate_experiment(input: SearchDiagnosticInput) -> tuple[str, str | None]:
    current = input.experiment.identity
    if current is None:
        return "unknown", None
    if not current.deterministic or current.replication_required:
        return "necessary_replication", None
    for previous in reversed(input.experiment_history):
        if (previous.identity == current
                and previous.verifier_contract_hash == input.experiment.verifier_contract_hash):
            return "duplicate", previous.settlement_id
    if input.history_coverage == "truncated" or any(row.identity is None for row in input.experiment_history):
        return "unknown", None
    return "not_duplicate", None


def evaluate_search_diagnostic_guidance(input: SearchDiagnosticInput, *,
        parameters: SearchDiagnosticGuidanceParameters) -> DiagnosticEvaluation:
    findings, scores, p = input.findings, input.scores, parameters
    def noop(reason):
        return DiagnosticEvaluation(reason_code=reason)
    if findings.hypothesis is None:
        return noop("missing_hypothesis")
    if not input.public_references.get("hypothesis", "").strip():
        return noop("missing_hypothesis")
    # A finding may paraphrase an uncited part of its input. Refuse all guidance if
    # the evaluated diff was not entirely candidate-visible, not just diff citations.
    if "diff" not in input.public_references:
        return noop("private_evidence")
    claims = (findings.hypothesis, findings.confounder, findings.open_question, findings.boundary)
    if any(claim.scope_ref != findings.hypothesis.scope_ref for claim in filter(None, claims)):
        return noop("diagnostic_mismatch")
    if any(ref not in input.public_references for claim in filter(None, claims) for ref in claim.evidence_refs):
        return noop("private_evidence")
    confounded = findings.confounder is not None and scores["local_confounder_evidence"] >= p.confounder_threshold
    established = findings.boundary is not None and scores["boundary_evidence_strength"] >= p.boundary_confirmed_threshold
    if confounded and established:
        return noop("diagnostic_conflict")
    reason = steering_limit_reason(input.usage, issue_key=input.issue_key,
        evidence_revision=input.evidence_revision, now=input.observed_at, parameters=p)
    if reason:
        return noop(reason)
    if established:
        return noop("boundary_established")
    duplicate_status, duplicate_id = duplicate_experiment(input)
    action = None
    if duplicate_status == "duplicate":
        action = "deduplicate"
    elif confounded and scores["correctness_gap"] >= p.correctness_gap_threshold:
        action = "minimal_repair"
    elif (not confounded and findings.open_question is not None
          and scores["discriminating_test_gap"] >= p.test_gap_threshold):
        if findings.boundary is not None and scores["boundary_evidence_strength"] >= p.boundary_probe_threshold:
            action = "boundary_probe"
        elif scores["validation_gap"] >= p.validation_gap_threshold:
            action = "hypothesis_probe"
    return DiagnosticEvaluation(action=action, reason_code=action or "no_supported_action",
        duplicate_status=duplicate_status, duplicate_settlement_id=duplicate_id)
