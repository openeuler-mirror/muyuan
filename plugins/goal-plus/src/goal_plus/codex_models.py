"""Codex native model facts and role projection."""

import os
from typing import Any
from goal_plus import codex_hook_input as hook_input_codec

def parse_goal_plus_role_models(raw_goal: str) -> dict[str, str] | None:
    from goal_plus.domain.goal_commands import parse_goal_plus_command_config

    _objective, config = parse_goal_plus_command_config(raw_goal)
    if config.annotator is None and config.workers is None:
        return None
    result: dict[str, str] = {}
    if config.annotator is not None:
        result["annotator"] = config.annotator
    if config.workers is not None:
        result["workers"] = config.workers
        result["worker_directive"] = config.worker_directive or "workers"
    return result


def codex_native_model_config(hook_input: dict[str, Any]) -> dict[str, str]:
    turn = hook_input_codec.codex_turn_event(hook_input)
    turn_payload = turn.get("payload") if isinstance(turn, dict) else None
    if not isinstance(turn_payload, dict):
        turn_payload = {}

    def first_text(*values: Any) -> str | None:
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    model = first_text(turn_payload.get("model"), hook_input.get("model"))
    reasoning_effort = first_text(
        turn_payload.get("effort"),
        turn_payload.get("reasoning_effort"),
        hook_input.get("effort"),
        hook_input.get("reasoning_effort"),
    )
    return {
        key: value
        for key, value in {
            "model": model,
            "reasoning_effort": reasoning_effort,
        }.items()
        if value is not None
    }


def export_codex_native_model_config(hook_input: dict[str, Any]) -> None:
    from goal_plus.internal_agents import (
        CODEX_MODEL_ENV,
        CODEX_REASONING_EFFORT_ENV,
    )

    native = codex_native_model_config(hook_input)
    for name, key in (
        (CODEX_MODEL_ENV, "model"),
        (CODEX_REASONING_EFFORT_ENV, "reasoning_effort"),
    ):
        value = native.get(key)
        if value is not None:
            os.environ[name] = value


def role_model_context(
    role_models: dict[str, str] | None,
) -> str:
    if role_models is None:
        return ""
    lines = ["已解析 Codex Goal Plus 角色模型："]
    if "annotator" in role_models:
        lines.append(
            f"- annotator={role_models['annotator']}，解析后冻结到 "
            "strategy.evidence_annotator.model"
        )
    if "workers" in role_models:
        directive = role_models.get("worker_directive", "workers")
        lines.append(
            f"- {directive}={role_models['workers']}，解析后冻结到 strategy.models"
        )
    lines.append(
        "省略的角色继承当前 Codex Main 的 native model；不要从其他显式角色推导默认值。"
    )
    return "\n".join(lines)
