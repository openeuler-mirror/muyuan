"""Policy-private reconstruction of the transient SWE-Search UCT view."""

from __future__ import annotations

from dataclasses import dataclass

from goal_plus.domain.adaptive.models import SearchSnapshot
from goal_plus.domain.adaptive.policy_algorithms import (
    backpropagated_value_statistics_v1,
)


@dataclass(frozen=True)
class UctNodeView:
    node_id: str
    candidate_id: str
    parent_index: int | None
    child_indices: tuple[int, ...]
    depth: int
    child_count: int
    visit_count: int
    backed_up_utility_sum: float
    mean_allocation_utility: float
    progress_reward: float
    allocation_utility: float


@dataclass(frozen=True)
class UctTreeView:
    nodes: tuple[UctNodeView, ...]
    tip_indices: tuple[int, ...]


def rebuild_uct_view(snapshot: SearchSnapshot) -> UctTreeView:
    facts = [node for candidate in snapshot.candidates for node in candidate.nodes]
    indices = {node.node_id: index for index, node in enumerate(facts)}
    if len(indices) != len(facts):
        raise ValueError("UCT view requires unique snapshot nodes")

    parents: list[int | None] = []
    children: list[list[int]] = [[] for _item in facts]
    for index, node in enumerate(facts):
        parent = (
            indices.get(node.parent_node_id)
            if node.parent_node_id is not None
            else None
        )
        if node.parent_node_id is not None and parent is None:
            raise ValueError("UCT view contains a missing parent")
        parents.append(parent)
        if parent is not None:
            children[parent].append(index)

    depths: list[int | None] = [None] * len(facts)

    def depth_of(index: int, visiting: set[int]) -> int:
        if depths[index] is not None:
            return depths[index]
        if index in visiting:
            raise ValueError("UCT view contains a cycle")
        visiting.add(index)
        parent = parents[index]
        result = 1 if parent is None else depth_of(parent, visiting) + 1
        visiting.remove(index)
        depths[index] = result
        return result

    for index in range(len(facts)):
        depth_of(index, set())

    progress: list[float] = []
    utility: list[float] = []
    for node in facts:
        if set(node.allocation_scores) != {"progress_reward", "allocation_utility"}:
            raise ValueError("SWE-Search UCT nodes require both policy scores")
        reward = node.allocation_scores["progress_reward"]
        value = node.allocation_scores["allocation_utility"]
        if not -1.0 <= reward <= 1.0 or not 0.0 <= value <= 1.0:
            raise ValueError("SWE-Search UCT node scores are outside policy ranges")
        progress.append(reward)
        utility.append(value)
    statistics = backpropagated_value_statistics_v1(
        parent_indices=parents,
        values=utility,
    )

    nodes = tuple(
        UctNodeView(
            node_id=node.node_id,
            candidate_id=node.candidate_id,
            parent_index=parents[index],
            child_indices=tuple(children[index]),
            depth=int(depths[index]),
            child_count=len(children[index]),
            visit_count=statistics.visits[index],
            backed_up_utility_sum=statistics.value_sums[index],
            mean_allocation_utility=statistics.mean_values[index],
            progress_reward=progress[index],
            allocation_utility=utility[index],
        )
        for index, node in enumerate(facts)
    )
    tip_indices = tuple(
        indices[candidate.tip_node_id]
        for candidate in snapshot.candidates
        if candidate.tip_node_id is not None
    )
    return UctTreeView(nodes=nodes, tip_indices=tip_indices)


__all__ = ["UctNodeView", "UctTreeView", "rebuild_uct_view"]
