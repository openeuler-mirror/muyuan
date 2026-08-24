# Codex

Codex 是 Goal Plus 的原生 multi-agent host。它通过共享 MCP runtime 管理 Goal/Search 状态，通过 Codex collaboration tool 启动固定数量的并行 candidate loop。

## 安装与快速开始

### 用户安装

在当前 Muyuan checkout 的 Goal Plus 目录中，通过各自的 package manager 安装 runtime 和 Codex plugin：

```bash
cd <Muyuan checkout>/plugins/goal-plus
python -m pip install .
codex plugin marketplace add .
codex plugin add goal-plus@goal-plus
```

Python 环境可以自行选择，但其中的 `goal-plus` executable 必须出现在 Codex 继承的 `PATH` 中；如果目标环境已经安装正确版本，可以省略 pip 命令。安装期间需要保留 source directory，因为它仍是已配置的 local marketplace source。

安装完成后启动一个新的 Codex thread，通过 `/hooks` 检查并信任 Goal Plus handler，再开启 fresh thread，使全部 handler 从 `SessionStart` 起生效。进入目标 workspace 后检查：

```bash
codex plugin list
goal-plus-codex-doctor --project-root .
```

卸载 Codex 集成：

```bash
codex plugin remove goal-plus@goal-plus
codex plugin marketplace remove goal-plus
```

只有在其他 Pi 或 Codex 安装都不再使用该 Python 环境中的 Goal Plus 命令时，才运行 `python -m pip uninstall -y goal-plus`。

### Checkout 开发安装

```bash
cd <Muyuan checkout>/plugins/goal-plus
python -m pip install -e ".[dev]"
python scripts/install_codex_plugin.py --skip-runtime-install
```

这会把 Python runtime editable 安装到当前 interpreter，并把可发布的 Codex asset 放到持久目录 `${CODEX_HOME:-~/.codex}/local-marketplaces/goal-plus`；installer 会按需注册 `goal-plus-local` marketplace，安装或刷新 `goal-plus@goal-plus-local`。staging manifest 会获得一个 Codex cachebuster 后缀，因此 pull 新代码后重跑同一命令即可刷新，无需修改仓库中的 package version。

只有 `goal-plus` 命令已经来自目标 source 时才使用 `--skip-runtime-install`。installer 仅在 marketplace metadata 能证明它拥有 `goal-plus-local` 时替换 staging plugin，不会覆盖不受它管理的目标。

可安装的 Codex surface 包含：

- `skills/`：三个 Codex skill；
- `hooks/hooks.json`：Codex 0.144.1 及以上版本发现的六个 lifecycle hook；
- `.mcp.json`：共享的 `goal-plus --root .gp` server；
- `.codex-plugin/plugin.json`：plugin metadata。

plugin manifest 不发布 `.codex/agents/` custom role。Search candidate 会由 `CodexAdapter` 映射到 Codex 内置 `default` role，完整 worker contract 随 launch message 传递。required final check 仍优先使用 project-local `goal_plus_final_checker`；当前 `spawn_agent` schema 不暴露该 role 时，使用 `fallback_agent_type="default"`，并携带同样独立的 prompt、`fork_turns="none"`、task identity 和一次必需的 `goal_plus_submit_final_check` 调用。

Codex 会从约定路径 `hooks/hooks.json` 发现 hook，因此 manifest 不声明 `hooks` 字段。hook trust 绑定规范化后的当前 hash，hook 更新后必须重新检查并信任。

从准备运行任务的 workspace 执行只读 preflight：

```bash
goal-plus-codex-doctor --project-root .
```

doctor 会检查 Codex 0.144.1+、`goal-plus` executable 及 Python source provenance、plugin/skill 加载、MCP 启用状态、hook path 与 trust hash、内置/custom agent role 覆盖范围，以及只读 Codex model catalog。required check 失败时命令返回非零，支持 `--json`；安装、更新或修改 trust 后都应使用新 thread。`--skip-model-check` 只适合离线排障，正式 multi-model preflight 不应跳过该项。

移除受 installer 管理的开发安装：

```bash
python scripts/install_codex_plugin.py --uninstall --keep-runtime
```

如果 Codex 是唯一使用该 checkout editable runtime 的开发 host，可以省略 `--keep-runtime`，一并卸载 runtime。

### Project-local asset 加载

这是未安装 plugin 时的 opt-in 开发 fallback。启用 `goal-plus@...` 的同时不要使用它，否则 Codex 会从两处加载同一组 Goal Plus hook。

```bash
cd <Muyuan checkout>/plugins/goal-plus
python -m pip install -e ".[dev]"
cp .codex/config.example.toml .codex/config.toml
cp .codex/hooks.example.json .codex/hooks.json
```

在该目录启动 Codex，通过 `/hooks` 检查复制出的 `.codex/hooks.json`，然后使用新 thread。重新启用 installed plugin 前删除这两个复制文件。project-local `.codex/skills/` 是开发 source of truth，顶层 `skills/` 是 plugin 发布面；修改 Codex skill 后同步并检查两份 asset：

```bash
python scripts/sync_codex_plugin_assets.py
python scripts/sync_codex_plugin_assets.py --check
```

本地配置中的 MCP server 如下：

```toml
[mcp_servers.goal-plus]
command = "goal-plus"
args = ["--root", ".gp"]
startup_timeout_sec = 10
tool_timeout_sec = 300
enabled = true
default_tools_approval_mode = "approve"
```

`.codex/config.toml` 是本地文件，不应提交。配置中不要设置 MCP `cwd`，由 `codex -C` 同时决定项目根目录、MCP 状态目录和 hook 作用范围。示例配置已经转发 annotator 所需的命名环境变量；如果 `GOAL_PLUS_EVIDENCE_ANNOTATOR_API_KEY_ENV` 指向自定义变量，也要把该变量加入 `env_vars`。

`default_tools_approval_mode="approve"` 让非交互 Ultra run 可以连续推进 Goal Plus state transition，而不用为每次 MCP 调用暂停。它不放宽 filesystem 或 shell 权限；这些操作仍由 Codex sandbox 和 approval policy 管理。

完成上述任一安装后，可直接发起任务：

```text
$goal-plus Fix this bug and verify the test suite.
$goal-plus mode=probe Check whether vectorization is viable.
$goal-plus mode=autonomous Deeply optimize the kernel.
```

`mode=autonomous` 是默认模式，适合持续优化；`mode=probe` 用于较短的 feasibility/potential/blocker 探测。该选择只会规范化到 `raw_goal` 最后一行，不是 scheduler state。

## Host 集成与所有权

installed plugin 通过 `hooks/hooks.json` 提供 Goal Plus host hook；project-local fallback 从 `.codex/hooks.example.json` 复制出同一份声明。`UserPromptSubmit`、`SessionStart`、`PreToolUse`、`PostToolUse`、`Stop` 和 `SubagentStop` 都调用：

```text
goal-plus --goal-plus-host-hook
```

首次使用或更新 hook 后，应通过 `/hooks` 检查并信任它们。hook 负责预创建 `$goal-plus`、恢复 active record、限制 write/Search 调用、绑定 session 所有权，并保证 candidate 在自己的 verifier 提交持久化前会被阻止结束。Selection、promotion 和 final audit 始终由 parent 所有。

`PostToolUse(goal_plus_create)` 是所有权绑定的 fallback。普通 subagent 不继承 parent 的 next action。

顶层 `Stop` 遇到 active Goal Plus record 时，会重新提供完整 raw goal、elapsed time、phase、next action 和 final-check policy，要求 main agent 继续或记录终态。candidate 的 `SubagentStop` 只检查 candidate 自己的 verifier 提交，不会要求它执行 parent-only selection/report/promotion。只有解析到 Goal Plus Goal、Search 或 session context 的自动 Stop/SubagentStop 调用才会把 hook event 写入 `.gp/host-logs/codex-hook-events/`，供最终报告和排障使用。

Codex 与 runtime 的职责边界见 [Agent Host Adapters](agent-host-adapters.md)，Evidence 和 Tool View 生命周期见[共享平面](shared-plane.md#global-evidence)。

## 普通 Goal Mode

普通任务使用 [`goal-plus-ultra-v1`](ultra.md) work DAG，而不是 Search candidate pool。runtime 记录 work item、依赖、attempt 和 Evidence-backed transition；Codex 通过 collaboration tool 执行 `spawn_agent`、`wait_agent`、`followup_task` 等原生操作，main 负责 acceptance、rework、integration 和 completion。

Ultra 的逻辑要求是 main reasoning effort `max`。Codex host 应在第一个 main turn 启动前选择 `max`，并记录 `native_reasoning_effort=max`；project hook 只能记录和 gate，不能修改已经开始的首轮 reasoning effort。

只有 work item 具有 numeric metric、deterministic verifier、isolated edit surface、多个有价值 hypothesis 和足够 budget 时，main 才把它 route 到 Search Mode。普通 subagent work 与后文的 Search candidate lifecycle 相互独立。

## Search 并行循环

完成一次初始 candidate planning 后，`search_start_agent_session` 返回 host 原生 launch payload：

```json
{
  "tool": "spawn_agent",
  "task_name": "search_agent_001",
  "agent_type": "default",
  "fork_turns": "none",
  "message": "agent_session_id=agent_001; candidate_id=c001; idea: ..."
}
```

main agent 将 payload 投影到当前 tool schema。`task_name`、`message` 和 fork 字段是稳定字段；`agent_type="default"` 使用 Codex 内置的无配置角色，使 child 正常继承 parent model。`strategy.worker_launch` 中的可选 override 只有在 launch payload 和当前 tool schema 都支持时才能传递。

`spawn_agent` 没有 `cwd`，所以 launch 时不会把 child 切换到 candidate worktree。child
必须先调用 `search_get_agent_context`，然后把 `context.workspace` 显式用于每次文件、编辑
和 shell 操作。任务本地 Skill 无需配置：runtime 只发现 source Git 已跟踪的
`.agents/skills/<name>/SKILL.md`，并要求该 Skill 目录内所有文件都由 Git 跟踪；在 freeze 的
`task_skill_notice` 和 launch message 中明确
列出，并通过 `context.candidate_task.task_skill_paths` 返回当前 worktree 的绝对路径。
child 完整读取清单中的 Skill，不扫描 parent/global/plugin 或其他 candidate 路径。

标准并行循环如下：

1. freeze `strategy.orchestration_mode="parallel_loops"`，只规划一次并启动全部初始 candidate；
2. 用 `search_bind_agent_handle` 绑定 Codex handle；
3. 调用不带 target 的 `wait_agent`，每次唤醒后用 `list_agents` 收集新终态；
4. 对每个完成的 candidate 结算准确 verifier Evidence；
5. 如果全局停止条件尚未满足，通过 `search_continue_agent_session` 和 `followup_task` 继续同一 worker；
6. selection 前 drain 或 interrupt 所有活跃 agent。

每个 child 只负责一条自主 candidate loop：读取自己的 context 和 Global Evidence View，选择 hypothesis/pivot/rebase，只编辑自己的 workspace，并在每次真实尝试后调用 verifier。`view=null` 只表示 annotator 尚未发布，不要求等待。candidate 不能创建其他 candidate，也不能 select、report、promote 或修改 Goal Plus 状态。main 不会因为低分或单次没有提升而替换它。

## 模型选择

Codex 可以分别指定 annotator 和 worker 模型：

```text
$goal-plus annotator=gpt-5.6-terra workers=gpt-5.6-terra,gpt-5.6-sol max_parallel=4 optimize ...
```

已经运行的 Codex session 就是 main agent，`$goal-plus` 不能切换它的模型。应在启动时使用 `codex -m MODEL` 或 `codex exec -m MODEL ...`，也可以在提交 Goal Plus prompt 前使用 `/model`。正常使用应省略 `main=`；为兼容旧命令，`main=` 只作为当前 session model 的可选 assertion：`UserPromptSubmit` 会校验并在不匹配时阻止执行，但不会把它当成 selector。

`annotator=` 在同一 Codex host 上解析单个模型，并冻结到 `strategy.evidence_annotator.model`。`workers=` 将一个或多个模型解析到 `strategy.models`。`models=` 是 `workers=` 的兼容别名，两者同时出现会报错。省略某个 role 时保留其正常 host default 或继承行为，不从另一个显式 role 推导。

`workers=A,B max_parallel=4` 按 A、B、A、B round-robin；`workers=A,B A1B3 max_parallel=4` 是显式 A1B3，`workers=A*1,B*3` 与之等价。Goal Plus 通过 `goal_plus_list_models(host="codex")` 解析 annotator 和 worker 名称，并在 freeze 时保存有序 `selected_models`。candidate 的 worker 模型在后续 continuation 中保持不变。省略 `workers=` 和 `models=` 时，child 继承正常 parent/default model。

model catalog 只负责发现。若当前 `spawn_agent` schema 无法接收所选 model override，启动会明确失败。

## Worker Deadline

Codex 的 `spawn_agent` 没有 step 或 timeout 字段，因此 `worker_budget.max_runtime_seconds` 必填。Adapter 会返回两阶段 parent watchdog：

```json
{
  "budget_control": {
    "initial_wait_timeout_ms": 555000,
    "closeout_message": "Stop new work, final-verify, write the handoff, and return.",
    "final_wait_timeout_ms": 45000,
    "on_exceed": "interrupt"
  }
}
```

parent 先等待到 soft closeout，发送一次收尾消息，再等待 final window；只有第二次超时才调用 interrupt。`max_turns` 只是提示，不是 Codex 强制上限。每次 continuation 可以使用新的 dispatch budget，不修改 frozen spec。

持续 AutoResearch 可配置 `min_runtime_seconds`，并可选配置 `min_verifier_runs`：

```json
{
  "min_runtime_seconds": 300,
  "min_verifier_runs": 1,
  "max_runtime_seconds": 420,
  "on_exceed": "interrupt"
}
```

`SubagentStop` hook 会在同一 worker turn 内续跑过早结束的 child，直到 minimum lease 释放。示例在 375 秒开始 parent closeout，420 秒硬 interrupt。worker 必须继续 verifier-backed 研究，不能用 sleep 消耗时间；`VerifierWorkspaceSideEffect` 或 `candidate_action=stop_and_report` 会提前释放 lease，让基础设施故障立即返回 parent。

如果剩余时间已经短于观测到的 verifier 提交耗时，`PostToolUse` 可能注入一次 advisory，但不会停止 worker。

## 续跑与恢复

Codex worker 仍然存在时，始终优先 native continuation：

```text
search_continue_agent_session -> followup_task
```

follow-up 保持中性，只要求沿用已加载的 runtime context 和 Evidence 继续自主循环，不指定技术方向。

worker 已消失或明确需要 fresh context 时，调用 `search_redispatch_candidate`。新 session 继续使用同一 workspace、Git state、verifier history 和 `.tmp/handoff.json`。这是一条恢复路径，不是正常迭代方式。

## Observability 与日志

`search_get_agent_observability(agent_session_id)` 返回单个 worker 的归一化 model、reasoning effort、active/wall duration、terminal state、token/context、tool/message count 和 session artifact path，不包含 prompt、reasoning 或 tool payload 内容。

普通 Ultra work 可以在 `goal_plus_monitor_snapshot` 中启用 `orchestration` feature plugin。它把 work item、assignment/result/decision 和 Codex 原生 operation 投影为只读视图，但不会启动、等待或控制任何 thread。详见 [Monitor Feature Plugins](feature-plugins.md)。

Codex 优先读取 `SubagentStop.agent_transcript_path`；缺失时通过唯一 `task_name` 查找 `${CODEX_HOME:-~/.codex}/sessions/...` JSONL。即使 interrupted worker 没有发出 `SubagentStop`，也可以通过 task name 发现。

需要原始 event 时使用：

```bash
codex exec --json ... > .gp/host-logs/codex-run.jsonl
RUST_LOG=debug codex -c log_dir=./.codex-log
```

持久 transcript 位于 `${CODEX_HOME:-~/.codex}/sessions/YYYY/MM/DD/rollout-*.jsonl`。`codex exec -o` 只包含最终消息，不能代替 JSON event stream 或 rollout log。完整诊断流程见[调试运行时状态](debugging-runtime.md)。

## 支持范围与验证

Codex 支持以下可移植内置策略：

- `agent_guided`、`agent`、`default`
- `random`、`random_mode`

快速契约测试：

```bash
pytest -m codex -q
```

真实并行循环 smoke test：

```bash
ST_CODEX_MODEL=gpt-5.6-luna \
  pytest -m "st and st_codex" -k codex_parallel_loop_cycle -v -s -rs
```

`codex_parallel_loop_cycle` 验证单次初始 plan、wait-any、同 worker follow-up、verifier-backed best、drain、selection 和 promotion。Goal Plus report 只在 parent record 进入终态后生成一次。
