from __future__ import annotations

import inspect
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterable, Mapping

from .runtime import (
    MetaToolExecutor, MetaToolValidationError, ToolCallable, ToolRegistry,
    _validate_inputs,
)
from .spec import MetaToolSpec
from .read_batches import is_caller_bound_read_batch


class DispatchMode(str, Enum):
    """How PIE-style infrastructure should route a registered meta-tool."""

    DISABLED = "disabled"
    SAFE_AUTO = "safe_auto"
    FORCE = "force"


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "reasons": list(self.reasons)}


@dataclass(frozen=True)
class AccelerationPolicy:
    """Conservative admission policy for latency-only meta-tools.

    SAFE_AUTO requires reviewed, side-effect-free, data-flow-connected, pure
    candidates with complete LLM-turn evidence by default. FORCE bypasses
    those admission checks and is intended for experiments, not
    accuracy-preserving production dispatch.
    """

    mode: DispatchMode = DispatchMode.DISABLED
    require_reviewed: bool = True
    require_side_effect_free: bool = True
    require_no_compiler_warnings: bool = True
    require_internal_data_flow: bool = True
    min_varying_data_flows: int = 1
    allow_stable_runtime_data_flows: bool = False
    allow_caller_bound_read_batches: bool = False
    min_sequence_purity: float = 1.0
    min_branch_dominance: float = 1.0
    min_llm_turn_coverage: float = 1.0
    min_llm_calls_saved_per_occurrence: float = 1.0
    fallback_on_execution_error: bool = True
    expose_to_model: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.mode, DispatchMode):
            object.__setattr__(self, "mode", DispatchMode(str(self.mode)))
        for name in (
            "min_sequence_purity",
            "min_branch_dominance",
            "min_llm_turn_coverage",
        ):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
            object.__setattr__(self, name, value)
        calls_saved = float(self.min_llm_calls_saved_per_occurrence)
        if calls_saved < 0.0:
            raise ValueError(
                "min_llm_calls_saved_per_occurrence must be non-negative"
            )
        object.__setattr__(
            self, "min_llm_calls_saved_per_occurrence", calls_saved
        )
        varying_flows = int(self.min_varying_data_flows)
        if varying_flows < 0:
            raise ValueError("min_varying_data_flows must be non-negative")
        object.__setattr__(self, "min_varying_data_flows", varying_flows)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AccelerationPolicy":
        return cls(**dict(data))

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "require_reviewed": self.require_reviewed,
            "require_side_effect_free": self.require_side_effect_free,
            "require_no_compiler_warnings": self.require_no_compiler_warnings,
            "require_internal_data_flow": self.require_internal_data_flow,
            "min_varying_data_flows": self.min_varying_data_flows,
            "allow_stable_runtime_data_flows": self.allow_stable_runtime_data_flows,
            "allow_caller_bound_read_batches": self.allow_caller_bound_read_batches,
            "min_sequence_purity": self.min_sequence_purity,
            "min_branch_dominance": self.min_branch_dominance,
            "min_llm_turn_coverage": self.min_llm_turn_coverage,
            "min_llm_calls_saved_per_occurrence": (
                self.min_llm_calls_saved_per_occurrence
            ),
            "fallback_on_execution_error": self.fallback_on_execution_error,
            "expose_to_model": self.expose_to_model,
        }

    def evaluate(self, spec: MetaToolSpec) -> PolicyDecision:
        if self.mode is DispatchMode.DISABLED:
            return PolicyDecision(False, ("meta-tools are disabled",))
        if self.mode is DispatchMode.FORCE:
            return PolicyDecision(True)

        metadata = spec.metadata
        reasons: list[str] = []
        reviewed = (
            metadata.get("reviewed") is True
            or metadata.get("requires_domain_review") is False
        )
        if self.require_reviewed and not reviewed:
            reasons.append("candidate has not completed domain review")
        if (
            self.require_side_effect_free
            and metadata.get("side_effect_free") is not True
        ):
            reasons.append("candidate is not declared side-effect-free")
        if self.require_no_compiler_warnings and metadata.get(
            "compiler_warnings"
        ):
            reasons.append("candidate has unresolved compiler warnings")
        structural_flows = _count_internal_step_bindings(spec)
        declared_flows = _optional_int(metadata.get("internal_data_flows"))
        if declared_flows is not None and declared_flows != structural_flows:
            reasons.append(
                "internal_data_flows metadata does not match the compiled bindings"
            )
        caller_read_batch = self.allow_caller_bound_read_batches and is_caller_bound_read_batch(spec)
        if self.require_internal_data_flow and structural_flows < 1 and not caller_read_batch:
            reasons.append("candidate has no internal cross-step data flow")
        data_flow_reviewed = metadata.get("data_flow_reviewed") is True
        if not data_flow_reviewed and not caller_read_batch:
            stable_runtime_flows = _optional_int(
                metadata.get("stable_runtime_data_flows")
            ) or 0
            if not (
                self.allow_stable_runtime_data_flows
                and stable_runtime_flows > 0
                and metadata.get("runtime_local_data_flow_verified") is True
            ):
                _require_minimum(
                    metadata,
                    key="varying_data_flows",
                    minimum=float(self.min_varying_data_flows),
                    reasons=reasons,
                )
        _require_minimum(
            metadata,
            key="sequence_purity",
            minimum=self.min_sequence_purity,
            reasons=reasons,
        )
        _require_minimum(
            metadata,
            key="min_branch_dominance",
            minimum=self.min_branch_dominance,
            reasons=reasons,
        )
        _require_minimum(
            metadata,
            key="llm_turn_coverage",
            minimum=self.min_llm_turn_coverage,
            reasons=reasons,
        )
        _require_minimum(
            metadata,
            key="mean_llm_calls_saved_per_observed_occurrence",
            minimum=self.min_llm_calls_saved_per_occurrence,
            reasons=reasons,
        )
        return PolicyDecision(not reasons, tuple(reasons))


class MetaToolRegistry:
    """Version-neutral in-memory registry for reviewed meta-tool specs."""

    def __init__(
        self,
        specs: Iterable[MetaToolSpec] | Mapping[str, MetaToolSpec] | None = None,
    ) -> None:
        self._specs: dict[str, MetaToolSpec] = {}
        if isinstance(specs, Mapping):
            for name, spec in specs.items():
                if name != spec.name:
                    raise ValueError(
                        f"Registry key {name!r} does not match spec {spec.name!r}"
                    )
                self.register(spec)
        else:
            for spec in specs or ():
                self.register(spec)

    def register(self, spec: MetaToolSpec, *, replace: bool = False) -> None:
        if not isinstance(spec, MetaToolSpec):
            raise TypeError("MetaToolRegistry only accepts MetaToolSpec values")
        if spec.name in self._specs and not replace:
            raise ValueError(f"Meta-tool {spec.name!r} is already registered")
        self._specs[spec.name] = spec

    def get(self, name: str) -> MetaToolSpec:
        try:
            return self._specs[name]
        except KeyError as error:
            raise KeyError(f"Meta-tool {name!r} is not registered") from error

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._specs

    def decisions(
        self, policy: AccelerationPolicy
    ) -> dict[str, PolicyDecision]:
        return {
            name: policy.evaluate(self._specs[name]) for name in self.names()
        }

    def tool_schemas(
        self, policy: AccelerationPolicy | None = None
    ) -> tuple[dict[str, Any], ...]:
        return tuple(
            self._specs[name].as_tool_schema()
            for name in self.names()
            if policy is None or policy.evaluate(self._specs[name]).allowed
        )


@dataclass(frozen=True)
class DispatchEvent:
    """Compact side-channel telemetry; never contains tool arguments or output."""

    requested_tool: str
    path: str
    duration_seconds: float
    meta_tool: str | None = None
    primitive_steps: int = 0
    decision: PolicyDecision | None = None
    deopt_reason: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "requested_tool": self.requested_tool,
            "path": self.path,
            "duration_seconds": self.duration_seconds,
            "primitive_steps": self.primitive_steps,
        }
        if self.meta_tool is not None:
            value["meta_tool"] = self.meta_tool
        if self.decision is not None:
            value["decision"] = self.decision.to_dict()
        if self.deopt_reason:
            value["deopt_reason"] = self.deopt_reason
        if self.error:
            value["error"] = self.error
        return value


class MetaToolRejectedError(RuntimeError):
    def __init__(self, name: str, decision: PolicyDecision) -> None:
        reason = "; ".join(decision.reasons) or "rejected by policy"
        super().__init__(f"Meta-tool {name!r} was not dispatched: {reason}")
        self.name = name
        self.decision = decision


TelemetrySink = Callable[[DispatchEvent], Any]


class MetaToolDispatcher:
    """PIE-style registry and policy dispatch with compact model output.

    A dispatched meta-tool returns only MetaToolResult.output. Primitive step
    details stay inside the executor and compact telemetry. A user-program
    fallback callable can implement the original, accuracy-preserving path when
    a fast path is disabled or rejected.
    """

    def __init__(
        self,
        primitives: ToolRegistry | Mapping[str, ToolCallable],
        meta_tools: MetaToolRegistry | Iterable[MetaToolSpec],
        *,
        policy: AccelerationPolicy | None = None,
        fallbacks: Mapping[str, ToolCallable] | None = None,
        telemetry: TelemetrySink | None = None,
    ) -> None:
        self.primitives = (
            primitives
            if isinstance(primitives, ToolRegistry)
            else ToolRegistry(primitives)
        )
        self.meta_tools = (
            meta_tools
            if isinstance(meta_tools, MetaToolRegistry)
            else MetaToolRegistry(meta_tools)
        )
        collisions = set(self.primitives.names()) & set(self.meta_tools.names())
        if collisions:
            names = ", ".join(sorted(collisions))
            raise ValueError(f"Primitive/meta-tool name collision: {names}")
        self.policy = policy or AccelerationPolicy()
        self.fallbacks = dict(fallbacks or {})
        self.telemetry = telemetry
        self.last_event: DispatchEvent | None = None
        self.telemetry_failures = 0
        self._executor = MetaToolExecutor(self.primitives)

    def model_tool_schemas(self) -> tuple[dict[str, Any], ...]:
        if not self.policy.expose_to_model:
            return ()
        return self.meta_tools.tool_schemas(self.policy)

    def dispatch(self, name: str, arguments: Mapping[str, Any]) -> Any:
        if not isinstance(arguments, Mapping):
            raise TypeError("Tool arguments must be a mapping")
        if name in self.meta_tools:
            return self._dispatch_meta(name, dict(arguments))

        function = self.primitives.get(name)
        started = time.perf_counter()
        try:
            output = _invoke_sync(function, arguments)
        except Exception as error:
            self._emit(
                DispatchEvent(
                    requested_tool=name,
                    path="primitive",
                    duration_seconds=time.perf_counter() - started,
                    error=_error_text(error),
                )
            )
            raise
        self._emit(
            DispatchEvent(
                requested_tool=name,
                path="primitive",
                duration_seconds=time.perf_counter() - started,
            )
        )
        return output

    async def adispatch(self, name: str, arguments: Mapping[str, Any]) -> Any:
        if not isinstance(arguments, Mapping):
            raise TypeError("Tool arguments must be a mapping")
        if name in self.meta_tools:
            return await self._adispatch_meta(name, dict(arguments))

        function = self.primitives.get(name)
        started = time.perf_counter()
        try:
            output = function(**dict(arguments))
            if inspect.isawaitable(output):
                output = await output
        except Exception as error:
            self._emit(
                DispatchEvent(
                    requested_tool=name,
                    path="primitive",
                    duration_seconds=time.perf_counter() - started,
                    error=_error_text(error),
                )
            )
            raise
        self._emit(
            DispatchEvent(
                requested_tool=name,
                path="primitive",
                duration_seconds=time.perf_counter() - started,
            )
        )
        return output

    def _dispatch_meta(self, name: str, arguments: Mapping[str, Any]) -> Any:
        spec = self.meta_tools.get(name)
        decision = self.policy.evaluate(spec)
        started = time.perf_counter()
        if not decision.allowed:
            return self._deopt_sync(
                spec,
                arguments,
                decision,
                started,
                reason="; ".join(decision.reasons),
            )
        try:
            execution = self._executor.execute(spec, arguments)
        except Exception as error:
            if (
                self.policy.fallback_on_execution_error
                and not isinstance(error, MetaToolValidationError)
                and spec.metadata.get("side_effect_free") is True
                and name in self.fallbacks
            ):
                return self._deopt_sync(
                    spec,
                    arguments,
                    decision,
                    started,
                    reason=f"meta execution failed: {_error_text(error)}",
                )
            self._emit(
                DispatchEvent(
                    requested_tool=name,
                    path="meta",
                    meta_tool=name,
                    primitive_steps=len(spec.steps),
                    decision=decision,
                    duration_seconds=time.perf_counter() - started,
                    error=_error_text(error),
                )
            )
            raise
        self._emit(
            DispatchEvent(
                requested_tool=name,
                path="meta",
                meta_tool=name,
                primitive_steps=len(execution.steps),
                decision=decision,
                duration_seconds=time.perf_counter() - started,
            )
        )
        return execution.output

    async def _adispatch_meta(
        self, name: str, arguments: Mapping[str, Any]
    ) -> Any:
        spec = self.meta_tools.get(name)
        decision = self.policy.evaluate(spec)
        started = time.perf_counter()
        if not decision.allowed:
            return await self._deopt_async(
                spec,
                arguments,
                decision,
                started,
                reason="; ".join(decision.reasons),
            )
        try:
            execution = await self._executor.aexecute(spec, arguments)
        except Exception as error:
            if (
                self.policy.fallback_on_execution_error
                and not isinstance(error, MetaToolValidationError)
                and spec.metadata.get("side_effect_free") is True
                and name in self.fallbacks
            ):
                return await self._deopt_async(
                    spec,
                    arguments,
                    decision,
                    started,
                    reason=f"meta execution failed: {_error_text(error)}",
                )
            self._emit(
                DispatchEvent(
                    requested_tool=name,
                    path="meta",
                    meta_tool=name,
                    primitive_steps=len(spec.steps),
                    decision=decision,
                    duration_seconds=time.perf_counter() - started,
                    error=_error_text(error),
                )
            )
            raise
        self._emit(
            DispatchEvent(
                requested_tool=name,
                path="meta",
                meta_tool=name,
                primitive_steps=len(execution.steps),
                decision=decision,
                duration_seconds=time.perf_counter() - started,
            )
        )
        return execution.output

    def _deopt_sync(
        self,
        spec: MetaToolSpec,
        arguments: Mapping[str, Any],
        decision: PolicyDecision,
        started: float,
        *,
        reason: str,
    ) -> Any:
        fallback = self.fallbacks.get(spec.name)
        if fallback is None:
            error = MetaToolRejectedError(spec.name, decision)
            self._emit(
                DispatchEvent(
                    requested_tool=spec.name,
                    path="rejected",
                    meta_tool=spec.name,
                    decision=decision,
                    duration_seconds=time.perf_counter() - started,
                    deopt_reason=reason,
                    error=_error_text(error),
                )
            )
            raise error
        try:
            _validate_inputs(spec, arguments)
            output = _invoke_sync(fallback, arguments)
        except Exception as error:
            self._emit(
                DispatchEvent(
                    requested_tool=spec.name,
                    path="fallback",
                    meta_tool=spec.name,
                    decision=decision,
                    duration_seconds=time.perf_counter() - started,
                    deopt_reason=reason,
                    error=_error_text(error),
                )
            )
            raise
        self._emit(
            DispatchEvent(
                requested_tool=spec.name,
                path="fallback",
                meta_tool=spec.name,
                decision=decision,
                duration_seconds=time.perf_counter() - started,
                deopt_reason=reason,
            )
        )
        return output

    async def _deopt_async(
        self,
        spec: MetaToolSpec,
        arguments: Mapping[str, Any],
        decision: PolicyDecision,
        started: float,
        *,
        reason: str,
    ) -> Any:
        fallback = self.fallbacks.get(spec.name)
        if fallback is None:
            error = MetaToolRejectedError(spec.name, decision)
            self._emit(
                DispatchEvent(
                    requested_tool=spec.name,
                    path="rejected",
                    meta_tool=spec.name,
                    decision=decision,
                    duration_seconds=time.perf_counter() - started,
                    deopt_reason=reason,
                    error=_error_text(error),
                )
            )
            raise error
        try:
            _validate_inputs(spec, arguments)
            output = fallback(**dict(arguments))
            if inspect.isawaitable(output):
                output = await output
        except Exception as error:
            self._emit(
                DispatchEvent(
                    requested_tool=spec.name,
                    path="fallback",
                    meta_tool=spec.name,
                    decision=decision,
                    duration_seconds=time.perf_counter() - started,
                    deopt_reason=reason,
                    error=_error_text(error),
                )
            )
            raise
        self._emit(
            DispatchEvent(
                requested_tool=spec.name,
                path="fallback",
                meta_tool=spec.name,
                decision=decision,
                duration_seconds=time.perf_counter() - started,
                deopt_reason=reason,
            )
        )
        return output

    def _emit(self, event: DispatchEvent) -> None:
        self.last_event = event
        if self.telemetry is None:
            return
        try:
            self.telemetry(event)
        except Exception:
            # Observability must not change user-program behavior.
            self.telemetry_failures += 1


def _require_minimum(
    metadata: Mapping[str, Any],
    *,
    key: str,
    minimum: float,
    reasons: list[str],
) -> None:
    raw = metadata.get(key)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        if minimum > 0.0:
            reasons.append(f"missing numeric metadata {key!r}")
        return
    if float(raw) < minimum:
        reasons.append(f"{key}={float(raw):g} is below {minimum:g}")


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _count_internal_step_bindings(spec: MetaToolSpec) -> int:
    count = 0
    prior_steps: set[str] = set()

    def visit(value: Any) -> None:
        nonlocal count
        if isinstance(value, Mapping):
            source = value.get("$step")
            if isinstance(source, str) and source in prior_steps:
                count += 1
                return
            for item in value.values():
                visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    for step in spec.steps:
        visit(step.arguments)
        prior_steps.add(step.step_id)
    return count


def _invoke_sync(
    function: ToolCallable, arguments: Mapping[str, Any]
) -> Any:
    output = function(**dict(arguments))
    if inspect.isawaitable(output):
        close = getattr(output, "close", None)
        if callable(close):
            close()
        raise TypeError("Asynchronous tool used with dispatch(); use adispatch()")
    return output


def _error_text(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"
