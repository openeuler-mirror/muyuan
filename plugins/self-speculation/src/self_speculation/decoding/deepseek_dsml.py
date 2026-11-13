"""Parser for DeepSeek V4's DSML tool-call branch."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html import unescape
from typing import Any

from ..models import ToolCall


_DSML = r"(?:\||｜)DSML(?:\||｜)"
_INVOKE_RE = re.compile(
    rf"<\s*{_DSML}\s*invoke\b([^>]*)>"
    rf"(.*?)"
    rf"<\s*/\s*{_DSML}\s*invoke\s*>",
    re.DOTALL | re.IGNORECASE,
)
_PARAMETER_RE = re.compile(
    rf"<\s*{_DSML}\s*parameter\b([^>]*)>"
    rf"(.*?)"
    rf"<\s*/\s*{_DSML}\s*parameter\s*>",
    re.DOTALL | re.IGNORECASE,
)
_ATTRIBUTE_RE = re.compile(r"([\w.-]+)\s*=\s*(['\"])(.*?)\2", re.DOTALL)
_PARAMETER_OPEN_RE = re.compile(
    rf"<\s*{_DSML}\s*parameter\b",
    re.IGNORECASE,
)
_PARAMETER_CLOSE_RE = re.compile(
    rf"<\s*/\s*{_DSML}\s*parameter\s*>",
    re.IGNORECASE,
)
_INVOKE_OPEN_RE = re.compile(
    rf"<\s*{_DSML}\s*invoke\b",
    re.IGNORECASE,
)
_INVALID_TYPED_VALUE = object()


def _attributes(raw: str) -> dict[str, str] | None:
    attributes: dict[str, str] = {}
    offset = 0
    for match in _ATTRIBUTE_RE.finditer(raw):
        if raw[offset : match.start()].strip():
            return None
        name = match.group(1).lower()
        if name in attributes:
            return None
        attributes[name] = unescape(match.group(3))
        offset = match.end()
    if raw[offset:].strip():
        return None
    return attributes


def _typed_value(raw: str, is_string: bool) -> Any:
    raw = unescape(raw)
    if is_string:
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return _INVALID_TYPED_VALUE


@dataclass(frozen=True, slots=True)
class DeepSeekDsmlToolCallParser:
    name: str = "deepseek_dsml"

    def parse(self, text: str) -> tuple[ToolCall, ...]:
        calls: list[ToolCall] = []
        for index, invoke in enumerate(_INVOKE_RE.finditer(text)):
            invoke_attributes = _attributes(invoke.group(1))
            if invoke_attributes is None:
                continue
            function_name = invoke_attributes.get("name", "").strip()
            if not function_name:
                continue
            body = invoke.group(2)
            parameters = tuple(_PARAMETER_RE.finditer(body))
            if (
                len(_PARAMETER_OPEN_RE.findall(body)) != len(parameters)
                or len(_PARAMETER_CLOSE_RE.findall(body)) != len(parameters)
                or _INVOKE_OPEN_RE.search(body)
            ):
                continue
            arguments: dict[str, Any] = {}
            malformed = False
            for parameter in parameters:
                attributes = _attributes(parameter.group(1))
                if attributes is None:
                    malformed = True
                    break
                parameter_name = attributes.get("name", "").strip()
                if not parameter_name or parameter_name in arguments:
                    malformed = True
                    break
                string_flag = attributes.get("string", "true").lower()
                if string_flag not in {"true", "false"}:
                    malformed = True
                    break
                value = _typed_value(
                    parameter.group(2),
                    string_flag == "true",
                )
                if value is _INVALID_TYPED_VALUE:
                    malformed = True
                    break
                arguments[parameter_name] = value
            if malformed:
                continue
            calls.append(
                ToolCall(
                    name=function_name,
                    arguments=arguments,
                    call_id=(
                        invoke_attributes.get("call_id")
                        or invoke_attributes.get("id")
                        or None
                    ),
                    index=index,
                    format=self.name,
                    raw=invoke.group(0),
                )
            )
        return tuple(calls)
