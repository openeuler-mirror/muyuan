from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


pytestmark = pytest.mark.codex


def test_source_assets_do_not_publish_host_discovery_entries() -> None:
    for relative in (".agents/plugins/marketplace.json", ".codex-plugin/plugin.json", ".mcp.json", "package.json"):
        assert not (ROOT / relative).exists()
    for relative in (".codex/skills", ".codex/agents", ".pi/extensions", ".pi/skills", ".pi/prompts", "skills", "hooks"):
        assert not any(path.is_file() for path in (ROOT / relative).rglob("*"))
    assert not list((ROOT / "assets/codex").rglob("SKILL.md"))
    manifest = json.loads((ROOT / "assets/codex/plugin.json").read_text())
    assert "skills" not in manifest


def test_codex_mcp_and_hook_assets_register_runtime_entrypoints() -> None:
    config = (ROOT / "assets" / "codex" / "config.example.toml").read_text(encoding="utf-8")
    hooks = json.loads((ROOT / "assets/codex/hooks.json").read_text())

    assert "[mcp_servers.goal-plus]" in config
    assert 'command = "bash"' in config
    assert 'scripts/run-runtime.sh", "--root", ".gp"]' in config
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
    plugin = json.loads((ROOT / "assets/codex/mcp.json").read_text())
    assert "GOAL_PLUS_DEBUG" in plugin["mcpServers"]["goal-plus"]["env_vars"]
    for path in ("assets/codex/config.example.toml", "assets/codex/agents/search_candidate_common_agent.toml"):
        config = tomllib.loads((ROOT / path).read_text())
        assert "GOAL_PLUS_DEBUG" in config["mcp_servers"]["goal-plus"]["env_vars"]

def test_codex_plugin_default_prompts_use_exact_host_command() -> None:
    manifest = json.loads(
        (ROOT / "assets/codex/plugin.json").read_text(encoding="utf-8")
    )

    prompts = manifest["interface"]["defaultPrompt"]
    assert prompts
    assert all(prompt.startswith("$goal-plus") for prompt in prompts)


def test_codex_and_pi_worker_launch_contracts_share_the_portable_core() -> None:
    codex_prompt = tomllib.loads(
        (ROOT / "assets" / "codex" / "agents" / "search_candidate_agent.toml").read_text(
            encoding="utf-8"
        )
    )["developer_instructions"].strip()
    pi_prompt = (ROOT / "assets" / "pi" / "prompts" / "search-candidate-worker.md").read_text(
        encoding="utf-8"
    ).strip()
    codex_normalized = " ".join(codex_prompt.split())
    pi_normalized = " ".join(pi_prompt.split())

    for instruction in (
        "你是 Search 候选 worker，不是搜索编排器。",
        "goal_plus_search_get_agent_context(agent_session_id)",
        "以返回的 `candidate_task` 为当前工作契约",
        "不要创建或管理其他 candidate",
        "沿用已加载的运行时上下文和 Evidence",
    ):
        assert instruction in codex_normalized
        assert instruction in pi_normalized
    assert "ThinkThread" not in codex_normalized
    assert "ThinkThread context 不提供 Root 本地路径" in pi_normalized
    assert "访问 Root 或 sibling Child" in pi_normalized
