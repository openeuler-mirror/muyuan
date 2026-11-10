from __future__ import annotations

import inspect
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .spec import MetaToolSpec
from .validation import MetaToolValidationError, validate_inputs


ToolCallable = Callable[..., Any]


class ToolRegistry:
    def __init__(self, tools: Mapping[str, ToolCallable] | None = None) -> None:
        self._tools: dict[str, ToolCallable] = dict(tools or {})

    def register(
        self, name: str | None = None, function: ToolCallable | None = None
    ):
        if callable(name) and function is None:
            direct = name
            self._register(direct.__name__, direct)
            return direct
        if function is not None:
            self._register(name or function.__name__, function)
            return function

        def decorator(inner: ToolCallable) -> ToolCallable:
            self._register(name or inner.__name__, inner)
            return inner

        return decorator

    def _register(self, name: str, function: ToolCallable) -> None:
        if not callable(function):
            raise TypeError(f"Tool {name!r} is not callable")
        if name in self._tools:
            raise ValueError(f"Tool {name!r} is already registered")
        self._tools[name] = function

    def get(self, name: str) -> ToolCallable:
        try:
            return self._tools[name]
        except KeyError as error:
            raise KeyError(f"Tool {name!r} is not registered") from error

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))


@dataclass(frozen=True)
class StepExecution:
    step_id: str
    tool: str
    arguments: Mapping[str, Any]
    output: Any
    duration_seconds: float


@dataclass(frozen=True)
class MetaToolResult:
    meta_tool: str
    output: Any
    steps: tuple[StepExecution, ...]
    duration_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "meta_tool": self.meta_tool,
            "output": self.output,
            "duration_seconds": self.duration_seconds,
            "steps": [
                {
                    "id": step.step_id,
                    "tool": step.tool,
                    "arguments": dict(step.arguments),
                    "output": step.output,
                    "duration_seconds": step.duration_seconds,
                }
                for step in self.steps
            ],
        }


class MetaToolExecutionError(RuntimeError):
    def __init__(
        self,
        *,
        meta_tool: str,
        step_id: str,
        tool: str,
        completed_steps: tuple[StepExecution, ...],
        cause: BaseException,
    ) -> None:
        super().__init__(
            f"Meta-tool {meta_tool!r} failed at {step_id!r} "
            f"({tool!r}): {cause}"
        )
        self.meta_tool = meta_tool
        self.step_id = step_id
        self.tool = tool
        self.completed_steps = completed_steps
        self.__cause__ = cause


class StepOutputPathError(LookupError):
    """A declared path is absent from a completed primitive's output."""


class MetaToolExecutor:
    """Execute a meta-tool deterministically with fail-fast semantics."""

    def __init__(self, registry: ToolRegistry | Mapping[str, ToolCallable]) -> None:
        self.registry = (
            registry if isinstance(registry, ToolRegistry) else ToolRegistry(registry)
        )

    def execute(
        self, spec: MetaToolSpec, inputs: Mapping[str, Any]
    ) -> MetaToolResult:
        _validate_inputs(spec, inputs)
        started = time.perf_counter()
        outputs: dict[str, Any] = {}
        executions: list[StepExecution] = []

        for step in spec.steps:
            step_started = time.perf_counter()
            try:
                arguments = _resolve(
                    step.arguments, inputs=inputs, outputs=outputs
                )
                function = self.registry.get(step.tool)
                output = function(**arguments)
                if inspect.isawaitable(output):
                    close = getattr(output, "close", None)
                    if callable(close):
                        close()
                    raise TypeError(
                        f"Tool {step.tool!r} is asynchronous; use aexecute()"
                    )
            except Exception as error:
                raise MetaToolExecutionError(
                    meta_tool=spec.name,
                    step_id=step.step_id,
                    tool=step.tool,
                    completed_steps=tuple(executions),
                    cause=error,
                ) from error
            duration = time.perf_counter() - step_started
            outputs[step.step_id] = output
            executions.append(
                StepExecution(
                    step_id=step.step_id,
                    tool=step.tool,
                    arguments=arguments,
                    output=output,
                    duration_seconds=duration,
                )
            )

        final_output = _resolve(spec.output, inputs=inputs, outputs=outputs)
        return MetaToolResult(
            meta_tool=spec.name,
            output=final_output,
            steps=tuple(executions),
            duration_seconds=time.perf_counter() - started,
        )

    async def aexecute(
        self, spec: MetaToolSpec, inputs: Mapping[str, Any]
    ) -> MetaToolResult:
        _validate_inputs(spec, inputs)
        started = time.perf_counter()
        outputs: dict[str, Any] = {}
        executions: list[StepExecution] = []

        for step in spec.steps:
            step_started = time.perf_counter()
            try:
                arguments = _resolve(
                    step.arguments, inputs=inputs, outputs=outputs
                )
                function = self.registry.get(step.tool)
                output = function(**arguments)
                if inspect.isawaitable(output):
                    output = await output
            except Exception as error:
                raise MetaToolExecutionError(
                    meta_tool=spec.name,
                    step_id=step.step_id,
                    tool=step.tool,
                    completed_steps=tuple(executions),
                    cause=error,
                ) from error
            duration = time.perf_counter() - step_started
            outputs[step.step_id] = output
            executions.append(
                StepExecution(
                    step_id=step.step_id,
                    tool=step.tool,
                    arguments=arguments,
                    output=output,
                    duration_seconds=duration,
                )
            )

        final_output = _resolve(spec.output, inputs=inputs, outputs=outputs)
        return MetaToolResult(
            meta_tool=spec.name,
            output=final_output,
            steps=tuple(executions),
            duration_seconds=time.perf_counter() - started,
        )


def _resolve(
    value: Any,
    *,
    inputs: Mapping[str, Any],
    outputs: Mapping[str, Any],
) -> Any:
    if isinstance(value, Mapping):
        keys = set(value)
        if keys == {"$input"}:
            return inputs[str(value["$input"])]
        if keys in (
            {"$step"},
            {"$step", "path"},
            {"$step", "path", "fallback"},
        ):
            step_id = str(value["$step"])
            if step_id not in outputs:
                raise KeyError(f"Step output {step_id!r} is unavailable")
            try:
                return _resolve_path(outputs[step_id], value.get("path", []))
            except StepOutputPathError:
                if "fallback" not in value:
                    raise
                return _resolve(
                    value["fallback"], inputs=inputs, outputs=outputs
                )
        return {
            str(key): _resolve(item, inputs=inputs, outputs=outputs)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_resolve(item, inputs=inputs, outputs=outputs) for item in value]
    if isinstance(value, tuple):
        return tuple(_resolve(item, inputs=inputs, outputs=outputs) for item in value)
    return value


def _resolve_path(value: Any, path: Any) -> Any:
    if not isinstance(path, (list, tuple)):
        raise TypeError("A step binding path must be a list")
    current = value
    for index, part in enumerate(path):
        try:
            if isinstance(current, Mapping):
                current = current[str(part)]
            elif isinstance(current, (list, tuple)):
                current = current[int(part)]
            else:
                current = getattr(current, str(part))
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
            traversed = list(path[: index + 1])
            raise StepOutputPathError(
                f"Step output path {list(path)!r} is unavailable at "
                f"{traversed!r}"
            ) from error
    return current


def _validate_inputs(spec: MetaToolSpec, inputs: Mapping[str, Any]) -> None:
    validate_inputs(spec.input_schema, inputs)
