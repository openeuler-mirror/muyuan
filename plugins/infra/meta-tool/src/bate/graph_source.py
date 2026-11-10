"""Generate branch-specific candidates from normalized execution paths.

The current builder supplies primitive action paths, not an iteratively
compressed graph. Enumerate consecutive action-name sequences, preserve their
original occurrences, split by canonical primitive sequence, then refine to a
connected data-flow span. Argument-shape mismatches are checked downstream;
grouping by action name alone does not guarantee shape alignment.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .dataflow import analyze_data_flow
from .candidates import _sequence_occurrence_matches, _slice_sequence_matches
from .graph import StateGraphBuilder


@dataclass
class GraphCandidate:
    """A branch-split group of graph-aligned occurrences."""

    sequence: tuple[str, ...]        # canonical primitive names, flattened
    structural: tuple[str, ...]      # normalized action names
    examples: tuple = ()             # aligned tuples of CallOccurrence
    trace_ids: tuple[str, ...] = ()
    support: int = 0
    occurrences: int = 0

    @property
    def n_steps(self) -> int:
        return len(self.sequence)

    @property
    def occurrences_count(self) -> int:
        """Alias kept for parity with sequences.SequenceCandidate."""

        return self.occurrences

    @property
    def decisions_saved_per_occurrence(self) -> int:
        return max(self.n_steps - 1, 0)

    @property
    def total_decisions_saved(self) -> int:
        return self.decisions_saved_per_occurrence * self.occurrences

    @property
    def prefix(self) -> tuple[str, ...]:
        return self.sequence[:-1]

    def to_dict(self) -> dict:
        return {
            "sequence": list(self.sequence),
            "structural": list(self.structural),
            "n_steps": self.n_steps,
            "support": self.support,
            "occurrences": self.occurrences,
            "total_decisions_saved": self.total_decisions_saved,
        }


@dataclass
class GraphCorpus:
    paths: list = field(default_factory=list)
    build: object = None
    graph: object = None


def build_corpus(traces, norm) -> GraphCorpus:
    builder = StateGraphBuilder(norm)
    build = builder.build(list(traces))
    return GraphCorpus(paths=list(build.paths), build=build, graph=build.graph)


def enumerate_graph_candidates(
    corpus: GraphCorpus,
    *,
    min_steps: int = 2,
    max_steps: int = 8,
    min_support: int = 3,
    max_bridge_steps: int = 2,
) -> list[GraphCandidate]:
    """Enumerate observed sequences and refine each aligned occurrence group."""

    by_structural: dict[tuple[str, ...], list] = {}
    for path in corpus.paths:
        names = [action.name for action in path.actions]
        upper = min(max_steps, len(names))
        for steps in range(min_steps, upper + 1):
            for start in range(0, len(names) - steps + 1):
                structural = tuple(names[start : start + steps])
                by_structural.setdefault(structural, []).append((path, start))

    out: list[GraphCandidate] = []
    for structural, occurrences in by_structural.items():
        if len({path.trace_id for path, _ in occurrences}) < min_support:
            continue
        matches = _sequence_occurrence_matches(occurrences, len(structural))

        groups: dict[tuple[str, ...], list] = {}
        for match in matches:
            groups.setdefault(match.canonical_sequence, []).append(match)

        for _sequence, group in groups.items():
            examples = [match.calls for match in group]
            if len({len(example) for example in examples}) != 1:
                continue
            try:
                analysis = analyze_data_flow(examples)
            except ValueError:
                continue
            spans = analysis.connected_spans(
                min_steps=min_steps, max_bridge_steps=max_bridge_steps
            )
            if not spans:
                continue
            span = max(spans, key=lambda s: s.score)
            refined = _slice_sequence_matches(group, span)
            if not refined:
                continue
            sequences = {m.canonical_sequence for m in refined}
            if len(sequences) != 1:
                continue
            sequence = next(iter(sequences))
            trace_ids = tuple(sorted({m.trace_id for m in refined}))
            if len(trace_ids) < min_support:
                continue
            out.append(
                GraphCandidate(
                    sequence=sequence,
                    structural=tuple(a.name for a in refined[0].actions),
                    examples=tuple(m.calls for m in refined),
                    trace_ids=trace_ids,
                    support=len(trace_ids),
                    occurrences=len(refined),
                )
            )

    # de-duplicate identical (sequence, occurrence-set) results
    unique: dict = {}
    for cand in out:
        key = (cand.sequence, cand.trace_ids, cand.occurrences)
        unique.setdefault(key, cand)
    result = list(unique.values())
    result.sort(key=lambda c: (-c.total_decisions_saved, -c.support, c.sequence))
    return result


def structural_continuation(corpus: GraphCorpus, structural: tuple[str, ...]):
    """Next-action fan-out after `structural` on observed execution paths.

    Kept as a diagnostic, not an admission gate.
    """

    import collections

    counts: collections.Counter = collections.Counter()
    width = len(structural)
    for path in corpus.paths:
        names = [action.name for action in path.actions]
        for start in range(0, len(names) - width):
            if tuple(names[start : start + width]) == structural:
                counts[names[start + width]] += 1
    return counts


def select_non_nested(candidates: list[GraphCandidate]) -> list[GraphCandidate]:
    """Keep mutually non-nested branch groups, best decision-savings first."""

    from .sequences import is_contiguous_subsequence

    ordered = sorted(
        candidates, key=lambda c: (-c.total_decisions_saved, -c.support, c.sequence)
    )
    kept: list[GraphCandidate] = []
    for cand in ordered:
        if any(
            is_contiguous_subsequence(cand.sequence, k.sequence)
            or is_contiguous_subsequence(k.sequence, cand.sequence)
            for k in kept
        ):
            continue
        kept.append(cand)
    return kept


def sibling_groups(candidates: list[GraphCandidate]) -> dict:
    """Branch groups sharing a structural prefix but differing in continuation."""

    groups: dict[tuple[str, ...], list[GraphCandidate]] = {}
    for cand in candidates:
        groups.setdefault(cand.structural, []).append(cand)
    return {
        key: sorted(members, key=lambda c: (-c.support, c.sequence))
        for key, members in groups.items()
        if len({m.sequence for m in members}) > 1
    }
