"""SWE-Search UCT allocation policy and Goal Plus lane adaptation."""

from __future__ import annotations

import math
import re

from goal_plus.domain.adaptive.models import (
    AllocationExpansionIntent,
    AllocationPlan,
    SearchSnapshot,
)
from goal_plus.domain.adaptive.policy import SweSearchUctPolicySpec
from goal_plus.domain.adaptive.policy_algorithms import (
    swe_search_depth_terms_v1,
    trailing_low_reward_streak_v1,
    uct_exploration_v1,
)
from goal_plus.domain.adaptive.uct_view import UctTreeView, rebuild_uct_view
from goal_plus.domain.serialization import canonical_json, sha256_text


def _path_rewards(view: UctTreeView, index: int) -> tuple[float, ...]:
    rewards: list[float] = []
    current: int | None = index
    while current is not None:
        node = view.nodes[current]
        rewards.append(node.progress_reward)
        current = node.parent_index
    rewards.reverse()
    return tuple(rewards)


def _uct_score(
    view: UctTreeView,
    index: int,
    *,
    policy: SweSearchUctPolicySpec,
) -> float:
    node = view.nodes[index]
    parent_visits = (
        view.nodes[node.parent_index].visit_count
        if node.parent_index is not None
        else len(view.nodes)
    )
    exploration = uct_exploration_v1(
        parent_visits=parent_visits,
        node_visits=node.visit_count,
        exploration_weight=policy.parameters.exploration_weight,
    )
    depth = swe_search_depth_terms_v1(
        depth=node.depth,
        bonus_weight=policy.parameters.early_depth_bonus_weight,
        bonus_decay=policy.parameters.early_depth_bonus_decay,
        penalty_weight=policy.parameters.late_depth_penalty_weight,
    )
    score = (
        node.mean_allocation_utility
        + exploration
        + depth.early_depth_bonus
        - depth.late_depth_penalty
    )
    if not math.isfinite(score):
        raise ValueError("SWE-Search UCT score must be finite")
    return score


def evaluate_swe_search_uct_v1(
    snapshot: SearchSnapshot,
    *,
    policy: SweSearchUctPolicySpec,
) -> AllocationPlan | None:
    if snapshot.policy_id != policy.policy:
        raise ValueError("allocation policy does not match the snapshot")
    view = rebuild_uct_view(snapshot)
    if len(view.tip_indices) < snapshot.max_parallel:
        return None
    fact_by_id = {
        node.node_id: node
        for candidate in snapshot.candidates
        for node in candidate.nodes
    }

    scored: dict[int, float] = {}
    source_indices: list[int] = []
    for index, node in enumerate(view.nodes):
        abandoned = trailing_low_reward_streak_v1(
            rewards=_path_rewards(view, index),
            threshold=policy.parameters.low_reward_threshold,
            patience=policy.parameters.low_reward_streak,
        ).should_abandon
        if (
            node.depth < policy.parameters.max_depth
            and node.child_count < policy.parameters.max_children_per_node
            and not abandoned
        ):
            scored[index] = _uct_score(view, index, policy=policy)
            source_indices.append(index)
    if not source_indices:
        return None

    def identity(index: int) -> tuple[str, int, str, str]:
        fact = fact_by_id.get(view.nodes[index].node_id)
        if fact is None:
            raise ValueError("UCT result cannot be bound to its snapshot node")
        return (
            fact.candidate_id,
            fact.iteration,
            fact.settlement_id,
            fact.verifier_record_id,
        )

    tip_scores = {index: _uct_score(view, index, policy=policy) for index in view.tip_indices}
    retire_ids = [view.nodes[index].candidate_id for index in sorted(
        view.tip_indices, key=lambda index: (tip_scores[index], *identity(index)))]

    expansions = []
    for index in sorted(source_indices, key=lambda index: (-scored[index], *identity(index))):
        node = view.nodes[index]
        fact = fact_by_id[node.node_id]
        direction_key = re.sub(r"[^a-zA-Z0-9_.:-]", "_",
            f"swe_search_uct:v1:{node.node_id}:child-{node.child_count + 1:04d}")
        expansions.append(AllocationExpansionIntent(
            source_candidate_id=fact.candidate_id, source_iteration=fact.iteration,
            source_settlement_id=fact.settlement_id, source_node_id=fact.node_id,
            source_verifier_record_id=fact.verifier_record_id, direction_key=direction_key,
            eligible_retirements=([item for item in retire_ids if item != node.candidate_id]
                                  if snapshot.max_parallel > 1 else retire_ids),
            rationale="ranked non-abandoned SWE-Search UCT expansion"))
    body = {
        "schema_version": 5,
        "based_on_snapshot_revision": snapshot.snapshot_revision,
        "policy_id": snapshot.policy_id,
        "retire_candidate_ids": retire_ids,
        "expansions": [item.model_dump(mode="json") for item in expansions],
        "reason_codes": ["swe_search_uct_best_expandable_node"],
    }
    return AllocationPlan(
        **body,
        plan_hash=sha256_text(canonical_json(body)),
    )


__all__ = ["evaluate_swe_search_uct_v1"]
