"""Shared frozen/task worker budget and default inner-agent policy."""

import re
from typing import Any
from goal_plus.domain.agents import (
    WorkerBudget, SelectedModel, AgentHostKind,
    INNER_AGENT_COMMON,
    INNER_AGENT_AUTORESEARCH,
)
from goal_plus.domain.search import CandidateTask, FrozenSpec, StrategySpec, SearchPlan, DevicePoolSpec, DevicePoolInventory


def validate_worker_budget_for_host(
    *,
    worker_host: str,
    worker_budget: WorkerBudget | None,
) -> None:
    if worker_host in {"pi-rpc", "thinkthread"} and (
        worker_budget is None or worker_budget.max_runtime_seconds is None
    ):
        raise ValueError(
            f"{worker_host} worker_budget requires max_runtime_seconds so the "
            "host supervisor can enforce a process deadline"
        )
    if worker_budget is None:
        return
    if worker_host == "codex" and worker_budget.max_runtime_seconds is None:
        raise ValueError(
            "codex worker_budget requires max_runtime_seconds so the "
            "parent agent can enforce a watchdog deadline"
        )


def worker_budget_dict(strategy: StrategySpec) -> dict[str, Any] | None:
    if strategy.worker_budget is None:
        return None
    return strategy.worker_budget.model_dump(mode="json")


def candidate_worker_budget(
    frozen: FrozenSpec,
    candidate_task: CandidateTask,
) -> dict[str, Any] | None:
    worker_policy = candidate_task.strategy_metadata.get("worker_policy", {})
    budget = worker_policy.get("worker_budget")
    if budget is not None:
        return dict(budget)
    return worker_budget_dict(frozen.spec.strategy)


def normalize_worker_budget_override(
    *,
    worker_host: str,
    worker_budget: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if worker_budget is None:
        return None
    parsed = WorkerBudget.model_validate(worker_budget)
    validate_worker_budget_for_host(
        worker_host=worker_host,
        worker_budget=parsed,
    )
    return parsed.model_dump(mode="json")


def resolved_inner_agent_id(strategy: StrategySpec, host: AgentHostKind) -> str:
    if strategy.inner_agent is not None:
        return strategy.inner_agent
    if host == "thinkthread":
        return INNER_AGENT_COMMON
    return INNER_AGENT_AUTORESEARCH


def selected_model_provenance(model: SelectedModel) -> dict[str, Any]:
    launch = {
        "model": model.model,
        "reasoning_effort": model.reasoning_effort,
        "service_tier": model.service_tier,
    }
    return {
        "selected_model": model.model,
        "provider": model.provider,
        "exact_model_ref": model.model,
        "adapter_version": model.adapter_version,
        "context_policy": model.context_policy,
        "worker_launch": {
            key: value for key, value in launch.items() if value is not None
        },
    }


def selected_model_for_slot(
    plan: SearchPlan, slot: int
) -> SelectedModel | None:
    if not plan.selected_models:
        return None
    return plan.selected_models[slot - 1]


def global_evidence_mode(config: dict[str, Any]) -> str:
    if "global_evidence_mode" in config:
        return str(config["global_evidence_mode"])
    if config.get("share_global_evidence") is False:
        return "independent"
    if config.get("inject_global_evidence_after_verifier") is True:
        return "auto"
    return "manual"


def process_environment_for_index(
    device_pools: list[DevicePoolSpec],
    device_inventory: dict[str, DevicePoolInventory],
    candidate_index: int,
) -> dict[str, str]:
    environment: dict[str, str] = {}
    for pool in device_pools:
        configured = device_inventory.get(pool.pool_id)
        if configured is None:
            raise RuntimeError(
                f"frozen device inventory is missing {pool.pool_id!r}"
            )
        if not 0 <= candidate_index < len(configured.devices):
            raise RuntimeError(
                f"candidate index {candidate_index} exceeds frozen device pool "
                f"{pool.pool_id!r} capacity"
            )
        environment[configured.visibility_env] = configured.devices[candidate_index]
    return environment


def candidate_process_environment(
    frozen: FrozenSpec,
    candidate_index: int,
) -> dict[str, str]:
    return process_environment_for_index(
        frozen.spec.device_pools,
        frozen.device_inventory,
        candidate_index,
    )


def parse_worker_model_counts(value: str) -> list[tuple[str, int | None]]:
    parsed: list[tuple[str, int | None]] = []
    count_modes: set[bool] = set()
    for item in value.split(","):
        model = item.strip()
        count: int | None = None
        count_match = re.fullmatch(r"(.+)\*(\d+)", model)
        if count_match is not None:
            model = count_match.group(1).strip()
            count = int(count_match.group(2))
            if count <= 0:
                raise ValueError("workers= model counts must be positive")
        if not model:
            raise ValueError("workers= contains an empty model reference")
        count_modes.add(count is not None)
        parsed.append((model, count))
    if len(count_modes) > 1:
        raise ValueError(
            "workers= must specify counts for every model or for no models"
        )
    return parsed
