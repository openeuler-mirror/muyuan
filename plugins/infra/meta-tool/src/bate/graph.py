from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .models import SemanticType, ToolCall, Trace
from .normalization import CanonicalCall, TraceNormalizer


def occurrences_for(
    trace: Trace, start: int, length: int, normalizer: TraceNormalizer
) -> list[CallOccurrence]:
    """Canonicalize a contiguous trace slice for data-flow analysis."""
    return [
        CallOccurrence(
            trace_id=trace.trace_id,
            call_index=start + offset,
            canonical=normalizer.canonicalize(trace.calls[start + offset]),
        )
        for offset in range(length)
    ]


@dataclass(frozen=True)
class CallOccurrence:
    trace_id: str
    call_index: int
    canonical: CanonicalCall

    @property
    def raw(self) -> ToolCall:
        return self.canonical.raw


@dataclass(frozen=True)
class ActionToken:
    """One primitive transition in an observed execution path."""

    name: str
    calls: tuple[CallOccurrence, ...]

    @classmethod
    def primitive(cls, occurrence: CallOccurrence) -> "ActionToken":
        return cls(name=occurrence.canonical.name, calls=(occurrence,))

    @property
    def key(self) -> str:
        return self.calls[0].canonical.action_key

    @property
    def canonical_sequence(self) -> tuple[str, ...]:
        return tuple(call.canonical.name for call in self.calls)


@dataclass
class ExecutionPath:
    trace_id: str
    nodes: list[str]
    actions: list[ActionToken]

    def __post_init__(self) -> None:
        if len(self.nodes) != len(self.actions) + 1:
            raise ValueError(
                f"Invalid path {self.trace_id!r}: "
                f"{len(self.nodes)} nodes for {len(self.actions)} actions"
            )


@dataclass
class GraphNode:
    node_id: str
    state: Mapping[str, Any]
    min_depth: int
    trace_ids: set[str] = field(default_factory=set)
    visits: int = 0

    @property
    def weight(self) -> int:
        return len(self.trace_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.node_id,
            "state": dict(self.state),
            "min_depth": self.min_depth,
            "executions": self.weight,
            "visits": self.visits,
        }


@dataclass
class GraphEdge:
    source: str
    target: str
    trace_ids: set[str] = field(default_factory=set)
    traversals: int = 0
    action_counts: Counter[str] = field(default_factory=Counter)
    action_examples: dict[str, ActionToken] = field(default_factory=dict)

    @property
    def weight(self) -> int:
        """Number of executions containing the transition, as in the paper."""

        return len(self.trace_ids)

    @property
    def representative_action(self) -> ActionToken:
        if not self.action_counts:
            raise ValueError("Edge has no action")
        key = sorted(
            self.action_counts,
            key=lambda item: (-self.action_counts[item], item),
        )[0]
        return self.action_examples[key]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "weight": self.weight,
            "traversals": self.traversals,
            "actions": [
                {
                    "key": key,
                    "name": self.action_examples[key].name,
                    "count": count,
                    "primitive_sequence": list(
                        self.action_examples[key].canonical_sequence
                    ),
                }
                for key, count in sorted(
                    self.action_counts.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ],
        }


@dataclass
class GraphStats:
    nodes: int
    edges: int
    self_loops: int
    sinks: int
    average_in_degree: float
    total_edge_weight: int
    total_traversals: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": self.nodes,
            "edges": self.edges,
            "self_loops": self.self_loops,
            "sinks": self.sinks,
            "average_in_degree": self.average_in_degree,
            "total_edge_weight": self.total_edge_weight,
            "total_traversals": self.total_traversals,
        }


@dataclass
class StateGraph:
    nodes: dict[str, GraphNode]
    edges: dict[tuple[str, str], GraphEdge]
    root_id: str = "root"

    @classmethod
    def from_paths(
        cls,
        paths: Iterable[ExecutionPath],
        state_payloads: Mapping[str, Mapping[str, Any]],
    ) -> "StateGraph":
        nodes: dict[str, GraphNode] = {}
        edges: dict[tuple[str, str], GraphEdge] = {}

        for path in paths:
            for depth, node_id in enumerate(path.nodes):
                node = nodes.get(node_id)
                if node is None:
                    node = GraphNode(
                        node_id=node_id,
                        state=state_payloads.get(node_id, {}),
                        min_depth=depth,
                    )
                    nodes[node_id] = node
                node.min_depth = min(node.min_depth, depth)
                node.trace_ids.add(path.trace_id)
                node.visits += 1

            for index, action in enumerate(path.actions):
                source = path.nodes[index]
                target = path.nodes[index + 1]
                edge = edges.setdefault(
                    (source, target), GraphEdge(source=source, target=target)
                )
                edge.trace_ids.add(path.trace_id)
                edge.traversals += 1
                edge.action_counts[action.key] += 1
                edge.action_examples.setdefault(action.key, action)

        nodes.setdefault(
            "root",
            GraphNode(node_id="root", state={}, min_depth=0),
        )
        return cls(nodes=nodes, edges=edges)

    def outgoing(self, node_id: str, *, include_self: bool = True) -> list[GraphEdge]:
        edges = [
            edge for (source, _), edge in self.edges.items() if source == node_id
        ]
        if not include_self:
            edges = [edge for edge in edges if edge.source != edge.target]
        return sorted(edges, key=lambda edge: (edge.target, edge.source))

    def incoming(self, node_id: str, *, include_self: bool = True) -> list[GraphEdge]:
        edges = [
            edge for (_, target), edge in self.edges.items() if target == node_id
        ]
        if not include_self:
            edges = [edge for edge in edges if edge.source != edge.target]
        return sorted(edges, key=lambda edge: (edge.source, edge.target))

    def stats(self) -> GraphStats:
        self_loops = sum(
            1 for edge in self.edges.values() if edge.source == edge.target
        )
        sinks = sum(
            1
            for node_id in self.nodes
            if not self.outgoing(node_id, include_self=False)
        )
        indegrees = defaultdict(int)
        for edge in self.edges.values():
            if edge.source != edge.target:
                indegrees[edge.target] += 1
        average = (
            sum(indegrees.get(node_id, 0) for node_id in self.nodes)
            / len(self.nodes)
            if self.nodes
            else 0.0
        )
        return GraphStats(
            nodes=len(self.nodes),
            edges=len(self.edges),
            self_loops=self_loops,
            sinks=sinks,
            average_in_degree=average,
            total_edge_weight=sum(edge.weight for edge in self.edges.values()),
            total_traversals=sum(edge.traversals for edge in self.edges.values()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root_id,
            "stats": self.stats().to_dict(),
            "nodes": [
                node.to_dict() for node in sorted(self.nodes.values(), key=_node_key)
            ],
            "edges": [
                edge.to_dict()
                for edge in sorted(
                    self.edges.values(),
                    key=lambda item: (
                        self.nodes[item.source].min_depth,
                        item.source,
                        item.target,
                    ),
                )
            ],
        }

    def to_dot(self) -> str:
        lines = ["digraph meta_tool {", "  rankdir=LR;"]
        for node in sorted(self.nodes.values(), key=_node_key):
            short = "root" if node.node_id == "root" else node.node_id[:8]
            label = f"{short}\\nexec={node.weight}"
            shape = "doublecircle" if node.node_id == self.root_id else "circle"
            lines.append(
                f'  "{node.node_id}" [label="{label}", shape={shape}];'
            )
        for edge in sorted(
            self.edges.values(), key=lambda item: (item.source, item.target)
        ):
            action = edge.representative_action.name.replace('"', r"\"")
            lines.append(
                f'  "{edge.source}" -> "{edge.target}" '
                f'[label="{action} | w={edge.weight}"];'
            )
        lines.append("}")
        return "\n".join(lines) + "\n"


@dataclass
class BuildResult:
    graph: StateGraph
    paths: list[ExecutionPath]
    state_payloads: dict[str, Mapping[str, Any]]
    ignored_annotators: int


class StateGraphBuilder:
    def __init__(self, normalizer: TraceNormalizer | None = None) -> None:
        self.normalizer = normalizer or TraceNormalizer()

    def build(self, traces: Iterable[Trace]) -> BuildResult:
        paths: list[ExecutionPath] = []
        state_payloads: dict[str, Mapping[str, Any]] = {"root": {}}
        ignored_annotators = 0

        for trace in traces:
            state = self.normalizer.new_state()
            nodes = ["root"]
            actions: list[ActionToken] = []

            for call_index, raw_call in enumerate(trace.calls):
                call = self.normalizer.canonicalize(raw_call)
                if (
                    call.semantic_type is SemanticType.ANNOTATOR
                    and not self.normalizer.config.include_annotators
                ):
                    ignored_annotators += 1
                    continue

                state.apply(call)
                node_id = state.state_id()
                state_payloads.setdefault(node_id, state.payload())
                occurrence = CallOccurrence(
                    trace_id=trace.trace_id,
                    call_index=call_index,
                    canonical=call,
                )
                nodes.append(node_id)
                actions.append(ActionToken.primitive(occurrence))

            paths.append(
                ExecutionPath(
                    trace_id=trace.trace_id,
                    nodes=nodes,
                    actions=actions,
                )
            )

        graph = StateGraph.from_paths(paths, state_payloads)
        return BuildResult(
            graph=graph,
            paths=paths,
            state_payloads=state_payloads,
            ignored_annotators=ignored_annotators,
        )


def _node_key(node: GraphNode) -> tuple[int, str]:
    return node.min_depth, node.node_id
