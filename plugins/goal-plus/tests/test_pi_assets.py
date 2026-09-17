from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.pi


def test_pi_package_exports_the_supported_user_assets() -> None:
    expected = {
        "assets/pi/extensions/goal-plus.ts",
    }
    assert all((ROOT / path).is_file() for path in expected)

    package = json.loads((ROOT / "assets/pi/package.json").read_text(encoding="utf-8"))
    assert package["pi"]["extensions"] == ["assets/pi/extensions/goal-plus.ts"]
    assert "skills" not in package["pi"]
    assert not (ROOT / "assets/pi/prompts/goal-plus.md").exists()
    direct_extension_files = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "assets" / "pi" / "extensions").iterdir()
        if path.suffix in {".js", ".ts"}
    )
    assert direct_extension_files == package["pi"]["extensions"]
    assert (ROOT / "assets/pi/extensions/lib/goal-plus-progress.ts").is_file()
    skill_files = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "assets" / "pi" / "skills").glob("*/SKILL.md")
    )
    assert skill_files == []


def test_pi_search_recovery_tools_are_registered_only_for_main() -> None:
    text = (ROOT / "assets/pi/extensions/goal-plus.ts").read_text(encoding="utf-8")
    main = text.split("const mainTools = [", 1)[1].split("];", 1)[0]
    worker = text.split("const workerTools = [", 1)[1].split("];", 1)[0]
    for name in ("goal_plus_search_recovery_status", "goal_plus_search_prepare_recovery", "goal_plus_search_recover_candidate"):
        assert f'"{name}"' in main
        assert f'"{name}"' not in worker


def test_pi_model_context_injections_are_ledger_registered() -> None:
    """Every known model-context injection site pairs with a ledger registration."""
    text = (ROOT / "assets/pi/extensions/goal-plus.ts").read_text(encoding="utf-8")
    # Context-participating custom messages plus the before_agent_start context.
    injectors = {
        "goal-plus-must-continue": "must_continue",
        "goal-plus-publication-notification-delivery": "publication_notification",
        "goal-plus-command-context": "goal_start",
        "goal-plus-missing-attempt": "missing_attempt_nudge",
        "goal-plus-native-context": "goal_context_full",
    }
    registrations = text.count("appendInjectionObservation(")
    assert registrations >= len(injectors) + 3  # full/brief/skipped, steer, transforms
    for marker, kind in injectors.items():
        assert f'"{marker}"' in text, kind
    for literal in ("goal_context_full", "goal_context_brief", "goal_context_skipped",
                    "must_continue", "worker_steer", "input_transform"):
        assert f'"{literal}"' in text
    # The stop nudge is code-owned, not prompt-owned, and covers every worker
    # provider, not only ThinkThread.
    assert 'latestDispatchMetadata(ctx).thinkthreadRole !== "annotator"' in text
    archive = (ROOT / "assets/pi/extensions/lib/goal-plus-session-archive.ts").read_text(encoding="utf-8")
    assert '"goal-plus-injection-observation"' in archive


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

    assert '"goal_plus_thinkthread_annotation_next"' in tool_names
    assert '"goal_plus_thinkthread_artifact_view_list"' in tool_names
    assert '"goal_plus_thinkthread_artifact_view_stat"' in tool_names
    assert '"goal_plus_thinkthread_artifact_view_pread"' in tool_names
    assert '"goal_plus_thinkthread_artifact_view_diff"' in tool_names
    assert '"goal_plus_thinkthread_annotation_submit"' in tool_names
    assert "artifact_view_run" not in text


def test_pi_verifier_kicks_internal_agents_through_agent_harness_path() -> None:
    text = (ROOT / "assets/pi/extensions/goal-plus.ts").read_text(encoding="utf-8")

    assert 'name === "goal_plus_search_run_verifier"' in text
    assert '"goal_plus_host_kick_internal_agents"' in text
    assert "{ run_id: runId }" in text


def test_pi_allocation_uses_shared_host_cancellation() -> None:
    text = (ROOT / "assets/pi/extensions/goal-plus.ts").read_text(encoding="utf-8")
    assert "host_execution_scope_interrupt_allocation" not in text
    assert "workerAllocationDecisionId" not in text


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
    assert 'name === "goal_plus_search_freeze_spec"' in runtime_tool
    assert "await probePiSearchSpecModels(ctx, forwardedParams)" in runtime_tool
    assert 'model_pattern: `${ctx.model.provider}/${ctx.model.id}`' in text
    assert "native_agent_config: nativeAgentConfig" in text


def test_pi_search_bootstrap_requires_goal_plus_id(tmp_path) -> None:
    from goal_plus.mcp_client import mcp_manifest

    schemas = {tool["name"]: tool["inputSchema"] for tool in mcp_manifest(tmp_path / ".gp")["tools"]}
    assert "goal_plus_id" in schemas["goal_plus_search_freeze_spec"]["required"]
    assert "goal_plus_id" in schemas["goal_plus_search_create"]["required"]


def test_pi_loads_shared_allocation_schema(tmp_path) -> None:
    from goal_plus.mcp_client import mcp_manifest

    manifest = mcp_manifest(tmp_path / ".gp")
    freeze = next(tool for tool in manifest["tools"] if tool["name"] == "goal_plus_search_freeze_spec")
    strategy = freeze["inputSchema"]["properties"]["spec"]["properties"]["strategy"]["properties"]
    assert "verifier" not in strategy and "search_scheduler" not in strategy
    policy = strategy["allocation"]
    assert set(policy["properties"]) == {"features", "switch_margin"}
    assert policy["properties"]["switch_margin"]["minimum"] == 0
    assert policy["properties"]["switch_margin"]["maximum"] == 1


def test_pi_package_manifest_exports_only_the_user_integration() -> None:
    package = json.loads((ROOT / "assets/pi/package.json").read_text(encoding="utf-8"))

    assert package["name"] == "goal-plus"
    assert "pi-package" in package["keywords"]
    assert package["pi"] == {
        "extensions": ["assets/pi/extensions/goal-plus.ts"],
    }


def test_pyproject_exposes_pi_console_scripts() -> None:
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["scripts"]

    assert "goal-plus-pi-worker" not in scripts
    assert "goal-plus-pi-pool" not in scripts
    assert scripts["goal-plus-pi-tool"] == "goal_plus.pi_tool:main"
