from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.pi


def test_pi_assets_exist() -> None:
    for path in (
        ".pi/prompts/goal-plus.md",
        ".pi/skills/goal-plus/SKILL.md",
        ".pi/prompts/search-candidate-worker.md",
        ".pi/extensions/goal-plus.ts",
    ):
        assert (ROOT / path).exists(), f"missing {path}"

    skill_files = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / ".pi" / "skills").glob("*/SKILL.md")
    )
    assert skill_files == [".pi/skills/goal-plus/SKILL.md"]


def test_pi_package_manifest_exports_only_the_user_integration() -> None:
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))

    assert package["name"] == "goal-plus"
    assert "pi-package" in package["keywords"]
    assert package["pi"] == {
        "extensions": [".pi/extensions/goal-plus.ts"],
        "skills": [".pi/skills/goal-plus/SKILL.md"],
    }


def test_pyproject_exposes_pi_console_scripts() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'requires-python = ">=3.10"' in text
    assert 'goal-plus-codex-doctor = "goal_plus.codex_doctor:main"' in text
    assert 'goal-plus-pi-tool = "goal_plus.pi_tool:main"' in text
    assert 'goal-plus-pi-worker = "goal_plus.pi_worker:main"' in text
    assert 'goal-plus-pi-pool = "goal_plus.pi_pool:main"' in text


def test_pi_goal_plus_prompt_cannot_bootstrap_goal_plus() -> None:
    text = (ROOT / ".pi" / "prompts" / "goal-plus.md").read_text(encoding="utf-8")

    assert "此文件只是兼容说明，不能启动 Goal Plus" in text
    assert "不得调用 `goal_plus_create`" in text
    assert "原生 Pi extension" in text
    assert "host command" in text
    assert 'worker_host: "pi-rpc"' in text
    assert 'worker_mode: "agent-session-pool"' not in text
    assert 'orchestration_mode: "parallel_loops"' in text
    assert '`workspace.backend="git_worktree"`' in text
    assert '`workspace.promotion_mode` 默认是 `apply`' in text
    assert "DirtySourceWorkspace" in text
    assert "workspace_capacity_notice" in text
    assert "fits_available_space=false" in text
    assert '有限数值类型的' in text
    assert '`spec.metric_name`' in text
    assert ".goal-plus-verifiers/" in text
    assert "`expected_outputs` 只列出产物路径或 glob" in text
    assert "GOAL_PLUS_VERIFIER_TMPDIR" in text
    assert "固定的 `/tmp`" in text
    assert "origin `source_path` 运行一次" in text
    assert "WorkspaceRelocationVerifierFailed" in text
    assert "verifier_target_mismatch" in text
    assert "bootstrap 结果不是 candidate iteration" in text
    assert "`goal_plus_record_triage` 之前不要读取或审计目标文件" in text
    assert "当前未设置最大运行时间" in text
    assert "当前对话上下文" in text
    assert "不要询问或等待确认" in text
    assert "告知后立即继续执行" in text
    assert "feature_plugins=[\"orchestration\"]" in text
    assert "`main=` 不是受支持的" in " ".join(text.split())
    assert ".goal-plus-verifiers/" in text
    assert "`expected_outputs` 只列出" in text
    assert "{{input}}" not in text
    assert "把 `$ARGUMENTS` 当作已授权目标" in text


def test_pi_goal_plus_skill_records_modes_and_gate() -> None:
    text = (ROOT / ".pi" / "skills" / "goal-plus" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(text.split())

    assert "name: goal-plus" in text
    assert "Goal Mode" in text
    assert "Spec Discovery Mode" in text
    assert "Search Mode" in text
    assert "自动升级到 Search Mode" in text
    assert "不要要求用户" in text
    assert "用户提示有用但可选" in text
    assert "不得调用 `goal_plus_create`" in text
    assert "缺少授权" in text
    assert "goal_plus_gate" in text
    assert "goal_plus_monitor_snapshot" in text
    assert "主要只读监控路径" in text
    assert "goal-plus-pi-tool goal_plus_monitor_snapshot" in text
    assert "不要把手动 tail 文件作为主要监控路径" in text
    assert "goal_plus_link_search_run" in text
    assert 'worker_host: "pi-rpc"' in text
    assert 'worker_mode: "agent-session-pool"' not in text
    assert 'orchestration_mode: "parallel_loops"' in text
    assert '`workspace.backend="git_worktree"`' in text
    assert '`workspace.promotion_mode` 默认是' in text
    assert "DirtySourceWorkspace" in text
    assert "origin `source_path` 运行一次 process-verifier suite" in text
    assert "WorkspaceRelocationVerifierFailed" in text
    assert "verifier_target_mismatch" in text
    assert "bootstrap 结果不是 candidate iteration" in text
    assert "workspace_capacity_notice" in text
    assert "fits_available_space=false" in text
    assert ".agents/skills/<name>/SKILL.md" in text
    assert "task_skill_notice" in text
    assert "candidate_task.task_skill_paths" in text
    assert "每个返回项" in text
    assert "仓外 Skill 继续由 host 管理" in text
    assert '`strategy.config.global_evidence_mode` 必须使用 `"manual"`' in text
    assert "Pi 支持的 strategy name" in text
    assert "`agent_guided`、`agent` 或 `default`" in text
    assert "`random` 或 `random_mode`" in text
    assert "可复用现有 `frozen_spec_id`" in text
    assert "pi_search_pool_open" in text
    assert "max_parallel=<budget.max_parallel>" in text
    assert "pi_search_run_batch" not in text
    assert "pi_search_run_candidate" not in text
    assert "复用当前 durable Evidence" in text
    assert "缺失时补父级 `search_run_verifier`" in text
    assert "search_select" in text
    assert "search_report" in text
    assert "search_promote" in text
    assert "search_apply_promotion" in text
    assert "apply_cwd" in text
    assert "apply_directory" in text
    assert "跨进程原生 session continuation" in text
    assert "search_continue_agent_session" in text
    assert "session_jsonl_restart" not in text
    assert "尽早运行" in text
    assert "未修改初始状态的验证" in text
    assert "verifier 记录运行时 iteration" in text
    assert "完整的面向用户 skill" in text
    assert "goal_plus_record_search_result" in text
    assert "最终原始目标审计" in text
    assert "/goal-plus-with-final-check" in text
    assert "goal_plus_update_goal" in text
    assert "把最新用户消息视为本轮权威依据" in normalized
    assert "范围、交付物或成功标准" in normalized
    assert "在修订或恢复前先澄清" in normalized
    assert "不要仅因 Goal Plus 记录处于 active" in normalized
    assert "Goal Plus 命令不能切换它" in normalized
    assert "`main=` 不是受支持的 Goal Plus 命令配置" in normalized
    assert "goal_plus_prepare_final_check" in text
    assert "pi_goal_plus_run_final_check" in text
    assert "goal_plus_submit_final_check" in text
    assert "绝不能生成中间 Goal Plus" in text
    assert "只有 Goal Plus 记录达到终态" in text
    assert "原生 Pi `/goal-plus` 命令会" in text
    assert "整个 Goal Plus 任务的最大运行" in normalized
    assert "当前对话上下文" in normalized
    assert "不询问、不等待确认" in normalized
    assert "/goal-plus mode=autonomous" in text
    assert "/goal-plus mode=probe" in text
    assert "`raw_goal` 的规范末行" in normalized
    assert "任何 worker lease 结束都不会完成" in text
    assert "`max_duration_seconds`" in text
    assert "`outer_deadline_at`" in text
    assert "缺省时不要编造" in text
    assert "加入队列" in text
    assert "continuation prompt" in text
    assert "`goal_plus_record_triage` 前不要读取或审计目标文件" in text
    assert "不要为了最终展示再显式调用一次 `goal_plus_monitor_snapshot`" in text
    assert "Goal Plus 已结算，结果见下方完成卡。" in text
    assert "不要另写 “Goal Plus 统计”段落" in normalized
    assert "Pi extension 会在 `agent_end` 后" in normalized


def test_pi_prompt_and_extension_defer_report_until_terminal_state() -> None:
    prompt = (ROOT / ".pi" / "prompts" / "goal-plus.md").read_text(
        encoding="utf-8"
    )
    extension = (ROOT / ".pi" / "extensions" / "goal-plus.ts").read_text(
        encoding="utf-8"
    )
    assert "在 Search 执行" in prompt
    assert "绝不能调用 `search_report`" in prompt
    assert "Goal Plus 记录达到终态后" in prompt
    assert "调用且只调用一次 `search_report`" in prompt
    assert "不在自然语言回复中重复报告路径" in prompt
    assert "可展开完成卡展示" in prompt
    assert "active 的已链接 Goal Plus 记录会被拒绝" in extension
    terminal = extension[extension.index("if (isTerminalStatus(status?.status))") :]
    assert terminal.index("appendGoalStats") < terminal.index(
        '"pi_goal_plus_finalize_reports"'
    )


def test_pi_goal_plus_skill_documents_parallel_loop_policy() -> None:
    text = (ROOT / ".pi" / "skills" / "goal-plus" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(text.split())

    assert 'orchestration_mode: "parallel_loops"' in text
    assert "每个初始候选工作区都是长期自主循环" in normalized
    assert "调用且只调用一次" in normalized
    assert "运行时会拒绝" in normalized
    assert "第二份 plan" in normalized
    assert "首个 run 必须省略 `source_run_id`" in text
    assert "pi_search_pool_wait_any" in text
    assert "只是本次轮询超时" in normalized
    assert "不能调用 `pi_search_pool_close`" in normalized
    assert "有效最低 lease 和 closeout 边界只由 supervisor 判定" in normalized
    assert "等待准确 snapshot 的 `active_count=0`" in normalized
    assert "pi_search_pool_continue" in text
    assert "pi_search_pool_submit" not in text
    assert "pi_search_pool_close" in text
    assert "worker_budgets" in text
    assert "每个 proposal 都必须包含 `intent`" in normalized
    assert "`worker_budgets` 必须按 `candidate_id` 映射" in normalized
    assert "继续同一条自主搜索循环" in text
    assert "candidate_ready" in text
    assert "最低累计 lease" in normalized
    assert "自动恢复同一个 session 和 worktree" in normalized
    assert "创建新的 `agent_session_id`" in normalized
    assert "已有工作区改动和 durable Evidence 保留" in normalized
    assert "复用匹配当前产物的 durable Evidence" in normalized
    assert "同一工作区" in normalized
    assert "保留 `agent_session_id` 和候选身份" in normalized
    assert "不要调用 `search_plan_next`、`search_start_batch`" in normalized
    assert "不要根据排名或改进情况" in normalized
    assert "分数低" in normalized
    assert "停止或替换" in normalized
    assert "原始完整 worker lease 能放入剩余时间" in normalized
    assert "外层剩余时间减最终收尾预留" in normalized
    assert "source_run_id" in text
    assert "search_invalidate_run" in text
    assert "active_count=0" in text
    assert "deepen_incumbent" not in text
    assert "transfer_feature" not in text
    assert "macro_restart" not in text


def test_pi_worker_prompt_defers_to_runtime_candidate_contract() -> None:
    text = (ROOT / ".pi" / "prompts" / "search-candidate-worker.md").read_text(
        encoding="utf-8"
    )

    for expected in (
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


def test_pi_workspace_schema_needs_no_task_skill_configuration() -> None:
    text = (ROOT / ".pi" / "extensions" / "goal-plus.ts").read_text(
        encoding="utf-8"
    )
    workspace_schema = text.split("const WorkspaceSpec", 1)[1].split(
        "const SharedDirSpec", 1
    )[0]

    assert "skill_roots" not in workspace_schema
    assert "promotion_mode" in workspace_schema
    assert 'Type.Literal("apply")' in workspace_schema
    assert 'Type.Literal("artifact_only")' in workspace_schema


def test_pi_goal_plus_skill_uses_the_same_family_revision_contract() -> None:
    text = (ROOT / ".pi" / "skills" / "goal-plus" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    for expected in (
        "revision_allowed=true",
        "revision_head.tool_id",
        "capability_extension",
        "adoption_fix",
        "contract_change",
        "新增稳定契约键",
        "不得靠改名或同义键制造增量",
        "revision_allowed=false",
        "revision_blocker",
        "no_toolizable_material",
        "existing_family_sufficient",
        "no_material_tool_delta",
        "draft_not_ready",
        "max_published_versions_reached",
    ):
        assert expected in text
    assert "no_material_tool_delta` 仅用于已有" in text
    assert "catalog 尚无 family 时拒绝" in text


def test_pi_skill_documents_post_tool_time_advisory() -> None:
    text = (ROOT / ".pi" / "skills" / "goal-plus" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(text.split())

    assert "完成 worker 工具后检查提示性时间估算" in normalized
    assert "最后一次 subagent verifier - 首个候选 session" in normalized
    assert "GOAL_PLUS_OUTER_DEADLINE_AT" in text
    assert "一次提示性 `steer`" in text
    assert "不会停止 worker" in text


def test_pi_extension_registers_role_tools_gate_and_workspace_guard() -> None:
    text = (ROOT / ".pi" / "extensions" / "goal-plus.ts").read_text(
        encoding="utf-8"
    )

    assert "GOAL_PLUS_PI_ROLE" in text
    assert 'role === "main"' in text
    assert 'role === "worker"' in text
    assert "goal_plus_host_start" in text
    assert "runHostCommandCli" in text
    assert '"--host-entrypoint"' in text
    assert '"--host-capability"' in text
    assert "capabilityToken = randomUUID()" in text
    assert 'flag: "wx"' in text
    assert "goalPlusSessionAuthorized" in text
    assert "goalPlusTurnAuthorized" not in text
    assert "search_get_agent_context" in text
    assert "search_run_verifier" in text
    assert "workspace/results.tsv" in text
    assert "且只追加一条已验证记录" in text
    assert "VerifierWorkspaceSideEffect" in text
    assert "GOAL_PLUS_VERIFIER_TMPDIR" in text
    assert "clean origin 运行 process verifier" in text
    assert "c001 worktree 验证 relocation" in text
    assert "bootstrap 结果不是 candidate iteration" in text
    assert "一次性源码副本" not in text
    assert "pi_rpc_run_worker" not in text
    assert "pi_search_run_batch" not in text
    assert "pi_search_run_candidate" not in text
    assert 'pi.registerCommand("goal-plus"' in text
    assert 'pi.registerCommand("goal-plus-with-final-check"' in text
    assert "[mode=autonomous|probe]" in text
    assert "typed command config" in text
    assert "GOAL_PLUS_COMMAND_CONFIG_USAGE" in text
    assert "max_duration_seconds=正整数（秒）" in text
    assert "workers|models=" in text
    assert "workspace_backend=copy|git_worktree|thinkthread" in text
    assert "promotion_mode=apply|artifact_only" in text
    assert "command_config 中的字段是 runtime 强约束" in text
    assert "parseGoalPlusRoleModels" in text
    assert "resolveGoalPlusRoleModels" in text
    assert "assertPiMainModel" not in text
    assert "const current = ctx.model" not in text
    assert "main= is not supported" in text
    assert "with /model before /goal-plus" in text
    assert "pi.setModel(main)" not in text
    assert "已校验当前 Pi session，未切换模型" not in text
    update_start = text.split("async function updateGoalPlusStart", 1)[1].split(
        "function startGoalPlusMainTurn", 1
    )[0]
    assert "parseGoalPlusRoleModels(rawGoal)" in update_start
    assert '"workers" | "models"' in text
    assert "workers= and models= are aliases" in text
    assert "strategy.evidence_annotator.model" in text
    assert "strategy.models" in text
    assert "goal-plus-native-state" in text
    assert 'pi.on("session_start"' in text
    assert 'pi.on("before_agent_start"' in text
    assert 'pi.on("agent_end"' in text
    assert 'lastMessage?.role === "assistant"' in text
    assert 'lastMessage.stopReason === "error"' in text
    assert 'lastMessage.stopReason === "aborted"' in text
    assert 'lastMessage.stopReason === "length"' in text
    agent_end = text.split('pi.on("agent_end"', 1)[1]
    provider_error_branch = agent_end.split(
        'lastMessage.stopReason === "error"', 1
    )[1].split('lastMessage.stopReason === "aborted"', 1)[0]
    aborted_branch = agent_end.split(
        'lastMessage.stopReason === "aborted"', 1
    )[1].split("if (ctx.hasPendingMessages())", 1)[0]
    assert "goalPlusSessionAuthorized = false" not in provider_error_branch
    assert "goalPlusSessionAuthorized = false" not in aborted_branch
    assert "authorizes this native session" in provider_error_branch
    input_handler = text.split('pi.on("input"', 1)[1].split(
        'pi.on("tool_call"', 1
    )[0]
    ordinary_input_branch = input_handler.split(
        "if (request === undefined)", 1
    )[1].split("if (ctx.mode", 1)[0]
    assert "goalPlusSessionAuthorized = false" not in ordinary_input_branch
    session_start = text.split('pi.on("session_start"', 1)[1].split(
        'pi.on("before_agent_start"', 1
    )[0]
    assert "goalPlusSessionAuthorized = false" in session_start
    assert "countToolCalls(lastMessage.content) === 0" in text
    assert "if (lengthWithoutToolCall) return;" in text
    assert 'recoveryReason: "length_without_tool_call"' not in text
    assert "上一轮因达到输出长度上限而结束" not in text
    assert 'pi.on("tool_call"' in text
    assert 'goal_plus_gate' in text
    assert "tool_name" in text
    assert "goal-plus-stop-continuation" in text
    assert "GOAL_PLUS_PI_WORKER_CONTINUE_UNTIL_MS" in text
    assert "goal-plus-worker-continuation" in text
    assert "沿用已加载的运行时上下文和 Evidence；探索新方向" in text
    assert '{ triggerTurn: true, deliverAs: "followUp" }' in text
    assert "goal-plus-stats" in text
    assert "goal-plus-runtime-start" in text
    assert "buildPiRuntimeStartEntry" in text
    assert "agent=${entry.agent} · host=${entry.host} · mode=${entry.mode}" in text
    assert "ctx.isProjectTrusted()" in text
    assert "pi.getActiveTools()" in text
    assert 'pi_sandbox: "none"' in text
    assert 'pi_builtin_tool_approval: "none"' in text
    assert 'filesystem_access: "launcher-process-user"' in text
    assert 'network_access: "host-policy-unreported"' in text
    assert "appendPiRuntimeStartEntry(pi, ctx)" in text
    assert "event.reason !== \"reload\"" in text
    assert "registerEntryRenderer<PiRuntimeStartEntry>" in text
    assert "registerEntryRenderer<GoalPlusStatsEntry>" in text
    assert "appendEntry<GoalPlusStatsEntry>" in text
    assert 'customType: "goal-plus-stats"' not in text
    assert "assistantMessages" in text
    assert "buildPiCompletionPresentation" in text
    assert "goal_plus_monitor_snapshot" in text
    assert "collapsedLineCount" in text
    assert "candidateCompletionsFromMonitor" in text
    assert "monitorSnapshotPresentation" in text
    assert 'renderCall: name === "goal_plus_monitor_snapshot"' in text
    assert 'renderResult: name === "goal_plus_monitor_snapshot"' in text
    assert "new Box(1, 0" in text
    assert "new Text(rendered, 0, 0)" in text
    assert "clearPiProgress(ctx);\n\t\t\tappendGoalStats" in text
    assert "ctx.ui.notify(statsMessage" not in text
    assert 'GOAL_PLUS_COMMAND_CONTEXT_TYPE = "goal-plus-command-context"' in text
    assert "startGoalPlusMainTurn" in text
    assert "customType: GOAL_PLUS_COMMAND_CONTEXT_TYPE" in text
    assert 'entrypoint_status: "host_verified"' in text
    assert "display: false" in text
    assert "{ triggerTurn: true }" in text
    assert "sendUserMessage" not in text
    assert "GOAL_PLUS_SOURCE_PATH" in text
    assert "sys.path.insert" in text
    assert "goal_plus.pi_tool" in text
    assert "goal_plus.pi_worker" in text
    assert "isPrintLikeInvocation" in text
    assert 'process.argv.includes("-p")' in text
    assert "if (!isPrintLikeInvocation)" in text
    assert 'mode !== "print"' in text
    assert "function canPersistGoalState" in text
    assert 'pi.on("input"' in text
    assert 'action: "transform"' in text
    assert "goalPlusRequestFromSlashInput" in text
    assert 'body === "resume"' in text
    assert 'body === "edit" || body.startsWith("edit ")' in text
    assert 'action: "invalid"' in text
    assert "subcommands must be exact lowercase edit or resume" in text
    assert "createGoalPlusStart" in text
    assert "updateGoalPlusStart" in text
    assert "resumeGoalPlusStart" in text
    assert "/goal-plus resume" in text
    assert "当前未设置最大运行时间" in text
    assert "当前对话上下文" in text
    assert "不要询问或等待确认" in text
    assert "告知后立即继续执行" in text
    assert 'action: "resume"' in text
    assert "不要将其降级为普通 Goal Mode" in text
    assert "以最新用户消息作为" in text
    assert "范围、交付物或成功标准" in text
    assert "/goal-plus edit <完整修订目标>" in text
    assert "绝不能编造 frozen_spec_id" in text
    assert 'if (name === "goal_plus_submit_final_check")' in text
    assert "activateGoal(pi, result.details, startEntryCount, canPersistPiState)" in text
    assert 'if (name === "goal_plus_create" && canPersistPiState)' not in text
    main_tools = text.split("const mainTools = [", 1)[1].split("];", 1)[0]
    assert '"goal_plus_create"' not in main_tools
    assert '"goal_plus_update_goal"' not in main_tools
    assert "activateGoal(pi, status, startEntryCount, canPersistGoalState(ctx.mode))" in text
    assert "await ctx.waitForIdle()" not in text
    assert "在 goal_plus_record_triage 前不要读取或审计目标文件" in text
    assert "workspaceGuard" in text
    assert "resource_lock: Type.Optional(Type.String({ minLength: 1 }))" in text
    assert "device_pools: Type.Optional(Type.Array(DevicePoolSpec))" in text
    assert "MAIN_GATED_TOOLS" in text
    assert "pi_rpc_run_worker" not in text
    assert "GOAL_PLUS_PI_EXPOSE_LOW_LEVEL_WORKER" not in text
    assert '"pi_search_run_candidate"' not in text
    assert '"pi_search_run_batch"' not in text
    assert "block" in text
    assert 'role === "final-checker"' in text
    assert "registerPiFinalCheckTool" in text
    assert "最终检查审查员只能进行只读操作" in text
    assert 'verdict: "interrupted"' in text
    assert "timed out before submitting a verdict" in text


def test_pi_extension_has_gated_progress_ui_preview() -> None:
    extension = (ROOT / ".pi" / "extensions" / "goal-plus.ts").read_text(
        encoding="utf-8"
    )
    progress = (
        ROOT / ".pi" / "extensions" / "goal-plus-progress.ts"
    ).read_text(encoding="utf-8")

    assert "GOAL_PLUS_UI_PREVIEW" in extension
    assert 'pi.registerCommand("goal-plus-ui-preview"' in extension
    assert "PI_COMPLETION_PREVIEW" in extension
    assert 'mode === "result"' in extension
    assert "appendGoalCompletionPreview" in extension
    assert "ctx.ui.setStatus" in extension
    assert "ctx.ui.setWidget" in extension
    assert "PI_PROGRESS_PREVIEWS[progressPreviewMode]" in extension
    assert "goal_plus_create" not in progress
    assert "goal_plus_monitor_snapshot" not in progress
    assert "SEARCH ACTIVE" in progress
    assert "SEARCH COMPLETE" in progress
    assert 'metricName: "sum_radii"' in progress
    assert "147_734" not in progress
    assert "verifierRunsTotal" in progress
    assert "selectedCandidateId" in progress
    assert "promotionPassed" in progress
    assert "continue candidate loops" in progress
    assert "buildPiCompletionPresentation" in progress
    assert "PI_COMPLETION_PREVIEW" in progress
    assert "LATE +" not in progress
    assert 'lateMarker = " · LATE "' not in extension
    assert "presetDurationSeconds" in progress
    assert "预设时间为" in progress
    assert "实际完成收尾时间为" in progress
    assert 'formatUsage("Usage ", totalUsage)' in progress
    assert 'lines.push("Lanes")' in progress


def test_pi_extension_has_precise_tool_schemas_and_error_classification() -> None:
    text = (ROOT / ".pi" / "extensions" / "goal-plus.ts").read_text(
        encoding="utf-8"
    )

    assert "RuntimeToolSchemas" in text
    assert "parameters: toolParameters(name)" in text
    assert "parameters: JsonArgs" not in text
    assert "goal_plus_record_triage: Type.Object" in text
    assert "const SearchSpecSchema = Type.Object" in text
    assert "const AcceptanceViewSpec = Type.Object" not in text
    assert "acceptance_view: Type.Optional" not in text
    assert 'Type.Literal("parallel_loops")' in text
    assert "search_invalidate_run: Type.Object" in text
    assert "source_run_id: Type.Optional(" in text
    assert 'pattern: "^run_"' in text
    assert "初始 run 必须省略 source_run_id，或在 strict schema 下传 null" in text
    assert "const SearchSpecDraftSchema = Type.Partial(SearchSpecSchema)" in text
    assert "spec: SearchSpecSchema" in text
    assert "search_spec: SearchSpecDraftSchema" in text
    assert "metric_direction: Type.Union" in text
    assert "process_verifiers: Type.Array(VerifierCommand" in text
    assert "const SharedDirSpec = Type.Object" in text
    for signal in [
        "repeated_workflow",
        "domain_construction_or_probe",
        "behavior_or_invariant_checker",
        "reproducer_fixture_or_case_generator",
        "parser_trace_or_comparator",
        "peer_setup_or_feedback_reduction",
    ]:
        assert f'Type.Literal("{signal}")' in text
    decision_schema = text.split("const ToolizationDecision = Type.Object", 1)[1].split(
        "const SearchSpecSchema = Type.Object", 1
    )[0]
    assert "maxItems: 6" in decision_schema
    for removed_signal in [
        "repeated_sequence",
        "domain_probe",
        "parser_or_trace",
        "peer_setup_reduction",
    ]:
        assert f'Type.Literal("{removed_signal}")' not in text
    assert 'Type.Literal("max_published_versions_reached")' in text
    assert "max_pending_revisions_per_family: Type.Optional(Type.Literal(1))" in text
    assert (
        "max_published_versions_per_family: "
        "Type.Optional(Type.Integer({ minimum: 1, maximum: 32 }))"
    ) in text
    assert "shared_dir: Type.Optional(SharedDirSpec)" in text
    assert "worker_budget: Type.Optional(Type.Union" in text
    assert "min_runtime_seconds: Type.Optional(NullablePositiveInteger)" in text
    assert "min_verifier_runs: Type.Optional(NullablePositiveInteger)" in text
    assert "const CandidateProposal = Type.Object" in text
    assert "proposals: Type.Optional(Type.Array(CandidateProposal))" in text
    assert "inherited_feature_limit" not in text
    assert "inherited_pitfall_limit" not in text
    assert "const RuntimeToolDescriptions" in text
    assert "RuntimeToolDescriptions[name]" in text
    assert "search_get_agent_context:" in text
    assert "search_copy_shared_tool:" in text
    assert "search_stage_shared_tool:" in text
    stage_schema = text.split("search_stage_shared_tool: Type.Object", 1)[1].split(
        "search_get_agent_observability: Type.Object", 1
    )[0]
    assert "maxItems: 32" in stage_schema
    assert "maxItems: 64" in stage_schema
    assert stage_schema.count("uniqueItems: true") == 2
    assert "candidate_task.share_out_dir 非空表示已启用 shared_dir" in text
    assert "revision_head 指定唯一更新基线" in text
    assert "revision_allowed、published_version_count" in text
    assert "revision_allowed=false 时使用 revision_blocker 的准确值" in text
    assert "adoption_fix 必须有同 family 的真实 copy/adoption 事实" in text
    assert "不要求制造新键" in text
    assert "draft_not_ready 只用于具体的安全性" in text
    assert "供阅读源码或自主复用；复制不要求调用原工具" in text
    assert "当前 adopted_tools 只证明快照曾复制进本轮上下文" in text
    assert "toolization_decision 使用 repeated_workflow、domain_construction_or_probe" in text
    assert "behavior_or_invariant_checker" in text
    assert "reproducer_fixture_or_case_generator" in text
    assert "parser_trace_or_comparator" in text
    assert "peer_setup_or_feedback_reduction" in text
    assert "candidate 自己编写并纳入最终交付的正式回归测试" in text
    assert "测试框架、test_ 文件名、属于正式测试或验证同一目标行为" in text
    assert "candidate 产品实现、冻结 verifier/runner/grader、隐藏答案/评分逻辑" in text
    assert "正式回归测试可发布" in text
    assert "no_toolizable_material 表示累计回顾后确实没有工具" in text
    assert "catalog 尚无 family 时拒绝" in text
    assert "toolization_decision：staged 至少包含一个当前 capability-oriented signal" in text
    assert "toolization_review_missing、toolization_stage_missing 或 toolization_decision_mismatch" in text
    assert "初始创建并实际并行工作的候选 Agent 数量" in text
    assert "标准流程令 requested_k 等于 max_parallel" in text
    freeze_schema = text.split("search_freeze_spec: Type.Object", 1)[1].split(
        "search_create: Type.Object", 1
    )[0]
    assert "spec: LooseObject" not in freeze_schema
    create_schema = text.split("search_create: Type.Object", 1)[1].split(
        "search_status: Type.Object", 1
    )[0]
    assert "Type.Null()" in create_schema
    select_schema = text.split("search_select: Type.Object", 1)[1].split(
        "search_report: Type.Object", 1
    )[0]
    assert "run_id: Type.String()" in select_schema
    assert "strategy" not in select_schema
    annotator_schema = text.split("const EvidenceAnnotator = Type.Object", 1)[
        1
    ].split("const ModelSpec = Type.Object", 1)[0]
    assert "pi_provider: Type.Optional(NullableString)" in annotator_schema
    assert "goal_plus_monitor_snapshot: Type.Object" in text
    assert 'Type.Literal("bind")' in text
    assert "goal_plus_work_item" in text
    assert "sessionId = String(workItem.agent_id)" in text
    assert "feature_plugins: Type.Optional" in text
    assert 'native_operation: "pi_goal_plus_run_work_item"' in text
    assert 'native_operation: "goal-plus-pi-worker"' in text
    assert "orchestration_monitor" in text
    assert "search_get_agent_observability: Type.Object" in text
    assert "goal_plus_create: Type.Object" in text
    assert "goal_plus_update_goal: Type.Object" in text
    assert "goal_plus_prepare_final_check: Type.Object" in text
    assert "goal_plus_submit_final_check: Type.Object" in text
    assert "pi_goal_plus_run_final_check: Type.Object" in text
    assert "worker_budget: Type.Optional(WorkerBudget)" in text
    assert "worker_budgets: Type.Optional(Type.Record(Type.String(), WorkerBudget))" in text
    assert "candidate_ids: Type.Optional(Type.Array(Type.String()))" in text
    assert "max_parallel: Type.Optional(PositiveInteger)" in text
    assert "pi_search_pool_open: Type.Object" in text
    assert "pi_search_pool_submit: Type.Object" not in text
    assert "pi_search_pool_wait_any: Type.Object" in text
    assert "pi_search_pool_snapshot: Type.Object" in text
    assert "pi_search_pool_continue: Type.Object" in text
    assert "pi_search_pool_close: Type.Object" in text
    assert "runtime_multiplier" not in text

    main_tools = text.split("const mainTools = [", 1)[1].split("];", 1)[0]
    assert '"search_start_agent_session"' not in main_tools
    assert '"search_bind_agent_handle"' not in main_tools
    assert '"search_continue_agent_session"' not in main_tools
    assert '"pi_search_pool_wait_any"' in main_tools
    assert '"search_invalidate_run"' in main_tools
    assert '"pi_search_pool_continue"' in main_tools
    assert '"search_get_agent_observability"' in main_tools
    worker_tools = text.split("const workerTools = [", 1)[1].split("];", 1)[0]
    assert '"search_stage_shared_tool"' in worker_tools
    assert "final_verify: Type.Optional(Type.Boolean())" in text
    assert "triage: GoalPlusTriage" in text
    assert "is_optimization: Type.Boolean()" in text
    assert 'Type.Literal("spec_discovery")' in text
    assert "isEnvironmentFailure" in text
    assert "ModuleNotFoundError" in text
    assert "INSTALL_HINT" in text


def test_pi_assets_use_open_posthoc_evaluation_as_non_gating_feedback() -> None:
    skill = (ROOT / ".pi" / "skills" / "goal-plus" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "软 rubric 或预设评价维度" in skill
    assert "开放式补充评价发生在每次 Evidence 结算之后" in skill
    assert "不来自\nFrozenSpec" in skill
    assert "动态比较" in skill
    assert "不改变结算、硬 score 或最终 PASS/FAIL" in skill


def test_pi_docs_record_runner_logs_and_native_stop_gate() -> None:
    pi_doc = (ROOT / "docs" / "pi.md").read_text(encoding="utf-8")
    adapters = (ROOT / "docs" / "agent-host-adapters.md").read_text(encoding="utf-8")
    debug = (ROOT / "docs" / "debugging-runtime.md").read_text(encoding="utf-8")
    examples = (ROOT / "examples" / "README.md").read_text(encoding="utf-8")

    combined = "\n".join([pi_doc, adapters, debug, examples])
    normalized = " ".join(combined.split())
    assert "worker_host=\"pi-rpc\"" in combined
    assert "goal-plus-pi-worker" in combined
    assert "goal-plus-pi-tool" in combined
    assert "pi_search_run_batch" not in combined
    assert "GOAL_PLUS_PI_EXPOSE_LOW_LEVEL_WORKER=1" not in combined
    assert "pi_search_run_candidate" not in combined
    assert "goal_plus_monitor_snapshot" in combined
    assert "search_get_agent_observability" in combined
    assert "只读" in combined
    assert "一个面向用户的 `goal-plus` skill" in combined
    assert "不暴露独立的用户级 `search` skill" in combined
    assert "pi_search_pool_open" in combined
    assert "pi_search_pool_continue" in combined
    assert "Pi 与 Codex 的差异" in combined
    assert "Pi 当前只支持以下可移植内置策略" in combined
    assert "在模型运行前创建或授权 Goal Plus" in combined
    assert "pi -p" in combined
    assert "--session-dir" in combined
    assert ".gp/host-sessions/pi/" in combined
    assert "只含元数据" in combined
    assert "跨进程恢复同一 native session" in normalized
    assert "session_jsonl_restart" not in combined
    assert ".gp/host-logs/pi-rpc-" in combined
    assert "只含元数据的 event log" in combined
    assert "GOAL_PLUS_PI_RAW_LOG=1" in combined
    assert "原生 turn-level stop gate" in combined
    assert "Goal Plus 完成卡" in combined
    assert "custom entry" in combined
    assert "main+worker 合计用量" in combined
    assert "不再额外发送一份重复的统计通知" in combined
    assert "goal-plus-runtime-start" in combined
    assert "project_trusted=true" in combined
    assert "launcher-process-user" in combined
    assert "host-policy-unreported" in combined
    assert "metadata.runtime_start" in combined
    assert "不会触发另一个 assistant turn" in combined
    assert "不是 host process 级 Stop hook" in normalized


def test_pi_goal_plus_skill_documents_multiple_search_tasks_and_monitoring() -> None:
    text = (ROOT / ".pi" / "skills" / "goal-plus" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    extension = (ROOT / ".pi" / "extensions" / "goal-plus.ts").read_text(
        encoding="utf-8"
    )

    assert "同一个 `goal_plus_id`" in text
    assert "`search_tasks` 是其" in text
    assert "仅追加的 Search 任务历史" in text
    assert "每项任务的规划/已启动" in text
    assert "聚合任务" in text
    assert "search_tasks?: unknown[]" in extension
    assert "search_tasks_total?: number" in extension
    assert "status.search_tasks_total" in extension


def test_pi_goal_plus_reassesses_spec_after_real_result() -> None:
    skill = (ROOT / ".pi" / "skills" / "goal-plus" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    prompt = (ROOT / ".pi" / "prompts" / "goal-plus.md").read_text(
        encoding="utf-8"
    )
    combined = "\n".join([skill, prompt])
    flattened_skill = " ".join(skill.split())

    assert "首次获得有意义的优化结果后" in combined
    assert "相对提升很大" in combined
    assert "绝对目标" in skill
    assert "验收阈值" in skill
    assert "成功标准" in flattened_skill
    assert "更深的结构优化" in skill
    assert "`upgrade_spec`" in skill
    assert "`keep_spec_with_justification`" in skill
    assert "`revise_goal`" in skill
    assert "goal_plus_update_goal" in skill
    assert "不是新的运行时状态" in skill
    assert "不是新的运行时阶段" in prompt
    assert "不得调用 `goal_plus_create`" in prompt
    assert "不得调用 `goal_plus_update_goal`" in prompt
