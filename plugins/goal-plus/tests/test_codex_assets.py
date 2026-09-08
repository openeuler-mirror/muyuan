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


def test_codex_mcp_and_hook_assets_register_runtime_entrypoints() -> None:
    config = (ROOT / ".codex" / "config.example.toml").read_text(encoding="utf-8")
    hooks = json.loads((ROOT / ".codex" / "hooks.example.json").read_text())
    packaged_hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())

    assert "[mcp_servers.goal-plus]" in config
    assert 'command = "bash"' in config
    assert 'scripts/run-runtime.sh", "--root", ".gp"]' in config
    assert hooks == packaged_hooks
    assert set(hooks["hooks"]) == {
        "UserPromptSubmit",
        "SessionStart",
        "Interrupt",
        "PreToolUse",
        "PostToolUse",
        "Stop",
        "SubagentStop",
    }
    for event, handlers in hooks["hooks"].items():
        handler = handlers[0]["hooks"][0]
        assert handler["type"] == "command"
        assert handler["command"] == 'bash "${PLUGIN_ROOT}/scripts/run-runtime.sh" --goal-plus-host-hook'
        assert handler["timeout"] == (3 if event == "Interrupt" else 30)


def test_codex_main_and_candidate_mcp_inherit_debug_switch() -> None:
    plugin = json.loads((ROOT / ".mcp.json").read_text())
    assert "GOAL_PLUS_DEBUG" in plugin["mcpServers"]["goal-plus"]["env_vars"]
    for path in (".codex/config.example.toml", ".codex/agents/search_candidate_common_agent.toml"):
        config = tomllib.loads((ROOT / path).read_text())
        assert "GOAL_PLUS_DEBUG" in config["mcp_servers"]["goal-plus"]["env_vars"]

def test_codex_plugin_default_prompts_use_goal_plus_skill() -> None:
    manifest = json.loads(
        (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )

    prompts = manifest["interface"]["defaultPrompt"]
    assert prompts
    assert all(prompt.startswith("$goal-plus") for prompt in prompts)


def test_codex_and_pi_worker_launch_contracts_share_the_portable_core() -> None:
    codex_prompt = tomllib.loads(
        (ROOT / ".codex" / "agents" / "search_candidate_agent.toml").read_text(
            encoding="utf-8"
        )
    )["developer_instructions"].strip()
    pi_prompt = (ROOT / ".pi" / "prompts" / "search-candidate-worker.md").read_text(
        encoding="utf-8"
    ).strip()
    codex_normalized = " ".join(codex_prompt.split())
    pi_normalized = " ".join(pi_prompt.split())

    for instruction in (
        "你是 Search 候选 worker，不是搜索编排器。",
        "search_get_agent_context(agent_session_id)",
        "以返回的 `candidate_task` 为当前工作契约",
        "不要创建或管理其他 candidate",
        "沿用已加载的运行时上下文和 Evidence",
    ):
        assert instruction in codex_normalized
        assert instruction in pi_normalized
    assert "ThinkThread" not in codex_normalized
    assert "ThinkThread context 不提供 Root 本地路径" in pi_normalized
    assert "访问 Root 或 sibling Child" in pi_normalized
