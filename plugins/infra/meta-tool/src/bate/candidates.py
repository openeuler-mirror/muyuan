"""Candidate contracts and aligned occurrence helpers shared by BATE stages.

These helpers preserve provenance and contain no greedy candidate selector.
"""
from __future__ import annotations
import hashlib
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Sequence
from .dataflow import DataFlowSpan
from .graph import ActionToken, CallOccurrence, ExecutionPath
from .models import stable_json


@dataclass(frozen=True)
class OccurrenceMatch:
    trace_id: str
    start_index: int
    actions: tuple[ActionToken, ...]
    calls: tuple[CallOccurrence, ...]

    @property
    def canonical_sequence(self) -> tuple[str, ...]:
        return tuple(item.canonical.name for item in self.calls)


@dataclass
class MetaToolCandidate:
    candidate_id: str
    state_path: tuple[str, ...]
    canonical_sequence: tuple[str, ...]
    support: int
    occurrences: int
    purity: float
    edge_weights: tuple[int, ...]
    branch_dominance: tuple[float, ...]
    incremental_decisions_saved: int
    observed_llm_turn_occurrences: int = 0
    llm_turn_coverage: float = 0.0
    incremental_llm_calls_saved: int = 0
    mean_llm_calls_saved_per_observed_occurrence: float = 0.0
    internal_data_flows: int = 0
    varying_data_flows: int = 0
    constant_data_flows: int = 0
    caller_parameters: int = 0
    free_text_parameters: int = 0
    source_trace_ids: tuple[str, ...] = ()
    examples: tuple[tuple[CallOccurrence, ...], ...] = field(
        repr=False, default=()
    )

    @property
    def primitive_steps(self) -> int:
        return len(self.canonical_sequence)

    @property
    def min_branch_dominance(self) -> float:
        return min(self.branch_dominance, default=1.0)

    @cached_property
    def workflow_fingerprint(self) -> str:
        """Stable identity for the workflow, independent of graph/run-local IDs."""

        return _workflow_fingerprint(self.examples, self.canonical_sequence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.candidate_id,
            "state_path": list(self.state_path),
            "canonical_sequence": list(self.canonical_sequence),
            "support": self.support,
            "occurrences": self.occurrences,
            "purity": self.purity,
            "edge_weights": list(self.edge_weights),
            "branch_dominance": list(self.branch_dominance),
            "min_branch_dominance": self.min_branch_dominance,
            "primitive_steps": self.primitive_steps,
            "incremental_decisions_saved": self.incremental_decisions_saved,
            "observed_llm_turn_occurrences": (
                self.observed_llm_turn_occurrences
            ),
            "llm_turn_coverage": self.llm_turn_coverage,
            "incremental_llm_calls_saved": self.incremental_llm_calls_saved,
            "mean_llm_calls_saved_per_observed_occurrence": (
                self.mean_llm_calls_saved_per_observed_occurrence
            ),
            "internal_data_flows": self.internal_data_flows,
            "varying_data_flows": self.varying_data_flows,
            "constant_data_flows": self.constant_data_flows,
            "caller_parameters": self.caller_parameters,
            "free_text_parameters": self.free_text_parameters,
            "source_trace_ids": list(self.source_trace_ids),
            "workflow_fingerprint": self.workflow_fingerprint,
        }


def _sequence_occurrence_matches(
    occurrences: Sequence[tuple[ExecutionPath, int]],
    steps: int,
) -> list[OccurrenceMatch]:
    matches: list[OccurrenceMatch] = []
    for path, start in occurrences:
        actions = tuple(path.actions[start : start + steps])
        if len(actions) != steps:
            continue
        calls = tuple(call for action in actions for call in action.calls)
        matches.append(
            OccurrenceMatch(
                trace_id=path.trace_id,
                start_index=start,
                actions=actions,
                calls=calls,
            )
        )
    return matches


def _slice_sequence_matches(
    matches: Sequence[OccurrenceMatch], span: DataFlowSpan
) -> list[OccurrenceMatch]:
    selected: list[OccurrenceMatch] = []
    stop = span.end_index + 1
    for match in matches:
        actions = match.actions[span.start_index:stop]
        calls = tuple(call for action in actions for call in action.calls)
        selected.append(
            OccurrenceMatch(
                trace_id=match.trace_id,
                start_index=match.start_index + span.start_index,
                actions=actions,
                calls=calls,
            )
        )
    return selected


def _workflow_fingerprint(
    examples: Sequence[Sequence[CallOccurrence]],
    sequence: Sequence[str],
) -> str:
    """Hash structural call shapes, excluding state paths and ordinals.

    Canonical structural fingerprints preserve discriminator arguments while
    hiding ordinary runtime values. Taking the per-step set keeps the identity
    stable when the same workflow is observed in a different trace order, yet
    distinguishes workflows with different argument schemas.
    """

    step_shapes: list[list[str]] = []
    for step_index, name in enumerate(sequence):
        shapes = {
            (
                example[step_index].canonical.structural_fingerprint
                or example[step_index].canonical.fingerprint
            )
            for example in examples
            if step_index < len(example)
        }
        step_shapes.append(sorted(shapes))
    payload = {"sequence": list(sequence), "step_shapes": step_shapes}
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()
