from __future__ import annotations

import pytest
from pydantic import ValidationError

from goal_plus.domain.adaptive.models import (
    SearchSnapshot,
    SnapshotCandidate,
    SnapshotNode,
    snapshot_identity,
)
from goal_plus.domain.adaptive.policy import (
    SweSearchUctPolicySpec,
)


def _node(
    candidate_id: str,
    utility: float,
    progress: float,
    *,
    iteration: int = 1,
    parent_node_id: str | None = None,
    edge_kind: str = "initial",
) -> SnapshotNode:
    return SnapshotNode(
        candidate_id=candidate_id,
        iteration=iteration,
        node_id=f"{candidate_id}/iteration-{iteration:04d}",
        parent_node_id=parent_node_id,
        edge_kind=edge_kind,
        settlement_id=f"settlement_{candidate_id}_{iteration}",
        verifier_record_id=f"record_{candidate_id}_{iteration}",
        verifier_subject_hash="a" * 64,
        quality_score=utility,
        allocation_scores={
            "progress_reward": progress,
            "allocation_utility": utility,
        },
    )


def _snapshot(
    candidates: list[SnapshotCandidate],
    *,
    policy_id: str = "swe_search_uct/v1",
    max_parallel: int = 2,

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
        "run_id": "run_1",
        "frozen_spec_id": "frozen_1",
        "policy_id": policy_id,
        "max_parallel": max_parallel,
        "verifier_cutoff_sequence": 1,
        "candidates": [item.model_dump(mode="json") for item in candidates],
        "verifier_record_ids": [],
    }
    revision, digest = snapshot_identity(body)
    return SearchSnapshot(**body, snapshot_revision=revision, input_hash=digest)


def test_policy_spec_is_strict_and_owns_parameters() -> None:
    policy = SweSearchUctPolicySpec(policy="swe_search_uct/v1")
    assert policy.parameters.max_children_per_node == 3
    assert policy.parameters.max_depth == 20

    with pytest.raises(ValidationError, match="Extra inputs"):
        SweSearchUctPolicySpec(policy="swe_search_uct/v1", unexpected=True)
    with pytest.raises(ValidationError, match="Extra inputs"):
        SweSearchUctPolicySpec(policy="swe_search_uct/v1", max_depth=10)


def test_snapshot_requires_canonical_exact_lineage() -> None:
    root = _node("c001", 0.7, 0.5)
    child = _node(
        "c002",
        0.6,
        -0.2,
        parent_node_id=root.node_id,
        edge_kind="allocation_expansion",
    )
    snapshot = _snapshot(
        [
            SnapshotCandidate(
                candidate_id="c001",
                allocation_depth=0,
                nodes=[root],
            ),
            SnapshotCandidate(
                candidate_id="c002",
                allocation_depth=1,
                nodes=[child],
            ),
        ],
        max_parallel=1,
    )
    assert snapshot.candidates[1].nodes[0].parent_node_id == root.node_id

    body = snapshot.model_dump(
        mode="json", exclude={"snapshot_revision", "input_hash"}
    )
    body["candidates"] = list(reversed(body["candidates"]))
    revision, digest = snapshot_identity(body)
    with pytest.raises(ValidationError, match="canonical"):
        SearchSnapshot(**body, snapshot_revision=revision, input_hash=digest)

    body = snapshot.model_dump(
        mode="json", exclude={"snapshot_revision", "input_hash"}
    )
    body["candidates"][1]["nodes"][0]["parent_node_id"] = "missing/node"
    revision, digest = snapshot_identity(body)
    with pytest.raises(ValidationError, match="missing parent"):
        SearchSnapshot(**body, snapshot_revision=revision, input_hash=digest)
