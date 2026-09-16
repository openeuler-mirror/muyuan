"""Pure trailing low-reward detection."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class LowRewardStreak:
    streak: int
    should_abandon: bool


def trailing_low_reward_streak_v1(
    *,
    rewards: Sequence[float],
    threshold: float,
    patience: int,
) -> LowRewardStreak:
    limit = float(threshold)
    if not math.isfinite(limit):
        raise ValueError("low reward threshold must be finite")
    if isinstance(patience, bool) or not isinstance(patience, int) or patience < 1:
        raise ValueError("low reward patience must be a positive integer")
    values = [float(value) for value in rewards]
    if any(not math.isfinite(value) for value in values):
        raise ValueError("rewards must be finite")
    streak = 0
    for value in reversed(values):
        if value > limit:
            break
        streak += 1
    return LowRewardStreak(
        streak=streak,
        should_abandon=streak >= patience,
    )


__all__ = ["LowRewardStreak", "trailing_low_reward_streak_v1"]
