"""Local catalog admission policy shared by benchmark integrations.

The policy identifier is preserved for compatibility with recorded catalogs.
There is no RPC transport or external repository loader in this module.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .dispatch import AccelerationPolicy, DispatchMode
from .spec import MetaToolSpec

LEGACY_SUPPORTED_TOOLS = {"read", "bash", "edit", "write", "grep", "find", "ls"}
DEFAULT_READ_ONLY_TOOLS = {"read", "grep", "find", "ls"}
AUTO_ADMISSION_POLICY = "pi-meta-tool-registered-tools-v2"


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


def _contains_step_binding(value: Any) -> bool:
    if isinstance(value, Mapping):
        if "$step" in value:
            return True
        return any(_contains_step_binding(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_step_binding(item) for item in value)
    return False


def auto_admit_specs(
    specs: list[MetaToolSpec],
    admission: Mapping[str, Any] | None = None,
    adapter_supported_tools: set[str] | None = None,
) -> tuple[list[MetaToolSpec], list[dict[str, Any]]]:
    """Build an active catalog that is executable and strongly evidenced.

    `admission` may narrow the Pi tool whitelist and override admission
    thresholds via `supported_tools`, `read_only_tools`, and `policy`.
    The adapter supplies the actual registered Pi tools it can execute.

    Read-only specs run through SAFE_AUTO. Specs containing a supported mutator
    remain useful for coding workflows, but carry a per-run confirmation guard
    and execute through FORCE only after Pi displays their fixed plan.
    """
    admission = admission or {}

    adapter_supported = (
        adapter_supported_tools
        if adapter_supported_tools is not None
        else LEGACY_SUPPORTED_TOOLS
    )
    configured_supported = _string_set_or_default(admission.get("supported_tools"))
    supported = (
        adapter_supported
        if configured_supported is None
        else configured_supported & adapter_supported
    )
    configured_read_only = {
        str(item)
        for item in admission.get("read_only_tools") or DEFAULT_READ_ONLY_TOOLS
    }
    read_only = configured_read_only & supported
    min_distinct_sessions = max(
        1, int(admission.get("min_distinct_sessions", 2))
    )
    allowed_dynamic_mutation_tools = {
        str(item)
        for item in admission.get("allow_runtime_derived_mutation_tools", [])
        if str(item) in supported
    }
    allow_confirmed_dynamic_mutations = bool(
        admission.get("allow_confirmed_runtime_derived_mutations", True)
    )
    denied_dynamic_mutation_tools = {
        str(item)
        for item in admission.get(
            "deny_runtime_derived_mutation_tools", ["bash", "edit", "write"]
        )
    }
    policy_config = _require_mapping(
        admission.get("policy", {}), "admission.policy"
    )
    policy = (
        AccelerationPolicy.from_dict(policy_config)
        if policy_config
        else AccelerationPolicy(
            mode=DispatchMode.SAFE_AUTO,
            require_side_effect_free=False,
        )
    )
    admitted: list[MetaToolSpec] = []
    rejected: list[dict[str, Any]] = []

    for spec in specs:
        primitive_tools = sorted({step.tool for step in spec.steps})
        unsupported = [name for name in primitive_tools if name not in supported]
        reasons: list[str] = []
        if unsupported:
            reasons.append(
                "unavailable primitive tools (not registered by Pi or a provider): "
                + ", ".join(unsupported)
            )
        distinct_sessions = spec.metadata.get("distinct_sessions")
        if (
            isinstance(distinct_sessions, int)
            and distinct_sessions < min_distinct_sessions
        ):
            reasons.append(
                "insufficient distinct session evidence: "
                f"{distinct_sessions} < {min_distinct_sessions}"
            )
        schema_conflicts = spec.metadata.get("tool_schema_conflicts", {})
        if isinstance(schema_conflicts, Mapping) and schema_conflicts:
            reasons.append(
                "inconsistent primitive schemas across source traces: "
                + ", ".join(sorted(str(name) for name in schema_conflicts))
            )
        dynamic_mutators = [
            (step.step_id, step.tool)
            for step in spec.steps
            if step.tool not in read_only and _contains_step_binding(step.arguments)
        ]
        blocked_dynamic_mutators = [
            (step_id, tool)
            for step_id, tool in dynamic_mutators
            if tool in denied_dynamic_mutation_tools
            or (
                tool not in allowed_dynamic_mutation_tools
                and not allow_confirmed_dynamic_mutations
            )
        ]
        if blocked_dynamic_mutators:
            reasons.append(
                "mutating steps use runtime-derived arguments that cannot be "
                "shown before execution: "
                + ", ".join(
                    f"{step_id} ({tool})" for step_id, tool in blocked_dynamic_mutators
                )
            )

        direct_read_only = (
            set(primitive_tools).issubset(read_only)
            and spec.metadata.get("side_effect_free") is True
        )
        execution_guard = "none" if direct_read_only else "confirm_each_run"

        metadata = {
            **dict(spec.metadata),
            "reviewed": True,
            "requires_domain_review": False,
            "human_reviewed": False,
            "auto_approved": True,
            "admission_policy": AUTO_ADMISSION_POLICY,
            "execution_guard": execution_guard,
            "lifecycle_state": "active",
            "runtime_derived_mutation_steps": [
                {"step_id": step_id, "tool": tool} for step_id, tool in dynamic_mutators
            ],
            "runtime_derived_mutation_tools_explicitly_allowed": sorted(
                {
                    tool
                    for _, tool in dynamic_mutators
                    if tool in allowed_dynamic_mutation_tools
                }
            ),
            "runtime_derived_mutation_tools_confirmed_at_step": sorted(
                {
                    tool
                    for _, tool in dynamic_mutators
                    if tool not in denied_dynamic_mutation_tools
                    and (
                        allow_confirmed_dynamic_mutations
                        or tool in allowed_dynamic_mutation_tools
                    )
                }
            ),
        }
        reviewed_spec = MetaToolSpec(
            name=spec.name,
            description=spec.description,
            input_schema=spec.input_schema,
            steps=spec.steps,
            output=spec.output,
            metadata=metadata,
        )
        reasons.extend(policy.evaluate(reviewed_spec).reasons)
        reasons = list(dict.fromkeys(reasons))

        if reasons:
            rejected.append(
                {
                    "name": spec.name,
                    "primitive_tools": primitive_tools,
                    "reasons": reasons,
                    "spec": reviewed_spec.to_dict(),
                }
            )
        else:
            admitted.append(reviewed_spec)

    return admitted, rejected
