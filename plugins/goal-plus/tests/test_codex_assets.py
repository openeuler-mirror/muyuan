from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATION_SKILLS = ("goal-plus", "goal-plus-with-final-check", "search")


def _skill_frontmatter(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines and lines[0] == "---"
    closing = lines.index("---", 1)
    return {
        key.strip(): value.strip()
        for line in lines[1:closing]
        for key, separator, value in [line.partition(":")]
        if separator
    }


pytestmark = pytest.mark.codex


def test_installable_plugin_skills_match_project_local_codex_skills() -> None:
    for name in ORCHESTRATION_SKILLS:
        project = ROOT / ".codex" / "skills" / name / "SKILL.md"
        packaged = ROOT / "skills" / name / "SKILL.md"
        assert packaged.read_bytes() == project.read_bytes()

    project_reference = (
        ROOT / ".codex" / "skills" / "goal-plus" / "references" / "adaptive-search.md"
    )
    packaged_reference = (
        ROOT / "skills" / "goal-plus" / "references" / "adaptive-search.md"
    )
    assert packaged_reference.read_bytes() == project_reference.read_bytes()


@pytest.mark.parametrize("name", ORCHESTRATION_SKILLS)
def test_codex_orchestration_skills_are_explicit_and_main_only(name: str) -> None:
    skill_dir = ROOT / ".codex" / "skills" / name
    metadata = _skill_frontmatter(skill_dir / "SKILL.md")
    interface = (skill_dir / "agents" / "openai.yaml").read_text(encoding="utf-8")

    assert metadata["name"] == name
    assert "Goal Plus Main Agent" in metadata["description"]
    assert "Search candidate worker" in metadata["description"]
    assert "allow_implicit_invocation: false" in interface


def test_codex_skills_use_current_goal_and_search_surfaces() -> None:
    goal = (ROOT / ".codex" / "skills" / "goal-plus" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    search = (ROOT / ".codex" / "skills" / "search" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    for operation in {
        "goal_plus_status",
        "goal_plus_record_triage",
        "goal_plus_record_work_event",
        "goal_plus_save_spec_draft",
        "goal_plus_record_search_result",
        "goal_plus_set_status",
    }:
        assert operation in goal
    assert "goal_plus_upsert_work_items" not in goal
    assert "work DAG" in goal
    assert "spawn_agent" in goal
    assert 'fork_turns="none"' in goal

    for operation in (
        "search_freeze_spec",
        "search_create",
        "goal_plus_link_search_run",
        "search_plan_next",
        "search_start_batch",
        "search_start_agent_session",
        "search_bind_agent_handle",
        "search_continue_agent_session",
        "search_select",
        "search_promote",
        "search_apply_promotion",
    ):
        assert operation in search
    assert "search_redispatch_candidate" in search
    assert "search_get_agent_observability" in search
    assert "search_invalidate_run" in search


def test_codex_worker_prompt_defers_optional_contracts_to_runtime() -> None:
    path = ROOT / ".codex" / "agents" / "search_candidate_agent.toml"
    config = tomllib.loads(path.read_text(encoding="utf-8"))
    prompt = config["developer_instructions"]

    assert config["name"] == "search_candidate_agent"
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


def test_codex_adaptive_reference_contains_only_main_host_actions() -> None:
    goal = (ROOT / ".codex" / "skills" / "goal-plus" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    search = (ROOT / ".codex" / "skills" / "search" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    reference = (
        ROOT / ".codex" / "skills" / "goal-plus" / "references" / "adaptive-search.md"
    ).read_text(encoding="utf-8")

    assert "adaptive-search.md" not in goal
    assert "adaptive-search.md" in search
    assert "search_list_allocation_decisions" in reference
    assert "search_apply_allocation_decision" in reference
    assert "search_bind_agent_handle" in reference
    assert "worker prompt 不承载" in reference
    for internal in ("SchedulerManager", "AllocationExecutor", "build_context"):
        assert internal not in reference


def test_codex_final_check_uses_independent_checker_launch() -> None:
    checker = (ROOT / ".codex" / "agents" / "goal_plus_final_checker.toml").read_text(
        encoding="utf-8"
    )
    alias = (
        ROOT / ".codex" / "skills" / "goal-plus-with-final-check" / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert 'name = "goal_plus_final_checker"' in checker
    assert "goal_plus_status" in checker
    assert "goal_plus_submit_final_check" in checker
    assert "goal_plus_set_status" in checker
    assert "goal_plus_prepare_final_check" in alias
    assert "spawn_agent" in alias
    assert 'fork_turns="none"' in alias


def test_codex_mcp_and_hook_assets_register_runtime_entrypoints() -> None:
    config = (ROOT / ".codex" / "config.example.toml").read_text(encoding="utf-8")
    hooks = json.loads((ROOT / ".codex" / "hooks.example.json").read_text())
    packaged_hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())

    assert "[mcp_servers.goal-plus]" in config
    assert 'command = "goal-plus"' in config
    assert 'args = ["--root", ".gp"]' in config
    assert hooks == packaged_hooks
    assert set(hooks["hooks"]) == {
        "UserPromptSubmit",
        "SessionStart",
        "PreToolUse",
        "PostToolUse",
        "Stop",
        "SubagentStop",
    }
    for handlers in hooks["hooks"].values():
        handler = handlers[0]["hooks"][0]
        assert handler["type"] == "command"
        assert handler["command"] == "goal-plus --goal-plus-host-hook"
        assert handler["timeout"] == 30

def test_codex_plugin_default_prompts_use_goal_plus_skill() -> None:
    manifest = json.loads(
        (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )

    prompts = manifest["interface"]["defaultPrompt"]
    assert prompts
    assert all(prompt.startswith("$goal-plus") for prompt in prompts)


def test_codex_and_pi_worker_launch_contracts_are_equivalent() -> None:
    codex = tomllib.loads(
        (ROOT / ".codex" / "agents" / "search_candidate_agent.toml").read_text(
            encoding="utf-8"
        )
    )["developer_instructions"].strip()
    pi = (ROOT / ".pi" / "prompts" / "search-candidate-worker.md").read_text(
        encoding="utf-8"
    ).strip()

    assert codex == pi
