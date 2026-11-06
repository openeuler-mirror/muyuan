"""JSON-lines bridge between the Pi extension and the meta-tool runtime."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
import uuid
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

META_TOOL_ROOT = Path(
    os.environ.get("META_TOOL_ROOT", Path(__file__).resolve().parents[3])
).resolve()
if str(META_TOOL_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(META_TOOL_ROOT / "src"))

from bate.dispatch import AccelerationPolicy, DispatchMode, MetaToolDispatcher
from bate.runtime import ToolRegistry
from bate.models import Trace
from bate.io import load_specs, load_traces
from discovery import discover_online as _discover

LEGACY_SUPPORTED_TOOLS = {"read", "bash", "edit", "write", "grep", "find", "ls"}
MAX_RPC_LINE_BYTES = 64 * 1024 * 1024


def _send(message: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


class RpcPeer:
    """Resolve Pi tool calls while the main coroutine executes a meta-tool."""

    def __init__(self, reader: asyncio.StreamReader) -> None:
        self._reader = reader
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)

    async def request(
        self, message_type: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        request_id = uuid.uuid4().hex
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[request_id] = future
        _send({"type": message_type, "id": request_id, **payload})
        try:
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def _read_loop(self) -> None:
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    raise EOFError("Pi closed the meta-tool bridge input")
                message = json.loads(line.decode("utf-8"))
                request_id = message.get("id")
                if not isinstance(request_id, str):
                    continue
                future = self._pending.get(request_id)
                if future is not None and not future.done():
                    future.set_result(message)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - propagate all RPC failures
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    return dict(value)


def _string_set_or_default(
    value: Any, default: set[str] | None = None
) -> set[str] | None:
    if isinstance(value, (list, tuple, set)):
        return {str(item) for item in value}
    return default


def _load_payload_traces(payload: Mapping[str, Any]) -> list[Trace]:
    trace_path = payload.get("tracePath")
    if isinstance(trace_path, str) and trace_path:
        return load_traces(trace_path)

    trace_export = _require_mapping(payload.get("trace"), "trace")
    raw_traces = trace_export.get("traces")
    if not isinstance(raw_traces, list):
        raise TypeError("trace.traces must be an array")
    traces: list[Trace] = []
    for index, value in enumerate(raw_traces, start=1):
        if not isinstance(value, Mapping):
            raise TypeError(f"trace.traces[{index - 1}] must be an object")
        traces.append(Trace.from_dict(value, default_id=f"pi-trace-{index:04d}"))
    return traces


def _decode_tool_result(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped.startswith(("{", "[")):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    return value


def _rpc_tools(
    names: set[str], peer: RpcPeer, supported_tools: set[str] | None = None
) -> ToolRegistry:
    supported = (
        supported_tools if supported_tools is not None else LEGACY_SUPPORTED_TOOLS
    )
    registry = ToolRegistry()
    for name in sorted(names):
        if name not in supported:
            raise ValueError(f"Meta-tool references unsupported Pi tool: {name}")

        def build_execute(tool_name: str) -> Callable[..., Awaitable[Any]]:
            async def execute(**arguments: Any) -> Any:
                response = await peer.request(
                    "tool_call", {"name": tool_name, "input": arguments}
                )
                if response.get("error"):
                    raise RuntimeError(str(response["error"]))
                return _decode_tool_result(response.get("result", ""))

            return execute

        registry.register(name, build_execute(name))
    return registry


async def _run(payload: Mapping[str, Any], peer: RpcPeer) -> dict[str, Any]:
    catalog_file = str(payload.get("catalogFile", ""))
    meta_tool = str(payload.get("metaTool", ""))
    inputs = _require_mapping(payload.get("inputs", {}), "inputs")
    mode = DispatchMode(str(payload.get("mode", DispatchMode.SAFE_AUTO.value)))
    specs = load_specs(catalog_file)
    by_name = {spec.name: spec for spec in specs}
    if meta_tool not in by_name:
        available = ", ".join(sorted(by_name)) or "none"
        raise ValueError(f"Unknown meta-tool {meta_tool!r}; available: {available}")
    spec = by_name[meta_tool]
    primitive_names = {step.tool for step in spec.steps}
    supported_tools_value = payload.get("supportedTools")
    supported_tools = (
        {str(item) for item in supported_tools_value}
        if isinstance(supported_tools_value, (list, tuple, set))
        else LEGACY_SUPPORTED_TOOLS
    )
    policy_data = dict(_require_mapping(payload.get("policy", {}), "policy"))
    # The execution mode is determined by the extension after applying its
    # interactive safety guard.  Preserve all other configured admission
    # thresholds so a candidate admitted with an explicit relaxed policy is
    # evaluated by that same policy at dispatch time.
    policy_data["mode"] = mode
    telemetry: list[dict[str, Any]] = []
    dispatcher = MetaToolDispatcher(
        _rpc_tools(primitive_names, peer, supported_tools),
        [spec],
        policy=AccelerationPolicy.from_dict(policy_data),
        telemetry=lambda event: telemetry.append(event.to_dict()),
    )
    _send(
        {
            "type": "progress",
            "message": (
                f"meta-tool: executing {meta_tool} ({len(spec.steps)} primitive steps)"
            ),
        }
    )
    output = await dispatcher.adispatch(meta_tool, inputs)
    return {
        "metaTool": meta_tool,
        "output": output,
        "telemetry": telemetry[-1] if telemetry else None,
    }


async def _dispatch(payload: Mapping[str, Any], peer: RpcPeer) -> Any:
    operation = payload.get("operation")
    if operation == "discover":
        return _discover(payload)
    if operation == "run":
        return await _run(payload, peer)
    raise ValueError(f"Unsupported meta-tool bridge operation: {operation!r}")


async def _main() -> int:
    reader = asyncio.StreamReader(limit=MAX_RPC_LINE_BYTES)
    protocol = asyncio.StreamReaderProtocol(reader)
    await asyncio.get_running_loop().connect_read_pipe(
        lambda: protocol, sys.stdin.buffer
    )
    first_line = await reader.readline()
    if not first_line:
        raise EOFError("Meta-tool bridge did not receive a request")
    payload = json.loads(first_line.decode("utf-8"))
    if not isinstance(payload, Mapping):
        raise TypeError("Meta-tool bridge request must be an object")

    peer = RpcPeer(reader)
    await peer.start()
    try:
        result = await _dispatch(payload, peer)
        _send({"type": "complete", "result": result})
        return 0
    except Exception as error:  # noqa: BLE001 - serialize boundary failures
        traceback.print_exc(file=sys.stderr)
        _send({"type": "error", "error": f"{type(error).__name__}: {error}"})
        return 1
    finally:
        await peer.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
