"""Read-only, reviewed BATE catalogs exposed to Codex over standard MCP.

The MCP SDK is imported lazily so offline extraction does not require MCP.
Upstream processes and credentials come only from an operator-owned config,
never from a model argument, catalog, or trace. Native Codex tools are not
emulated. Static catalogs are pinned; Online publishes immutable versions and
uses stable search/run entry points so hosts need not rediscover MCP tools.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from contextlib import AsyncExitStack
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping

from ..io import load_specs
from ..naming import template_fingerprint
from ..runtime import MetaToolExecutionError, MetaToolExecutor
from ..spec import MetaToolSpec
from ..validation import validate_inputs
from .traces import tool_output


def schema_fingerprint(schema: Mapping) -> str:
    return hashlib.sha256(json.dumps(
        schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode()).hexdigest()


def load_settings(path: str | Path) -> dict:
    path = Path(path).resolve()
    settings = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(settings, dict):
        raise ValueError("Codex config must be a JSON object")
    for key in ("servers", "bindings", "approved_meta_tools"):
        if not isinstance(settings.get(key), dict):
            raise ValueError(f"Codex config {key} must be an object")
    mode = settings.setdefault("mode", "static")
    if mode not in {"static", "online"}:
        raise ValueError("mode must be static or online")
    if settings.get("catalog") is None and mode == "online":
        settings["catalog"] = None
    elif not isinstance(settings.get("catalog"), str):
        raise ValueError("Codex config catalog must be a path")
    else:
        settings["catalog"] = str((path.parent / settings["catalog"]).resolve())
    if not isinstance(settings.setdefault("batch_enabled", True), bool):
        raise ValueError("batch_enabled must be a boolean")
    for key, default in (("session_control", False), ("start_enabled", True)):
        if not isinstance(settings.setdefault(key, default), bool):
            raise ValueError(f"{key} must be a boolean")
    if mode == "online" or "online" in settings:
        online = settings.get("online")
        if not isinstance(online, dict):
            raise ValueError("online mode requires online configuration")
        paths = online.get("trace_files")
        minimum = 1 if mode == "online" else 0
        if not isinstance(paths, list) or not minimum <= len(paths) <= 64 or not all(isinstance(p, str) and p for p in paths):
            raise ValueError("online.trace_files must name 1 to 64 explicit rollout files")
        online["trace_files"] = sorted({str((path.parent / p).resolve()) for p in paths})
        state = online.get("state_dir")
        if not isinstance(state, str) or not state:
            raise ValueError("online.state_dir must be explicit")
        online["state_dir"] = str((path.parent / state).resolve())
        if any(Path(p) == Path(online["state_dir"]) or Path(p).is_relative_to(online["state_dir"])
               for p in online["trace_files"]):
            raise ValueError("online inputs must be outside the generated state directory")
        for key, default, low, high in (("min_support", 3, 3, 10000), ("min_sessions", 2, 2, 10000),
                ("max_len", 8, 2, 16), ("max_meta_tools", 50, 1, 100),
                ("poll_seconds", 2, 1, 300), ("extraction_timeout_seconds", 120, 1, 600)):
            value = online.setdefault(key, default)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"online.{key} must be an integer in [{low}, {high}]")
        if not isinstance(online.setdefault("auto_admit_read_only", False), bool):
            raise ValueError("online.auto_admit_read_only must be a boolean")
    for name, server in settings["servers"].items():
        if not isinstance(server, dict) or not isinstance(server.get("command"), str):
            raise ValueError(f"servers.{name} requires an explicit stdio command")
        args = server.get("args", [])
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ValueError(f"servers.{name}.args must be a string array")
        server["cwd"] = str((path.parent / server.get("cwd", ".")).resolve())
        if not isinstance(server.get("env", {}), dict):
            raise ValueError(f"servers.{name}.env must be an object")
        env_vars = server.get("env_vars", [])
        if not isinstance(env_vars, list) or not all(isinstance(key, str) for key in env_vars):
            raise ValueError(f"servers.{name}.env_vars must be a string array")
    for name, binding in settings["bindings"].items():
        if (not isinstance(binding, dict) or binding.get("server") not in settings["servers"]
                or not isinstance(binding.get("tool"), str)):
            raise ValueError(f"Invalid primitive binding {name!r}")
    timeout = settings.get("timeout_seconds", 30)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 600:
        raise ValueError("timeout_seconds must be in (0, 600]")
    settings["timeout_seconds"] = timeout
    return settings


def _read_only(tool: Any) -> bool:
    return (tool.annotations is not None
            and tool.annotations.readOnlyHint is True
            and tool.annotations.destructiveHint is not True)


class CatalogService:
    def __init__(self, settings: dict, specs: list[MetaToolSpec]):
        self.settings, self.specs = settings, specs
        self.seed_specs = list(specs)
        self.learner = None
        self.automatic = {}
        self.sessions: dict[str, Any] = {}
        self.tools: dict[str, Any] = {}
        self.active: dict[str, MetaToolSpec] = {}
        self.rejected: list[dict] = []
        self._lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self.enabled = settings.get("start_enabled", True)
        self.mode = settings.get("mode", "static")
        # Runtime switches are local to this MCP connection; config remains immutable.
        self.batch_enabled = settings.get("batch_enabled", True)
        names = [spec.name for spec in specs]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate meta-tool names in catalog")
        for spec in specs:
            if spec.input_schema.get("type") != "object":
                raise ValueError(f"{spec.name}: input_schema must describe an object")

    def allowed_reads(self):
        return {name for name, binding in self.settings["bindings"].items()
                if binding.get("read_only") is True
                and (tool := self.tools.get(binding["server"], {}).get(binding["tool"])) is not None
                and _read_only(tool)
                and binding.get("input_schema_sha256", schema_fingerprint(tool.inputSchema))
                    == schema_fingerprint(tool.inputSchema)}

    def rebuild(self):
        from ..read_batches import read_batch_layout
        from ..naming import assign_short_names
        self.automatic = {}
        catalog = {}
        if self.batch_enabled and "read" in self.allowed_reads():
            for size in (2, 3, 4):
                schema, steps, output = read_batch_layout(size)
                spec = assign_short_names([MetaToolSpec(
                    name=f"read_batch_{size}", description=f"Read {size} caller-planned file ranges",
                    input_schema=schema, steps=steps, output=output,
                    metadata={"catalog_origin": "builtin_read_batch", "preinstalled": True,
                              "side_effect_free": True, "execution_guard": "none"})])[0]
                catalog[spec.name] = spec
                self.automatic[spec.name] = template_fingerprint(spec)
        if self.learner is not None:
            for spec in self.learner.specs:
                catalog[spec.name] = spec
                if self.settings["online"]["auto_admit_read_only"]:
                    self.automatic[spec.name] = template_fingerprint(spec)
        # Operator-owned seed catalog wins same-name collisions.
        for spec in self.seed_specs:
            catalog[spec.name] = spec
            self.automatic.pop(spec.name, None)
        self.specs = list(catalog.values())
        self.admit()

    async def refresh(self):
        async with self._lifecycle_lock:
            if self.enabled and self.mode == "online" and self.learner is not None:
                await self.learner.refresh(self.allowed_reads())
                self.rebuild()

    def start_learner(self):
        if self.learner is not None:
            return
        if not self.settings.get("online", {}).get("trace_files"):
            raise ValueError("online_sources_not_configured")
        from .online import OnlineLearner
        learner = OnlineLearner(self.settings)
        try:
            learner.restore()
        except BaseException:
            learner.close()
            raise
        self.learner = learner

    def close(self):
        if self.learner is not None:
            self.learner.close()

    async def control(self, action: str):
        if not self.settings.get("session_control", False):
            raise ValueError("session_control_not_enabled")
        if action not in {"status", "on", "off", "static", "online", "batch_on", "batch_off"}:
            raise ValueError("unknown_control_action")
        # Wait for in-flight learning/execution. After off returns, no queued
        # execution can start and no background refresh can publish a new version.
        async with self._lifecycle_lock, self._lock:
            if action == "online" or (action == "on" and self.mode == "online"):
                self.start_learner()  # Fail before changing the previous mode/state.
            if action in {"on", "static", "online"}:
                self.enabled = True
            elif action == "off":
                self.enabled = False
            if action in {"static", "online"}:
                self.mode = action
            if action in {"batch_on", "batch_off"}:
                self.batch_enabled = action == "batch_on"
            self.rebuild()
            return self.describe()

    async def learn_forever(self):
        while True:
            await self.refresh()
            await asyncio.sleep(self.settings.get("online", {}).get("poll_seconds", 2))

    async def _list_tools(self, server: str) -> dict:
        result = {}
        cursor = None
        seen_cursors = set()
        while True:
            page = await self.sessions[server].list_tools(cursor=cursor)
            for tool in page.tools:
                if tool.name in result:
                    raise ValueError(f"Duplicate upstream tool {server}/{tool.name}")
                result[tool.name] = tool
            cursor = page.nextCursor
            if not cursor:
                return result
            if cursor in seen_cursors:
                raise ValueError(f"Repeated tools/list cursor from {server}")
            seen_cursors.add(cursor)

    async def connect(self, stack: AsyncExitStack) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        for name, config in self.settings["servers"].items():
            env = dict(config.get("env", {}))
            for variable in config.get("env_vars", []):
                if variable not in os.environ:
                    raise ValueError(f"Missing environment variable {variable!r} for {name}")
                env[variable] = os.environ[variable]
            streams = await stack.enter_async_context(stdio_client(StdioServerParameters(
                command=config["command"], args=config.get("args", []),
                cwd=config["cwd"], env=env,
            )))
            session = await stack.enter_async_context(ClientSession(*streams))
            self.sessions[name] = session
            async with asyncio.timeout(self.settings["timeout_seconds"]):
                await session.initialize()
                self.tools[name] = await self._list_tools(name)
        self.rebuild()

    def admit(self) -> None:
        self.active.clear()
        self.rejected.clear()
        approved = self.settings["approved_meta_tools"]
        for spec in self.specs:
            reasons = []
            if (not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", spec.name)
                    or spec.name in {"bate_catalog", "meta_tool_search", "meta_tool_run", "meta_tool_read_batch", "meta_tool_control"}):
                reasons.append("invalid/reserved MCP name; extract with --naming stable-short")
            if (approved.get(spec.name) != template_fingerprint(spec)
                    and self.automatic.get(spec.name) != template_fingerprint(spec)):
                reasons.append("executable fingerprint not approved in operator config")
            if spec.metadata.get("compiler_warnings"):
                reasons.append("unresolved compiler warnings")
            if spec.metadata.get("execution_guard") not in (None, "none"):
                reasons.append("requires confirmation unavailable in this read-only adapter")
            if spec.metadata.get("lifecycle_state") in {"degraded", "revoked", "disabled"}:
                reasons.append("catalog entry is not healthy")
            for name in sorted({step.tool for step in spec.steps}):
                binding = self.settings["bindings"].get(name)
                if not binding:
                    reasons.append(f"{name}: primitive is not explicitly bound")
                    continue
                tool = self.tools.get(binding["server"], {}).get(binding["tool"])
                if tool is None:
                    reasons.append(f"{name}: upstream tool is unavailable")
                    continue
                if binding.get("read_only") is not True or not _read_only(tool):
                    reasons.append(f"{name}: both operator and upstream must declare read-only")
                expected = binding.get("input_schema_sha256")
                if expected is not None and expected != schema_fingerprint(tool.inputSchema):
                    reasons.append(f"{name}: configured input schema fingerprint mismatch")
            if reasons:
                self.rejected.append({"name": spec.name, "reasons": reasons})
            else:
                self.active[spec.name] = spec

    def describe(self) -> dict:
        return {
            "adapter": "bate-codex-mcp-v2", "mode": self.mode,
            "enabled": self.enabled, "batch_enabled": self.batch_enabled,
            "session_control": self.settings.get("session_control", False),
            "control_scope": "this MCP connection; resets on server restart",
            "online_available": bool(self.settings.get("online", {}).get("trace_files")),
            "active": [{"name": spec.name, "description": spec.description,
                        "fingerprint": template_fingerprint(spec),
                        "primitives": [step.tool for step in spec.steps]}
                       for spec in self.active.values()],
            "rejected": self.rejected,
            "native_codex_tools_supported": False,
            "online": self.learner.describe() if self.learner else None,
            "built_in_templates": sum(s.metadata.get("preinstalled") is True for s in self.active.values()),
        }

    def search(self, query="", limit=5):
        words = set(query.lower().split())
        ranked = sorted(self.active.values() if self.enabled else [], key=lambda s: (
            -sum(w in (s.name + " " + s.description + " " + " ".join(x.tool for x in s.steps)).lower()
                 for w in words), s.name))
        return {"enabled": self.enabled, "version": self.learner.version if self.learner else "static",
                "meta_tools": [{"name": s.name, "description": s.description,
                    "fingerprint": template_fingerprint(s), "input_schema": dict(s.input_schema),
                    "steps": [x.tool for x in s.steps]} for s in ranked[:limit]]}

    async def read_batch(self, ranges):
        from ..read_batches import read_batch_layout
        from ..naming import assign_short_names
        schema, steps, output = read_batch_layout(len(ranges))
        prototype = assign_short_names([MetaToolSpec(name="batch", description="batch",
            input_schema=schema, steps=steps, output=output)])[0]
        if not self.batch_enabled:
            raise ValueError("Batch reads are disabled")
        inputs = {f"{key}_{i}": value for i, row in enumerate(ranges, 1) for key, value in row.items()}
        return await self.execute(prototype.name, inputs, _batch=True)

    async def execute(self, name: str, arguments: dict, fingerprint: str | None = None, *, _batch=False) -> dict:
        from jsonschema import Draft202012Validator

        if name not in self.active:
            raise ValueError(f"Meta-tool {name!r} is not admitted")
        spec = self.active[name]
        if fingerprint is not None and fingerprint != template_fingerprint(spec):
            raise ValueError("Template changed; search again before execution")
        from bate.runtime import _validate_inputs
        _validate_inputs(spec, arguments)
        # No automatic retry or primitive fallback: partial execution must not
        # be silently repeated. Serialize executions against one upstream set.
        async with self._lock, asyncio.timeout(self.settings["timeout_seconds"]):
            if not self.enabled:
                raise ValueError("Meta-tools are disabled; use meta_tool_control on")
            if _batch and not self.batch_enabled:
                raise ValueError("Batch reads are disabled")
            if name not in self.active:
                raise ValueError("Meta-tool is no longer active")
            needed = {step.tool for step in spec.steps}
            live = {}
            for server in {self.settings["bindings"][key]["server"] for key in needed}:
                live[server] = await self._list_tools(server)
            for key in needed:
                binding = self.settings["bindings"][key]
                before = self.tools[binding["server"]][binding["tool"]]
                current = live[binding["server"]].get(binding["tool"])
                if (current is None or not _read_only(current)
                        or schema_fingerprint(current.inputSchema)
                        != schema_fingerprint(before.inputSchema)):
                    raise ValueError(f"Primitive {key!r} changed; restart and review its schema")

            def bind(key: str):
                binding = self.settings["bindings"][key]
                tool = live[binding["server"]][binding["tool"]]

                async def invoke(**kwargs):
                    Draft202012Validator(tool.inputSchema).validate(kwargs)
                    result = await self.sessions[binding["server"]].call_tool(binding["tool"], kwargs)
                    if result.isError:
                        # Do not dump upstream arguments, secrets or output into diagnostics.
                        raise RuntimeError(f"Upstream tool {key!r} returned isError")
                    return tool_output(result.model_dump(by_alias=True, exclude_none=True))

                return invoke

            execution = await MetaToolExecutor({key: bind(key) for key in needed}).aexecute(spec, arguments)
            return {
                "meta_tool": name, "output": execution.output,
                "completed_steps": [{"id": step.step_id, "tool": step.tool}
                                    for step in execution.steps],
                "duration_seconds": execution.duration_seconds,
            }


def create_server(service: CatalogService):
    from mcp.server.lowlevel import Server
    from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

    schemas = {
        "bate_catalog": {"type": "object", "properties": {}, "additionalProperties": False},
        "meta_tool_search": {"type": "object", "properties": {
            "query": {"type": "string", "maxLength": 2000},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, "additionalProperties": False},
        "meta_tool_run": {"type": "object", "properties": {
            "name": {"type": "string"}, "fingerprint": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "inputs": {"type": "object"}}, "required": ["name", "fingerprint", "inputs"],
            "additionalProperties": False},
        "meta_tool_read_batch": {"type": "object", "properties": {"ranges": {
            "type": "array", "minItems": 2, "maxItems": 4,
            "items": {"type": "object", "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 4096},
                "offset": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 300}},
                "required": ["path", "offset", "limit"], "additionalProperties": False}}},
            "required": ["ranges"], "additionalProperties": False},
    }
    descriptions = {
        "bate_catalog": "Inspect active workflows, rejection reasons and Online progress.",
        "meta_tool_search": "Search current workflows; returns input schemas and fingerprints. Use before meta_tool_run.",
        "meta_tool_run": "Run a complete read-only workflow using the name, fingerprint and inputs from search.",
        "meta_tool_read_batch": "Read 2–4 file ranges known in advance using the configured read MCP primitive (300 lines each).",
    }
    if service.settings.get("session_control", False):
        schemas["meta_tool_control"] = {
            "type": "object", "properties": {"action": {"type": "string", "enum": [
                "status", "on", "off", "static", "online", "batch_on", "batch_off"]}},
            "required": ["action"], "additionalProperties": False}
        descriptions["meta_tool_control"] = (
            "Control this BATE connection when the user asks: status, on/off, static/online, "
            "batch_on/batch_off. Off stops execution and learning after in-flight work completes. "
            "Online requires operator-configured trace files. Never enable implicitly after user off.")
    server = Server("bate-codex", version="1.0.0", instructions=(
        "Use a BATE meta-tool only when its complete fixed sequence matches the user's task. "
        "Search the current catalog with meta_tool_search, then call meta_tool_run with its fingerprint. "
        "These are read-only MCP composites, not native Codex shell/patch tools. "
        "Errors can occur after partial execution; do not retry blindly."
    ))

    @server.list_tools()
    async def list_tools():
        annotation = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
        controls = [Tool(name=name, description=descriptions[name], inputSchema=schema,
                         annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
                         if name == "meta_tool_control" else annotation)
                    for name, schema in schemas.items()
                    if name != "meta_tool_read_batch" or
                       ((service.batch_enabled or service.settings.get("session_control", False))
                        and "read" in service.allowed_reads())]
        # Static retains its direct per-template MCP tools. Online discovery is
        # through stable controls, independent of host tools/list caching.
        direct = service.active.values() if service.mode == "static" and not service.settings.get("session_control", False) else []
        return controls + [Tool(
            name=spec.name,
            description=(spec.description or "Execute a reviewed BATE template")
                        + " [Fixed read-only sequence: " + " -> ".join(step.tool for step in spec.steps) + "]",
            inputSchema=dict(spec.input_schema), annotations=annotation,
        ) for spec in direct]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        try:
            if name in schemas:
                validate_inputs(schemas[name], arguments)
            if name == "meta_tool_control" and name in schemas:
                try:
                    return await service.control(arguments["action"])
                except ValueError as error:
                    if str(error) == "online_sources_not_configured":
                        return CallToolResult(isError=True, content=[TextContent(type="text", text=json.dumps({
                            "error": "online_sources_not_configured",
                            "hint": "Configure explicit online.trace_files, then restart the MCP server."}))])
                    raise
            # Background polling publishes between calls. A catalog/search
            # request also flushes pending evidence for short-lived sessions.
            if name in {"bate_catalog", "meta_tool_search"}:
                await service.refresh()
            if name == "bate_catalog":
                return service.describe()
            if name == "meta_tool_search":
                return service.search(**arguments)
            if name == "meta_tool_run":
                return await service.execute(arguments["name"], arguments["inputs"], arguments["fingerprint"])
            if name == "meta_tool_read_batch":
                return await service.read_batch(arguments["ranges"])
            return await service.execute(name, arguments)
        except MetaToolExecutionError as error:
            payload = {"error": "primitive_execution_failed", "meta_tool": error.meta_tool,
                       "failed_step": error.step_id, "tool": error.tool,
                       "completed_steps": [step.step_id for step in error.completed_steps],
                       "retry_safe": False}
        except TimeoutError:
            payload = {"error": "execution_timeout", "partial_execution_possible": True,
                       "retry_safe": False}
        except Exception as error:
            # Schema validation errors may echo sensitive inputs; report the
            # class only. Startup/config errors are printed to stderr by CLI.
            payload = {"error": type(error).__name__, "partial_execution_possible": True,
                       "retry_safe": False}
        return CallToolResult(isError=True, content=[TextContent(type="text", text=json.dumps(payload))])

    return server


async def run_server(config_path: str | Path, *, check: bool = False) -> dict | None:
    from mcp.server.stdio import stdio_server

    settings = load_settings(config_path)
    service = CatalogService(settings, load_specs(settings["catalog"]) if settings["catalog"] else [])
    async with AsyncExitStack() as stack:
        await service.connect(stack)
        if check:
            return service.describe()
        stack.callback(service.close)
        if settings["mode"] == "online" and service.enabled:
            service.start_learner()
            service.rebuild()
        server = create_server(service)
        learning = asyncio.create_task(service.learn_forever()) if settings.get("online") else None
        try:
            async with stdio_server() as (reader, writer):
                await server.run(reader, writer, server.create_initialization_options())
        finally:
            if learning:
                learning.cancel()
                with suppress(asyncio.CancelledError):
                    await learning
    return None
