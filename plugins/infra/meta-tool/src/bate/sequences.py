"""Branch-aware enumeration of exact contiguous tool sequences.

The unit of extraction here is an **exact tool-name sequence**, not a structural
signature with an ambiguous continuation. That single change is what lets two
templates that share a prefix ("...->spotify.login" and "...->venmo.login") both
count as deterministic instead of being merged into one branching candidate.
"""
from __future__ import annotations

import collections
from dataclasses import dataclass, field


@dataclass(frozen=True, order=True)
class Occurrence:
    trace_id: str
    start: int


@dataclass
class SequenceCandidate:
    sequence: tuple[str, ...]
    occurrences: list[Occurrence] = field(default_factory=list)

    @property
    def n_steps(self) -> int:
        return len(self.sequence)

    @property
    def support(self) -> int:
        """Distinct traces containing the sequence (the paper's threshold unit)."""
        return len({o.trace_id for o in self.occurrences})

    @property
    def occurrences_count(self) -> int:
        return len(self.occurrences)

    @property
    def prefix(self) -> tuple[str, ...]:
        return self.sequence[:-1]

    @property
    def decisions_saved_per_occurrence(self) -> int:
        """A k-step template replaces k LLM decisions with one template choice."""
        return max(self.n_steps - 1, 0)

    @property
    def total_decisions_saved(self) -> int:
        return self.decisions_saved_per_occurrence * self.occurrences_count

    def to_dict(self) -> dict:
        return {
            "sequence": list(self.sequence),
            "n_steps": self.n_steps,
            "support": self.support,
            "occurrences": self.occurrences_count,
            "decisions_saved_per_occurrence": self.decisions_saved_per_occurrence,
            "total_decisions_saved": self.total_decisions_saved,
        }


def _trace_names(trace) -> list[str]:
    return [call.name for call in trace.calls]


def enumerate_exact(
    traces,
    *,
    min_len: int = 2,
    max_len: int = 8,
    min_support: int = 3,
) -> list[SequenceCandidate]:
    """Every exact contiguous tool-name sequence with >= min_support distinct traces."""

    index: dict[tuple[str, ...], list[Occurrence]] = {}
    for trace in traces:
        names = _trace_names(trace)
        n = len(names)
        upper = min(max_len, n)
        for length in range(min_len, upper + 1):
            for start in range(0, n - length + 1):
                seq = tuple(names[start : start + length])
                index.setdefault(seq, []).append(Occurrence(trace.trace_id, start))

    out: list[SequenceCandidate] = []
    for seq, occs in index.items():
        cand = SequenceCandidate(sequence=seq, occurrences=sorted(occs))
        if cand.support >= min_support:
            out.append(cand)
    out.sort(key=lambda c: (-c.n_steps, -c.support, c.sequence))
    return out


def is_contiguous_subsequence(small: tuple[str, ...], big: tuple[str, ...]) -> bool:
    width = len(small)
    if width > len(big):
        return False
    return any(big[i : i + width] == small for i in range(len(big) - width + 1))


def prune_to_maximal(candidates: list[SequenceCandidate]) -> list[SequenceCandidate]:
    """Drop a sequence that is a contiguous subsequence of a longer kept one.

    Emitting both "a->b" and "a->b->c" is redundant: the executor would always
    prefer the longer one, and nesting inflates the catalog without adding
    decision savings.
    """
    ordered = sorted(candidates, key=lambda c: (-c.n_steps, -c.support, c.sequence))
    kept: list[SequenceCandidate] = []
    for cand in ordered:
        if any(is_contiguous_subsequence(cand.sequence, k.sequence) for k in kept):
            continue
        kept.append(cand)
    kept.sort(key=lambda c: (-c.total_decisions_saved, -c.support, c.sequence))
    return kept


def select_non_nested(
    candidates: list[SequenceCandidate],
    *,
    key=None,
) -> list[SequenceCandidate]:
    """Greedily keep mutually non-nested sequences, best score first.

    `prune_to_maximal` is wrong for this purpose: extending a sequence lowers its
    support, so the longest path often saves *fewer* decisions in total than a
    shorter prefix of it. On AppWorld the 3-step login template
    (16 occurrences x 2 = 32 saved) beats the 8-step extension
    (4 occurrences x 7 = 28 saved).

    Nesting is the real redundancy: "a->b" and "a->b->c" describe the same
    workflow at different resolutions, so we keep whichever scores higher and
    drop the other. The default score is total decisions saved.
    """

    def score(c: SequenceCandidate) -> int:
        return key(c) if key is not None else c.total_decisions_saved

    ordered = sorted(candidates, key=lambda c: (-score(c), -c.support, c.sequence))
    kept: list[SequenceCandidate] = []
    for cand in ordered:
        if any(
            is_contiguous_subsequence(cand.sequence, k.sequence)
            or is_contiguous_subsequence(k.sequence, cand.sequence)
            for k in kept
        ):
            continue
        kept.append(cand)
    return kept


def continuation_distribution(traces, sequence: tuple[str, ...]) -> collections.Counter:
    """Next-tool fan-out after `sequence`, over all traces.

    This is exactly the quantity the existing `_sequence_branch_dominance` turns
    into a hard gate. We keep it as *diagnostics*: it explains why the baseline
    rejected a template, and it identifies which templates are siblings.
    """

    counts: collections.Counter = collections.Counter()
    width = len(sequence)
    for trace in traces:
        names = _trace_names(trace)
        for start in range(0, len(names) - width):
            if tuple(names[start : start + width]) == sequence:
                counts[names[start + width]] += 1
    return counts


def sibling_groups(
    candidates: list[SequenceCandidate],
) -> dict[tuple[str, ...], list[SequenceCandidate]]:
    """Templates sharing a prefix whose continuations differ -> a choice set.

    Each member is individually deterministic. Picking among them is one LLM
    decision (or a deterministic discriminator), while the k-1 internal steps of
    whichever is chosen are still saved.
    """

    groups: dict[tuple[str, ...], list[SequenceCandidate]] = {}
    for cand in candidates:
        groups.setdefault(cand.prefix, []).append(cand)
    return {
        prefix: sorted(members, key=lambda c: (-c.support, c.sequence))
        for prefix, members in groups.items()
        if len({m.sequence[-1] for m in members}) > 1
    }
