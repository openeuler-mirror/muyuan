"""Codex native model facts, explicit override preflight and role projection."""

import json
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from goal_plus import codex_hook_input as hook_input_codec

CODEX_MODEL_PROBE_TIMEOUT_SECONDS = 25


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


def codex_model_target(
    model: Any,
    *,
    reasoning_effort: Any = None,
) -> dict[str, str] | None:
    if not isinstance(model, str) or not model.strip():
        return None
    return {
        key: value.strip()
        for key, value in {
            "model": model,
            "reasoning_effort": (
                reasoning_effort if isinstance(reasoning_effort, str) else None
            ),
        }.items()
        if isinstance(value, str) and value.strip()
    }


def deduplicate_codex_model_targets(
    targets: list[dict[str, str] | None],
) -> list[dict[str, str]]:
    unique: dict[tuple[str, str], dict[str, str]] = {}
    for target in targets:
        if target is None:
            continue
        key = ("", target["model"].casefold())
        unique.setdefault(key, target)
    return list(unique.values())


def command_codex_model_targets(
    hook_input: dict[str, Any],
    role_models: dict[str, str] | None,
) -> list[dict[str, str]]:
    native = codex_native_model_config(hook_input)
    targets: list[dict[str, str] | None] = []
    if role_models is not None:
        targets.append(
            codex_model_target(
                role_models.get("annotator"),
                reasoning_effort=native.get("reasoning_effort"),
            )
        )
        workers = role_models.get("workers")
        if workers is not None:
            from goal_plus.domain.worker_policy import parse_worker_model_counts

            targets.extend(
                codex_model_target(
                    model,
                    reasoning_effort=native.get("reasoning_effort"),
                )
                for model, _count in parse_worker_model_counts(workers)
            )
    return deduplicate_codex_model_targets(targets)


def search_spec_codex_model_targets(
    hook_input: dict[str, Any],
) -> list[dict[str, str]]:
    native = codex_native_model_config(hook_input)
    targets: list[dict[str, str] | None] = []
    spec = hook_input_codec.tool_input(hook_input).get("spec")
    strategy = spec.get("strategy") if isinstance(spec, dict) else None
    if not isinstance(strategy, dict):
        return deduplicate_codex_model_targets(targets)
    default_effort = native.get("reasoning_effort")

    models = strategy.get("models")
    if isinstance(models, list):
        for item in models:
            if isinstance(item, dict):
                targets.append(
                    codex_model_target(
                        item.get("model"),
                        reasoning_effort=item.get("reasoning_effort") or default_effort,
                    )
                )
    worker_launch = strategy.get("worker_launch")
    if isinstance(worker_launch, dict):
        targets.append(
            codex_model_target(
                worker_launch.get("model"),
                reasoning_effort=(
                    worker_launch.get("reasoning_effort") or default_effort
                ),
            )
        )
    annotator = strategy.get("evidence_annotator")
    if isinstance(annotator, dict):
        targets.append(
            codex_model_target(
                annotator.get("model"),
                reasoning_effort=(annotator.get("reasoning_effort") or default_effort),
            )
        )
    scheduler = strategy.get("search_scheduler")
    if isinstance(scheduler, dict):
        targets.append(
            codex_model_target(
                scheduler.get("model"),
                reasoning_effort=(scheduler.get("reasoning_effort") or default_effort),
            )
        )
    return deduplicate_codex_model_targets(targets)


def probe_codex_model(target: dict[str, str]) -> str | None:
    command = [
        "codex",
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--ignore-rules",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "--model",
        target["model"],
    ]
    reasoning_effort = target.get("reasoning_effort")
    if reasoning_effort:
        command.extend(
            (
                "--config",
                "model_reasoning_effort=" + json.dumps(reasoning_effort),
            )
        )
    command.append("-")
    environment = os.environ.copy()
    environment["GOAL_PLUS_HOST_HOOK_DISABLED"] = "1"
    try:
        with tempfile.TemporaryDirectory(prefix="goal-plus-model-probe-") as cwd:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=environment,
                input="Reply with exactly: hi",
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=CODEX_MODEL_PROBE_TIMEOUT_SECONDS,
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"{target['model']}: {type(exc).__name__}"
    if completed.returncode != 0:
        return f"{target['model']}: exit {completed.returncode}"
    return None


def probe_codex_models(targets: list[dict[str, str]]) -> None:
    if not targets:
        return
    with ThreadPoolExecutor(max_workers=len(targets)) as executor:
        failures = [
            failure
            for failure in executor.map(probe_codex_model, targets)
            if failure is not None
        ]
    if failures:
        raise RuntimeError(
            "Codex model preflight failed before agent launch: " + "; ".join(failures)
        )


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
