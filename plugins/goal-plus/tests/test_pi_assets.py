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
    assert package["name"] == "goal-plus"
    assert "pi-package" in package["keywords"]
    assert package["pi"] == {"extensions": ["assets/pi/extensions/goal-plus.ts"]}
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
    assert '!["annotator", "verifier"].includes(latestDispatchMetadata(ctx).thinkthreadRole ?? "")' in text
    archive = (ROOT / "assets/pi/extensions/lib/goal-plus-session-archive.ts").read_text(encoding="utf-8")
    assert '"session_transcript"' in archive


def test_pi_thinkthread_worker_activates_context_from_logical_root() -> None:
    text = (ROOT / "assets/pi/extensions/goal-plus.ts").read_text(encoding="utf-8")
    activation = text.split("function activateWorkerContext(", 1)[1].split(
        "function commandContextFrom(", 1
    )[0]

    assert "const workspaceAccess = details.workspace_access;" in activation
    assert "isThinkThreadWorker && isRecord(workspaceAccess)" in activation
    assert "workspaceAccess.logical_root" in activation


def test_pi_worker_registers_shared_cache_tools_only_with_the_spec() -> None:
    text = (ROOT / "assets/pi/extensions/goal-plus.ts").read_text(encoding="utf-8")
    activation = text.split("function activateWorkerContext(", 1)[1].split(
        "function commandContextFrom(", 1
    )[0]

    # Spec signal: the host context carries the shared cache snapshot only when
    # the frozen spec enables it; a persisted resume entry carries the flag.
    assert "isRecord(details.shared_cache)" in activation
    assert "details.shared_cache_enabled === true" in activation
    assert "shared_cache_enabled: true" in activation
    # Registration happens through the shared constant, guarded by the flag.
    assert "workerSharedCacheEnabled = true" in activation
    assert "for (const tool of SHARED_CACHE_TOOL_NAMES)" in activation
    assert "registerRuntimeTool(pi, tool, registered)" in activation
    # ThinkThread transport must serialize the same two tools.
    serial = text.split("async function runThinkThreadWorkerToolSerial(", 1)[1].split(
        "async function", 1
    )[0]
    assert '"goal_plus_search_read_shared_cache"' in serial
    assert '"goal_plus_search_append_shared_cache"' in serial
    # Per-turn ThinkThread activation keeps them hidden until the context says so.
    scoping = text.split("function scopeThinkThreadWorkerTools(", 1)[1].split(
        "async function", 1
    )[0]
    assert "SHARED_CACHE_TOOL_NAMES" in scoping


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


def test_pi_model_admission_uses_catalog_without_synthetic_request() -> None:
    text = (ROOT / "assets/pi/extensions/goal-plus.ts").read_text(encoding="utf-8")
    assert "probePiModels" not in text
    assert 'content: "hi"' not in text
    assert "modelRegistry.complete(" not in text
    assert "GOAL_PLUS_PI_MODEL" not in text
    assert (
        "if (ctx.model && piModelMatchesReference(ctx.model, needle)) return ctx.model;"
        in text
    )
    assert "requested Pi model is not available" in text
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
    assert "verifier" in strategy and "search_scheduler" not in strategy
    verifier = next(
        item for item in strategy["verifier"]["anyOf"] if item.get("type") == "object"
    )
    assert set(verifier["properties"]["scores"]["items"]["enum"]) == {
        "progress_reward", "allocation_utility", "correctness_gap", "validation_gap",
        "local_confounder_evidence", "discriminating_test_gap", "boundary_evidence_strength"}
    policy_set = next(
        item for item in strategy["allocation"]["anyOf"] if "oneOf" in item
    )
    assert policy_set["required"] == ["policy"]
    policies = {
        item["properties"]["policy"]["const"]: item
        for item in policy_set["oneOf"]
    }
    assert set(policies) == {"swe_search_uct/v1"}
    swe = policies["swe_search_uct/v1"]["properties"]["parameters"]["properties"]
    assert swe["low_reward_threshold"]["minimum"] == -1
    assert swe["low_reward_threshold"]["maximum"] == 1
    assert swe["max_children_per_node"]["minimum"] == 1


def test_pyproject_exposes_pi_console_scripts() -> None:
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["scripts"]

    assert "goal-plus-pi-worker" not in scripts
    assert "goal-plus-pi-pool" not in scripts
    assert scripts["goal-plus-pi-tool"] == "goal_plus.pi_tool:main"
