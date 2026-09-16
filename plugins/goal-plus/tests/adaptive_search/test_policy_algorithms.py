from __future__ import annotations

import math

import pytest

from goal_plus.domain.adaptive.policy_algorithms import (
    backpropagated_value_statistics_v1,
    swe_search_depth_terms_v1,
    trailing_low_reward_streak_v1,
    uct_exploration_v1,
)


def test_backpropagation_matches_hand_calculated_tree() -> None:
    # 0 -> (1, 2), 1 -> 3
    result = backpropagated_value_statistics_v1(
        parent_indices=[None, 0, 0, 1],
        values=[0.2, 0.4, 0.8, 1.0],
    )

    assert result.visits == (4, 2, 1, 1)
    assert result.value_sums == pytest.approx((2.4, 1.4, 0.8, 1.0))
    assert result.mean_values == pytest.approx((0.6, 0.7, 0.8, 1.0))


def test_uct_formula_golden_vector() -> None:
    exploration = uct_exploration_v1(
        parent_visits=10,
        node_visits=2,
        exploration_weight=0.01,
    )
    depth = swe_search_depth_terms_v1(
        depth=3,
        bonus_weight=2.0,
        bonus_decay=0.8,
        penalty_weight=0.008,
    )

    assert exploration == pytest.approx(0.01 * math.sqrt(math.log(10) / 2))
    assert depth.early_depth_bonus == pytest.approx(2 * math.exp(-1.6))
    assert depth.late_depth_penalty == pytest.approx(0.008 * math.sqrt(3))
    assert (
        0.6
        + exploration
        + depth.early_depth_bonus
        - depth.late_depth_penalty
    ) == pytest.approx(1.0006664597)


def test_reward_streak_counts_only_the_trailing_run() -> None:
    assert trailing_low_reward_streak_v1(
        rewards=[-0.5, -0.2, 0.4], threshold=0, patience=2
    ).streak == 0
    result = trailing_low_reward_streak_v1(
        rewards=[-0.5, 0.4, 0.0, -0.2], threshold=0, patience=2
    )
    assert result.streak == 2
    assert result.should_abandon is True
    assert trailing_low_reward_streak_v1(
        rewards=[], threshold=0, patience=2
    ).should_abandon is False
