from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.pi


def test_pi_package_exports_the_supported_user_assets() -> None:
    expected = {
        "assets/pi/prompts/goal-plus.md",
        "assets/pi/skills/goal-plus/SKILL.md",
        "assets/pi/extensions/goal-plus.ts",
    }
    assert all((ROOT / path).is_file() for path in expected)

    package = json.loads((ROOT / "assets/pi/package.json").read_text(encoding="utf-8"))
    assert package["pi"]["extensions"] == ["assets/pi/extensions/goal-plus.ts"]
    assert package["pi"]["skills"] == ["assets/pi/skills/goal-plus/SKILL.md"]
    direct_extension_files = sorted(
        str(path.relative_to(ROOT))
        for path in (ROOT / "assets" / "pi" / "extensions").iterdir()
        if path.suffix in {".js", ".ts"}
    )
    assert direct_extension_files == package["pi"]["extensions"]
    assert (ROOT / "assets/pi/extensions/lib/goal-plus-progress.ts").is_file()
    skill_files = sorted(
        str(path.relative_to(ROOT))
        for path in (ROOT / "assets" / "pi" / "skills").glob("*/SKILL.md")
    )
    assert skill_files == ["assets/pi/skills/goal-plus/SKILL.md"]


def test_pi_search_recovery_tools_are_registered_only_for_main() -> None:
    text = (ROOT / "assets/pi/extensions/goal-plus.ts").read_text(encoding="utf-8")
    main = text.split("const mainTools = [", 1)[1].split("];", 1)[0]
    worker = text.split("const workerTools = [", 1)[1].split("];", 1)[0]
    for name in ("search_recovery_status", "search_prepare_recovery", "search_recover_candidate"):
        assert f'"{name}"' in main
        assert f'"{name}"' not in worker
        schemas = (ROOT / "assets/pi/extensions/lib/goal-plus-schemas.ts").read_text(encoding="utf-8")
        assert f"{name}: Type.Object" in schemas


def test_pi_thinkthread_worker_activates_context_from_logical_root() -> None:
    text = (ROOT / "assets/pi/extensions/goal-plus.ts").read_text(encoding="utf-8")
    activation = text.split("function activateWorkerContext(", 1)[1].split(
        "function commandContextFrom(", 1
    )[0]

    assert "const workspaceAccess = details.workspace_access;" in activation
    assert "isThinkThreadWorker && isRecord(workspaceAccess)" in activation
    assert "workspaceAccess.logical_root" in activation


def test_pi_thinkthread_worker_delegates_generation_fencing_to_root() -> None:
    text = (ROOT / "assets/pi/extensions/goal-plus.ts").read_text(encoding="utf-8")
    guard = text.split("function workspaceGuard(", 1)[1].split(
        "async function mainGate(", 1
    )[0]

    assert (
        'if (role === "worker" && workerGeneration && !isThinkThreadWorker)'
        in guard
    )
    assert 'record.execution_generation !== workerGeneration.generation' in guard


def test_pi_thinkthread_annotation_activates_only_read_tools() -> None:
    text = (ROOT / "assets/pi/extensions/goal-plus.ts").read_text(encoding="utf-8")
    tool_names = text.split("const THINKTHREAD_ANNOTATION_TOOL_NAMES = [", 1)[1].split(
        "] as const;", 1
    )[0]

    assert '"thinkthread_annotation_next"' in tool_names
    assert '"thinkthread_artifact_view_list"' in tool_names
    assert '"thinkthread_artifact_view_stat"' in tool_names
    assert '"thinkthread_artifact_view_pread"' in tool_names
    assert '"thinkthread_artifact_view_diff"' in tool_names
    assert '"thinkthread_annotation_submit"' in tool_names
    assert "artifact_view_run" not in text


def test_pi_verifier_kicks_internal_agents_through_native_host_path() -> None:
    text = (ROOT / "assets/pi/extensions/goal-plus.ts").read_text(encoding="utf-8")

    assert 'name === "search_run_verifier"' in text
    assert '"goal_plus_host_kick_internal_agents"' in text
    assert "{ run_id: runId }" in text


def test_pi_model_preflight_uses_native_registry_concurrently() -> None:
    text = (ROOT / "assets/pi/extensions/goal-plus.ts").read_text(encoding="utf-8")
    preflight = text.split("async function probePiModels(", 1)[1].split(
        "async function probePiModelReferences(", 1
    )[0]
    runtime_tool = text.split("function registerRuntimeTool(", 1)[1].split(
        "function activateWorkerContext(", 1
    )[0]

    assert "await Promise.all([...unique.values()].map(async (model)" in preflight
    assert "ctx.modelRegistry.complete(" in preflight
    assert 'content: "hi"' in preflight
    assert "if (unique.size === 0) return;" in preflight
    assert "GOAL_PLUS_PI_MODEL" not in text
    assert (
        "if (ctx.model && piModelMatchesReference(ctx.model, needle)) return ctx.model;"
        in text
    )
    assert "await probePiModels(ctx, resolved?.models ?? [])" in text
    assert "const references: Array<string | undefined> = [];" in text
    assert 'name === "search_freeze_spec"' in runtime_tool
    assert "await probePiSearchSpecModels(ctx, forwardedParams)" in runtime_tool
    assert 'model_pattern: `${ctx.model.provider}/${ctx.model.id}`' in text
    assert "native_agent_config: nativeAgentConfig" in text
    assert "inheritPiNativeAgentConfig(launch, nativeAgentConfig)" in text


def test_pi_search_bootstrap_requires_goal_plus_id() -> None:
    text = (ROOT / "assets/pi/extensions/lib/goal-plus-schemas.ts").read_text(encoding="utf-8")

    freeze_schema = text.split("\tsearch_freeze_spec: Type.Object(", 1)[1].split(
        "\tsearch_create: Type.Object(", 1
    )[0]
    create_schema = text.split("\tsearch_create: Type.Object(", 1)[1].split(
        "\tsearch_status: Type.Object(", 1
    )[0]

    assert "goal_plus_id: Type.String()" in freeze_schema
    assert "goal_plus_id: Type.String()" in create_schema


def test_pi_package_manifest_exports_only_the_user_integration() -> None:
    package = json.loads((ROOT / "assets/pi/package.json").read_text(encoding="utf-8"))

    assert package["name"] == "goal-plus"
    assert "pi-package" in package["keywords"]
    assert package["pi"] == {
        "extensions": ["assets/pi/extensions/goal-plus.ts"],
        "skills": ["assets/pi/skills/goal-plus/SKILL.md"],
    }


def test_pyproject_exposes_pi_console_scripts() -> None:
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["scripts"]

    assert scripts["goal-plus-pi-worker"] == "goal_plus.pi_worker:main"
    assert scripts["goal-plus-pi-tool"] == "goal_plus.pi_tool:main"


def test_pi_compatibility_prompt_delegates_to_the_skill() -> None:
    prompt = (ROOT / "assets" / "pi" / "prompts" / "goal-plus.md").read_text(
        encoding="utf-8"
    )

    assert "/skill:goal-plus" in prompt
    assert "$ARGUMENTS" in prompt
