# Codex

Codex 通过共同 MCP 管理 Goal/Search，并使用共同 session 工具驱动独立的原生 app-server
会话。Main 决定候选的启动、继续和停止；业务授权、Evidence 与发布规则和 Pi 共用。
不发布随包 skill；Main、Search、Adaptive 和最终检查的流程由共同
工具定义、当前状态与角色 context 提供。

当外部 controller 接管 Search 收尾时，设置
`GOAL_PLUS_CONTROLLER_ONLY_CLOSEOUT=1` 可让 Main MCP 隐藏 select、promote、
apply promotion 和 final Search result 工具。controller 通过可信 runtime 的
`select_for_controller()` 提交精确候选；该 API 不进入模型工具目录，原生 promotion
gate 仍然有效。

## Execution Scope

共同 launcher 将 candidate、普通 delegation 和 final checker 的原生进程登记到 Main 的
host-local Execution Scope，登记成功后才放行执行。每次操作核验可信 MCP thread identity、
当前 Goal revision/control、Search 归属和 candidate generation；模型不能指定 caller 身份。

`goal_plus_session_run` 准备、绑定并执行 candidate session；`wait` 读取
准确 `call_id` 的结果。Main 在选择前通过 `interrupt`、`stop` 或 `close` 让自己的候选静默。
调用返回、Evidence 结算与 scope 释放是不同事实；释放不证明系统进程退出。

明确的 aborted/interrupted/cancelled Stop 和 Goal 终态会触发 scope cleanup，
关闭已登记 session 和内部角色资源。普通 active Goal Stop 仍由 continuation gate 处理。
Cleanup 不改写 durable Goal/Search 终态，也不授予 resume；失败时保留准确资源身份并重试。
Scope owner 使用可信 native session；GP 不读取祖先进程、不核验 PID、不启动 guardian。
宿主未发送退出事件时，遗留进程由 agent/Codex 处理。

显式 resume 只在旧 scope 已关闭后创建下一代 scope。该 generation 持久化 resume identity，
同一 Main 的后续操作持续复用；旧 scope 尚未关闭时收到的 resume 不能在稍后关闭后再次授权。

原生 collaboration 的 hook 仍记录 Goal Plus scope 内实际发生的调用、task target 和 wake
身份，供归属核验与清理观察；迟到回执不能关闭新调用或重开已关闭 scope。共同 session 启动器
管理自己启动的 app-server，它不能接管另一个 Codex TUI 的 active turn。

## Candidate 原生写入

Search candidate 的原生 `apply_patch` 在 PreToolUse 使用持久化 session、当前执行代际与
Goal/Search admission 获取只读写入边界。Add/Delete/Update/Move 的全部目标必须位于自己的
workspace；命名路径和解析符号链接后的目标均检查，拒绝 shared hard link、`.git`、
`denied_files` 和冻结 verifier。相对路径使用宿主 hook 的 `cwd`，不猜测为 candidate cwd。
无法识别的 patch transport 拒绝执行；不解析外层 JavaScript 来推断授权。

Workspace 内的 scratch 仍可写，`allowed_files` 继续由 verifier settlement 检查交付范围。
这是原生 patch 的执行前 gate，不是 shell 或内核级文件系统沙箱；并发替换路径和任意 shell
副作用不由该 gate 原子隔离，不能据此声称所有 candidate 文件访问已受宿主沙箱保护。

## 安装与快速开始

### 正常安装

在当前 Muyuan checkout 的 Goal Plus 目录中运行统一安装器：

```bash
cd <Muyuan checkout>/plugins/goal-plus
./install.sh --codex
```

正常安装使用不可变 release 和唯一 `goal-plus@goal-plus` 注册。已有正常来源可在安装器
`[y/N]` 提示中确认替换；旧 `goal-plus-local` staging 需先移除。受管 Python、来源替换、
更新与卸载步骤统一见 [开发指南](development.md#安装模式)。

公共 instruct 只维护一个 [txt](../assets/goal-plus-instructions.txt)：首行概括核心流程，
后续补充说明，安装和使用无需选择长短版本。Codex 延迟工具摘要只取首行、最多 250 个
Unicode 字符；完整插件 namespace 描述则受 1,000 UTF-8 字节限制。因此首行控制在
250 字符内，全文控制在 500 字符且不超过 1,000 字节；500 字符是本插件的写作预算，
不是 Codex 保证显示 500 个中文字。
上述限制来自 Codex 的 `context/world_state/tools.rs` 与 `tools/handlers/mcp.rs`；
摘要选择由宿主工具呈现路径决定，MCP 初始化未提供该次选择信号，与会话压缩事件不同。
MCP 始终返回全文，Codex Goal/compact hook 和 Pi system prompt 也继续加载全文。

安装完成后启动一个新的 Codex thread，通过 `/hooks` 检查并信任 Goal Plus handler，再开启 fresh
thread，使全部 handler 从 `SessionStart` 起生效。进入目标 workspace 后检查：

```bash
codex plugin list
./install.sh --runtime-info
```

### 无模型查询当前 runtime

在终端执行 `~/.local/share/goal-plus/bin/goal-plus-info codex` 即可查看该宿主登记的
build、Python、DEBUG 和 worker sandbox 默认策略，无需打开 Codex。支持 `--json`；
自定义安装目录和查询范围见 [终端查询](development.md#安装模式)。

在 Codex 中输入 `/mcp verbose`，Goal Plus 的 Resources 行会显示版本、build 短标识、
worker sandbox 默认后端及 DEBUG 开关。无需先启动 Goal，也不调用模型。
只读资源 `goal-plus://runtime/info` 的 JSON 保存完整 build、实际 Python/package 路径、
宿主与平台、runtime root 和 sandbox 默认策略；可通过 MCP `resources/read` 读取。
MCP 握手的 `serverInfo.version` 同时携带完整 build，便于客户端核对实际服务进程。

该资源只对 Codex Main MCP 开放，不枚举其他安装或任意环境变量，也不创建 `.gp`。
`/mcp verbose` 显示资源摘要和 URI，不展开 JSON 正文。Sandbox 字段描述 worker 默认接线，
`execution_verified=false` 表示本查询没有运行隔离探针，不证明某个 worker 已受保护。
Main 权限仍由 Codex 自己管理；ThinkThread Root 显示 provider 边界。
Codex 底层程序显示 `managed by Codex (not inspected)`；ThinkThread 中则显示
`managed by ThinkThread (not inspected)`。在外部终端使用 `goal-plus-info codex --thinkthread`
选择 `goal-plus-codex` Profile 的安装绑定，不需要启动 Root。

### Checkout 更新与宿主资产

修改后重跑 `./install.sh --codex`，按提示更新注册并开启新 thread；运行中的 hooks/MCP
不会随源码变化。多 checkout 的配置边界和旧 release 清理见
[Codex 开发](development.md#开发-codex-集成)及[卸载说明](development.md#卸载与版本清理)。

可安装的 Codex surface 包含：

- `hooks/hooks.json`：七个 lifecycle hook，包括用于持久化用户暂停的 `Interrupt`；
- `.mcp.json`：共享的 `goal-plus --root .gp` server；
- `.codex-plugin/plugin.json`：plugin metadata。

plugin manifest 不发布 `.codex/agents/` custom role。共同 session 工具通过 app-server 启动
candidate 并绑定其 native thread。required final check 由 `goal_plus_run_final_check` 启动
独立、只读的 app-server session；MCP 和 hook 核对持久化的 Goal/check/revision 与准确
native thread，checker 自行调用 `goal_plus_submit_final_check`。Main 不再映射 native
spawn payload，旧 custom checker 模板已删除。

Codex 会从约定路径 `hooks/hooks.json` 发现 hook，因此 manifest 不声明 `hooks` 字段。hook trust
绑定规范化后的当前 hash，hook 更新后必须重新检查并信任。

正常 plugin 的 `.mcp.json` 使用固定 server 名 `goal-plus`，等价的 Codex 配置键是：

```toml
[mcp_servers.goal-plus]
command = "goal-plus"
args = ["--root", ".gp"]
```

安装身份使用 `./install.sh --runtime-info` 查询；从准备运行任务的 workspace 执行
`goal-plus-codex-doctor --json` 检查宿主环境。

doctor 会记录 Codex 版本供诊断，不按版本号设置最低门槛。它检查 Codex 与 `goal-plus`
executable、Python source provenance、plugin
加载、MCP 启用状态、hook path 与 trust hash、内置/custom agent role 覆盖范围，以及只读 Codex model
catalog。required check 失败时命令返回非零，支持 `--json`；安装、更新或修改 trust 后都应使用新
thread。`--skip-model-check` 只适合离线排障；正式检查仍应读取并核对 model catalog。

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
探测。该选择只会规范化到 `raw_goal` 最后一行，不是 Search runtime state。

## Host 集成与所有权

installed plugin 通过 `hooks/hooks.json` 提供 Goal Plus host hook。开发源码中的
`assets/codex/hooks.json` 是唯一源码声明，安装时生成 release 内的 hook 配置。
`UserPromptSubmit`、`SessionStart`、`Interrupt`、`PreToolUse`、`PostToolUse`、`Stop` 和
`SubagentStop` 都调用：

```text
goal-plus --goal-plus-host-hook
```

首次使用或更新 hook 后，应通过 `/hooks` 检查并信任它们。hook 负责预创建 `$goal-plus`、恢复
active record、限制 write/Search 调用和绑定 session 所有权。候选返回不强制补足最低验证次数，
交付用例仍核验准确 Evidence 并按需补验。`UserPromptSubmit` 和 `SessionStart` 会在绑定
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
`PreToolUse` 检查 fail closed；hook 层只有显示和只读监控故障可以 fail open。详见
[运行逻辑](runtime-logic.md)。控制命令按宿主交付时机生效，不承诺原生 `/goal` 的无模型 UI。
Host MCP server 还会用请求中的 native thread identity 复核 Main 的 Goal/Search 目标；绕过 hook
直接连接 MCP 不能借目标 ID 获得另一 native session 的权限。Search report 只有在 run 已关联
host-verified Goal 后才会进入报告用例。
普通 subagent 不继承 parent 的 next action。
新的 Codex session 可用 `$goal-plus <完整目标>` 在同一 workspace 创建独立 Goal；其他 session
的未终态 Goal 不会阻止创建。Search bootstrap 的 `goal_plus_search_freeze_spec` 和 `goal_plus_search_create` 必须传入
当前 session 绑定的 `goal_plus_id`，hook 会在 MCP 调用前核对该归属。

Candidate 的初始 `UserPromptSubmit` 只注入 runtime 绑定的 role 与
`agent_session_id`；完整工作说明已包含在 host launch message，并由
`goal_plus_search_get_agent_context` 返回权威合同。Hook 不重复权限、workspace 或交接文案。
Codex native subagent 仍会按宿主规则自动加载适用的 `AGENTS.md`；当前没有关闭该行为的
subagent launch 字段，Goal Plus 不再用额外 prompt 抵消它。

顶层 `Stop` 遇到 active Goal Plus record 时，会重新提供完整 raw goal、elapsed time、phase、next
action 和 final-check policy，要求 main agent 继续或记录终态。candidate 的 `SubagentStop` 记录
自己的返回事实，不要求它执行 parent-only selection/report/promotion。只有解析到
Goal Plus Goal、Search 或 session context 的自动 Stop/SubagentStop 调用才会把 hook event 写入
`.gp/host-logs/codex-hook-events/`，供最终报告和排障使用。

当 Goal 已终态或 Stop reason 为 aborted/interrupted/cancelled 时，hook 会先关闭 Execution Scope。
若仍有未清理资源，保留 cleanup fence 并要求 Main 关闭所属资源后重试；该分支
不把异常退出改写为 Goal/Search 的 durable 终态。session 已与 Goal 分离但仍拥有旧 scope 时，普通
顶层 Stop 也执行同样的清理。`goal_plus_search_select` 的 cleanup fence 可幂等重试。

Goal Plus session 结束后可以调用 `goal_plus_search_report` 生成报告。

Codex 与 runtime 的职责边界见 [Agent Host Adapters](agent-host-adapters.md)，Evidence 和 Tool View
生命周期见[共享平面](shared-plane.md#global-evidence)。

## 普通 Goal Mode

普通任务使用 [`goal-plus-ultra-v1`](ultra.md) 轻量 dispatch 记录。Main 自行拆解任务，通过
`goal_plus_run_work_item` 启动原生 session 并取得结果；同一 `work_item_id` 的 `result_ready`
可以复用原 session 返工。实际调用才记录 attempt，Main 负责 acceptance、integration 和 completion。
必需的独立最终检查通过 `goal_plus_run_final_check(goal_plus_id)` 执行；
单次只读检查有 300 秒操作超时，超时记为 interrupted 并关闭资源，不作为通过证据。

Ultra 的逻辑要求是 main reasoning effort `max`。Codex host 应在第一个 main turn 启动前选择 `max`，
并记录 `native_reasoning_effort=max`；project hook 只能记录和 gate，不能修改已经开始的首轮
reasoning effort。

只有任务具有 numeric metric、deterministic verifier、isolated edit surface、多个有价值
hypothesis 和足够 budget 时，main 才进入 Search Mode。普通 subagent 记录与后文的 Search
candidate lifecycle 相互独立。

## Search 并行循环

完成一次初始 candidate planning 和物化后，Main 调用
`goal_plus_session_run(run_id, candidate_id, call_id, prompt, timeout_seconds)`；可信 launcher
读取完整 launch 输入，以 candidate workspace 为 cwd 启动 Codex app-server，并绑定实际
native thread。公开回执不包含需要 Main 手工映射的 spawn 脚本。

Candidate 先调用 `goal_plus_search_get_agent_context`，再把 `context.candidate_task.workspace`
用于文件、编辑和 shell 操作。MCP 角色声明及原生 hook 共同核对当前绑定，避免退回 Main 权限。
任务本地 Skill 无需配置：runtime 只发现 source Git 已跟踪的 `.agents/skills/<name>/SKILL.md`，
并要求该 Skill 目录内所有文件都由 Git 跟踪；发现结果持久化并在 launch message
中明确列出，通过 `context.candidate_task.task_skill_paths` 返回当前 worktree 的绝对路径。child
完整读取清单中的 Skill，不扫描 parent/global/plugin 或其他 candidate 路径。

标准 `parallel_loops` 并行循环如下：

1. freeze `strategy.orchestration_mode="parallel_loops"`，只规划一次并启动全部初始 candidate；
2. 对每个 candidate 用独立 `call_id` 调用 `goal_plus_session_run`；
3. 把全部活跃调用放进同一次 `goal_plus_session_wait_any`，在首个完成的调用处收取结果；
   等待超时只返回仍在运行的 roster，不取消执行；
4. 共同交付用例复用准确 verifier Evidence，对需要补验的修改执行原 verifier/barrier；
5. Main 判断仍需继续时，以新的 `call_id` wake 同一 session；
6. selection 前让所有活跃候选静默并关闭资源，再进行 selection、promotion 和 report。

每个 child 只负责自己的 candidate：读取 context 和 Global Evidence View，选择技术方向，只编辑
自己的 workspace，并在真实尝试后调用 verifier。`inner_agent=autoresearch` 在一次 dispatch 内
自主多轮；`inner_agent=common` 完成一个有界 attempt 后返回。`view=null` 只表示 annotator 尚未
发布，不要求等待。candidate 不能创建其他 candidate，也不能 select、report、promote 或修改
Goal Plus 状态。

成功的 `goal_plus_search_run_verifier` 返回后，Codex PostToolUse hook 从当前 Codex host 进程同时唤醒
Annotation 和可选 LLM Verifier queue。内部 `codex exec` 完整继承当前 host 配置，并携带
launcher 写入的 host provenance；MCP server 不启动模型，也不需要配置服务认证变量。

## 模型选择

开头连续的 `key=value` 会被 Codex host hook 持久化为 typed `command_config`；第一个非配置
token 开始就是目标。显式配置会在 ready SearchSpec draft 保存时机械校验，缺省字段才由
Codex main 判断。完整 key 列表见 [API](api.md#host-command-config)。

Codex 可以分别指定 annotator 和 worker 模型：

```text
$goal-plus inner_agent=autoresearch max_parallel=4 annotator=gpt-5.6-terra workers=gpt-5.6-terra,gpt-5.6-sol optimize ...
```

已经运行的 Codex session 就是 main agent，`$goal-plus` 不能切换它的模型。应在启动时使用
`codex -m MODEL` 或 `codex exec -m MODEL ...`，也可以在提交 Goal Plus prompt 前使用 `/model`。
`main=` 不是受支持的 Goal Plus 命令配置；新命令填写它会在创建记录前失败。

`annotator=` 在同一 Codex host 上解析单个模型，并冻结到 `strategy.evidence_annotator.model`。
`workers=` 将一个或多个模型解析到 `strategy.models`。`models=` 是 `workers=` 的兼容别名，
两者同时出现会报错。省略某个 role 时保留其正常 host default 或继承行为，不从另一个显式 role 推导。

`max_parallel=4 workers=A,B` 未固定各模型数量；spec 也省略 count 时按 A、B、A、B round-robin；
`max_parallel=4 workers=A*1,B*3` 是显式 A1B3。Goal Plus 通过
`goal_plus_list_models(agent_harness="codex", runtime_provider="direct")` 默认返回可选择的
`provider`/`model_id` 及 reasoning levels；需要完整目录时传 `detail=true`。它解析
annotator 和 worker 名称，并在 freeze 时保存有序 `selected_models`。candidate 的 worker 模型在后续
continuation 中保持不变。省略 `workers=` 和 `models=` 时，child 继承正常 parent/default model。

Codex + ThinkThread 的 Root 使用 `goal_plus_host_start`，没有普通 Codex hook 的
`UserPromptSubmit` preflight 路径。该入口在创建 Goal 持久状态前读取隔离 `CODEX_CONFIG_DIR`，要求
Profile 明确选择 `model_provider`、provider `base_url` 和可用 credential。缺少 provider 时会直接拒绝，
避免 Codex 静默退回 `openai-codex`；配置错误会在 Goal 启动处返回。Search 的显式模型仍必须先出现在 TT Profile allowlist 中，随后由
`goal_plus_list_models`/freeze 使用 exact `provider/model` 校验；candidate spawn 把同一精确引用
传给 TT，而不是回退到 Root 默认模型。

Model catalog 负责发现；可信 launcher 将冻结的 model/reasoning/service-tier override
传入原生 app-server。未显式选择时使用 scope 中保存的 Main 模型快照。

Goal command 和 `goal_plus_search_freeze_spec` hook 会收集显式 worker、annotator model，
通过宿主 model catalog 和 TT allowlist 做零请求的 exact `provider/model` 准入。认证、endpoint
与其他外部配置由当前 Codex 环境原样继承；不会为安装或 freeze 发送合成请求。实际 candidate
invocation 才证明 provider 可用，失败会保留 phase、code、model、provider、endpoint、上游状态码、
可重试性和脱敏错误来源，并出现在 monitor/report 中。

`adaptive_search` run 配置确定性 Allocation policy 后自动启用固定 LLM-Verifier；policy 版本同时
管理动态追加的 allocation 评分规则。`parallel_loops` 则仅在 Selection 使用 `quality_score` 时启用。
Verifier 使用当前 Codex host 的继承配置，没有独立 model override。未终态 Verifier task 只在
run-scoped 队列中顺序积压，不阻塞对应 lane 的下一轮；main 不等待或自行解释 Verifier 输出。
Decision 出现后，Main 调用 `goal_plus_search_apply_allocation_decision`；可信 session host
先完成 Executor 预检与 retirement fence，再中断准确归属的 session/generation/call，保存取消回执，
再由 Allocation owner 重核并幂等 apply。
拒绝保持 prepared，结果未知要求恢复；接受回执不代表资源已经退出，旧 session 仍需关闭。
Main 随后通过 `goal_plus_session_run` 启动派生 candidate，公共回执不包含完整 launch payload。
child 从 Decision 指定的
verifier-backed Git settlement 启动，并继承 run Evidence 和模型 provenance；Adaptive metric
不覆盖最终 Selection 的冻结排序规则。

## Worker Deadline

Codex/Pi 共用 session 执行预算。`strategy.worker_budget` 可省略；显式设置
`max_runtime_seconds` 时，它限制本次执行，不要求 candidate 用满时间。

```json
{
  "budget_control": {
    "mode": "session_control",
    "max_runtime_seconds": 600,
    "on_exceed": "interrupt"
  }
}
```

`goal_plus_session_run` 的执行额度由共同 Application 同时受准备好的 worker 上限与
当前 Goal deadline 约束。显式继续不会重置 Goal deadline；wait 超时只返回仍在运行，
不取消执行。`max_turns` 仍只是提示。模型返回后由 Main 判断是否再次 wake 同一 session。

没有最低探索时长、最低 verifier 次数或提前收尾窗口。`SubagentStop` 不因工作时间短而
强制续跑；返回不等于验证成功，交付修改仍经过共同 Evidence 验收与必要补验。

如果剩余时间已经短于观测到的 verifier 提交耗时，`PostToolUse` 可能注入一次 advisory，但不会停止
worker。

## 续跑与恢复

Main 使用共同入口继续同一 candidate：

```text
goal_plus_session_run(run_id, candidate_id, new_call_id, prompt, timeout_seconds)
```

后续任务保持技术方向中性，并沿用 frozen inner-agent binding：`autoresearch` 继续自主循环，
`common` 只完成下一个有界 attempt。

Controller 或原生进程退出后，host 取得独占执行 lease，从持久绑定恢复原 session，
保留当前 workspace、未验证修改、Evidence 和预算。原 session 无法加载时，Main 可显式
使用 `new_session=true` 建立新对话接手现场。明确放弃现场时才使用
[checkpoint 恢复](search-recovery.md)。

## Observability 与日志

`goal_plus_search_get_agent_observability(agent_session_id)` 返回单个 worker 的归一化 model、reasoning
effort、active/wall duration、terminal state、token/context、tool/message count 和 session
artifact path，不包含 prompt、reasoning 或 tool payload 内容。

普通 Ultra work 可以在 `goal_plus_monitor_snapshot` 中启用 `orchestration` feature plugin。它把
work item、assignment/result/decision 和 Codex 原生 operation 投影为只读视图，但不会启动、
等待或控制任何 thread。详见
[Monitor Feature Plugin](adaptive-search.md#monitor-feature-plugin)。

普通 native subagent 的 scope 跟踪优先读取 `SubagentStop.agent_transcript_path` 中的
canonical agent path，也接受 hook 直接提供的
`task_name`。`spawn_agent` PostToolUse 保存 canonical task target；即使 interrupted worker 没有发出
`SubagentStop`，成功且关联当前 wake 的 `interrupt_agent(target=...)` 仍可确认同一 target
静默。该机制不用于 Search candidate；candidate 通过准确 app-server handle 识别，
以共同 session 的 invocation 记录和进程资源确认结果及退出。
native identity 与 canonical identity 指向不同 child 时不结算任一记录；hook 保存结构化
reconciliation 结果，scope 保持 active 并由 cleanup fail-closed。
再次唤醒后的观察规则见本页 [Execution Scope](#execution-scope)。

需要原始 event 时使用：

```bash
codex exec --json ... > .gp/host-logs/codex-run.jsonl
RUST_LOG=debug codex -c log_dir=./.codex-log
```

持久 transcript 位于 `${CODEX_HOME:-~/.codex}/sessions/YYYY/MM/DD/rollout-*.jsonl`。
`codex exec -o` 只包含最终消息，不能代替 JSON event stream 或 rollout log。完整诊断流程见
[调试运行时状态](debugging-runtime.md)。

## 支持范围与验证

Codex 支持本地 `git_worktree` / `copy`，并支持 `agent_harness=codex` +
`runtime_provider=thinkthread` 的 ThinkThread Search（经 `./install.sh --codex --thinkthread`
安装 `goal-plus-codex` Profile）。TT 0.1.0-21 修复了 Codex Child 加载 stdio MCP 后
首条任务停留在输入框、未开始执行的启动提交问题；Goal Plus 集成已完成真实宿主验收。

已验证范围：TT 子模块矩阵（managed profile、wake 续跑、signal 中断语义、fs
branch/snapshot 生命周期、消息分块重组、并发 Child、hook 隔离、annotator 角色）与
缩减版 circle 验收（并行 candidate、Annotation Evidence、selection、apply、cleanup）。
`parallel_loops` 与 `adaptive_search` 均使用同一 TT snapshot/Child 合同；Adaptive 的派生
candidate 从 AllocationDecision 选定的精确 snapshot 启动。

模型切换：`./install.sh --codex --thinkthread --codex-profile <name>` 会把用户
`CODEX_HOME` 下的 `NAME.config.toml` 顶层覆盖层（`model`、`model_provider`、
`model_providers`、`model_reasoning_effort`、`service_tier`、`model_catalog_json`）
投影进受管 TT Codex 配置，使 Child 以非默认模型运行，例如 DeepSeek 的
`deepseek-flash`（原生 Responses API，无需协议转换）；不指定时仍投影 base config
的默认模型，原 GPT/sub2api 管线不受影响。覆盖层对 `model_providers` 等嵌套表按
键合并，保留 base 中其它 provider 定义。`model_catalog_json` 会规范化为解析后的
真实路径并加入 Profile read 白名单；系统 CA bundle（RHEL/Oracle 系的
`/etc/pki/.../tls-ca-bundle.pem` 等）同样加入白名单——沙箱内 `/etc/ssl` 符号链接
目标未挂载，缺少该白名单时 HTTPS provider 会以连接失败静默重试。

安装 `--codex --thinkthread` 时，安装器只验证最终投影的 Profile、隔离配置、MCP 环境接线和
ThinkThread exact `provider/model` delegation allowlist 可读取，不发送真实模型请求。
配置失败会报告具体的 Profile、model、provider、endpoint 或 credential 名称，不会把密钥
写入输出。allowlist 中未被当前任务选择的模型可能有意不配置凭据、额度或可用 endpoint，因此
不会阻断安装。`--codex-profile` 只选择默认配置 overlay，不是“只允许 Luna”的开关；多个 TT
模型先进入 TT registry/allowlist，任务实际选择的 worker/annotator 模型在 freeze 时通过 catalog
解析与配置准入；真实请求失败由执行记录准确原因。安装与任务准入不发送额外的推理请求，
执行成功才证明当前服务可用。

TT Codex 下不支持/未验证：`reasoning_effort`/`service_tier`
（TT 拒绝，freeze 前报错）；TT Codex Root 内的 Ultra delegation 与独立 final check；
Stop-hook 续跑（`$goal-plus resume` 不可用）。

Codex 支持以下可移植内置策略：

- `agent_guided`、`agent`、`default`
- `random`、`random_mode`

快速契约测试：

```bash
pytest -m codex -q
```

真实并行循环 smoke test：

```bash
pytest --run-system-tests -n 0 -s -q tests/st/test_st_unified_main.py -k circle
```

共同 Main Circle 验证初始 plan、两个候选、同 session 续跑、精确 Evidence、selection、
publication 和 cleanup。Goal Plus report 只在 parent record 进入终态后生成。
