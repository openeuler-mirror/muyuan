"""Pi transport adapter for the local BATE extractor, never a second algorithm.

The CLI owns branch splitting, determinism, compilation and stable naming.
This module only supplies files and adds host admission/provenance metadata.
"""
from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from bate.admission import auto_admit_specs, LEGACY_SUPPORTED_TOOLS
from bate.cli import main as extract
from bate.io import load_specs, load_traces, write_json
from bate.models import Trace
from bate.models import stable_json
from bate.catalog import build_examples


def discover_online(payload: Mapping[str, Any]) -> dict[str, Any]:
    output_value = payload.get("outputDir")
    if not isinstance(output_value, str) or not output_value.strip():
        raise ValueError("outputDir is required")
    output = Path(output_value).resolve()
    config = dict(payload.get("config") or {})
    if (config.get("optimization") or {}).get("enabled", False):
        raise ValueError("BATE does not use a normalization-rule optimization loop")
    if payload.get("tracePath"):
        traces = load_traces(payload["tracePath"])
    else:
        raw = (payload.get("trace") or {}).get("traces")
        if not isinstance(raw, list):
            raise ValueError("trace.traces must be an array")
        traces = [Trace.from_dict(item, default_id=f"pi-trace-{i}")
                  for i, item in enumerate(raw)]
    if not traces:
        raise ValueError("No traces supplied")
    if len({t.trace_id for t in traces}) != len(traces):
        raise ValueError("Trace IDs must be unique")
    normalization = dict(config.get("normalization") or {})
    normalization.update(argument_matching="structural")
    extraction = dict(config.get("extraction") or {})
    admission = dict(config.get("admission") or {})
    adapter = dict(payload.get("adapter") or {})
    supported = set(adapter.get("supportedTools", LEGACY_SUPPORTED_TOOLS))
    observed = set(adapter.get("observedTools", supported))
    output.mkdir(parents=True, exist_ok=True)
    trace_path = output / "input_traces.json"
    config_path = output / "normalization_config.json"
    write_json(trace_path, {"traces": [t.to_dict() for t in traces]})
    write_json(config_path, {"normalization": normalization, "extraction": extraction})
    argv = [
        "extract", "--traces", str(trace_path), "--out", str(output),
        "--config", str(config_path), "--source", "graph", "--also-raw",
        "--naming", "stable-short",
        "--min-support", str(extraction.get("threshold", 3)),
        "--min-len", str(extraction.get("min_steps", 2)),
        "--max-len", str(extraction.get("max_steps", 8)),
        "--max-meta-tools", str(extraction.get("max_meta_tools", 50)),
        "--min-varying-data-flows", str(extraction.get("min_varying_data_flows", 1)),
    ]
    if not extraction.get("require_internal_data_flow", True):
        argv.append("--no-require-internal-data-flow")
    if extraction.get("require_fully_bound", False):
        argv.append("--require-fully-bound")
    if not (admission.get("policy") or {}).get("require_no_compiler_warnings", True):
        argv.append("--allow-compiler-warnings")
    turn_evidence = {}

    def observe_templates(templates, source_traces, normalizer):
        for template in templates:
            examples = build_examples(template.candidate, source_traces, normalizer)
            observed_occurrences = 0
            savings = 0
            for example in examples:
                turns = [call.raw.turn_id for call in example]
                if turns and all(turn is not None for turn in turns):
                    observed_occurrences += 1
                    savings += max(0, len({stable_json(t) for t in turns}) - 1)
            turn_evidence[template.spec.name] = {
                "observed_llm_turn_occurrences": observed_occurrences,
                "llm_turn_coverage": observed_occurrences / len(examples) if examples else 0,
                "observed_incremental_llm_calls_saved": savings,
                "mean_llm_calls_saved_per_observed_occurrence":
                    savings / observed_occurrences if observed_occurrences else 0,
                "llm_turn_evidence_source": "bate-aligned-occurrences",
            }

    # CLI output is not part of the JSON-lines transport.
    with redirect_stdout(io.StringIO()):
        result = extract(argv, on_templates=observe_templates)
    if result:
        raise RuntimeError(f"BATE extraction failed with status {result}")
    original = load_specs(output / "bate_only_catalog.json")
    by_id = {t.trace_id: t for t in traces}
    specs = []
    for spec in original:
        evidence = [by_id[i] for i in spec.metadata.get("source_trace_ids", []) if i in by_id]
        schemas = {}
        conflicts = {}
        for name in {step.tool for step in spec.steps}:
            values = {
                str(t.metadata.get("tool_schema_fingerprints", {}).get(name))
                for t in evidence
                if isinstance(t.metadata.get("tool_schema_fingerprints"), Mapping)
                and t.metadata["tool_schema_fingerprints"].get(name) not in (None, "", "unknown")
            }
            if len(values) == 1:
                schemas[name] = next(iter(values))
            elif values:
                conflicts[name] = sorted(values)
        specs.append(replace(spec, metadata={
            **spec.metadata,
            **turn_evidence[spec.name],
            "extractor": "bate-branch-aware",
            "generalization_mode": "structural",
            "task_goals": list(dict.fromkeys(str(t.metadata["goal"]) for t in evidence
                                             if t.metadata.get("goal")))[:8],
            "distinct_sessions": len({str(t.metadata.get("session_id") or
                                          t.metadata.get("sessionId") or t.trace_id)
                                      for t in evidence}),
            "project_tags": sorted({str(v) for t in evidence for v in t.metadata.get("project_tags", [])}),
            "providers": sorted({str(v) for t in evidence for v in t.metadata.get("providers", [])}),
            "required_tool_schema_fingerprints": schemas,
            "tool_schema_conflicts": conflicts,
            "lifecycle_state": "provisional",
        }))
    admitted, rejected = auto_admit_specs(specs, admission, supported)
    write_json(output / "meta_tools.json", {"meta_tools": [s.to_dict() for s in specs]})
    write_json(output / "active_meta_tools.json", {"meta_tools": [s.to_dict() for s in admitted]})
    # Keep BATE rejection diagnostics intact; Pi admission is a separate stage.
    write_json(output / "pi_rejected_meta_tools.json", {"rejected": rejected})
    summary = json.loads((output / "extraction_summary.json").read_text())
    direct = sum(s.metadata.get("execution_guard") == "none" for s in admitted)
    guarded = sum(s.metadata.get("execution_guard") == "confirm_each_run" for s in admitted)
    report = {
        **summary,
        "meta_tools": {"compiled": len(specs), "active": len(admitted), "skipped": {}},
        "pi_meta_tool_admission": {"discovered": len(specs), "admitted": len(admitted),
                                   "direct": direct, "guarded": guarded, "rejected": rejected},
        "pi_meta_tool_adapter": {"executable_tools": sorted(supported),
                                 "observed_tools": sorted(observed),
                                 "observed_but_not_executable": sorted(observed - supported)},
    }
    write_json(output / "report.json", report)
    return {
        "extractor": "bate-branch-aware", "output": str(output), "traces": len(traces),
        "toolCalls": sum(len(t.calls) for t in traces),
        "discoveredMetaTools": len(specs), "metaTools": len(admitted),
        "activeMetaTools": len(admitted), "directMetaTools": direct,
        "guardedMetaTools": guarded, "rejectedMetaTools": len(rejected),
        "discoveredNames": [s.name for s in specs], "names": [s.name for s in admitted],
        "skipped": {},
    }
