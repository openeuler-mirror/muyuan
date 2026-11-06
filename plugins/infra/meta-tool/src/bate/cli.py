"""Extract branch-aware fixed workflows from explicit historical traces."""
from __future__ import annotations
import argparse
import dataclasses
import json
from pathlib import Path
from . import __version__, graph_source
from .io import load_traces
from .models import SemanticType
from .normalization import NormalizationConfig, ToolRule, TraceNormalizer
from .catalog import compile_admitted, write_outputs
from .determinism import assess_all, assess_all_graph
from .naming import assign_short_names
from .sequences import enumerate_exact, select_non_nested, sibling_groups


def _filter_kwargs(cls, payload: dict) -> dict:
    allowed = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in payload.items() if k in allowed}


def build_normalization(payload: dict):
    kwargs = _filter_kwargs(NormalizationConfig, dict(payload))
    if isinstance(kwargs.get("default_semantic_type"), str):
        kwargs["default_semantic_type"] = SemanticType(
            kwargs["default_semantic_type"]
        )
    rules = kwargs.pop("tool_rules", None)
    if rules is not None:
        converted = []
        for rule in rules:
            if not isinstance(rule, dict):
                converted.append(rule)
                continue
            rule_kwargs = _filter_kwargs(ToolRule, dict(rule))
            if isinstance(rule_kwargs.get("semantic_type"), str):
                rule_kwargs["semantic_type"] = SemanticType(
                    rule_kwargs["semantic_type"]
                )
            converted.append(ToolRule(**rule_kwargs))
        kwargs["tool_rules"] = tuple(converted)
    return NormalizationConfig(**kwargs)


def main(argv=None, *, on_templates=None):
    parser = argparse.ArgumentParser(prog="bate", description=__doc__)
    parser.add_argument("--version", action="version", version=f"meta-tool-online {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    ex = sub.add_parser("extract", help="Extract and compile BATE workflows")
    ex.add_argument("--traces", required=True, type=Path)
    ex.add_argument("--out", required=True, type=Path)
    ex.add_argument("--config", "--baseline-config", dest="config", type=Path)
    ex.add_argument("--extraction-config", type=Path)
    ex.add_argument("--source", choices=["graph", "raw"], default="graph")
    ex.add_argument("--min-len", type=int, default=2)
    ex.add_argument("--max-len", type=int, default=8)
    ex.add_argument("--min-support", type=int)
    ex.add_argument("--max-meta-tools", type=int, default=50)
    ex.add_argument("--naming", choices=["legacy", "stable-short"], default="legacy")
    ex.add_argument("--no-require-internal-data-flow", action="store_true")
    ex.add_argument("--min-varying-data-flows", type=int, default=1)
    ex.add_argument("--require-fully-bound", action="store_true")
    ex.add_argument("--allow-compiler-warnings", action="store_true")
    ex.add_argument("--keep-nested", action="store_true")
    ex.add_argument("--also-raw", action="store_true")
    # Compatibility with existing host invocations; no old baseline exists here.
    ex.add_argument("--no-baseline", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.min_len < 2 or args.max_len < args.min_len or args.max_meta_tools < 1:
        parser.error("require 2 <= min-len <= max-len and max-meta-tools >= 1")
    traces = load_traces(args.traces)
    config_payload = None
    if args.config:
        config_payload = json.loads(args.config.read_text())
        if not isinstance(config_payload, dict):
            parser.error("config must be an object")
        config_payload = config_payload.get("config", config_payload)
    if args.min_support is not None and args.min_support < 1:
        parser.error("min-support must be positive")
    norm_config = None
    if isinstance(config_payload, dict):
        nested = config_payload.get("normalization")
        if isinstance(nested, dict):
            norm_config = build_normalization(nested)
        elif any(
            key in config_payload
            for key in ("argument_matching", "default_semantic_type", "tool_rules")
        ):
            flat = {
                k: v
                for k, v in config_payload.items()
                if k not in ("extraction", "admission", "compiler")
            }
            norm_config = build_normalization(flat)

    norm = TraceNormalizer(norm_config or NormalizationConfig())
    require_idf = not args.no_require_internal_data_flow

    min_support = args.min_support
    if min_support is None:
        recorded = None
        if isinstance(config_payload, dict):
            recorded = (config_payload.get("extraction") or {}).get("threshold")
        if recorded is None and args.extraction_config and args.extraction_config.is_file():
            lane = json.loads(args.extraction_config.read_text())
            if isinstance(lane, dict):
                recorded = (lane.get("extraction") or {}).get("threshold")
        min_support = int(recorded) if recorded else 3

    if args.source == "graph":
        corpus = graph_source.build_corpus(traces, norm)
        enumerated = graph_source.enumerate_graph_candidates(
            corpus,
            min_steps=args.min_len,
            max_steps=args.max_len,
            min_support=min_support,
        )
        candidates = (
            enumerated if args.keep_nested
            else graph_source.select_non_nested(enumerated)
        )
        reports = assess_all_graph(
            candidates,
            corpus,
            require_internal_data_flow=require_idf,
            min_varying_data_flows=args.min_varying_data_flows,
            require_fully_bound=args.require_fully_bound,
        )
        siblings = graph_source.sibling_groups(candidates)
    else:
        enumerated = enumerate_exact(
            traces,
            min_len=args.min_len,
            max_len=args.max_len,
            min_support=min_support,
        )
        candidates = (
            enumerated if args.keep_nested else select_non_nested(enumerated)
        )
        reports = assess_all(
            candidates,
            traces,
            norm,
            require_internal_data_flow=require_idf,
            min_varying_data_flows=args.min_varying_data_flows,
            require_fully_bound=args.require_fully_bound,
        )
        siblings = sibling_groups(candidates)

    admitted_pairs = [
        (c, r) for c, r in zip(candidates, reports) if r.admitted
    ]

    if args.source == "graph" and args.also_raw:
        raw_enumerated = enumerate_exact(
            traces,
            min_len=args.min_len,
            max_len=args.max_len,
            min_support=min_support,
        )
        raw_candidates = (
            raw_enumerated if args.keep_nested
            else select_non_nested(raw_enumerated)
        )
        raw_reports = assess_all(
            raw_candidates,
            traces,
            norm,
            require_internal_data_flow=require_idf,
            min_varying_data_flows=args.min_varying_data_flows,
            require_fully_bound=args.require_fully_bound,
        )
        have = {tuple(c.sequence) for c, _ in admitted_pairs}
        for cand, rep in zip(raw_candidates, raw_reports):
            if rep.admitted and tuple(cand.sequence) not in have:
                admitted_pairs.append((cand, rep))
                have.add(tuple(cand.sequence))
        for key, members in sibling_groups(raw_candidates).items():
            siblings.setdefault(key, members)
        reports.extend(raw_reports)

    admitted_pairs.sort(
        key=lambda p: (-p[0].total_decisions_saved, -p[0].support)
    )
    templates = compile_admitted(
        admitted_pairs, traces, norm, max_meta_tools=args.max_meta_tools
    )

    warnings_rejected = 0
    if not args.allow_compiler_warnings:
        kept = [t for t in templates if not t.compiler_warnings]
        warnings_rejected = len(templates) - len(kept)
        templates = kept


    parameters = {
        "min_len": args.min_len, "max_len": args.max_len, "min_support": min_support,
        "require_internal_data_flow": require_idf,
        "min_varying_data_flows": args.min_varying_data_flows,
        "require_fully_bound": args.require_fully_bound,
        "require_no_compiler_warnings": not args.allow_compiler_warnings,
        "keep_nested": args.keep_nested, "max_meta_tools": args.max_meta_tools,
        "rejected_for_compiler_warnings": warnings_rejected,
    }
    if args.naming == "stable-short":
        renamed = assign_short_names([t.spec for t in templates])
        templates = [dataclasses.replace(t, spec=s) for t, s in zip(templates, renamed)]
        parameters["naming"] = "stable-short-v1"
    if on_templates is not None:
        on_templates(tuple(templates), tuple(traces), norm)
    summary = write_outputs(args.out, templates=templates, reports=reports,
                            siblings=siblings, traces=traces, parameters=parameters)
    print(json.dumps({"traces": len(traces), **summary["result"]}, ensure_ascii=False))
    return 0
