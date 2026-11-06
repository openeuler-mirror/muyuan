"""Emit meta-tool specs for admitted branch-aware templates.

The specs are produced by the local `bate.compiler.MetaToolCompiler`, retaining
the existing catalog format. The only semantic change is
that `branch_dominance` is left empty: for a fully-specified exact sequence the
within-template next-step determinism is 1.0 by construction, and prefix fan-out
is recorded separately as a sibling diagnostic.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .compiler import CompilerOptions, MetaToolCompiler
from .candidates import MetaToolCandidate
from .graph import occurrences_for
from .spec import MetaToolSpec
from .determinism import DeterminismReport
from .sequences import SequenceCandidate


def candidate_identifier(index: int, sequence: tuple[str, ...]) -> str:
    joined = "_".join(sequence).replace(".", "_").replace("-", "_")
    digest = hashlib.sha256("|".join(sequence).encode("utf-8")).hexdigest()[:8]
    return f"meta_tool_{index:02d}_{joined}_{digest}"


def build_examples(candidate: SequenceCandidate, traces, norm):
    # Preserve the exact normalized occurrences and data-flow span selected by
    # the graph stage; do not reconstruct them by slicing raw trace indices.
    prebuilt = getattr(candidate, "examples", None)
    if prebuilt:
        return tuple(prebuilt)
    by_id = {trace.trace_id: trace for trace in traces}
    return tuple(
        tuple(
            occurrences_for(
                by_id[occ.trace_id], occ.start, candidate.n_steps, norm
            )
        )
        for occ in candidate.occurrences
    )


@dataclass
class CompiledTemplate:
    candidate: SequenceCandidate
    report: DeterminismReport
    spec: MetaToolSpec

    @property
    def compiler_warnings(self) -> list[str]:
        meta = getattr(self.spec, "metadata", {}) or {}
        return list(meta.get("compiler_warnings", []))


def compile_admitted(
    pairs: list[tuple[SequenceCandidate, DeterminismReport]],
    traces,
    norm,
    *,
    max_meta_tools: int = 50,
) -> list[CompiledTemplate]:
    compiler = MetaToolCompiler(CompilerOptions())
    out: list[CompiledTemplate] = []
    for index, (candidate, report) in enumerate(pairs[:max_meta_tools], start=1):
        examples = build_examples(candidate, traces, norm)
        # GraphCandidate carries trace_ids directly and occurrences as a count;
        # SequenceCandidate carries a list of occurrence objects.
        trace_ids = getattr(candidate, "trace_ids", None)
        if not trace_ids:
            trace_ids = tuple(sorted({o.trace_id for o in candidate.occurrences}))
        meta_candidate = MetaToolCandidate(
            candidate_id=candidate_identifier(index, candidate.sequence),
            state_path=candidate.sequence,
            canonical_sequence=candidate.sequence,
            support=report.support,
            occurrences=report.occurrences,
            purity=1.0,
            edge_weights=tuple(report.support for _ in candidate.sequence),
            # Corrected semantics: the sequence pins every step, so there is no
            # within-template branch. Prefix fan-out lives in `report`.
            branch_dominance=(),
            incremental_decisions_saved=candidate.decisions_saved_per_occurrence,
            internal_data_flows=report.internal_flows,
            varying_data_flows=report.varying_flows,
            constant_data_flows=report.constant_flows,
            caller_parameters=report.caller_parameters,
            free_text_parameters=report.free_text_parameters,
            source_trace_ids=tuple(trace_ids),
            examples=examples,
        )
        spec = compiler.compile(meta_candidate)
        out.append(CompiledTemplate(candidate=candidate, report=report, spec=spec))
    return out


def write_outputs(
    out_dir: Path,
    *,
    templates: list[CompiledTemplate],
    reports: list[DeterminismReport],
    siblings: dict,
    traces,
    parameters: dict,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)

    bate_catalog = [t.spec.to_dict() for t in templates]
    catalog = bate_catalog

    (out_dir / "catalog.json").write_text(
        json.dumps({"meta_tools": catalog}, ensure_ascii=False, indent=2)
    )
    (out_dir / "active_meta_tools.json").write_text(
        json.dumps({"meta_tools": catalog}, ensure_ascii=False, indent=2)
    )
    (out_dir / "meta_tools.json").write_text(
        json.dumps({"meta_tools": catalog}, ensure_ascii=False, indent=2)
    )
    (out_dir / "bate_only_catalog.json").write_text(
        json.dumps({"meta_tools": bate_catalog}, ensure_ascii=False, indent=2)
    )

    rejected = [
        {"sequence": list(r.sequence), "reasons": list(r.reasons)}
        for r in reports
        if not r.admitted
    ]
    (out_dir / "rejected_meta_tools.json").write_text(
        json.dumps({"policy": "bate-branch-aware-v1", "rejected": rejected},
                   ensure_ascii=False, indent=2)
    )

    sibling_payload = {
        " -> ".join(prefix): [
            {
                "sequence": list(m.sequence),
                "support": m.support,
                "occurrences": m.occurrences_count,
            }
            for m in members
        ]
        for prefix, members in siblings.items()
    }

    total_calls = sum(len(t.calls) for t in traces)
    summary = {
        "extractor": "bate-branch-aware",
        "version": "0.1.0",
        "parameters": parameters,
        "input": {
            "traces": len(traces),
            "tool_calls": total_calls,
            "successful_traces": sum(t.success is True for t in traces),
        },
        "result": {
            "admitted_meta_tools": len(templates),
            "emitted_meta_tools": len(catalog),
            "candidates_assessed": len(reports),
            "rejected": len(rejected),
            "sibling_groups": len(siblings),
            "total_decisions_saved": sum(
                t.candidate.total_decisions_saved for t in templates
            ),
        },
        "templates": [
            {
                "name": t.spec.name,
                "sequence": list(t.candidate.sequence),
                "support": t.candidate.support,
                "occurrences": t.candidate.occurrences_count,
                "decisions_saved": t.candidate.total_decisions_saved,
                "parent_fanout": t.report.parent_fanout,
                "parent_dominance": round(t.report.parent_dominance, 4),
                "varying_flows": t.report.varying_flows,
                "compiler_warnings": t.compiler_warnings,
                "bindings": t.report.bindings,
            }
            for t in templates
        ],
        "rejected_details": [r.to_dict() for r in reports if not r.admitted],
        "siblings": sibling_payload,
    }
    (out_dir / "extraction_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2)
    )
    return summary
