"""共享工具：JSON 安全值、浮点数、里程碑、JSON 提取。"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

def _json_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    return value


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _milestone(message: str) -> None:
    """关键循环事件的进度行；即时刷新，nohup 日志可实时更新。"""
    print(f"[ahe] {message}", flush=True)


def _last_json_object(text: str) -> Mapping[str, Any] | None:
    for line in reversed([line.strip() for line in text.splitlines() if line.strip()]):
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(value, Mapping):
            return value
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, Mapping) else None


def _strip_json_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```"):
        first_newline = value.find("\n")
        if first_newline >= 0:
            value = value[first_newline + 1 :]
        if value.endswith("```"):
            value = value[:-3]
    return value.strip()


def _extract_json_object(text: str) -> Any:
    """模型附加散文或代码围栏时解析 JSON 对象。"""
    value = _strip_json_fence(text)
    try:
        return json.loads(value)
    except json.JSONDecodeError as first_error:
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", value):
            try:
                candidate, end = decoder.raw_decode(value[match.start() :])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and not value[match.start() + end :].strip():
                return candidate
        raise first_error

