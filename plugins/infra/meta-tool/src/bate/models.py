from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping


JsonValue = (
    None
    | bool
    | int
    | float
    | str
    | list["JsonValue"]
    | dict[str, "JsonValue"]
)


class SemanticType(str, Enum):
    """Side-effect class used by horizontal state merging."""

    ANNOTATOR = "annotator"
    ACCESSOR = "accessor"
    MUTATOR = "mutator"


def stable_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


@dataclass(frozen=True)
class ToolCall:
    """One externally observable tool call in an agent execution."""

    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    result: Any = None
    call_id: str | None = None
    turn_id: str | int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ToolCall":
        if "function" in data and isinstance(data["function"], Mapping):
            function = data["function"]
            name = str(function.get("name", data.get("name", "")))
            arguments = _parse_arguments(
                function.get("arguments", data.get("arguments", {}))
            )
        else:
            name = str(data.get("name", data.get("tool", "")))
            arguments = _parse_arguments(
                data.get("arguments", data.get("args", data.get("input", {})))
            )

        if not name:
            raise ValueError(f"Tool call has no name: {data!r}")
        if not isinstance(arguments, Mapping):
            arguments = {"value": arguments}

        metadata = data.get("metadata", {})
        if not isinstance(metadata, Mapping):
            metadata = {"value": metadata}

        return cls(
            name=name,
            arguments=dict(arguments),
            result=data.get("result", data.get("output")),
            call_id=_optional_str(data.get("call_id", data.get("id"))),
            turn_id=data.get("turn_id", data.get("llm_turn_id")),
            metadata=dict(metadata),
        )

    def to_dict(self, *, include_result: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "name": self.name,
            "arguments": dict(self.arguments),
        }
        if include_result and self.result is not None:
            value["result"] = self.result
        if self.call_id is not None:
            value["call_id"] = self.call_id
        if self.turn_id is not None:
            value["turn_id"] = self.turn_id
        if self.metadata:
            value["metadata"] = dict(self.metadata)
        return value


@dataclass(frozen=True)
class Trace:
    """An ordered tool-call trajectory."""

    trace_id: str
    calls: tuple[ToolCall, ...]
    success: bool | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any], *, default_id: str | None = None
    ) -> "Trace":
        raw_calls = data.get("calls", data.get("tool_calls"))
        if raw_calls is None and "messages" in data:
            calls = tuple(_calls_from_messages(data["messages"]))
        elif isinstance(raw_calls, Iterable) and not isinstance(
            raw_calls, (str, bytes, Mapping)
        ):
            calls = tuple(
                call if isinstance(call, ToolCall) else ToolCall.from_dict(call)
                for call in raw_calls
            )
        else:
            raise ValueError("Trace must contain a 'calls' or 'messages' list")

        trace_id = str(
            data.get("trace_id", data.get("id", default_id or "trace"))
        )
        metadata = data.get("metadata", {})
        if not isinstance(metadata, Mapping):
            metadata = {"value": metadata}
        success = data.get("success")
        return cls(
            trace_id=trace_id,
            calls=calls,
            success=bool(success) if success is not None else None,
            metadata=dict(metadata),
        )

    def to_dict(self, *, include_results: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "trace_id": self.trace_id,
            "calls": [
                call.to_dict(include_result=include_results) for call in self.calls
            ],
        }
        if self.success is not None:
            value["success"] = self.success
        if self.metadata:
            value["metadata"] = dict(self.metadata)
        return value


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _parse_arguments(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                pass
    return value


def _calls_from_messages(messages: Any) -> list[ToolCall]:
    if not isinstance(messages, Iterable) or isinstance(
        messages, (str, bytes, Mapping)
    ):
        raise ValueError("'messages' must be a list")

    pending: list[ToolCall] = []
    by_id: dict[str, int] = {}
    for turn_index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            continue
        role = message.get("role")
        if role == "assistant":
            for raw in message.get("tool_calls", []) or []:
                call = ToolCall.from_dict({**raw, "turn_id": turn_index})
                by_id[call.call_id or f"__{len(pending)}"] = len(pending)
                pending.append(call)
        elif role == "tool":
            call_id = _optional_str(message.get("tool_call_id"))
            if call_id is None or call_id not in by_id:
                continue
            index = by_id[call_id]
            old = pending[index]
            pending[index] = ToolCall(
                name=old.name,
                arguments=old.arguments,
                result=_parse_arguments(message.get("content")),
                call_id=old.call_id,
                turn_id=old.turn_id,
                metadata=old.metadata,
            )
    return pending
