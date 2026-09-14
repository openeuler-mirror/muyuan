from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from goal_plus.domain.adaptive.deterministic import (
    PriorityReplacePolicySpec,
    SearchSnapshot,
    SnapshotCandidate,
    SnapshotNode,
    compute_allocation_plan,
    compute_feature_view,
    snapshot_identity,
)
from goal_plus.domain.selection import (
    ScoreResolution,
    SelectionOption,
    SelectionSubjectKey,
    resolve_candidate_pool,
    select_winner,
)


@dataclass(frozen=True)
class Source:
    key: str
    direction: str
    values: dict[str, float]

    def resolve(self, option: SelectionOption) -> ScoreResolution:
        value = self.values.get(option.subject.candidate_id)
        return (
            ScoreResolution.resolved(value)
            if value is not None
            else ScoreResolution.unavailable()
        )


def option(candidate_id: str, hard_score: float, index: int) -> SelectionOption:
    return SelectionOption(
        SelectionSubjectKey(
            candidate_id,
            1,
            f"settlement_{candidate_id}",
            "f" * 64,
            "e" * 64,
        ),
        hard_score,
        index,
    )


def test_selection_accepts_zero_negative_scores_and_progressive_tolerance() -> None:
    options = [option("c001", -2.0, 0), option("c002", 0.0, 1), option("c003", -1.0, 2)]
    sources = {
        "quality_score": Source(
            "quality_score", "maximize", {"c001": 0.9, "c002": 0.86, "c003": 0.2}
        ),
        "hard_score": Source(
            "hard_score", "maximize", {item.subject.candidate_id: item.hard_score for item in options}
        ),
    }
    pool = resolve_candidate_pool(options, ["quality_score", "hard_score"], sources)
    assert pool.state == "ready"
    result = select_winner(
        pool.pool,
        ["quality_score", "hard_score"],
        [0.05, 0.0],
        sources,
    )
    assert result.winner.option.subject.candidate_id == "c002"
    assert [stage["survivor_count"] for stage in result.trace] == [2, 1]


def test_pending_required_source_waits_for_the_whole_pool() -> None:
    options = [option("c001", 1.0, 0)]

    @dataclass(frozen=True)
    class Pending:
        key: str = "quality_score"
        direction: str = "maximize"

        def resolve(self, option: SelectionOption) -> ScoreResolution:
            return ScoreResolution.pending()

    assert resolve_candidate_pool(
        options, ["quality_score"], {"quality_score": Pending()}
    ).state == "wait"


def test_snapshot_policy_is_replayable_and_allocates_no_candidate_id() -> None:
    policy = PriorityReplacePolicySpec()
    candidates = []
    for candidate_id, utility in (("c001", 0.9), ("c002", 0.1)):
        candidates.append(
            SnapshotCandidate(
                candidate_id=candidate_id,
                allocation_eligibility="eligible",
                allocation_depth=0,
                nodes=[
                    SnapshotNode(
                        candidate_id=candidate_id,
                        iteration=1,
                        settlement_id=f"settlement_{candidate_id}",
                        node_id=f"{candidate_id}/iteration-0001",
                        verifier_record_id=f"record_{candidate_id}",
                        verifier_subject_hash="f" * 64,
                        quality_score=utility,
                        allocation_scores={
                            "progress_reward": 0.0,
                            "allocation_utility": utility,
                        },
                    )
                ],
            )
        )
    body = {
        "schema_version": 2,
        "run_id": "run_1",
        "frozen_spec_id": "frozen_1",
        "policy_id": "priority_replace/v1",
        "max_parallel": 2,
        "max_candidates": 3,
        "candidates_total": 2,
        "candidates": [item.model_dump(mode="json") for item in candidates],
        "historical_decision_ids": [],
    }
    revision, digest = snapshot_identity(body)
    snapshot = SearchSnapshot(
        **body, snapshot_revision=revision, input_hash=digest
    )
    features = compute_feature_view(
        snapshot, policy.features
    )
    first = compute_allocation_plan(snapshot, features, policy=policy)
    second = compute_allocation_plan(snapshot, features, policy=policy)
    assert first == second and first is not None
    assert first.retire_candidate_ids == ["c002"]
    assert first.expansions[0].source_candidate_id == "c001"
    assert "candidate_id" not in first.expansions[0].model_dump()

    corrupted = snapshot.model_dump(mode="json")
    corrupted["candidates_total"] = 3
    with pytest.raises(ValidationError, match="identity does not match"):
        SearchSnapshot.model_validate(corrupted)

    forged_features = features.model_dump(mode="json")
    forged_features["candidates"][0]["latest_quality"] = 0.0
    with pytest.raises(ValidationError, match="hash does not match"):
        type(features).model_validate(forged_features)

    forged_plan = first.model_dump(mode="json")
    forged_plan["reason_codes"] = ["forged"]
    with pytest.raises(ValidationError, match="hash does not match"):
        type(first).model_validate(forged_plan)

    trend_only = compute_feature_view(snapshot, ["progress_trend/v1"])
    assert trend_only.candidates[0].latest_quality is None
    assert trend_only.candidates[0].latest_allocation_utility is None
    assert "priority" not in trend_only.candidates[0].model_dump()


def test_policy_can_derive_from_an_earlier_scored_attempt() -> None:
    policy = PriorityReplacePolicySpec()

    def node(candidate_id: str, iteration: int, utility: float) -> SnapshotNode:
        return SnapshotNode(
            candidate_id=candidate_id,
            iteration=iteration,
            settlement_id=f"settlement_{candidate_id}_{iteration}",
            node_id=f"{candidate_id}/iteration-{iteration:04d}",
            verifier_record_id=f"record_{candidate_id}_{iteration}",
            verifier_subject_hash="f" * 64,
            quality_score=utility,
            allocation_scores={
                "progress_reward": 0.0,
                "allocation_utility": utility,
            },
        )

    candidates = [
        SnapshotCandidate(
            candidate_id="c001",
            allocation_eligibility="eligible",
            allocation_depth=0,
            nodes=[node("c001", 1, 0.95), node("c001", 2, 0.4)],
        ),
        SnapshotCandidate(
            candidate_id="c002",
            allocation_eligibility="eligible",
            allocation_depth=0,
            nodes=[node("c002", 1, 0.1)],
        ),
    ]
    body = {
        "schema_version": 2,
        "run_id": "run_1",
        "frozen_spec_id": "frozen_1",
        "policy_id": "priority_replace/v1",
        "max_parallel": 2,
        "max_candidates": 3,
        "candidates_total": 2,
        "candidates": [item.model_dump(mode="json") for item in candidates],
        "historical_decision_ids": [],
    }
    revision, digest = snapshot_identity(body)
    snapshot = SearchSnapshot(**body, snapshot_revision=revision, input_hash=digest)
    features = compute_feature_view(
        snapshot, policy.features
    )

    plan = compute_allocation_plan(snapshot, features, policy=policy)

    assert plan is not None
    assert plan.retire_candidate_ids == ["c002"]
    assert plan.expansions[0].source_candidate_id == "c001"
    assert plan.expansions[0].source_iteration == 1
    assert plan.expansions[0].source_verifier_record_id == "record_c001_1"


def test_retired_candidate_score_rows_remain_available_as_sources() -> None:
    policy = PriorityReplacePolicySpec()

    def node(candidate_id: str, utility: float) -> SnapshotNode:
        return SnapshotNode(
            candidate_id=candidate_id,
            iteration=1,
            settlement_id=f"settlement_{candidate_id}",
            node_id=f"{candidate_id}/iteration-0001",
            verifier_record_id=f"record_{candidate_id}",
            verifier_subject_hash="f" * 64,
            quality_score=utility,
            allocation_scores={
                "progress_reward": 0.0,
                "allocation_utility": utility,
            },
        )

    candidates = [
        SnapshotCandidate(
            candidate_id="c001",
            allocation_eligibility="retired",
            allocation_depth=0,
            nodes=[node("c001", 0.95)],
        ),
        SnapshotCandidate(
            candidate_id="c002",
            allocation_eligibility="eligible",
            allocation_depth=0,
            nodes=[node("c002", 0.4)],
        ),
        SnapshotCandidate(
            candidate_id="c003",
            allocation_eligibility="eligible",
            allocation_depth=0,
            nodes=[node("c003", 0.1)],
        ),
    ]
    body = {
        "schema_version": 2,
        "run_id": "run_1",
        "frozen_spec_id": "frozen_1",
        "policy_id": "priority_replace/v1",
        "max_parallel": 2,
        "max_candidates": 4,
        "candidates_total": 3,
        "candidates": [item.model_dump(mode="json") for item in candidates],
        "historical_decision_ids": [],
    }
    revision, digest = snapshot_identity(body)
    snapshot = SearchSnapshot(**body, snapshot_revision=revision, input_hash=digest)
    features = compute_feature_view(
        snapshot, policy.features
    )

    plan = compute_allocation_plan(snapshot, features, policy=policy)

    assert plan is not None
    assert plan.retire_candidate_ids == ["c003"]
    assert plan.expansions[0].source_candidate_id == "c001"


def test_priority_replace_averages_normalized_lane_features() -> None:
    policy = PriorityReplacePolicySpec(switch_margin=0.5)

    def node(candidate_id: str, utility: float) -> SnapshotNode:
        return SnapshotNode(
            candidate_id=candidate_id,
            iteration=1,
            settlement_id=f"settlement_{candidate_id}",
            node_id=f"{candidate_id}/iteration-0001",
            verifier_record_id=f"record_{candidate_id}",
            verifier_subject_hash="f" * 64,
            quality_score=0.5,
            allocation_scores={
                "progress_reward": utility,
                "allocation_utility": utility,
            },
        )

    candidates = [
        SnapshotCandidate(
            candidate_id="c001",
            allocation_eligibility="eligible",
            allocation_depth=0,
            nodes=[node("c001", 0.9)],
        ),
        SnapshotCandidate(
            candidate_id="c002",
            allocation_eligibility="eligible",
            allocation_depth=0,
            nodes=[node("c002", 0.4)],
        ),
    ]
    body = {
        "schema_version": 2,
        "run_id": "run_1",
        "frozen_spec_id": "frozen_1",
        "policy_id": policy.policy,
        "max_parallel": 2,
        "max_candidates": 3,
        "candidates_total": 2,
        "candidates": [item.model_dump(mode="json") for item in candidates],
        "historical_decision_ids": [],
    }
    revision, digest = snapshot_identity(body)
    snapshot = SearchSnapshot(**body, snapshot_revision=revision, input_hash=digest)
    features = compute_feature_view(snapshot, policy.features)

    assert compute_allocation_plan(snapshot, features, policy=policy) is not None
    assert (
        compute_allocation_plan(
            snapshot,
            features,
            policy=policy.model_copy(update={"switch_margin": 0.51}),
        )
        is None
    )


def test_priority_replace_does_not_treat_quality_as_allocation_utility() -> None:
    policy = PriorityReplacePolicySpec(
        features=["progress_trend/v1"], switch_margin=0.1
    )
    candidates = [
        SnapshotCandidate(
            candidate_id=candidate_id,
            allocation_eligibility="eligible",
            allocation_depth=0,
            nodes=[
                SnapshotNode(
                    candidate_id=candidate_id,
                    iteration=1,
                    settlement_id=f"settlement_{candidate_id}",
                    node_id=f"{candidate_id}/iteration-0001",
                    verifier_record_id=f"record_{candidate_id}",
                    verifier_subject_hash="f" * 64,
                    quality_score=quality,
                    allocation_scores={"progress_reward": progress},
                )
            ],
        )
        for candidate_id, quality, progress in (
            ("c001", 0.95, 0.8),
            ("c002", 0.1, 0.2),
        )
    ]
    body = {
        "schema_version": 2,
        "run_id": "run_1",
        "frozen_spec_id": "frozen_1",
        "policy_id": policy.policy,
        "max_parallel": 2,
        "max_candidates": 3,
        "candidates_total": 2,
        "candidates": [item.model_dump(mode="json") for item in candidates],
        "historical_decision_ids": [],
    }
    revision, digest = snapshot_identity(body)
    snapshot = SearchSnapshot(**body, snapshot_revision=revision, input_hash=digest)
    features = compute_feature_view(snapshot, policy.features)

    assert compute_allocation_plan(snapshot, features, policy=policy) is None


def test_priority_replace_can_rebranch_a_single_complete_lane() -> None:
    policy = PriorityReplacePolicySpec()

    def node(iteration: int, utility: float) -> SnapshotNode:
        return SnapshotNode(
            candidate_id="c001",
            iteration=iteration,
            settlement_id=f"settlement_c001_{iteration}",
            node_id=f"c001/iteration-{iteration:04d}",
            verifier_record_id=f"record_c001_{iteration}",
            verifier_subject_hash="f" * 64,
            quality_score=utility,
            allocation_scores={
                "progress_reward": utility,
                "allocation_utility": utility,
            },
        )

    candidate = SnapshotCandidate(
        candidate_id="c001",
        allocation_eligibility="eligible",
        allocation_depth=0,
        nodes=[node(1, 0.9), node(2, 0.1)],
    )
    body = {
        "schema_version": 2,
        "run_id": "run_1",
        "frozen_spec_id": "frozen_1",
        "policy_id": policy.policy,
        "max_parallel": 1,
        "max_candidates": 2,
        "candidates_total": 1,
        "candidates": [candidate.model_dump(mode="json")],
        "historical_decision_ids": [],
    }
    revision, digest = snapshot_identity(body)
    snapshot = SearchSnapshot(**body, snapshot_revision=revision, input_hash=digest)

    plan = compute_allocation_plan(
        snapshot,
        compute_feature_view(snapshot, policy.features),
        policy=policy,
    )

    assert plan is not None
    assert plan.retire_candidate_ids == ["c001"]
    assert plan.expansions[0].source_node_id == "c001/iteration-0001"


def test_priority_replace_waits_for_every_active_lane_to_be_comparable() -> None:
    policy = PriorityReplacePolicySpec()
    candidates = [
        SnapshotCandidate(
            candidate_id="c001",
            allocation_eligibility="eligible",
            allocation_depth=0,
            nodes=[
                SnapshotNode(
                    candidate_id="c001",
                    iteration=1,
                    settlement_id="settlement_c001_1",
                    node_id="c001/iteration-0001",
                    verifier_record_id="record_c001_1",
                    verifier_subject_hash="f" * 64,
                    quality_score=0.9,
                    allocation_scores={
                        "progress_reward": 0.9,
                        "allocation_utility": 0.9,
                    },
                )
            ],
        ),
        SnapshotCandidate(
            candidate_id="c002",
            allocation_eligibility="eligible",
            allocation_depth=0,
            nodes=[],
        ),
    ]
    body = {
        "schema_version": 2,
        "run_id": "run_1",
        "frozen_spec_id": "frozen_1",
        "policy_id": policy.policy,
        "max_parallel": 2,
        "max_candidates": 3,
        "candidates_total": 2,
        "candidates": [item.model_dump(mode="json") for item in candidates],
        "historical_decision_ids": [],
    }
    revision, digest = snapshot_identity(body)
    snapshot = SearchSnapshot(**body, snapshot_revision=revision, input_hash=digest)

    assert (
        compute_allocation_plan(
            snapshot,
            compute_feature_view(snapshot, policy.features),
            policy=policy,
        )
        is None
    )


@pytest.mark.parametrize(
    ("max_parallel", "max_candidates", "candidates_total"),
    [(2, 3, 1), (1, 1, 1)],
)
def test_priority_replace_respects_capacity_boundaries(
    max_parallel: int,
    max_candidates: int,
    candidates_total: int,
) -> None:
    policy = PriorityReplacePolicySpec()
    candidate = SnapshotCandidate(
        candidate_id="c001",
        allocation_eligibility="eligible",
        allocation_depth=0,
        nodes=[
            SnapshotNode(
                candidate_id="c001",
                iteration=1,
                settlement_id="settlement_c001_1",
                node_id="c001/iteration-0001",
                verifier_record_id="record_c001_1",
                verifier_subject_hash="f" * 64,
                quality_score=0.9,
                allocation_scores={
                    "progress_reward": 0.9,
                    "allocation_utility": 0.9,
                },
            )
        ],
    )
    body = {
        "schema_version": 2,
        "run_id": "run_1",
        "frozen_spec_id": "frozen_1",
        "policy_id": policy.policy,
        "max_parallel": max_parallel,
        "max_candidates": max_candidates,
        "candidates_total": candidates_total,
        "candidates": [candidate.model_dump(mode="json")],
        "historical_decision_ids": [],
    }
    revision, digest = snapshot_identity(body)
    snapshot = SearchSnapshot(**body, snapshot_revision=revision, input_hash=digest)

    assert (
        compute_allocation_plan(
            snapshot,
            compute_feature_view(snapshot, policy.features),
            policy=policy,
        )
        is None
    )
