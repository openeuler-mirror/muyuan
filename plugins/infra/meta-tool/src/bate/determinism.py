"""Check data-flow consistency within each fixed primitive sequence.

Parent prefix fan-out is diagnostic, not an admission gate. This checks whether
bindings can be replayed without an intermediate model decision; it does not
prove task correctness or deterministic results from external services.
"""
from __future__ import annotations

import collections
from dataclasses import dataclass, field

from .dataflow import analyze_data_flow
from .graph import occurrences_for
from .sequences import SequenceCandidate, continuation_distribution


@dataclass
class DeterminismReport:
    sequence: tuple[str, ...]
    n_steps: int
    support: int
    occurrences: int
    fully_bound: bool = False
    steps_fully_bound: tuple[bool, ...] = ()
    internal_flows: int = 0
    varying_flows: int = 0
    constant_flows: int = 0
    caller_parameters: int = 0
    free_text_parameters: int = 0
    inconsistent_steps: tuple[int, ...] = ()
    bindings: list[dict] = field(default_factory=list)
    parent_fanout: dict[str, int] = field(default_factory=dict)
    parent_dominance: float = 1.0
    structural: tuple[str, ...] = ()
    admitted: bool = False
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "sequence": list(self.sequence),
            "structural": list(self.structural),
            "n_steps": self.n_steps,
            "support": self.support,
            "occurrences": self.occurrences,
            "fully_bound": self.fully_bound,
            "steps_fully_bound": list(self.steps_fully_bound),
            "internal_data_flows": self.internal_flows,
            "varying_data_flows": self.varying_flows,
            "constant_data_flows": self.constant_flows,
            "caller_parameters": self.caller_parameters,
            "free_text_parameters": self.free_text_parameters,
            "inconsistent_steps": list(self.inconsistent_steps),
            "parent_fanout": dict(self.parent_fanout),
            "parent_dominance": round(self.parent_dominance, 4),
            "admitted": self.admitted,
            "reasons": list(self.reasons),
        }


def _analyse(
    sequence: tuple[str, ...],
    examples,
    *,
    support: int,
    require_internal_data_flow: bool,
    min_varying_data_flows: int,
    require_fully_bound: bool,
) -> DeterminismReport:
    """Binding evidence for one occurrence group, verified across all of it."""

    report = DeterminismReport(
        sequence=sequence,
        n_steps=len(sequence),
        support=support,
        occurrences=len(examples),
    )
    if not examples:
        report.reasons.append("no occurrences")
        return report
    lengths = {len(example) for example in examples}
    if len(lengths) != 1:
        report.reasons.append(
            f"occurrences have inconsistent step counts: {sorted(lengths)}"
        )
        return report

    try:
        analysis = analyze_data_flow(examples)
    except ValueError as exc:
        report.reasons.append(f"data-flow analysis failed: {exc}")
        return report

    steps_bound: list[bool] = []
    inconsistent: list[int] = []
    for step_index, step in enumerate(analysis.steps):
        steps_bound.append(step.fully_bound_for_bridge)
        if step.inconsistent_paths:
            inconsistent.append(step_index)
        for argument in step.arguments:
            if argument.kind == "flow":
                report.internal_flows += 1
                if argument.dependency is not None and argument.dependency.is_varying:
                    report.varying_flows += 1
                else:
                    report.constant_flows += 1
                if argument.dependency is not None:
                    report.bindings.append(
                        {
                            "step": step_index,
                            "argument": ".".join(str(p) for p in argument.path),
                            "from_step": argument.dependency.source_step,
                            "result_path": list(argument.dependency.result_path),
                            "distinct_values": argument.dependency.distinct_values,
                        }
                    )
            elif argument.kind == "parameter":
                report.caller_parameters += 1
            elif argument.kind == "free_text":
                report.free_text_parameters += 1

    report.steps_fully_bound = tuple(steps_bound)
    report.inconsistent_steps = tuple(inconsistent)
    report.fully_bound = all(steps_bound)

    reasons: list[str] = []
    if require_fully_bound and not report.fully_bound:
        if inconsistent:
            reasons.append(
                "inconsistent argument paths in steps "
                + ",".join(str(i + 1) for i in inconsistent)
            )
        if report.free_text_parameters:
            reasons.append(
                f"{report.free_text_parameters} free-text argument(s) cannot be bound"
            )
    if require_internal_data_flow and report.internal_flows < 1:
        reasons.append("no internal cross-step data flow")
    if require_internal_data_flow and report.varying_flows < min_varying_data_flows:
        reasons.append(
            f"varying_data_flows={report.varying_flows} < {min_varying_data_flows}"
        )
    report.reasons = reasons
    report.admitted = not reasons
    return report


def _fanout_from_counts(counts: collections.Counter, report: DeterminismReport) -> None:
    report.parent_fanout = dict(counts.most_common())
    total = sum(counts.values())
    report.parent_dominance = (max(counts.values()) / total) if total else 1.0


def assess(
    candidate: SequenceCandidate,
    traces,
    norm,
    *,
    require_internal_data_flow: bool = True,
    min_varying_data_flows: int = 1,
    require_fully_bound: bool = False,
) -> DeterminismReport:
    """Assess a raw contiguous candidate (legacy source)."""

    by_id = {trace.trace_id: trace for trace in traces}
    length = candidate.n_steps
    examples = [
        occurrences_for(by_id[occ.trace_id], occ.start, length, norm)
        for occ in candidate.occurrences
    ]
    report = _analyse(
        candidate.sequence,
        examples,
        support=candidate.support,
        require_internal_data_flow=require_internal_data_flow,
        min_varying_data_flows=min_varying_data_flows,
        require_fully_bound=require_fully_bound,
    )
    _fanout_from_counts(
        continuation_distribution(traces, candidate.prefix), report
    )
    return report


def assess_graph(
    candidate,
    corpus=None,
    *,
    require_internal_data_flow: bool = True,
    min_varying_data_flows: int = 1,
    require_fully_bound: bool = False,
) -> DeterminismReport:
    """Assess a graph-aligned branch group (preferred source)."""

    report = _analyse(
        candidate.sequence,
        candidate.examples,
        support=candidate.support,
        require_internal_data_flow=require_internal_data_flow,
        min_varying_data_flows=min_varying_data_flows,
        require_fully_bound=require_fully_bound,
    )
    report.structural = tuple(getattr(candidate, "structural", ()) or ())
    if corpus is not None:
        from .graph_source import structural_continuation

        _fanout_from_counts(
            structural_continuation(corpus, report.structural), report
        )
    return report


def assess_all(candidates, traces, norm, **kwargs) -> list[DeterminismReport]:
    return [assess(c, traces, norm, **kwargs) for c in candidates]


def assess_all_graph(candidates, corpus, **kwargs) -> list[DeterminismReport]:
    return [assess_graph(c, corpus, **kwargs) for c in candidates]
