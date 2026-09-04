# Codex

Codex 是 Goal Plus 的原生 multi-agent host。它通过共享 MCP runtime 管理 Goal/Search 状态，通过
Codex collaboration tool 启动固定数量的并行 candidate loop。

## 安装与快速开始

### 正常安装

在当前 Muyuan checkout 的 Goal Plus 目录中运行统一安装器：

```bash
cd <Muyuan checkout>/plugins/goal-plus
./install.sh --codex
```

安装器使用当前 shell 的 `python3`。先激活 Codex 启动时要继承的 Python 环境；也可以通过
`PYTHON=/absolute/path/to/python` 指定其中的解释器，但该环境的 scripts 目录必须已经位于
`PATH`。如果 `goal-plus` 或 Pi runtime 命令解析到另一 Python 环境，安装器会在写入前停止。
检查通过后，它安装 runtime、注册当前 checkout 作为 `goal-plus` marketplace，并安装
`goal-plus@goal-plus`。安装期间需要保留 source directory。

安装前会读取 Codex plugin 和 marketplace inventory。来自其他 checkout 的同名安装，或已退休的
`goal-plus@goal-plus-local` staging 安装都会被视为冲突并停止；安装器不会静默覆盖。

安装完成后启动一个新的 Codex thread，通过 `/hooks` 检查并信任 Goal Plus handler，再开启 fresh
thread，使全部 handler 从 `SessionStart` 起生效。进入目标 workspace 后检查：

```bash
codex plugin list
goal-plus-codex-doctor --project-root .
```

卸载 Codex 集成：

```bash
codex plugin remove goal-plus@goal-plus
codex plugin marketplace remove goal-plus
```

只有在其他 Pi 或 Codex 安装都不再使用该 Python 环境中的 Goal Plus 命令时，才运行
`python -m pip uninstall -y goal-plus`。

### Checkout 更新

Codex 不再提供单独的 `goal-plus-local` staging 开发安装。开发或更新当前 checkout 后，重新运行
正常安装命令更新 Python runtime 和 Codex 缓存中的 plugin assets，再开启新的 Codex thread：

```bash
cd <Muyuan checkout>/plugins/goal-plus
./install.sh --codex
```

即使插件已经安装，安装器也会重新执行 `codex plugin add goal-plus@goal-plus` 刷新缓存；
只更新 Python runtime 或 marketplace source 不会更新运行中的 hooks/skills。

统一安装器不会修改 `CODEX_HOME`，也不会为多个 checkout 创建隐式 profile。同一个 Codex 配置中
只保留一个 `goal-plus@goal-plus` 来源；多 checkout 并行开发需要由开发者显式使用独立账号或
独立、完整的 Codex 配置边界，不能由本安装器切换。

可安装的 Codex surface 包含：

- `skills/`：三个 Codex skill；
- `hooks/hooks.json`：七个 lifecycle hook，包括用于持久化用户暂停的 `Interrupt`；
- `.mcp.json`：共享的 `goal-plus --root .gp` server；
- `.codex-plugin/plugin.json`：plugin metadata。

plugin manifest 不发布 `.codex/agents/` custom role。Search candidate 会由 `CodexAdapter` 映射到
Codex 内置 `default` role，完整 worker contract 随 launch message 传递。required final check
仍优先使用 project-local `goal_plus_final_checker`；当前 `spawn_agent` schema 不暴露该 role 时，
使用 `fallback_agent_type="default"`，并携带同样独立的 prompt、`fork_turns="none"`、task identity
和一次必需的 `goal_plus_submit_final_check` 调用。

Codex 会从约定路径 `hooks/hooks.json` 发现 hook，因此 manifest 不声明 `hooks` 字段。hook trust
绑定规范化后的当前 hash，hook 更新后必须重新检查并信任。

正常 plugin 的 `.mcp.json` 使用固定 server 名 `goal-plus`，等价的 Codex 配置键是：

```toml
[mcp_servers.goal-plus]
command = "goal-plus"
args = ["--root", ".gp"]
```

从准备运行任务的 workspace 执行只读 preflight：

```bash
goal-plus-codex-doctor --project-root .
```

doctor 会记录 Codex 版本供诊断，不按版本号设置最低门槛。它检查 Codex 与 `goal-plus`
executable、Python source provenance、plugin/skill
加载、MCP 启用状态、hook path 与 trust hash、内置/custom agent role 覆盖范围，以及只读 Codex model
catalog。required check 失败时命令返回非零，支持 `--json`；安装、更新或修改 trust 后都应使用新
thread。`--skip-model-check` 只适合离线排障，正式 multi-model preflight 不应跳过该项。

如果升级前仍装有旧 staging 版本，先显式删除旧注册：

```bash
codex plugin remove goal-plus@goal-plus-local
codex plugin marketplace remove goal-plus-local
```

确认旧目录确实属于已退休的 Goal Plus installer 后，可以再手动删除
`${CODEX_HOME:-~/.codex}/local-marketplaces/goal-plus`；统一安装器不会替用户删除该目录。

完成正常安装后，可直接发起任务：

```text
$goal-plus Fix this bug and verify the test suite.
$goal-plus mode=probe Check whether vectorization is viable.
$goal-plus mode=autonomous Deeply optimize the kernel.
```

`mode=autonomous` 是默认模式，适合持续优化；`mode=probe` 用于较短的 feasibility/potential/blocker
探测。该选择只会规范化到 `raw_goal` 最后一行，不是 scheduler state。

## Host 集成与所有权

installed plugin 通过 `hooks/hooks.json` 提供 Goal Plus host hook。开发源码中的
`.codex/hooks.example.json` 保持同一份声明，供资产一致性测试使用，不是另一条安装路径。
`UserPromptSubmit`、`SessionStart`、`Interrupt`、`PreToolUse`、`PostToolUse`、`Stop` 和
`SubagentStop` 都调用：

```text
goal-plus --goal-plus-host-hook
```

首次使用或更新 hook 后，应通过 `/hooks` 检查并信任它们。hook 负责预创建 `$goal-plus`、恢复
active record、限制 write/Search 调用、绑定 session 所有权，并保证 candidate
在自己的 verifier 提交持久化前会被阻止结束。`UserPromptSubmit` 和 `SessionStart` 会在绑定
Goal Plus 前识别 child：普通 subagent 不接收 Main context，Search candidate 仅在 initial
或 fresh launch 时接收紧凑的 candidate context，同一 session continuation 不重复注入。
Selection、promotion 和 final audit 始终由 parent 所有。

只有大小写精确的 `$goal-plus`、`$goal-plus edit` 和
`$goal-plus-with-final-check` 会授权 Goal Plus。`/goal-plus`、自然语言请求、skill 自动加载和
MCP/CLI 直调都不能创建记录。`goal_plus_create`/`goal_plus_update_goal` 是 deny-only tombstone，
不再存在 `PostToolUse(goal_plus_create)` 所有权 fallback。

使用 `codex resume <session-id>` 恢复交互式 session，`SessionStart` 只恢复上下文。
明确中断后的任务必须用 `$goal-plus resume` 重启 Main；普通聊天不解除 control=paused。
无交互写法为 `codex exec resume <session-id> '$goal-plus resume'`。
`$goal-plus` 查询摘要；pause 禁止后续续跑/新派发，不强杀当前 turn；clear 撤权且保留历史。
正常运行中收到新思考会先处理再继续，不要求 resume。不同 session、fork、detached/stale
绑定和终态 Goal 都不会继承 continuation 权限。未授权 session 对 Goal Plus/Search 变更的
`PreToolUse` 检查 fail closed；只有显示和只读监控故障可以 fail open。详见
[运行逻辑](runtime-logic.md)。控制命令按宿主交付时机生效，不承诺原生 `/goal` 的无模型 UI。
普通 subagent 不继承 parent 的 next action。

Candidate 的初始 `UserPromptSubmit` 只注入 runtime 绑定的 role 与
`agent_session_id`；完整工作说明已包含在 host launch message，并由
`search_get_agent_context` 返回权威合同。Hook 不重复权限、workspace 或交接文案。
Codex native subagent 仍会按宿主规则自动加载适用的 `AGENTS.md`；当前没有关闭该行为的
subagent launch 字段，Goal Plus 不再用额外 prompt 抵消它。

顶层 `Stop` 遇到 active Goal Plus record 时，会重新提供完整 raw goal、elapsed time、phase、next
action 和 final-check policy，要求 main agent 继续或记录终态。candidate 的 `SubagentStop` 只检查
candidate 自己的 verifier 提交，不会要求它执行 parent-only selection/report/promotion。只有解析到
Goal Plus Goal、Search 或 session context 的自动 Stop/SubagentStop 调用才会把 hook event 写入
`.gp/host-logs/codex-hook-events/`，供最终报告和排障使用。

Goal Plus session 结束后可以调用 `search_report` 生成报告。

Codex 与 runtime 的职责边界见 [Agent Host Adapters](agent-host-adapters.md)，Evidence 和 Tool View
生命周期见[共享平面](shared-plane.md#global-evidence)。

## 普通 Goal Mode

普通任务使用 [`goal-plus-ultra-v1`](ultra.md) 轻量 dispatch 记录，而不是 Search candidate pool。
Main 自行拆解任务；只在实际调用 collaboration tool 时记录 attempt。Codex 通过
`spawn_agent`、`wait_agent`、`followup_task` 等原生操作执行，main 负责 acceptance、rework、
integration 和 completion。

Ultra 的逻辑要求是 main reasoning effort `max`。Codex host 应在第一个 main turn 启动前选择 `max`，
并记录 `native_reasoning_effort=max`；project hook 只能记录和 gate，不能修改已经开始的首轮
reasoning effort。

只有任务具有 numeric metric、deterministic verifier、isolated edit surface、多个有价值
hypothesis 和足够 budget 时，main 才进入 Search Mode。普通 subagent 记录与后文的 Search
candidate lifecycle 相互独立。

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

main agent 将 payload 投影到当前 tool schema。`task_name`、`message` 和 fork 字段是稳定字段；
`agent_type="default"` 使用 Codex 内置的无配置角色，使 child 正常继承 parent model。
`strategy.worker_launch` 中的可选 override 只有在 launch payload 和当前 tool schema
都支持时才能传递。

`spawn_agent` 没有 `cwd`，所以 launch 时不会把 child 切换到 candidate worktree。child 必须先调用
`search_get_agent_context`，然后把 `context.candidate_task.workspace` 显式用于每次文件、编辑和
shell 操作。
任务本地 Skill 无需配置：runtime 只发现 source Git 已跟踪的 `.agents/skills/<name>/SKILL.md`，
并要求该 Skill 目录内所有文件都由 Git 跟踪；在 freeze 的 `task_skill_notice` 和 launch message
中明确列出，并通过 `context.candidate_task.task_skill_paths` 返回当前 worktree 的绝对路径。child
完整读取清单中的 Skill，不扫描 parent/global/plugin 或其他 candidate 路径。

标准 `parallel_loops` 并行循环如下：

1. freeze `strategy.orchestration_mode="parallel_loops"`，只规划一次并启动全部初始 candidate；
2. 对每个 `search_start_agent_session` 返回的 payload 真实调用 `spawn_agent`；该 session
   allocation 本身不是 worker launch；
3. 只在 spawn 成功并返回非空 thread id 后，用 `search_bind_agent_handle` 绑定该真实
   `external_id`；预分配 task name 不能作为启动证据；
4. 只有 live receiver 集合非空时才调用不带 target 的 `wait_agent`，每次唤醒后用 `list_agents`
   收集新终态；
5. 对每个完成的 candidate 结算准确 verifier Evidence；
6. 如果全局停止条件尚未满足，通过 `search_continue_agent_session` 和 `followup_task` 继续同一
   worker；
7. selection 前 drain 或 interrupt 所有活跃 agent。

每个 child 只负责一条自主 candidate loop：读取自己的 context 和 Global Evidence View，选择
hypothesis/pivot/rebase，只编辑自己的 workspace，并在每次真实尝试后调用 verifier。`view=null`
只表示 annotator 尚未发布，不要求等待。candidate 不能创建其他 candidate，也不能 select、report、
promote 或修改 Goal Plus 状态。main 不会因为低分或单次没有提升而替换它。

## 模型选择

开头连续的 `key=value` 会被 Codex host hook 持久化为 typed `command_config`；第一个非配置
token 开始就是目标。显式配置会在 ready SearchSpec draft 保存时机械校验，缺省字段才由
Codex main 判断。完整 key 列表见 [API](api.md#host-command-config)。

Codex 可以分别指定 annotator 和 worker 模型：

```text
$goal-plus max_parallel=4 annotator=gpt-5.6-terra workers=gpt-5.6-terra,gpt-5.6-sol optimize ...
```

已经运行的 Codex session 就是 main agent，`$goal-plus` 不能切换它的模型。应在启动时使用
`codex -m MODEL` 或 `codex exec -m MODEL ...`，也可以在提交 Goal Plus prompt 前使用 `/model`。
`main=` 不是受支持的 Goal Plus 命令配置；新命令填写它会在创建记录前失败。

`annotator=` 在同一 Codex host 上解析单个模型，并冻结到 `strategy.evidence_annotator.model`。
`workers=` 将一个或多个模型解析到 `strategy.models`。`models=` 是 `workers=` 的兼容别名，
两者同时出现会报错。省略某个 role 时保留其正常 host default 或继承行为，不从另一个显式 role 推导。

`max_parallel=4 workers=A,B` 按 A、B、A、B round-robin；
`max_parallel=4 workers=A*1,B*3` 是显式 A1B3。Goal Plus 通过
`goal_plus_list_models(host="codex")` 解析
annotator 和 worker 名称，并在 freeze 时保存有序 `selected_models`。candidate 的 worker 模型在后续
continuation 中保持不变。省略 `workers=` 和 `models=` 时，child 继承正常 parent/default model。

model catalog 只负责发现。若当前 `spawn_agent` schema 无法接收所选 model override，
启动会明确失败。

`adaptive_search` run 要求显式配置 `search_scheduler`，保持相同初始步骤，但 verifier 结算后可能返回 runtime
拥有的 `allocation_decision`。main 不再继续已退休 worker，而是幂等应用准确 decision，把返回的
派生 session launch payload 映射到 `spawn_agent`，并确保活跃数不超过 `max_parallel`。child
从 decision 指定的 verifier-backed Git settlement 启动，并继承 run Evidence 和模型
provenance；reward 不改变最终 hard-score selection。

## Worker Deadline

Codex 的 `spawn_agent` 没有 step 或 timeout 字段，因此 `worker_budget.max_runtime_seconds` 必填。
Adapter 会返回两阶段 parent watchdog：

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

parent 先等待到 soft closeout，发送一次收尾消息，再等待 final window；只有第二次超时才调用
interrupt。两段原生等待各自至少为 10 秒，因此 Codex worker budget 至少为 20 秒。
`max_turns` 只是提示，不是 Codex 强制上限。每次 continuation 可以使用新的 dispatch budget，
不修改 frozen spec。

持续 AutoResearch 可配置 `min_runtime_seconds`，并可选配置 `min_verifier_runs`：

```json
{
  "min_runtime_seconds": 300,
  "min_verifier_runs": 1,
  "max_runtime_seconds": 420,
  "on_exceed": "interrupt"
}
```

`SubagentStop` hook 会在同一 worker turn 内续跑过早结束的 child，直到 minimum lease 释放。示例在
375 秒开始 parent closeout，420 秒硬 interrupt。worker 必须继续 verifier-backed 研究，不能用 sleep
消耗时间；`VerifierWorkspaceSideEffect` 或 `candidate_action=stop_and_report` 会提前释放 lease，
让基础设施故障立即返回 parent。

如果剩余时间已经短于观测到的 verifier 提交耗时，`PostToolUse` 可能注入一次 advisory，但不会停止
worker。

## 续跑与恢复

Codex worker 仍然存在时，始终优先 native continuation：

```text
search_continue_agent_session -> followup_task
```

follow-up 保持中性，只要求沿用已加载的 runtime context 和 Evidence 继续自主循环，不指定技术方向。

worker 已消失或明确需要 fresh context 时，调用 `search_redispatch_candidate`。新 session
继续使用同一 workspace、Git state、verifier history 和 `.tmp/handoff.json`。这是一条恢复路径，
不是正常迭代方式。

## Observability 与日志

`search_get_agent_observability(agent_session_id)` 返回单个 worker 的归一化 model、reasoning
effort、active/wall duration、terminal state、token/context、tool/message count 和 session
artifact path，不包含 prompt、reasoning 或 tool payload 内容。

普通 Ultra work 可以在 `goal_plus_monitor_snapshot` 中启用 `orchestration` feature plugin。它把
work item、assignment/result/decision 和 Codex 原生 operation 投影为只读视图，但不会启动、
等待或控制任何 thread。详见 [Monitor Feature Plugins](feature-plugins.md)。

Codex 优先读取 `SubagentStop.agent_transcript_path`；缺失时通过唯一 `task_name` 查找
`${CODEX_HOME:-~/.codex}/sessions/...` JSONL。即使 interrupted worker 没有发出 `SubagentStop`，
也可以通过 task name 发现。

需要原始 event 时使用：

```bash
codex exec --json ... > .gp/host-logs/codex-run.jsonl
RUST_LOG=debug codex -c log_dir=./.codex-log
```

持久 transcript 位于 `${CODEX_HOME:-~/.codex}/sessions/YYYY/MM/DD/rollout-*.jsonl`。
`codex exec -o` 只包含最终消息，不能代替 JSON event stream 或 rollout log。完整诊断流程见
[调试运行时状态](debugging-runtime.md)。

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

`codex_parallel_loop_cycle` 验证单次初始 plan、wait-any、同 worker follow-up、verifier-backed
best、drain、selection 和 promotion。Goal Plus report 只在 parent record 进入终态后生成一次。
