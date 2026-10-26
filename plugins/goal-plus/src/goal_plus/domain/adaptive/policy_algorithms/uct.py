"""Pure UCT exploration and SWE-Search depth terms."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class DepthTerms:
    early_depth_bonus: float
    late_depth_penalty: float


def _finite_nonnegative(name: str, value: float) -> float:
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return result


def uct_exploration_v1(
    *,
    parent_visits: int,
    node_visits: int,
    exploration_weight: float,
) -> float:
    if (
        isinstance(parent_visits, bool)
        or isinstance(node_visits, bool)
        or not isinstance(parent_visits, int)
        or not isinstance(node_visits, int)
    ):
        raise ValueError("UCT visit counts must be integers")
    if parent_visits < node_visits or node_visits < 0:
        raise ValueError("UCT requires parent_visits >= node_visits >= 0")
    weight = _finite_nonnegative("exploration_weight", exploration_weight)
    if node_visits == 0:
        return math.inf
    if parent_visits < 1:
        raise ValueError("visited UCT nodes require a visited parent")
    return weight * math.sqrt(math.log(parent_visits) / node_visits)


def swe_search_depth_terms_v1(
    *,
    depth: int,
    bonus_weight: float,
    bonus_decay: float,
    penalty_weight: float,
) -> DepthTerms:
    if isinstance(depth, bool) or not isinstance(depth, int) or depth < 1:
        raise ValueError("SWE-Search node depth must be a positive integer")
    alpha = _finite_nonnegative("bonus_weight", bonus_weight)
    beta = _finite_nonnegative("bonus_decay", bonus_decay)
    gamma = _finite_nonnegative("penalty_weight", penalty_weight)
    bonus = alpha * math.exp(-beta * (depth - 1))
    penalty = gamma * math.sqrt(depth)
    if not math.isfinite(bonus) or not math.isfinite(penalty):
        raise ValueError("SWE-Search depth terms must be finite")
    return DepthTerms(
        early_depth_bonus=bonus,
        late_depth_penalty=penalty,
    )


__all__ = ["DepthTerms", "swe_search_depth_terms_v1", "uct_exploration_v1"]
