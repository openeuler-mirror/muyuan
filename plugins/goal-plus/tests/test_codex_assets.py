from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_installable_plugin_skills_match_project_local_codex_skills() -> None:
    for name in ("goal-plus", "goal-plus-with-final-check", "search"):
        project_skill = ROOT / ".codex" / "skills" / name / "SKILL.md"
        plugin_skill = ROOT / "skills" / name / "SKILL.md"
        assert plugin_skill.read_bytes() == project_skill.read_bytes()
pytestmark = pytest.mark.codex


def test_codex_goal_plus_skill_records_modes_and_mcp_tools() -> None:
    text = (ROOT / ".codex" / "skills" / "goal-plus" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    for expected in (
        "name: goal-plus",
        "goal_plus_create",
        "goal_plus_record_triage",
        "goal_plus_save_spec_draft",
        "goal_plus_gate",
        "Goal Mode",
        "Spec Discovery Mode",
        "Search Mode",
        '"recommended_phase": "goal"',
        "goal_mode",
        "不要发送",
        "`mode`",
        "`reason`",
        "Search 是自主升级",
        "不要要求用户",
        "search_freeze_spec",
        "原始目标审计",
        "mode=autonomous",
        "mode=probe",
        ".goal-plus-verifiers/",
        "`expected_outputs`",
        '`workspace.backend="git_worktree"`',
        '`workspace.promotion_mode` 默认是 `apply`',
        "DirtySourceWorkspace",
        "workspace_capacity_notice",
        "fits_available_space=false",
        "$goal-plus-with-final-check",
        "$goal-plus edit",
        "$goal-plus mode=autonomous",
        "$goal-plus mode=probe",
        "GoalPlusRecord.command_config",
        "只有缺省字段才由 Main",
        "ready draft",
        "`raw_goal`",
        "规范末行",
        "候选 lease 结束绝不会完成",
        "不单独存储任务 deadline",
        "把最新用户消息视为",
        "范围、交付物或成功标准",
        "goal_plus_update_goal",
        "在修订或恢复前先澄清",
        "不要仅因 Goal Plus 记录处于 active",
        "当前未设置最大运行时间",
        "当前对话上下文",
        "不询问、不等待确认",
        "goal_plus_prepare_final_check",
        "goal_plus_submit_final_check",
        "spawn_agent",
        'fork_turns="none"',
        "`work_item_id`、`title`、`objective`",
        "绝不能代表审查员提交结论",
        "软 rubric 或预设评价维度",
        "开放式补充评价发生在每次 Evidence 结算之后",
        "也不改变硬",
        "candidate-local 结算",
    ):
        assert expected in text
    assert "mode_hint" not in text
    assert (
        "Goal Mode 下不要创建 SearchSpec" in text
        or "不要在 Goal Mode 创建 SearchSpec" in text
    )


def test_codex_mcp_config_registers_search_runtime() -> None:
    text = (ROOT / ".codex" / "config.example.toml").read_text(encoding="utf-8")
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "[mcp_servers.goal-plus]" in text
    assert 'command = "goal-plus"' in text
    assert 'args = ["--root", ".gp"]' in text
    assert 'default_tools_approval_mode = "approve"' in text
    for variable in (
        "CODEX_HOME",
        "OPENAI_API_KEY",
        "SFORGE_AGENT_API_KEY",
        "GOAL_PLUS_OUTER_DEADLINE_AT",
        "GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED",
        "GOAL_PLUS_SUPPLEMENTAL_EVALUATION_REQUIRED",
        "GOAL_PLUS_EVIDENCE_ANNOTATOR_MODEL",
        "GOAL_PLUS_EVIDENCE_ANNOTATOR_REASONING_EFFORT",
        "GOAL_PLUS_EVIDENCE_ANNOTATOR_BASE_URL",
        "GOAL_PLUS_EVIDENCE_ANNOTATOR_PROVIDER_ID",
        "GOAL_PLUS_EVIDENCE_ANNOTATOR_PROVIDER_NAME",
        "GOAL_PLUS_EVIDENCE_ANNOTATOR_API_KEY_ENV",
        "GOAL_PLUS_EVIDENCE_ANNOTATOR_WIRE_API",
    ):
        assert f'"{variable}"' in text
    assert 'cwd = "."' not in text
    assert ".codex/config.toml" in gitignore


def test_codex_assets_wire_goal_plus_host_hooks() -> None:
    hooks = json.loads(
        (ROOT / ".codex" / "hooks.example.json").read_text(encoding="utf-8")
    )
    expected_events = {
        "UserPromptSubmit",
        "SessionStart",
        "PreToolUse",
        "PostToolUse",
        "Stop",
        "SubagentStop",
    }

    assert hooks["hooks"].keys() == expected_events
    for event in expected_events:
        handler = hooks["hooks"][event][0]["hooks"][0]
        assert handler["type"] == "command"
        assert handler["command"] == "goal-plus --goal-plus-host-hook"
        assert "python3" not in handler["command"]
        assert handler["timeout"] == 30

    text = (ROOT / "docs" / "codex.md").read_text(encoding="utf-8")
    assert "installed plugin 通过 `hooks/hooks.json` 提供 Goal Plus host hook" in text
    assert "不再存在 `PostToolUse(goal_plus_create)`" in text
    assert "SessionStart` 只保留可恢复" in text
    assert "fail closed" in text
    assert "UserPromptSubmit" in text
    assert "PreToolUse" in text
    assert "SubagentStop" in text
    assert "turn_context" in text
    assert "不从 TOML、环境变量或" in text
    assert "自己的 verifier 提交持久化前会被阻止结束" in text
    assert "普通 subagent 不继承 parent 的 next action" in text
    assert "goal-plus --goal-plus-host-hook" in text


def test_installed_plugin_is_the_only_default_goal_plus_hook_source() -> None:
    project_hooks = ROOT / ".codex" / "hooks.json"
    example_hooks = ROOT / ".codex" / "hooks.example.json"
    plugin_hooks = ROOT / "hooks" / "hooks.json"

    assert not project_hooks.exists()
    assert example_hooks.read_bytes() == plugin_hooks.read_bytes()
    assert ".codex/hooks.json" in (ROOT / ".gitignore").read_text(encoding="utf-8")


def test_codex_plugin_default_prompts_use_skill_syntax() -> None:
    manifest = json.loads(
        (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )

    prompts = manifest["interface"]["defaultPrompt"]
    assert prompts
    assert all(prompt.startswith("$goal-plus") for prompt in prompts)


def test_codex_goal_plus_skills_are_explicit_only() -> None:
    for root in (ROOT / ".codex" / "skills", ROOT / "skills"):
        for name in ("goal-plus", "goal-plus-with-final-check", "search"):
            metadata = root / name / "agents" / "openai.yaml"
            assert metadata.is_file()
            assert "allow_implicit_invocation: false" in metadata.read_text(
                encoding="utf-8"
            )


def test_codex_search_skill_uses_spawn_agent_and_generic_bind() -> None:
    text = (ROOT / ".codex" / "skills" / "search" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "search_start_agent_session" in text
    assert "search_redispatch_candidate" in text
    assert "search_get_agent_observability" in text
    assert "spawn_agent" in text
    assert "search_bind_agent_handle" in text
    assert "只分配持久 session" in text
    assert "非空 receiver thread id" in text
    assert "真实返回的 thread id" in text
    assert "不得调用空 `wait`/`wait_agent`" in text
    assert "终态绑定会自动采集" in text
    assert "search_bind_opencode_session" not in text
    assert "background" not in text.lower()
    assert "## Verifier 冻结契约" in text
    assert '数值类型的 `spec.metric_name`' in text
    assert ".goal-plus-verifiers/" in text
    assert "`expected_outputs` 只接受" in text
    assert "GOAL_PLUS_VERIFIER_TMPDIR" in text
    assert "VerifierWorkspaceSideEffect" in text
    assert "固定 `/tmp`" in text
    assert "origin 运行一次 process-verifier suite" in text
    assert "WorkspaceRelocationVerifierFailed" in text
    assert "verifier_target_mismatch" in text
    assert "只有通过后才创建其余 lane" in text
    assert "一次性源码副本" not in text
    assert '`workspace.backend="git_worktree"`' in text
    assert '`workspace.promotion_mode="apply"`' in text
    assert "DirtySourceWorkspace" in text
    assert "workspace_capacity_notice" in text
    assert "fits_available_space=false" in text
    assert ".agents/skills/<name>/SKILL.md" in text
    assert "task_skill_notice" in text
    assert "candidate_task.task_skill_paths" in text
    assert "每个返回项" in text
    assert "仓外 Skill 继续由 host 管理" in text
    assert "parent cwd 不是 candidate workspace" in text
    assert '`strategy.config.global_evidence_mode` 必须使用 `"manual"`' in text


def test_codex_skills_use_native_mcp_tools_without_resource_discovery() -> None:
    for name in ("goal-plus", "search"):
        text = (ROOT / ".codex" / "skills" / name / "SKILL.md").read_text(
            encoding="utf-8"
        )
        normalized = " ".join(text.split())

        assert "list_mcp_resources" in text
        assert "list_mcp_resource_templates" in text
        assert "不枚举 tool" in normalized or "不会列出 MCP tool" in normalized
        assert "原生 MCP tools" in normalized
        assert "直接调用当前 tool registry" in normalized
        assert "`mcp_tool_call`" in text
        assert "shell、Python" in normalized
        assert "原生 MCP tool call" in normalized


def test_codex_search_skill_projects_launch_metadata_to_current_tool_schema() -> None:
    text = (ROOT / ".codex" / "skills" / "search" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(text.split())

    assert "当前 `spawn_agent` 工具 schema" in text
    assert "`task_name`、`message` 和 `fork_turns`" in text
    assert "继承父 Codex model" in text
    assert "绝不能合成可选 launch metadata" in normalized
    assert "agent_type=launch.agent_type" not in text


def test_codex_assets_document_explicit_role_model_syntax() -> None:
    goal_skill = (
        ROOT / ".codex" / "skills" / "goal-plus" / "SKILL.md"
    ).read_text(encoding="utf-8")
    search_skill = (ROOT / ".codex" / "skills" / "search" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    checked_skill = (
        ROOT / ".codex" / "skills" / "goal-plus-with-final-check" / "SKILL.md"
    ).read_text(encoding="utf-8")
    docs = (ROOT / "docs" / "codex.md").read_text(encoding="utf-8")
    combined = "\n".join((goal_skill, search_skill, checked_skill, docs))
    normalized_docs = " ".join(docs.split())

    assert "annotator=B workers=C,D" in combined
    assert "main=" in goal_skill
    assert "annotator=" in goal_skill
    assert "workers=" in goal_skill
    assert "`models=` 是 `workers=` 的兼容别名" in combined
    assert "strategy.evidence_annotator.model" in goal_skill
    assert "strategy.models" in goal_skill
    assert "不能由 Goal Plus prompt 切换" in goal_skill
    assert "UserPromptSubmit" in docs
    assert "不能切换它的模型" in docs
    assert "正常使用应省略 `main=`" in normalized_docs


def test_codex_search_skill_documents_parallel_loop_policy() -> None:
    text = (ROOT / ".codex" / "skills" / "search" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(text.split())

    assert "orchestration_mode: parallel_loops" in text
    assert "一个候选工作区就是一条自主搜索循环" in normalized
    assert "只创建一次" in normalized
    assert "运行时拒绝对该 run 的第二次 `search_plan_next`" in normalized
    assert "使用无 target" in text
    assert "`wait_agent`" in text
    assert "`list_agents`" in text
    assert "`followup_task`" in text
    assert "同一个原生 Codex subagent" in normalized
    assert "继续同一条自主搜索循环" in text
    assert "低分" in normalized
    assert "不是停止或替换" in normalized
    assert "不要再调用 `search_plan_next` 或 `search_start_batch`" in normalized
    assert "最终选择：只能在所有 worker drain 后" in normalized
    assert "source_run_id" in text
    assert "search_invalidate_run" in text
    assert "interrupt_agent" in text
    assert "deepen_incumbent" not in text
    assert "transfer_feature" not in text
    assert "macro_restart" not in text


def test_codex_search_skill_documents_worker_budget_watchdog() -> None:
    text = (ROOT / ".codex" / "skills" / "search" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(text.split())

    assert "budget_control" in text
    assert "parent_watchdog" in text
    assert "initial_wait_timeout_ms" in text
    assert "soft_closeout_seconds" in text
    assert "closeout_message" in text
    assert "final_wait_timeout_ms" in text
    assert "wait_agent" in text
    assert "send_message" in text
    assert "interrupt_agent" in text
    assert "仅供参考的时间提示" in text
    assert "GOAL_PLUS_OUTER_DEADLINE_AT" in text
    assert "主 agent、普通 subagent 和最终检查员" in normalized
    assert "worker_budget.min_runtime_seconds" in text
    assert "budget_control.autoresearch_lease" in text
    assert "同一个 Codex worker" in text
    assert "该 lease active 时，绝不能发送父级 closeout" in text
    assert "不得仅因剩余时间装不下原始完整 lease 而停止" in normalized
    assert "外层剩余时间减最终收尾预留" in normalized


def test_codex_worker_prompt_defers_to_runtime_candidate_contract() -> None:
    text = (ROOT / ".codex" / "agents" / "search_candidate_agent.toml").read_text(
        encoding="utf-8"
    )

    for expected in (
        'name = "search_candidate_agent"',
        "search_get_agent_context",
        "candidate_task",
        "未列出的可选能力",
    ):
        assert expected in text
    for runtime_detail in (
        "search_get_global_evidence",
        "supplemental",
        "shared_dir",
    ):
        assert runtime_detail not in text


def test_codex_search_skill_documents_state_level_resume() -> None:
    text = (ROOT / ".codex" / "skills" / "search" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    agent = (ROOT / ".codex" / "agents" / "search_candidate_agent.toml").read_text(
        encoding="utf-8"
    )

    assert "candidate-local history 由运行时拥有，不是 `plan.md` 文件" in text
    assert "状态级恢复" in text
    assert "context.history" not in text
    assert "context.recent_iterations" in text
    assert "context.latest_result" in text
    assert "search_list_iterations" in text
    assert "worker_budget.max_runtime_seconds" in text
    assert "search_redispatch_candidate" in text
    assert "一次派发" in text
    assert "handoff metadata" in text
    assert "fresh session" in agent
    assert "沿用已加载的运行时上下文和 Evidence" in agent


def test_codex_and_pi_worker_launch_contracts_are_equivalent() -> None:
    codex_prompt = tomllib.loads(
        (ROOT / ".codex" / "agents" / "search_candidate_agent.toml").read_text(
            encoding="utf-8"
        )
    )["developer_instructions"].strip()
    pi_prompt = (ROOT / ".pi" / "prompts" / "search-candidate-worker.md").read_text(
        encoding="utf-8"
    ).strip()

    assert codex_prompt == pi_prompt


def test_codex_search_reuses_exact_worker_evidence_before_parent_verification() -> None:
    text = (ROOT / ".codex" / "skills" / "search" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    exclusion_clause = text.split("只有 `single_common_command`", 1)[1].split(
        "这些具体排除项才支持 `not_applicable`", 1
    )[0]
    exclusions = ["single_common_command", *exclusion_clause.split("`")[1::2]]

    assert "准确 worker Evidence" in text
    assert "仅在没有匹配 Evidence 时" in text
    assert "apply_cwd" in text
    assert "apply_directory" in text
    assert 'search_run_verifier(hypothesis="主流程完成验证")' not in text
    assert "shared_tools[*].tool_view" in text
    assert "Tool View 前不向 candidate 暴露" in text
    assert "search_copy_shared_tool" in text
    assert "search_stage_shared_tool" in text
    assert "toolization_decision" in text
    assert "repeated_workflow" in text
    assert "domain_construction_or_probe" in text
    assert "behavior_or_invariant_checker" in text
    assert "reproducer_fixture_or_case_generator" in text
    assert "parser_trace_or_comparator" in text
    assert "peer_setup_or_feedback_reduction" in text
    assert "repeated_sequence" not in text
    assert "domain_probe" not in text
    assert "parser_or_trace" not in text
    assert "peer_setup_reduction" not in text
    assert "测试代码按用途而不是文件名分类" in text
    assert "测试文件，以及 candidate" in text
    assert "`test_` 文件名、属于正式测试或验证同一" in text
    assert "candidate 自己编写并纳入最终交付或主补丁的正式回归测试" in text
    assert "显式选择的测试及依赖" in text
    assert "candidate 产品实现" in text
    assert "冻结 verifier/runner/grader" in text
    assert "隐藏答案或评分逻辑" in text
    assert "不得复制、代理或近似重建隐藏反馈" in text
    assert "revision_allowed=true" in text
    assert "revision_head.tool_id" in text
    assert "revision_allowed=false` 时使用 `revision_blocker` 的准确值" in text
    assert exclusions == [
        "single_common_command",
        "logic_free_wrapper",
        "restricted_artifact",
        "candidate_private_state",
        "duplicate_snapshot",
        "no_toolizable_material",
        "existing_family_sufficient",
        "no_material_tool_delta",
        "draft_not_ready",
        "max_published_versions_reached",
    ]
    assert "capability_extension` 必须新增稳定" in text
    assert "adoption_fix` 必须有同 family 的真实 copy/adoption 事实" in text
    assert "contract_change` 必须" in text
    assert "新增稳定的契约键" in text
    assert "不能靠改名或改写同义键" in text
    assert "draft_not_ready` 仅表示具体的安全性" in text
    assert "runtime 会在 catalog 尚无 family 时拒绝" in text
    assert "receipt" in text
    assert "Tool View 只用于发现和初筛" in text
    assert "不规定复用方式，也不是采用建议" in text
    assert "复制本身不要求调用原工具" in text
    assert "先阅读 manifest、入口和源码" in text
    assert "只读分析或改写源码不要求先运行原工具" in text
    assert "当前 `adopted_tools` 只证明该快照曾复制进" in text


def test_codex_search_uses_open_posthoc_evaluation_as_non_gating_feedback() -> None:
    skill = (ROOT / ".codex" / "skills" / "search" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "不来自 FrozenSpec" in skill
    assert "动态比较" in skill
    assert "不是分数、推荐或 promotion gate" in skill
    assert "不改变硬分结算规则" in skill


def test_codex_goal_plus_defers_report_until_terminal_state() -> None:
    goal_skill = (
        ROOT / ".codex" / "skills" / "goal-plus" / "SKILL.md"
    ).read_text(encoding="utf-8")
    search_skill = (ROOT / ".codex" / "skills" / "search" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    normalized_goal_skill = " ".join(goal_skill.split())

    assert "此时不要调用" in goal_skill
    assert "`search_report`" in goal_skill
    assert "绝不能生成中间 Goal Plus 报告" in normalized_goal_skill
    assert "只有 Goal Plus 记录达到终态" in goal_skill
    assert "不能创建 Goal Plus" in search_skill
    assert "创建 standalone run" in search_skill


def test_codex_final_checker_and_with_check_alias_are_read_only_and_independent() -> None:
    checker = (ROOT / ".codex" / "agents" / "goal_plus_final_checker.toml").read_text(
        encoding="utf-8"
    )
    alias = (
        ROOT / ".codex" / "skills" / "goal-plus-with-final-check" / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert 'name = "goal_plus_final_checker"' in checker
    assert "只能进行只读操作" in checker
    assert "goal_plus_status" in checker
    assert "goal_plus_submit_final_check" in checker
    assert "绝不能编辑文件" in checker
    assert "task_local_skill_paths" in checker
    assert ".agents/skills" not in checker
    assert ".codex/skills" in checker
    assert "plugins/goal-plus/skills" in checker
    assert "用户全局 Skill" in checker
    assert "name: goal-plus-with-final-check" in alias
    assert 'checker_host="codex"' in alias
    assert "spawn_agent" in alias
    assert "launch.fallback_agent_type" in alias
    assert 'fallback_agent_type="default"' in alias
    assert 'fork_turns="none"' in alias
    assert "$goal-plus resume" in alias


def test_codex_docs_record_log_inspection_paths() -> None:
    text = (ROOT / "docs" / "codex.md").read_text(encoding="utf-8")
    debug = (ROOT / "docs" / "debugging-runtime.md").read_text(encoding="utf-8")

    combined = text + "\n" + debug
    assert "codex exec --json" in combined
    assert "CODEX_HOME" in combined
    assert "rollout-*.jsonl" in combined
    assert "RUST_LOG=debug" in combined
    assert "log_dir=./.codex-log" in combined


def test_codex_docs_record_native_parity_contract() -> None:
    codex = (ROOT / "docs" / "codex.md").read_text(encoding="utf-8")
    adapters = (ROOT / "docs" / "agent-host-adapters.md").read_text(encoding="utf-8")
    debugging = (ROOT / "docs" / "debugging-runtime.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "Codex 0.144.1" in codex
    assert "initial_wait_timeout_ms" in codex
    assert "closeout_message" in codex
    assert "final_wait_timeout_ms" in codex
    assert "worker_launch" in codex
    assert "codex_parallel_loop_cycle" in codex
    assert "autonomous" in codex
    assert "parallel" in adapters
    assert "当前 `spawn_agent` schema" in adapters
    for text in (adapters, debugging, readme, agents):
        assert "UserPromptSubmit" in text
        assert "PreToolUse" in text
        assert "SubagentStop" in text
    assert "PreToolUse/SubagentStop gates remain manual" not in agents


def test_codex_goal_plus_skill_documents_multiple_search_tasks() -> None:
    text = (ROOT / ".codex" / "skills" / "goal-plus" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "`goal_plus_id`" in text
    assert "另一项 Search 任务" in text
    assert "`search_tasks` 是其仅追加" in text
    assert "仅追加" in text
    assert "`linked_search` 只是当前任务的兼容视图" in text
    assert "规划轮次和已启动搜索轮次" in text
