"""Versioned policy registry and configuration parser for Search Scheduler."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field, model_validator

from goal_plus.domain.base import SearchModel

SchedulerPolicyKind = Literal["reward", "allocation"]
_POLICY_REFERENCE_PATTERN = re.compile(
    r"^(?P<name>[A-Za-z][A-Za-z0-9_-]*)/v(?P<version>[1-9][0-9]*)$"
)


class ResolvedSchedulerPolicy(SearchModel):
    kind: SchedulerPolicyKind
    reference: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]*/v[1-9][0-9]*$")
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    version: int = Field(ge=1)
    instructions: str | None = Field(default=None, min_length=1)
    pruning_instructions: str | None = Field(default=None, min_length=1)
    derivation_instructions: str | None = Field(default=None, min_length=1)
    supplemental_prompt: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def snapshot_is_self_consistent(self) -> "ResolvedSchedulerPolicy":
        if self.reference != f"{self.name}/v{self.version}":
            raise ValueError("resolved scheduler policy reference is inconsistent")
        if self.kind == "reward":
            if self.instructions is None:
                raise ValueError("resolved reward policy requires instructions")
            if (
                self.pruning_instructions is not None
                or self.derivation_instructions is not None
            ):
                raise ValueError(
                    "resolved reward policy cannot define allocation instructions"
                )
        else:
            if self.instructions is not None:
                raise ValueError(
                    "resolved allocation policy cannot define reward instructions"
                )
            if (
                self.pruning_instructions is None
                or self.derivation_instructions is None
            ):
                raise ValueError(
                    "resolved allocation policy requires pruning and derivation instructions"
                )
        return self


class ResolvedSearchSchedulerPolicies(SearchModel):
    reward: ResolvedSchedulerPolicy
    allocation: ResolvedSchedulerPolicy

    @model_validator(mode="after")
    def kinds_match_fields(self) -> "ResolvedSearchSchedulerPolicies":
        if self.reward.kind != "reward" or self.allocation.kind != "allocation":
            raise ValueError("resolved scheduler policy kinds do not match their fields")
        return self


class SchedulerPolicyContextInput(SearchModel):
    """Runtime facts from which registered policies project model context."""

    current_candidate_id: str = Field(min_length=1)
    current_node_id: str = Field(min_length=1)
    metric_direction: Literal["minimize", "maximize"]
    phase: Literal["screen", "confirm"]
    candidates: list[dict[str, Any]]
    uct: dict[str, int | float]
    capacity: dict[str, int | None]
    reserved_candidate_ids: list[str] = Field(default_factory=list)


SchedulerPolicyContextBuilder = Callable[
    [SchedulerPolicyContextInput],
    dict[str, Any],
]


@dataclass(frozen=True)
class _SchedulerPolicyRegistration:
    policy: ResolvedSchedulerPolicy
    build_context: SchedulerPolicyContextBuilder


_POLICIES: dict[
    SchedulerPolicyKind,
    dict[str, _SchedulerPolicyRegistration],
] = {
    "reward": {},
    "allocation": {},
}


def _parse_reference(reference: str) -> tuple[str, int, str]:
    normalized = reference.strip()
    match = _POLICY_REFERENCE_PATTERN.fullmatch(normalized)
    if match is None:
        raise ValueError(
            "scheduler policy must use the versioned form <name>/v<positive-int>"
        )
    name = match.group("name")
    version = int(match.group("version"))
    return name, version, f"{name}/v{version}"


def _register_scheduler_policy(
    kind: SchedulerPolicyKind,
    reference: str,
    *,
    instructions: str | None = None,
    pruning_instructions: str | None = None,
    derivation_instructions: str | None = None,
    build_context: SchedulerPolicyContextBuilder,
) -> ResolvedSchedulerPolicy:
    """Register one immutable policy version defined in this module."""

    name, version, normalized = _parse_reference(reference)
    normalized_instructions = instructions.strip() if instructions else None
    normalized_pruning = (
        pruning_instructions.strip() if pruning_instructions else None
    )
    normalized_derivation = (
        derivation_instructions.strip() if derivation_instructions else None
    )
    registry = _POLICIES[kind]
    if normalized in registry:
        raise ValueError(f"scheduler policy is already registered: {normalized}")
    policy = ResolvedSchedulerPolicy(
        kind=kind,
        reference=normalized,
        name=name,
        version=version,
        instructions=normalized_instructions,
        pruning_instructions=normalized_pruning,
        derivation_instructions=normalized_derivation,
    )
    registry[normalized] = _SchedulerPolicyRegistration(
        policy=policy,
        build_context=build_context,
    )
    return policy


def _register_reward_policy(
    reference: str,
    *,
    instructions: str,
    build_context: SchedulerPolicyContextBuilder,
) -> ResolvedSchedulerPolicy:
    return _register_scheduler_policy(
        "reward",
        reference,
        instructions=instructions,
        build_context=build_context,
    )


def _register_allocation_policy(
    reference: str,
    *,
    pruning_instructions: str,
    derivation_instructions: str,
    build_context: SchedulerPolicyContextBuilder,
) -> ResolvedSchedulerPolicy:
    return _register_scheduler_policy(
        "allocation",
        reference,
        pruning_instructions=pruning_instructions,
        derivation_instructions=derivation_instructions,
        build_context=build_context,
    )


def parse_scheduler_policy_reference(value: str) -> tuple[str, str | None]:
    """Validate and normalize `name/vN @prompt ...` without registry access."""

    raw = value.strip()
    if not raw:
        raise ValueError("scheduler policy reference must be non-empty")
    if raw.count("@prompt") > 1:
        raise ValueError("scheduler policy can contain at most one @prompt suffix")
    reference, marker, supplemental = raw.partition("@prompt")
    _, _, normalized = _parse_reference(reference)
    supplemental_prompt = supplemental.strip() if marker else None
    if marker and not supplemental_prompt:
        raise ValueError("@prompt requires non-empty supplemental instructions")
    return normalized, supplemental_prompt


def resolve_scheduler_policy(
    value: str,
    *,
    kind: SchedulerPolicyKind,
) -> ResolvedSchedulerPolicy:
    """Resolve `name/vN @prompt ...` into frozen scheduler context."""

    normalized, supplemental_prompt = parse_scheduler_policy_reference(value)
    registration = _POLICIES[kind].get(normalized)
    if registration is None:
        available = ", ".join(sorted(_POLICIES[kind])) or "<none>"
        raise ValueError(
            f"unknown {kind} scheduler policy {normalized!r}; available: {available}"
        )
    return registration.policy.model_copy(
        update={"supplemental_prompt": supplemental_prompt}
    )


def resolve_search_scheduler_policies(
    *,
    reward: str,
    allocation: str,
) -> ResolvedSearchSchedulerPolicies:
    return ResolvedSearchSchedulerPolicies(
        reward=resolve_scheduler_policy(reward, kind="reward"),
        allocation=resolve_scheduler_policy(allocation, kind="allocation"),
    )


def _candidate(
    source: SchedulerPolicyContextInput,
    candidate_id: str,
) -> dict[str, Any]:
    for candidate in source.candidates:
        if candidate["candidate_id"] == candidate_id:
            return candidate
    raise ValueError(f"policy context has no candidate {candidate_id!r}")


def _completed_nodes(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    return [node for node in candidate["nodes"] if node.get("reward") is not None]


def _compact_node(node: dict[str, Any] | None) -> dict[str, Any] | None:
    if node is None:
        return None
    return {
        "node_id": node["node_id"],
        "settlement_id": node["settlement_id"],
        "schedule_task_id": node["reward_task_id"],
        "iteration": node["iteration"],
        "commit": node["commit"],
        "artifact_hash": node["artifact_hash"],
        "score": node["score"],
        "hypothesis": node["hypothesis"],
        "disposition": node["disposition"],
        "changed_files": list(node["changed_files"]),
        "reward": node["reward"],
    }


def _best_score_node(
    source: SchedulerPolicyContextInput,
    nodes: list[dict[str, Any]],
) -> dict[str, Any] | None:
    scored = [node for node in nodes if node["score"] is not None]
    if not scored:
        return None
    return max(
        scored,
        key=lambda node: (
            float(node["score"])
            if source.metric_direction == "maximize"
            else -float(node["score"])
        ),
    )


def _recent_rewards(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "node_id": node["node_id"],
            "reward": node["reward"]["reward"],
            "components": node["reward"]["components"],
        }
        for node in nodes[-4:]
    ]


def _build_marginal_reward_context(
    source: SchedulerPolicyContextInput,
) -> dict[str, Any]:
    nodes = _completed_nodes(_candidate(source, source.current_candidate_id))
    best_score = _best_score_node(source, nodes)
    best_reward = max(
        nodes,
        key=lambda node: float(node["reward"]["reward"]),
        default=None,
    )
    last_positive = next(
        (
            node
            for node in reversed(nodes)
            if float(node["reward"]["reward"]) > 0
        ),
        None,
    )
    return {
        "candidate_history": {
            "previous_node": _compact_node(nodes[-1] if nodes else None),
            "recent_nodes": [_compact_node(node) for node in nodes[-4:]],
            "milestones": {
                "best_score_node": _compact_node(best_score),
                "best_reward_node": _compact_node(best_reward),
                "last_positive_marginal_node": _compact_node(last_positive),
            },
        },
        "uct": dict(source.uct),
    }


def _build_value_guided_allocation_context(
    source: SchedulerPolicyContextInput,
) -> dict[str, Any]:
    current = _candidate(source, source.current_candidate_id)
    current_node = next(
        node
        for node in current["nodes"]
        if node["node_id"] == source.current_node_id
    )
    current_history = _completed_nodes(current)
    best_score = _best_score_node(source, current_history)
    peer_frontiers = []
    for candidate in source.candidates:
        nodes = _completed_nodes(candidate)
        peer_frontiers.append(
            {
                "candidate_id": candidate["candidate_id"],
                "allocation_eligibility": candidate["allocation_eligibility"],
                "allocation_depth": candidate["allocation_depth"],
                "latest_node": _compact_node(nodes[-1] if nodes else None),
                "best_score_node": _compact_node(
                    _best_score_node(source, nodes)
                ),
                "recent_rewards": _recent_rewards(nodes),
            }
        )
    context: dict[str, Any] = {
        "phase": source.phase,
        "current_lane": {
            "candidate_id": source.current_candidate_id,
            "allocation_eligibility": current["allocation_eligibility"],
            "latest_score": current_node["score"],
            "previous_best_score": (
                best_score["score"] if best_score is not None else None
            ),
            "latest_artifact_hash": current_node["artifact_hash"],
            "recent_rewards": _recent_rewards(current_history),
        },
        "peer_frontiers": peer_frontiers,
        "capacity": dict(source.capacity),
    }
    if source.phase == "confirm":
        context["candidates"] = source.candidates
        context["reserved_candidate_ids"] = list(source.reserved_candidate_ids)
    return context


def _build_policy_context(
    policy: ResolvedSchedulerPolicy,
    source: SchedulerPolicyContextInput,
) -> dict[str, Any]:
    registration = _POLICIES[policy.kind].get(policy.reference)
    if registration is None:
        raise ValueError(
            f"no context builder is registered for {policy.kind} policy "
            f"{policy.reference!r}"
        )
    projected = registration.build_context(source)
    if "policy" in projected:
        raise ValueError("scheduler policy context builder cannot replace policy")
    exclude = (
        {"derivation_instructions"}
        if policy.kind == "allocation" and source.phase == "screen"
        else set()
    )
    return {
        "policy": policy.model_dump(
            mode="json",
            exclude_none=True,
            exclude=exclude,
        ),
        **projected,
    }


def build_scheduler_policy_contexts(
    policies: ResolvedSearchSchedulerPolicies,
    source: SchedulerPolicyContextInput,
) -> dict[str, dict[str, Any]]:
    """Build both policy-owned contexts through one scheduler-facing interface."""

    return {
        "reward_context": _build_policy_context(policies.reward, source),
        "allocation_context": _build_policy_context(policies.allocation, source),
    }


_register_reward_policy(
    "evidence_llm_value/v3",
    instructions=(
        "Compute the immutable local reward as the marginal value added by the current "
        "settled attempt, not as the absolute quality of the candidate. Use the current "
        "facts in scheduler_context and compare them with "
        "reward_context.candidate_history. Compute three weighted components. "
        "Outcome gain O is in {-60,-30,0,15,60}: use -60 for a public metric regression, "
        "a failed process, or a harmful discard; -30 for a weaker demonstrated regression; "
        "60 for a new candidate-best public result or a first fully passing public result; "
        "15 only for recovery to an already observed best result; otherwise 0 for an "
        "unchanged result. Information gain E is in {-25,-12.5,0,12.5,25}: reward only a "
        "new publicly grounded behavior boundary, causal finding, counterexample, or reusable "
        "diagnostic absent from recent_nodes; repeated confirmation of the same result is 0, "
        "and contradictory or damaging evidence is negative. Direction gain D is in "
        "{-15,-7.5,0,7.5,15}: reward a distinct, credible implementation or coverage direction; "
        "an unchanged artifact, no diff, repeated validation, repeated tool publication, or "
        "comment-only maintenance is 0; duplicative or harmful change is negative. Compute "
        "R=clamp(-100,100,O+E+D), set reward=rubric_score=R, and record components named "
        "outcome_gain, information_gain, and direction_gain. A repeated public pass with no "
        "new behavior, implementation, or direction must receive R=0, not a positive reward. "
        "From reward_context.uct read N=total_visits, n=node_visits, and Q=mean_reward before "
        "this attempt. Compute n1=n+1, N1=N+1, Q1=(n*Q+R)/n1, C=20, "
        "X=C*sqrt(ln(max(N1,2))/n1), then set exploitation=Q1, exploration=X, and uct=Q1+X. "
        "When allocation_context.phase is confirm, also build an ephemeral lineage projection "
        "from allocation_context.candidates[].nodes. Bind identity only through node_id, "
        "parent_node_id, settlement_id, and reward_task_id. Include the current node with R. "
        "For every reward r_z at node z and ancestor a at distance d use gamma=0.8 and "
        "w=gamma^d; accumulate visits[a]+=1, value_sum[a]+=w*r_z, and "
        "value_weight[a]+=w. Compute projected_value[a]=value_sum[a]/value_weight[a] and "
        "projected_uct[a]=projected_value[a]+20*sqrt(ln(max(root_visits,2))/max(visits[a],1)). "
        "These projections are allocation working values only and never modify a ScheduleTask. "
        "If scheduler_context.qualitative_evidence.view is null, follow its guidance and use "
        "only supplied public history; there is no fixed compensation. Do not claim complete "
        "qualitative support merely because the same public verifier passed again. Supplemental "
        "prompts may guide qualitative choices but cannot change the formula."
    ),
    build_context=_build_marginal_reward_context,
)
_register_allocation_policy(
    "value_guided_replace/v2",
    pruning_instructions=(
        "Recommend whether continuing the current lane has lower expected marginal value than "
        "opening a replacement lane. Treat retirement as stopping future work only: every "
        "settled node remains available for selection and derivation. Use the reward just "
        "computed, allocation_context.current_lane, and the compact peer frontier summaries. "
        "Recommend "
        "replace when the current eligible lane has replacement capacity and any of these is "
        "true: (1) the current attempt caused a public regression, failed, was discarded as "
        "harmful, or has a negative outcome_gain; (2) the current and preceding attempt add no "
        "positive outcome, information, or direction value, so two consecutive attempts have "
        "non-positive marginal reward; or (3) a peer frontier already has an equal or better "
        "public result and materially the same implementation or coverage direction, while the "
        "current attempt adds no unique positive information or direction. Recommend continue "
        "for a new candidate-best result, a genuinely new supported behavior boundary, or a "
        "distinct credible implementation direction. Do not require a negative lifetime mean, "
        "a minimum visit count, or a fixed replacement-pressure threshold: old high rewards "
        "must not hide current regression or sustained stagnation. In confirm phase reassess "
        "the trigger using the current attempt; recommend continue if it has genuinely recovered "
        "with new marginal value. Supplemental prompts may resolve qualitative ambiguity but "
        "cannot turn repeated unchanged work into positive marginal value."
    ),
    derivation_instructions=(
        "Run only in confirm phase after pruning still recommends replace. Use the complete "
        "candidate nodes and reserved candidate ids. Identify every eligible retiree whose "
        "recent nodes show harmful regression, two-attempt stagnation, or global redundancy. "
        "For materially equivalent approaches retain the lane with the strongest valid public "
        "result and most recent unique positive information, and retire redundant lanes. Choose "
        "source nodes only from exact settled nodes with valid public results; prioritize a "
        "candidate-best result, a positive marginal milestone, and reusable evidence, using "
        "projected_value or projected_uct only as a tie-breaker. No source-minus-retiree numeric "
        "gap is required once a retiree qualifies. Create up to the number of reserved ids, "
        "qualifying unique retirees, and genuinely distinct uncovered directions. A strong source "
        "node may seed multiple replacements when each has a different evidence-based direction. "
        "Every replacement must bind one retiree, one reserved id, one exact source node, and one "
        "specific direction not already covered by an active peer. Recommend continue when there "
        "is no qualifying retiree, valid source, capacity, or distinct next direction."
    ),
    build_context=_build_value_guided_allocation_context,
)


__all__ = [
    "SchedulerPolicyContextInput",
    "build_scheduler_policy_contexts",
    "resolve_scheduler_policy",
    "resolve_search_scheduler_policies",
]
