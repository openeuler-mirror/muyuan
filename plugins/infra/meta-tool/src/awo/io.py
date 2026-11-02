from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import Trace
from .spec import MetaToolSpec


def load_traces(path: str | Path) -> list[Trace]:
    source = Path(path)
    if source.suffix.lower() == ".jsonl":
        records: list[Any] = []
        with source.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as error:
                    raise ValueError(
                        f"Invalid JSON on {source}:{line_number}: {error}"
                    ) from error
    else:
        with source.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, Mapping) and "traces" in payload:
            records = list(payload["traces"])
        elif isinstance(payload, list):
            records = payload
        elif isinstance(payload, Mapping):
            records = [payload]
        else:
            raise ValueError("Trace file must contain an object or array")

    traces: list[Trace] = []
    for index, record in enumerate(records, start=1):
        if not isinstance(record, Mapping):
            raise ValueError(f"Trace record {index} is not an object")
        traces.append(Trace.from_dict(record, default_id=f"trace-{index:04d}"))
    _validate_trace_ids(traces)
    return traces


def load_specs(path: str | Path) -> list[MetaToolSpec]:
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, Mapping) and "meta_tools" in payload:
        values = payload["meta_tools"]
    elif isinstance(payload, list):
        values = payload
    elif isinstance(payload, Mapping):
        values = [payload]
    else:
        raise ValueError("Spec file must contain an object or array")
    return [MetaToolSpec.from_dict(item) for item in values]


def write_json(path: str | Path, value: Any) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False, default=str)
        handle.write("\n")
    temporary.replace(destination)


def write_text(path: str | Path, value: str) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(destination)


def write_jsonl(path: str | Path, values: Iterable[Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for value in values:
            json.dump(value, handle, ensure_ascii=False, default=str)
            handle.write("\n")
    temporary.replace(destination)


def _validate_trace_ids(traces: Iterable[Trace]) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for trace in traces:
        if trace.trace_id in seen:
            duplicates.add(trace.trace_id)
        seen.add(trace.trace_id)
    if duplicates:
        names = ", ".join(sorted(duplicates))
        raise ValueError(f"Trace IDs must be unique; duplicates: {names}")
