from __future__ import annotations

import pytest

from goal_plus.domain.adaptive.models import (
    SearchSnapshot,
    SnapshotCandidate,
    SnapshotNode,
    snapshot_identity,
)
from goal_plus.domain.adaptive.policy import SweSearchUctPolicySpec, evaluate_allocation_policy
from goal_plus.domain.adaptive.uct_view import rebuild_uct_view


def _node(
    candidate_id: str,
    iteration: int,
    *,
    utility: float,
    progress: float,
    parent: str | None = None,
    edge: str = "initial",
) -> SnapshotNode:
    return SnapshotNode(
        candidate_id=candidate_id,
        iteration=iteration,
        node_id=f"{candidate_id}/iteration-{iteration:04d}",
        parent_node_id=parent,
        edge_kind=edge,
        settlement_id=f"settlement_{candidate_id}_{iteration}",
        verifier_record_id=f"record_{candidate_id}_{iteration}",
        verifier_subject_hash=(candidate_id[-1] * 64),
        quality_score=0.5,
        allocation_scores={
            "progress_reward": progress,
            "allocation_utility": utility,
        },
    )


def _snapshot(
    candidates: list[SnapshotCandidate],
    *,
    max_parallel: int,

) -> SearchSnapshot:
    candidates = [
        item.model_copy(
            update={
                "tip_node_id": item.nodes[-1].node_id if item.nodes else None
            }
        )
        for item in candidates
    ]
    body = {
        "schema_version": 6,
        "run_id": "run_uct",
        "frozen_spec_id": "frozen_uct",
        "policy_id": "swe_search_uct/v1",
        "max_parallel": max_parallel,
        "verifier_cutoff_sequence": 1,
        "candidates": [item.model_dump(mode="json") for item in candidates],
        "verifier_record_ids": [],
    }
    revision, digest = snapshot_identity(body)
    return SearchSnapshot(**body, snapshot_revision=revision, input_hash=digest)


def test_uct_view_rebuilds_depth_children_and_backpropagation() -> None:
    root = _node("c001", 1, utility=0.2, progress=0.5)
    continuation = _node(
        "c001",
        2,
        utility=0.6,
        progress=0.4,
        parent=root.node_id,
        edge="continuation",
    )
    expansion = _node(
        "c002",
        1,
        utility=0.8,
        progress=0.3,
        parent=continuation.node_id,
        edge="allocation_expansion",
    )
    other_root = _node("c003", 1, utility=0.4, progress=0.2)
    snapshot = _snapshot(
        [
            SnapshotCandidate(
                candidate_id="c001",
                allocation_depth=0,
                nodes=[root, continuation],
            ),
            SnapshotCandidate(
                candidate_id="c002",
                allocation_depth=1,
                nodes=[expansion],
            ),
            SnapshotCandidate(
                candidate_id="c003",
                allocation_depth=0,
                nodes=[other_root],
            ),
        ],
        max_parallel=2,
    )

    view = rebuild_uct_view(snapshot)

    assert [node.depth for node in view.nodes] == [1, 2, 3, 1]
    assert [node.child_indices for node in view.nodes] == [(1,), (2,), (), ()]
    assert [node.visit_count for node in view.nodes] == [3, 2, 1, 1]
    assert [node.mean_allocation_utility for node in view.nodes] == pytest.approx(
        [1.6 / 3, 0.7, 0.8, 0.4]
    )
    assert view.tip_indices == (1, 2, 3)
    assert "visit_count" not in snapshot.model_dump_json()


def test_swe_search_uct_uses_utility_not_progress_for_source_rank() -> None:
    snapshot = _snapshot(
        [
            SnapshotCandidate(
                candidate_id="c001",
                allocation_depth=0,
                nodes=[_node("c001", 1, utility=0.9, progress=-0.5)],
            ),
            SnapshotCandidate(
                candidate_id="c002",
                allocation_depth=0,
                nodes=[_node("c002", 1, utility=0.2, progress=1.0)],
            ),
        ],
        max_parallel=2,
    )

    plan = evaluate_allocation_policy(
        snapshot, policy=SweSearchUctPolicySpec(policy="swe_search_uct/v1")
    )

    assert plan is not None
    assert plan.expansions[0].source_candidate_id == "c001"
    assert plan.retire_candidate_ids == ["c002", "c001"]


def test_unscored_active_lane_is_a_warmup_no_op() -> None:
    snapshot = _snapshot(
        [
            SnapshotCandidate(
                candidate_id="c001",
                allocation_depth=0,
                nodes=[],
            ),
            SnapshotCandidate(
                candidate_id="c002",
                allocation_depth=0,
                nodes=[_node("c002", 1, utility=0.1, progress=0.1)],
            ),
        ],
        max_parallel=2,
    )

    assert evaluate_allocation_policy(
        snapshot, policy=SweSearchUctPolicySpec(policy="swe_search_uct/v1")
    ) is None


def test_low_reward_streak_is_a_hard_source_prune() -> None:
    root = _node("c001", 1, utility=1.0, progress=0.4)
    middle = _node(
        "c001",
        2,
        utility=1.0,
        progress=0.0,
        parent=root.node_id,
        edge="continuation",
    )
    tip = _node(
        "c001",
        3,
        utility=1.0,
        progress=-0.1,
        parent=middle.node_id,
        edge="continuation",
    )
    snapshot = _snapshot(
        [
            SnapshotCandidate(
                candidate_id="c001",
                allocation_depth=0,
                nodes=[root, middle, tip],
            ),
            SnapshotCandidate(
                candidate_id="c002",
                allocation_depth=0,
                nodes=[_node("c002", 1, utility=0.1, progress=0.5)],
            ),
        ],
        max_parallel=2,
    )
    policy = SweSearchUctPolicySpec(
        policy="swe_search_uct/v1",
        parameters={
            "max_children_per_node": 1,
            "low_reward_streak": 2,
            "early_depth_bonus_weight": 0,
            "late_depth_penalty_weight": 0,
        }
    )

    plan = evaluate_allocation_policy(snapshot, policy=policy)

    assert plan is not None
    assert plan.expansions[0].source_candidate_id == "c002"


def test_single_lane_can_fork_from_its_own_checkpoint() -> None:
    snapshot = _snapshot(
        [
            SnapshotCandidate(
                candidate_id="c001",
                allocation_depth=0,
                nodes=[_node("c001", 1, utility=0.8, progress=0.5)],
            )
        ],
        max_parallel=1,
    )

    plan = evaluate_allocation_policy(
        snapshot, policy=SweSearchUctPolicySpec(policy="swe_search_uct/v1")
    )

    assert plan is not None
    assert plan.retire_candidate_ids == ["c001"]
    assert plan.expansions[0].source_candidate_id == "c001"
    assert plan.expansions[0].direction_key.endswith("child-0001")
