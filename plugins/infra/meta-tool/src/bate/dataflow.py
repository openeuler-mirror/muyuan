"""Cross-occurrence data-flow evidence and deterministic meta-tool boundaries.

The boundary policy adapts the AWO-relevant idea from Etchplan's Apache-2.0
``meta_tools.py`` implementation (commit 11109b5) to this project's state-graph
types. It does not include Etchplan's JIT compilation or runtime lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping, Sequence

from .graph import CallOccurrence
from .models import stable_json


PathPart = str | int
ArgumentPath = tuple[PathPart, ...]
_MISSING = object()
_RUNTIME_LOCAL_ARGUMENT_RE = re.compile(
    r"(?i)(^|\.)(ref|element_id|node_id|backend_node_id|target_id)$"
)


@dataclass(frozen=True)
class DependencyEvidence:
    """A prior-result binding verified across every supporting occurrence."""

    source_step: int
    result_path: ArgumentPath
    distinct_values: int

    @property
    def is_varying(self) -> bool:
        return self.distinct_values > 1


@dataclass(frozen=True)
class ArgumentEvidence:
    path: ArgumentPath
    kind: str
    dependency: DependencyEvidence | None = None


@dataclass(frozen=True)
class StepFlowAnalysis:
    arguments: tuple[ArgumentEvidence, ...]
    inconsistent_paths: tuple[ArgumentPath, ...] = ()

    @property
    def fully_bound_for_bridge(self) -> bool:
        """Whether an unconnected step can be crossed without free-text glue."""

        return not self.inconsistent_paths and all(
            argument.kind != "free_text" for argument in self.arguments
        )


@dataclass(frozen=True)
class DataFlowSpan:
    start_index: int
    end_index: int
    internal_flows: int
    varying_flows: int
    constant_flows: int
    caller_parameters: int
    free_text_parameters: int
    constant_arguments: int
    runtime_local_flows: int = 0

    @property
    def n_steps(self) -> int:
        return self.end_index - self.start_index + 1

    @property
    def score(self) -> tuple[int, int, int]:
        """Etchplan-style quality order, with deterministic path length tie-break."""

        return self.varying_flows, self.internal_flows, self.n_steps

    def to_dict(self) -> dict[str, int]:
        return {
            "start_index": self.start_index,
            "end_index": self.end_index,
            "n_steps": self.n_steps,
            "internal_data_flows": self.internal_flows,
            "varying_data_flows": self.varying_flows,
            "constant_data_flows": self.constant_flows,
            "runtime_local_data_flows": self.runtime_local_flows,
            "caller_parameters": self.caller_parameters,
            "free_text_parameters": self.free_text_parameters,
            "constant_arguments": self.constant_arguments,
        }


@dataclass(frozen=True)
class DataFlowAnalysis:
    """Binding evidence for one aligned primitive sequence."""

    steps: tuple[StepFlowAnalysis, ...]

    def argument(
        self, step_index: int, path: Sequence[PathPart]
    ) -> ArgumentEvidence | None:
        expected = tuple(path)
        for argument in self.steps[step_index].arguments:
            if argument.path == expected:
                return argument
        return None

    def span_signature(self, start_index: int, end_index: int) -> DataFlowSpan:
        internal = varying = constant_flows = runtime_local_flows = 0
        caller = free_text = constants = 0
        for step_index in range(start_index, end_index + 1):
            step = self.steps[step_index]
            caller += len(step.inconsistent_paths)
            free_text += len(step.inconsistent_paths)
            for argument in step.arguments:
                dependency = argument.dependency
                if (
                    argument.kind == "flow"
                    and dependency is not None
                    and start_index <= dependency.source_step < step_index
                ):
                    internal += 1
                    if dependency.is_varying:
                        varying += 1
                    else:
                        constant_flows += 1
                        if _is_runtime_local_path(argument.path):
                            runtime_local_flows += 1
                elif argument.kind == "constant":
                    constants += 1
                else:
                    caller += 1
                    if argument.kind == "free_text":
                        free_text += 1
        return DataFlowSpan(
            start_index=start_index,
            end_index=end_index,
            internal_flows=internal,
            varying_flows=varying,
            constant_flows=constant_flows,
            runtime_local_flows=runtime_local_flows,
            caller_parameters=caller,
            free_text_parameters=free_text,
            constant_arguments=constants,
        )

    def connected_spans(
        self, *, min_steps: int = 2, max_bridge_steps: int = 2
    ) -> tuple[DataFlowSpan, ...]:
        """Find deterministic spans anchored by verified internal data flow.

        An unconnected step may be crossed only when all of its arguments are
        constants, verified flows, or parameter-shaped caller inputs. Varying
        prose, shell commands, and source text are hard boundaries.
        """

        spans: list[DataFlowSpan] = []
        index = 0
        while index < len(self.steps):
            if self.steps[index].inconsistent_paths:
                index += 1
                continue
            reach = index
            last_connected = index
            probe = index + 1
            while probe < len(self.steps):
                if self.steps[probe].inconsistent_paths:
                    break
                dependencies = (
                    argument.dependency
                    for argument in self.steps[probe].arguments
                    if argument.kind == "flow"
                )
                reconnects = any(
                    dependency is not None
                    and index <= dependency.source_step <= reach
                    for dependency in dependencies
                )
                if reconnects:
                    reach = last_connected = probe
                elif (
                    self.steps[probe].fully_bound_for_bridge
                    and probe - last_connected <= max_bridge_steps
                ):
                    reach = probe
                else:
                    break
                probe += 1
            if last_connected - index + 1 >= min_steps:
                spans.append(self.span_signature(index, last_connected))
                index = last_connected + 1
            else:
                index += 1
        return tuple(spans)


def analyze_data_flow(
    examples: Sequence[Sequence[CallOccurrence]],
) -> DataFlowAnalysis:
    """Infer argument bindings, verifying every claim across all examples."""

    if not examples:
        raise ValueError("Data-flow analysis requires at least one example")
    step_count = len(examples[0])
    if any(len(example) != step_count for example in examples):
        raise ValueError("Data-flow examples have inconsistent step counts")

    steps: list[StepFlowAnalysis] = []
    for step_index in range(step_count):
        path_sets = [
            set(_leaf_paths(dict(example[step_index].raw.arguments)))
            for example in examples
        ]
        common_paths = set.intersection(*path_sets) if path_sets else set()
        all_paths = set.union(*path_sets) if path_sets else set()
        arguments: list[ArgumentEvidence] = []
        for path in sorted(common_paths, key=_path_sort_key):
            values = [
                _get_path(example[step_index].raw.arguments, path)
                for example in examples
            ]
            dependency = find_dependency(examples, step_index, values)
            if dependency is not None:
                kind = "flow"
            elif _all_equal(values):
                kind = "constant"
            elif _is_parameter_shaped(values):
                kind = "parameter"
            else:
                kind = "free_text"
            arguments.append(
                ArgumentEvidence(path=path, kind=kind, dependency=dependency)
            )
        steps.append(
            StepFlowAnalysis(
                arguments=tuple(arguments),
                inconsistent_paths=tuple(
                    sorted(all_paths - common_paths, key=_path_sort_key)
                ),
            )
        )
    return DataFlowAnalysis(tuple(steps))


def find_dependency(
    examples: Sequence[Sequence[CallOccurrence]],
    step_index: int,
    argument_values: Sequence[Any],
) -> DependencyEvidence | None:
    """Find one exact prior-output source that holds in every occurrence."""

    if step_index == 0 or any(value is _MISSING for value in argument_values):
        return None
    for source_index in range(step_index - 1, -1, -1):
        first_result = examples[0][source_index].raw.result
        for result_path in _leaf_paths(first_result):
            result_values = [
                _get_path(example[source_index].raw.result, result_path)
                for example in examples
            ]
            if all(
                result is not _MISSING and _json_equal(result, argument)
                for result, argument in zip(result_values, argument_values)
            ):
                return DependencyEvidence(
                    source_step=source_index,
                    result_path=result_path,
                    distinct_values=len({_series_item(value) for value in result_values}),
                )
    return None


def _is_parameter_shaped(values: Sequence[Any]) -> bool:
    """Separate bounded scalar inputs from varying free-form agent text."""

    for value in values:
        if isinstance(value, bool) or isinstance(value, (int, float)):
            continue
        if (
            isinstance(value, str)
            and bool(value)
            and len(value) <= 64
            and not any(character.isspace() for character in value)
        ):
            continue
        return False
    return True


def _leaf_paths(
    value: Any, prefix: ArgumentPath = ()
) -> list[ArgumentPath]:
    # Keep nested []/{} visible to cross-occurrence binding and shape checks.
    if isinstance(value, Mapping):
        paths: list[ArgumentPath] = []
        for key in sorted(value, key=str):
            paths.extend(_leaf_paths(value[key], prefix + (str(key),)))
        return paths or ([prefix] if prefix else [])
    if isinstance(value, (list, tuple)):
        paths = []
        for index, item in enumerate(value):
            paths.extend(_leaf_paths(item, prefix + (index,)))
        return paths or ([prefix] if prefix else [])
    return [prefix]


def _get_path(value: Any, path: Sequence[PathPart]) -> Any:
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


def _path_sort_key(path: Sequence[PathPart]) -> tuple[str, ...]:
    return tuple(f"{type(part).__name__}:{part}" for part in path)


def _is_runtime_local_path(path: Sequence[PathPart]) -> bool:
    """Recognize runtime-local argument names without inspecting their values."""

    return bool(path) and bool(_RUNTIME_LOCAL_ARGUMENT_RE.search(str(path[-1])))
