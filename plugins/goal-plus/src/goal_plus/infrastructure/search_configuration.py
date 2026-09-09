"""Frozen host/model policy and environment configuration, without workspace I/O."""

from __future__ import annotations
import json
import os
from typing import Any
from goal_plus.agent_hosts import host_binding, host_model_catalog, resolve_inner_agent_binding, portable_strategy_mode, UnsupportedHostCapability
from goal_plus.domain.agents import AgentHostKind, ModelSpec, WorkerLaunchOptions
from goal_plus.domain.search import SearchSpec, StrategySpec, DevicePoolSpec, DevicePoolInventory
from goal_plus.domain.worker_policy import resolved_inner_agent_id, validate_worker_budget_for_host, worker_budget_dict
from goal_plus.ports.candidate_host import ResolvedInnerAgentBinding
from goal_plus.ports.search_configuration import ModelDiscovery

GLOBAL_EVIDENCE_MODE_ENV = "GOAL_PLUS_GLOBAL_EVIDENCE_MODE"


GLOBAL_EVIDENCE_MODES = frozenset({"manual", "auto", "independent"})


SUPPLEMENTAL_EVALUATION_ENABLED_ENV = "GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED"


SUPPLEMENTAL_EVALUATION_REQUIRED_ENV = "GOAL_PLUS_SUPPLEMENTAL_EVALUATION_REQUIRED"


DEVICE_POOLS_ENV = "GOAL_PLUS_DEVICE_POOLS"


def _boolean_environment_value(
    name: str,
    *,
    default: bool,
    environment: dict[str, str] | None = None,
) -> bool:
    source = os.environ if environment is None else environment
    raw = source.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value, found {raw!r}")


def supplemental_evaluation_enabled(
    environment: dict[str, str] | None = None,
) -> bool:
    return _boolean_environment_value(
        SUPPLEMENTAL_EVALUATION_ENABLED_ENV,
        default=False,
        environment=environment,
    )


def supplemental_evaluation_required(
    environment: dict[str, str] | None = None,
) -> bool:
    return _boolean_environment_value(
        SUPPLEMENTAL_EVALUATION_REQUIRED_ENV,
        default=False,
        environment=environment,
    )


class HostModelDiscovery:
    def list_available_models(
        self,
        host: AgentHostKind,
        query: str | None = None,
    ) -> dict[str, Any]:
        binding = host_binding(host)
        return {
            "host": host,
            "adapter_version": binding.adapter_version,
            "models": host_model_catalog(host).list_available_models(query),
        }


class SearchConfiguration:
    def __init__(self, models: ModelDiscovery) -> None:
        self.models = models

    @staticmethod
    def match_available_model(
        requested: str,
        available: list[dict[str, Any]],
    ) -> dict[str, Any]:
        needle = requested.strip().casefold()
        exact_refs = [
            model
            for model in available
            if str(model.get("model") or "").casefold() == needle
        ]
        if len(exact_refs) == 1:
            return exact_refs[0]
        exact_ids = [
            model
            for model in available
            if str(model.get("model_id") or "").casefold() == needle
        ]
        if len(exact_ids) == 1:
            return exact_ids[0]
        candidates = [
            model
            for model in available
            if needle
            in " ".join(
                str(model.get(key) or "")
                for key in ("model", "model_id", "display_name")
            ).casefold()
        ]
        if len(candidates) == 1:
            return candidates[0]
        if not candidates and not exact_refs and not exact_ids:
            raise ValueError(f"requested model is not available: {requested}")
        matches = exact_refs or exact_ids or candidates
        raise ValueError(
            f"requested model is ambiguous: {requested}; matches: "
            + ", ".join(str(model.get("model")) for model in matches)
        )

    def normalize_strategy_models(self, spec: SearchSpec, host: AgentHostKind) -> SearchSpec:
        requested = list(spec.strategy.models)
        if not requested:
            return spec
        explicit_counts = [model.count is not None for model in requested]
        if any(explicit_counts) and not all(explicit_counts):
            raise ValueError(
                "strategy.models must either specify count for every model or for none"
            )
        max_parallel = spec.budget.max_parallel
        if len(requested) > max_parallel:
            raise ValueError(
                "strategy.models cannot contain more entries than budget.max_parallel"
            )
        if all(explicit_counts):
            counts: list[int | None] = [int(model.count or 0) for model in requested]
            if sum(int(count or 0) for count in counts) != max_parallel:
                raise ValueError(
                    "explicit strategy.models counts must sum to budget.max_parallel"
                )
        else:
            counts = [None] * len(requested)

        discovery = self.models.list_available_models(host)
        available = discovery["models"]
        normalized_models: list[ModelSpec] = []
        for requested_model, count in zip(requested, counts, strict=True):
            match = self.match_available_model(requested_model.model, available)
            normalized_models.append(
                requested_model.model_copy(
                    update={
                        "model": match["model"],
                        "count": count,
                        "provider": match.get("provider"),
                        "adapter_version": discovery["adapter_version"],
                    }
                )
            )
        strategy = spec.strategy.model_copy(update={"models": normalized_models})
        return spec.model_copy(update={"strategy": strategy})

    @staticmethod
    def validate_host_workspace_backend(spec: SearchSpec, host: AgentHostKind) -> None:
        backend = spec.workspace.backend
        capabilities = host_binding(host).capabilities
        supported = capabilities.supported_workspace_backends
        if backend in supported and capabilities.search_enabled:
            return
        if backend in supported and not capabilities.search_enabled:
            raise UnsupportedHostCapability(
                capabilities.disabled_reason
                or f"{host} worker_host Search capability is disabled"
            )
        supported_names = ", ".join(sorted(supported))
        raise UnsupportedHostCapability(
            f"{host} worker_host does not support workspace backend {backend!r}; "
            f"supported backends: {supported_names}. Candidate checkout was "
            "rejected before Search run creation."
        )

    @classmethod
    def normalize_inner_agent_spec(cls, spec: SearchSpec, host: AgentHostKind) -> SearchSpec:
        inner_agent = resolved_inner_agent_id(spec.strategy, host)
        if spec.strategy.inner_agent == inner_agent:
            return spec
        strategy = spec.strategy.model_copy(update={"inner_agent": inner_agent})
        return spec.model_copy(update={"strategy": strategy})

    @classmethod
    def inner_agent_binding(
        cls,
        strategy: StrategySpec,
        host: AgentHostKind,
    ) -> ResolvedInnerAgentBinding:
        return resolve_inner_agent_binding(
            host,
            resolved_inner_agent_id(strategy, host),
            legacy_native_agent_type=strategy.worker_agent_type,
        )

    @classmethod
    def validate_inner_agent_binding(cls, strategy: StrategySpec, host: AgentHostKind) -> None:
        cls.inner_agent_binding(strategy, host)

    @staticmethod
    def apply_global_evidence_mode_from_environment(spec: SearchSpec) -> SearchSpec:
        environment_mode = os.environ.get(GLOBAL_EVIDENCE_MODE_ENV)
        if environment_mode is None:
            return spec
        environment_mode = environment_mode.strip()
        if environment_mode not in GLOBAL_EVIDENCE_MODES:
            allowed = ", ".join(sorted(GLOBAL_EVIDENCE_MODES))
            raise ValueError(f"{GLOBAL_EVIDENCE_MODE_ENV} must be one of {allowed}")
        configured_mode = spec.strategy.config.get("global_evidence_mode")
        if configured_mode is not None and configured_mode != environment_mode:
            raise ValueError(
                "strategy.config.global_evidence_mode conflicts with "
                f"{GLOBAL_EVIDENCE_MODE_ENV}"
            )
        config = {**spec.strategy.config, "global_evidence_mode": environment_mode}
        strategy = spec.strategy.model_copy(update={"config": config})
        return spec.model_copy(update={"strategy": strategy})

    @staticmethod
    def validate_strategy_config(strategy: StrategySpec) -> None:
        evidence_mode = strategy.config.get("global_evidence_mode", "manual")
        if evidence_mode not in GLOBAL_EVIDENCE_MODES:
            allowed = ", ".join(sorted(GLOBAL_EVIDENCE_MODES))
            raise ValueError(
                "strategy.config.global_evidence_mode must be one of " + allowed
            )
        misplaced = sorted(
            {"min_runtime_seconds", "min_verifier_runs"}.intersection(strategy.config)
        )
        if misplaced:
            raise ValueError(
                "strategy.config cannot contain worker lease fields "
                f"{', '.join(misplaced)}; place them in strategy.worker_budget"
            )

    @staticmethod
    def resolve_device_inventory(
        device_pools: list[DevicePoolSpec],
        max_parallel: int,
    ) -> dict[str, DevicePoolInventory]:
        raw_config = os.environ.get(DEVICE_POOLS_ENV, "").strip()
        if not device_pools:
            return {}
        if not raw_config:
            raise ValueError(
                f"device pools require trusted host configuration in {DEVICE_POOLS_ENV}"
            )
        try:
            payload = json.loads(raw_config)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{DEVICE_POOLS_ENV} must be a JSON object") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{DEVICE_POOLS_ENV} must be a JSON object")

        inventory: dict[str, DevicePoolInventory] = {}
        visibility_owners: dict[str, str] = {}
        for pool in device_pools:
            configured = payload.get(pool.pool_id)
            if configured is None:
                raise ValueError(
                    f"device pool {pool.pool_id!r} is not configured by the host"
                )
            resolved = DevicePoolInventory.model_validate(configured)
            prior_pool = visibility_owners.get(resolved.visibility_env)
            if prior_pool is not None:
                raise ValueError(
                    f"device pools {prior_pool!r} and {pool.pool_id!r} use the same "
                    f"visibility_env {resolved.visibility_env!r}"
                )
            if len(resolved.devices) < max_parallel:
                raise ValueError(
                    f"device pool {pool.pool_id!r} has {len(resolved.devices)} device(s), "
                    f"fewer than budget.max_parallel={max_parallel}"
                )
            visibility_owners[resolved.visibility_env] = pool.pool_id
            inventory[pool.pool_id] = resolved
        return inventory

    def strategy_mode(self, strategy: StrategySpec) -> str:
        return strategy.name.strip().lower().replace("-", "_")

    def validate_host_strategy(self, strategy: StrategySpec, host: AgentHostKind) -> None:
        self.validate_strategy_config(strategy)
        self.validate_inner_agent_binding(strategy, host)
        self.validate_worker_launch_for_host(strategy, host)
        validate_worker_budget_for_host(
            worker_host=host,
            worker_budget=strategy.worker_budget,
        )
        if not portable_strategy_mode(strategy.name):
            raise ValueError(
                f"{host} worker driver does not support strategy "
                f"{strategy.name}; use default/agent_guided or random"
            )

    def validate_worker_launch_for_host(self, strategy: StrategySpec, host: AgentHostKind) -> None:
        self.validate_worker_launch_options_for_host(
            host,
            strategy.worker_launch,
            "worker_launch",
        )
        for model in strategy.models:
            launch = WorkerLaunchOptions(
                model=model.model,
                reasoning_effort=model.reasoning_effort,
                service_tier=model.service_tier,
            )
            self.validate_worker_launch_options_for_host(
                host,
                launch,
                f"model {model.model!r}",
            )

    @staticmethod
    def validate_worker_launch_options_for_host(
        worker_host: str,
        worker_launch: WorkerLaunchOptions | None,
        label: str,
    ) -> None:
        if worker_launch is None:
            return
        adapter = host_binding(worker_host)
        requested = worker_launch.model_dump(mode="json", exclude_none=True)
        capability_by_field = {
            "model": adapter.capabilities.supports_model_override,
            "reasoning_effort": adapter.capabilities.supports_reasoning_effort,
            "service_tier": adapter.capabilities.supports_service_tier,
        }
        unsupported = sorted(
            field for field in requested if not capability_by_field[field]
        )
        if unsupported:
            raise ValueError(
                f"{worker_host} worker_host does not support {label} fields: "
                f"{', '.join(unsupported)}"
            )

    def worker_launch_dict(self, strategy: StrategySpec) -> dict[str, Any] | None:
        if strategy.worker_launch is None:
            return None
        return strategy.worker_launch.model_dump(mode="json", exclude_none=True)

    def worker_policy(self, strategy: StrategySpec, host: AgentHostKind) -> dict[str, Any]:
        adapter = host_binding(host)
        binding = self.inner_agent_binding(strategy, host)
        worker_budget = worker_budget_dict(strategy)
        worker_launch = self.worker_launch_dict(strategy)
        policy = {
            "host": host,
            "inner_agent": binding.inner_agent_id,
            "inner_agent_binding": binding.as_dict(),
            "worker_budget": worker_budget,
            "worker_launch": worker_launch,
            "pool": adapter.capabilities.pool.as_dict(),
            "reason": (
                f"主 agent 使用 search_start_agent_session 返回的 launch payload，"
                f"通过 host-pool 契约启动 {host} worker。"
            ),
        }
        if strategy.worker_agent_type is not None:
            policy["legacy_worker_agent_type"] = strategy.worker_agent_type
        return policy

    def normalize_worker_policy(
        self,
        strategy: StrategySpec,
        worker_policy: dict[str, Any] | None = None,
        *,
        host: AgentHostKind,
    ) -> dict[str, Any]:
        base_policy = self.worker_policy(strategy, host)
        policy = {**base_policy, **(worker_policy or {})}
        if policy.get("host") != host:
            raise ValueError("worker policy host must match the frozen Main host binding")
        inner_agent = str(
            policy.get("inner_agent") or resolved_inner_agent_id(strategy, host)
        )
        legacy_native_agent_type = policy.get("legacy_worker_agent_type")
        if legacy_native_agent_type is None:
            legacy_native_agent_type = policy.get("worker_agent_type")
        binding = resolve_inner_agent_binding(
            host,
            inner_agent,
            legacy_native_agent_type=(
                str(legacy_native_agent_type)
                if legacy_native_agent_type is not None
                else strategy.worker_agent_type
            ),
        )
        policy["inner_agent"] = binding.inner_agent_id
        policy["inner_agent_binding"] = binding.as_dict()
        policy.pop("worker_agent_type", None)
        if legacy_native_agent_type is not None:
            policy["legacy_worker_agent_type"] = str(legacy_native_agent_type)
        return policy
