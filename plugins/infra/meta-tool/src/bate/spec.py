from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


_NO_FALLBACK = object()


@dataclass(frozen=True)
class StepSpec:
    step_id: str
    tool: str
    arguments: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StepSpec":
        return cls(
            step_id=str(data["id"]),
            tool=str(data["tool"]),
            arguments=dict(data.get("arguments", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.step_id,
            "tool": self.tool,
            "arguments": dict(self.arguments),
        }


@dataclass(frozen=True)
class MetaToolSpec:
    """Portable deterministic composite-tool definition."""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    steps: tuple[StepSpec, ...]
    output: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Meta-tool name cannot be empty")
        if len(self.steps) < 2:
            raise ValueError("A meta-tool must contain at least two primitive steps")
        ids = [step.step_id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("Step IDs must be unique")
        _validate_bindings(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MetaToolSpec":
        return cls(
            name=str(data["name"]),
            description=str(data.get("description", "")),
            input_schema=dict(
                data.get(
                    "input_schema",
                    {"type": "object", "properties": {}, "required": []},
                )
            ),
            steps=tuple(StepSpec.from_dict(item) for item in data["steps"]),
            output=data.get("output"),
            metadata=dict(data.get("metadata", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
            "steps": [step.to_dict() for step in self.steps],
            "output": self.output,
        }
        if self.metadata:
            value["metadata"] = dict(self.metadata)
        return value

    def as_tool_schema(self) -> dict[str, Any]:
        """Return a provider-neutral function-tool schema."""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.input_schema),
            },
        }


def input_binding(name: str) -> dict[str, str]:
    return {"$input": name}


def step_binding(
    step_id: str,
    path: tuple[str | int, ...] = (),
    *,
    fallback: Any = _NO_FALLBACK,
) -> dict[str, Any]:
    binding: dict[str, Any] = {"$step": step_id, "path": list(path)}
    if fallback is not _NO_FALLBACK:
        binding["fallback"] = fallback
    return binding


def _validate_bindings(spec: MetaToolSpec) -> None:
    properties = spec.input_schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise ValueError("input_schema.properties must be an object")
    known_inputs = set(str(item) for item in properties)
    completed_steps: set[str] = set()

    for step in spec.steps:
        for binding in _walk_bindings(step.arguments):
            if "$input" in binding:
                name = str(binding["$input"])
                if name not in known_inputs:
                    raise ValueError(
                        f"Step {step.step_id!r} references unknown input {name!r}"
                    )
            elif "$step" in binding:
                source = str(binding["$step"])
                if source not in completed_steps:
                    raise ValueError(
                        f"Step {step.step_id!r} references unavailable step "
                        f"{source!r}; only earlier steps may be referenced"
                    )
        completed_steps.add(step.step_id)

    for binding in _walk_bindings(spec.output):
        if "$input" in binding and str(binding["$input"]) not in known_inputs:
            raise ValueError(f"Output references unknown input {binding['$input']!r}")
        if "$step" in binding and str(binding["$step"]) not in completed_steps:
            raise ValueError(f"Output references unknown step {binding['$step']!r}")


def _walk_bindings(value: Any):
    if isinstance(value, Mapping):
        if set(value) in (
            {"$input"},
            {"$step"},
            {"$step", "path"},
            {"$step", "path", "fallback"},
        ):
            yield value
            if "fallback" in value:
                yield from _walk_bindings(value["fallback"])
            return
        for item in value.values():
            yield from _walk_bindings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_bindings(item)
