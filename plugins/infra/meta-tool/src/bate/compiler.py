from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .dataflow import analyze_data_flow
from .candidates import MetaToolCandidate
from .models import stable_json
from .spec import MetaToolSpec, StepSpec, input_binding, step_binding


_MISSING = object()
_PLACEHOLDER_RE = re.compile(r"\{[A-Z][A-Z0-9_]*\}")


@dataclass(frozen=True)
class CompilerOptions:
    parameterize_all_scalars: bool = False
    sensitive_argument_pattern: str = (
        r"(?i)(password|passwd|token|secret|api.?key|credential|cookie)"
    )
    unstable_runtime_argument_pattern: str = (
        r"(?i)(^|\.)(ref|element_id|node_id|backend_node_id|target_id)$"
    )
    bind_stable_runtime_dependencies: bool = False
    parameterize_unbound_runtime_arguments: bool = False


class MetaToolCompiler:
    """Compile a discovered hot path into an executable, parameterized spec.

    AWO's paper hand-builds benchmark meta-tools after discovery. This compiler
    fills that engineering gap conservatively:

    * values that vary across occurrences become meta-tool inputs;
    * values copied from a previous tool result become data-flow bindings;
    * stable literals remain literals unless marked sensitive;
    * no example argument values are written to metadata.
    """

    def __init__(self, options: CompilerOptions | None = None) -> None:
        self.options = options or CompilerOptions()
        self._sensitive = re.compile(self.options.sensitive_argument_pattern)
        self._unstable_runtime_argument = re.compile(
            self.options.unstable_runtime_argument_pattern
        )

    def compile(self, candidate: MetaToolCandidate) -> MetaToolSpec:
        if not candidate.examples:
            raise ValueError(f"Candidate {candidate.candidate_id} has no examples")
        examples = [tuple(example) for example in candidate.examples]
        expected_steps = len(candidate.canonical_sequence)
        if any(len(example) != expected_steps for example in examples):
            raise ValueError("Candidate examples have inconsistent step counts")
        flow_analysis = analyze_data_flow(examples)
        flow_signature = flow_analysis.span_signature(0, expected_steps - 1)

        properties: dict[str, Any] = {}
        required: list[str] = []
        series_to_input: dict[tuple[str, ...], str] = {}
        used_names: set[str] = set()
        free_text_inputs: set[str] = set()
        fallback_step_bindings: list[dict[str, str]] = []
        stable_runtime_bindings: list[dict[str, Any]] = []
        runtime_local_inputs: list[str] = []
        warnings: list[str] = []
        steps: list[StepSpec] = []

        for step_index, canonical_name in enumerate(candidate.canonical_sequence):
            first = examples[0][step_index]
            template = copy.deepcopy(dict(first.raw.arguments))
            path_sets = [
                set(_leaf_paths(dict(example[step_index].raw.arguments)))
                for example in examples
            ]
            common_paths = set.intersection(*path_sets) if path_sets else set()
            all_paths = set.union(*path_sets) if path_sets else set()
            if common_paths != all_paths:
                warnings.append(
                    f"step {step_index + 1} has inconsistent argument shapes; "
                    "only paths present in every example were generalized"
                )

            for path in sorted(common_paths, key=_path_sort_key):
                values = [
                    _get_path(example[step_index].raw.arguments, path)
                    for example in examples
                ]
                canonical_values = [
                    _get_path(example[step_index].canonical.arguments, path)
                    for example in examples
                ]

                evidence = flow_analysis.argument(step_index, path)
                dependency = evidence.dependency if evidence is not None else None
                series = tuple(_series_item(value) for value in values)
                # Equal constants are weak evidence: unrelated fields such as
                # requested_k=1 and goal_revision=1 can coincide in every
                # observed run.  Only a dependency whose value varies across
                # supporting occurrences is safe to compile as a runtime
                # step binding.  A stable dependency may be compiled only when
                # the argument is explicitly classified as runtime-local and
                # the caller opted into this conservative provenance rule.
                argument_path = ".".join(str(part) for part in path)
                runtime_local_argument = bool(
                    self._unstable_runtime_argument.search(argument_path)
                )
                parameterize_runtime_argument = bool(
                    runtime_local_argument
                    and dependency is None
                    and self.options.parameterize_unbound_runtime_arguments
                )
                stable_runtime_dependency = bool(
                    dependency is not None
                    and not dependency.is_varying
                    and self.options.bind_stable_runtime_dependencies
                    and self._unstable_runtime_argument.search(argument_path)
                )
                if dependency is not None and (
                    dependency.is_varying or stable_runtime_dependency
                ):
                    fallback_input = series_to_input.get(series)
                    _set_path(
                        template,
                        path,
                        step_binding(
                            f"step_{dependency.source_step + 1}",
                            dependency.result_path,
                            **(
                                {
                                    "fallback": input_binding(fallback_input),
                                }
                                if fallback_input is not None
                                else {}
                            ),
                        ),
                    )
                    if fallback_input is not None:
                        fallback_step_bindings.append(
                            {
                                "step_id": f"step_{step_index + 1}",
                                "argument_path": _display_path(path),
                                "fallback_input": fallback_input,
                            }
                        )
                    if stable_runtime_dependency:
                        stable_runtime_bindings.append(
                            {
                                "step_id": f"step_{step_index + 1}",
                                "argument_path": _display_path(path),
                                "source_step": dependency.source_step + 1,
                                "result_path": list(dependency.result_path),
                            }
                        )
                    continue

                if runtime_local_argument and not parameterize_runtime_argument:
                    warnings.append(
                        f"step {step_index + 1} argument {_display_path(path)} "
                        "looks runtime-local and has no prior-result binding; "
                        "replay may target stale state"
                    )

                sensitive = bool(
                    self._sensitive.search(
                        ".".join(
                            str(part) for part in path if isinstance(part, str)
                        )
                    )
                )
                placeholder = any(
                    isinstance(value, str) and _PLACEHOLDER_RE.search(value)
                    for value in canonical_values
                    if value is not _MISSING
                )
                stable = _all_equal(values)
                if (
                    stable
                    and not sensitive
                    and not placeholder
                    and not parameterize_runtime_argument
                    and not self.options.parameterize_all_scalars
                ):
                    continue

                input_name = series_to_input.get(series)
                if input_name is None:
                    input_name = _unique_input_name(
                        path=path,
                        tool_name=canonical_name,
                        step_index=step_index,
                        used=used_names,
                    )
                    series_to_input[series] = input_name
                    used_names.add(input_name)
                    properties[input_name] = {
                        **_infer_schema(values),
                        "description": (
                            f"Value for {canonical_name} argument "
                            f"{_display_path(path)}"
                        ),
                    }
                required.append(input_name)
                if parameterize_runtime_argument:
                    runtime_local_inputs.append(input_name)
                if evidence is not None and evidence.kind == "free_text":
                    free_text_inputs.add(input_name)
                _set_path(template, path, input_binding(input_name))

            steps.append(
                StepSpec(
                    step_id=f"step_{step_index + 1}",
                    tool=canonical_name,
                    arguments=template,
                )
            )

        name = _valid_tool_name(candidate.candidate_id)
        description = (
            "Deterministic meta-tool: "
            + " -> ".join(candidate.canonical_sequence)
            + f". Discovered in {candidate.support} executions."
        )
        metadata: dict[str, Any] = {
            "source": "arXiv:2601.22037 independent implementation",
            "candidate_id": candidate.candidate_id,
            "workflow_fingerprint": candidate.workflow_fingerprint,
            "support": candidate.support,
            "occurrences": candidate.occurrences,
            "sequence_purity": candidate.purity,
            "min_branch_dominance": candidate.min_branch_dominance,
            "estimated_incremental_decisions_saved": (
                candidate.incremental_decisions_saved
            ),
            "observed_llm_turn_occurrences": (
                candidate.observed_llm_turn_occurrences
            ),
            "llm_turn_coverage": candidate.llm_turn_coverage,
            "observed_incremental_llm_calls_saved": (
                candidate.incremental_llm_calls_saved
            ),
            "mean_llm_calls_saved_per_observed_occurrence": (
                candidate.mean_llm_calls_saved_per_observed_occurrence
            ),
            "step_semantic_types": [
                call.canonical.semantic_type.value
                for call in candidate.examples[0]
            ],
            "side_effect_free": all(
                call.canonical.semantic_type.value != "mutator"
                for call in candidate.examples[0]
            ),
            **flow_signature.to_dict(),
            # `internal_data_flows` describes executable bindings, while the
            # broader observed count preserves all exact provenance evidence.
            "observed_internal_data_flows": flow_signature.internal_flows,
            "internal_data_flows": (
                flow_signature.varying_flows + len(stable_runtime_bindings)
            ),
            "data_flow_connected": (
                flow_signature.varying_flows + len(stable_runtime_bindings) > 0
            ),
            "has_varying_data_flow": flow_signature.varying_flows > 0,
            "stable_runtime_data_flows": len(stable_runtime_bindings),
            "runtime_local_data_flow_verified": bool(stable_runtime_bindings),
            "runtime_local_input_count": len(runtime_local_inputs),
            "data_flow_evidence": "verified_across_all_supporting_occurrences",
            "caller_input_count": len(properties),
            "free_text_input_count": len(free_text_inputs),
            "free_text_inputs": sorted(free_text_inputs),
            "source_trace_ids": list(candidate.source_trace_ids),
            "requires_domain_review": True,
        }
        if fallback_step_bindings:
            metadata["fallback_step_binding_count"] = len(
                fallback_step_bindings
            )
            metadata["fallback_step_bindings"] = fallback_step_bindings
        if stable_runtime_bindings:
            metadata["stable_runtime_bindings"] = stable_runtime_bindings
        if warnings:
            metadata["compiler_warnings"] = warnings

        return MetaToolSpec(
            name=name,
            description=description,
            input_schema={
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
            steps=tuple(steps),
            output=step_binding(f"step_{len(steps)}"),
            metadata=metadata,
        )

    def compile_all(
        self, candidates: Iterable[MetaToolCandidate]
    ) -> list[MetaToolSpec]:
        return [self.compile(candidate) for candidate in candidates]


def _leaf_paths(
    value: Any, prefix: tuple[str | int, ...] = ()
) -> list[tuple[str | int, ...]]:
    # Nested empty containers are values, not absent fields. An empty root
    # argument object has no fields to parameterize.
    if isinstance(value, Mapping):
        paths: list[tuple[str | int, ...]] = []
        for key in sorted(value, key=str):
            paths.extend(_leaf_paths(value[key], prefix + (str(key),)))
        return paths or ([prefix] if prefix else [])
    if isinstance(value, (list, tuple)):
        paths = []
        for index, item in enumerate(value):
            paths.extend(_leaf_paths(item, prefix + (index,)))
        return paths or ([prefix] if prefix else [])
    return [prefix]


def _get_path(value: Any, path: Sequence[str | int]) -> Any:
    current = value
    for part in path:
        try:
            if isinstance(part, int) and isinstance(current, (list, tuple)):
                current = current[part]
            elif isinstance(part, str) and isinstance(current, Mapping):
                current = current[part]
            else:
                return _MISSING
        except (IndexError, KeyError):
            return _MISSING
    return current


def _set_path(value: Any, path: Sequence[str | int], replacement: Any) -> None:
    if not path:
        raise ValueError("Cannot replace the root argument object")
    current = value
    for part in path[:-1]:
        current = current[part]
    current[path[-1]] = replacement


def _all_equal(values: Sequence[Any]) -> bool:
    if not values:
        return True
    return all(_json_equal(values[0], item) for item in values[1:])


def _json_equal(left: Any, right: Any) -> bool:
    if left is _MISSING or right is _MISSING:
        return left is right
    return stable_json(left) == stable_json(right)


def _series_item(value: Any) -> str:
    return "__MISSING__" if value is _MISSING else stable_json(value)


def _infer_schema(values: Sequence[Any]) -> dict[str, Any]:
    types = sorted(
        {
            _json_schema_type(value)
            for value in values
            if value is not _MISSING and value is not None
        }
    )
    has_null = any(value is None for value in values)
    if not types:
        schema_type: Any = "string"
    elif len(types) == 1 and not has_null:
        schema_type = types[0]
    else:
        schema_type = types + (["null"] if has_null else [])
    return {"type": schema_type}


def _json_schema_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    return "string"


def _unique_input_name(
    *,
    path: Sequence[str | int],
    tool_name: str,
    step_index: int,
    used: set[str],
) -> str:
    leaf = _slug(str(path[-1])) if path else "value"
    if leaf and leaf not in used:
        return leaf
    tool = _slug(tool_name.split(".")[-1]) or "tool"
    base = f"{tool}_{leaf}" if leaf else tool
    if base not in used:
        return base
    base = f"step_{step_index + 1}_{base}"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def _display_path(path: Sequence[str | int]) -> str:
    return ".".join(str(part) for part in path) or "<root>"


def _path_sort_key(path: Sequence[str | int]) -> tuple[str, ...]:
    return tuple(f"{type(part).__name__}:{part}" for part in path)


def _slug(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_]+", "_", value).strip("_").lower()
    if value and value[0].isdigit():
        value = f"value_{value}"
    return value


def _valid_tool_name(value: str) -> str:
    name = _slug(value) or "meta_tool"
    return name[:64].rstrip("_")
