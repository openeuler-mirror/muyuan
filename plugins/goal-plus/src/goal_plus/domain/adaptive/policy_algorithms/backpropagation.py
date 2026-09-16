"""Pure offline value backpropagation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class BackpropagatedStatistics:
    visits: tuple[int, ...]
    value_sums: tuple[float, ...]
    mean_values: tuple[float, ...]


def backpropagated_value_statistics_v1(
    *,
    parent_indices: Sequence[int | None],
    values: Sequence[float],
) -> BackpropagatedStatistics:
    """Rebuild subtree observation counts and value means without mutable nodes."""

    if len(parent_indices) != len(values):
        raise ValueError("parent indices and values must have equal length")
    size = len(values)
    parents: list[int | None] = []
    for index, parent in enumerate(parent_indices):
        if parent is not None and (
            isinstance(parent, bool)
            or not isinstance(parent, int)
            or parent < 0
            or parent >= size
        ):
            raise ValueError("parent index is outside the tree")
        if parent == index:
            raise ValueError("tree node cannot parent itself")
        parents.append(parent)
    numeric_values = [float(value) for value in values]
    if any(not math.isfinite(value) for value in numeric_values):
        raise ValueError("tree values must be finite")

    for start in range(size):
        seen: set[int] = set()
        current: int | None = start
        while current is not None:
            if current in seen:
                raise ValueError("parent indices contain a cycle")
            seen.add(current)
            current = parents[current]

    visits = [0] * size
    value_sums = [0.0] * size
    for index, value in enumerate(numeric_values):
        current: int | None = index
        while current is not None:
            visits[current] += 1
            value_sums[current] += value
            current = parents[current]
    means = [value_sums[index] / visits[index] for index in range(size)]
    return BackpropagatedStatistics(
        visits=tuple(visits),
        value_sums=tuple(value_sums),
        mean_values=tuple(means),
    )


__all__ = ["BackpropagatedStatistics", "backpropagated_value_statistics_v1"]
