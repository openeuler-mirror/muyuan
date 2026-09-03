from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.pi


def test_pi_package_exports_the_supported_user_assets() -> None:
    expected = {
        ".pi/prompts/goal-plus.md",
        ".pi/skills/goal-plus/SKILL.md",
        ".pi/extensions/goal-plus.ts",
    }
    assert all((ROOT / path).is_file() for path in expected)

    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    assert package["pi"]["extensions"] == [".pi/extensions/goal-plus.ts"]
    assert package["pi"]["skills"] == [".pi/skills/goal-plus/SKILL.md"]
    skill_files = sorted(
        str(path.relative_to(ROOT))
        for path in (ROOT / ".pi" / "skills").glob("*/SKILL.md")
    )
    assert skill_files == [".pi/skills/goal-plus/SKILL.md"]


def test_pyproject_exposes_pi_console_scripts() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'goal-plus-pi-worker = "goal_plus.pi_worker:main"' in text
    assert 'goal-plus-pi-tool = "goal_plus.pi_tool:main"' in text


def test_pi_compatibility_prompt_requires_host_authorization() -> None:
    prompt = (ROOT / ".pi" / "prompts" / "goal-plus.md").read_text(
        encoding="utf-8"
    )

    assert "/skill:goal-plus" in prompt
    assert "$ARGUMENTS" in prompt
    assert "active `goal_plus_id`" in prompt
    assert "goal_plus_create" not in prompt
    assert "goal_plus_update_goal" not in prompt


def test_pi_skill_uses_current_goal_search_and_pool_surfaces() -> None:
    skill = (ROOT / ".pi" / "skills" / "goal-plus" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    for operation in (
        "goal_plus_status",
        "goal_plus_record_triage",
        "goal_plus_record_work_event",
        "goal_plus_save_spec_draft",
        "goal_plus_record_search_result",
        "goal_plus_set_status",
        "pi_goal_plus_run_work_item",
        "search_freeze_spec",
        "search_create",
        "goal_plus_link_search_run",
        "search_plan_next",
        "search_start_batch",
        "pi_search_pool_open",
        "pi_search_pool_wait_any",
        "pi_search_pool_snapshot",
        "pi_search_pool_continue",
        "pi_search_pool_close",
        "search_select",
        "search_promote",
        "search_apply_promotion",
    ):
        assert operation in skill
    assert "goal_plus_upsert_work_items" not in skill
    assert "work DAG" in skill


def test_pi_worker_prompt_defers_optional_contracts_to_runtime() -> None:
    prompt = (ROOT / ".pi" / "prompts" / "search-candidate-worker.md").read_text(
        encoding="utf-8"
    )

    assert "search_get_agent_context" in prompt
    assert "candidate_task" in prompt
    assert "continuation" in prompt
    for optional_contract in (
        "allocation_decision",
        "scheduler_status",
        "search_get_global_evidence",
        "search_get_evidence_detail",
        "search_stage_shared_tool",
    ):
        assert optional_contract not in prompt


def test_pi_adaptive_reference_contains_only_main_pool_actions() -> None:
    skill = (ROOT / ".pi" / "skills" / "goal-plus" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    reference = (
        ROOT / ".pi" / "skills" / "goal-plus" / "references" / "adaptive-search.md"
    ).read_text(encoding="utf-8")

    assert "references/adaptive-search.md" in skill
    assert "search_list_allocation_decisions" in reference
    assert "search_apply_allocation_decision" in reference
    assert "pi_search_pool_open" in reference
    assert "Pi supervisor 不承载" in reference
    for internal in ("SchedulerManager", "AllocationExecutor", "build_context"):
        assert internal not in reference


def test_pi_extension_exposes_role_scoped_adaptive_tools() -> None:
    extension = (ROOT / ".pi" / "extensions" / "goal-plus.ts").read_text(
        encoding="utf-8"
    )

    assert "const mainTools" in extension
    assert "const workerTools" in extension
    main_tools = extension.split("const mainTools = [", 1)[1].split("];", 1)[0]
    worker_tools = extension.split("const workerTools = [", 1)[1].split("];", 1)[0]
    assert '"search_list_allocation_decisions"' in main_tools
    assert '"search_apply_allocation_decision"' in main_tools
    assert '"search_list_allocation_decisions"' not in worker_tools
    assert '"search_apply_allocation_decision"' not in worker_tools
    assert worker_tools.strip() == '"search_get_agent_context"'
    dynamic_tools = extension.split("for (const tool of [", 1)[1].split("])", 1)[0]
    assert '"search_run_verifier"' in dynamic_tools
    assert 'if (candidateTask.share_out_dir)' in extension
    assert "details.supplemental_evaluation_enabled === true" in extension
    assert "pi.appendEntry(WORKER_CONTEXT_ENTRY_TYPE" in extension
    assert (
        "activateWorkerContext(pi, entry?.data, registeredRuntimeTools, false)"
        in extension
    )


def test_pi_extension_keeps_typed_search_and_host_contracts() -> None:
    extension = (ROOT / ".pi" / "extensions" / "goal-plus.ts").read_text(
        encoding="utf-8"
    )

    for contract in (
        "const SearchSpecSchema = Type.Object",
        'Type.Literal("parallel_loops")',
        'Type.Literal("adaptive_search")',
        "const SearchSchedulerSpec = Type.Object",
        "search_invalidate_run: Type.Object",
        "search_get_agent_context: Type.Object",
        "search_run_verifier: Type.Object",
        "pi_search_pool_open: Type.Object",
        "pi_search_pool_continue: Type.Object",
        "goal_plus_prepare_final_check: Type.Object",
        "pi_goal_plus_run_final_check: Type.Object",
        "isEnvironmentFailure",
    ):
        assert contract in extension
    assert "parameters: JsonArgs" not in extension
    assert "goal_plus_upsert_work_items" not in extension


def test_pi_extension_uses_runtime_gates_and_persistent_monitoring() -> None:
    extension = (ROOT / ".pi" / "extensions" / "goal-plus.ts").read_text(
        encoding="utf-8"
    )

    assert "goal_plus_gate" in extension
    assert "goal_plus_monitor_snapshot" in extension
    assert "search_get_agent_observability" in extension
    assert "goal-plus-runtime-start" in extension
    assert "search_tasks_total" in extension
