# Pi

Pi 以可安装 package 的形式提供一个 extension，底层复用共享 Goal
Plus runtime。它不修改 Pi core，不发布 Goal Plus skill；开发 checkout
可以通过隔离的开发启动器直接加载同一组 asset。

## Controller-owned closeout

当外部 host 需要在 hard/process verifier 之后用自己的候选裁判完成收尾时，
可为 Main MCP/extension 设置 `GOAL_PLUS_CONTROLLER_ONLY_CLOSEOUT=1`。该模式会
从 Pi extension、MCP manifest 和 CLI fallback 同时移除 `select`、`promote`、
`apply_promotion` 与 `record_search_result`；worker 仍由原有角色门禁限制。外部
controller 通过可信 Goal Plus runtime 的 `select_for_controller()` 提交带 receipt hash
的精确 candidate/iteration/artifact directive，之后仍必须经过原生 promotion verifier。
该 API 不注册为模型工具。

## Execution Scope

Pi main 在第一次启动 Goal Plus candidate、delegation 或 final checker session 时惰性创建 host-local
Execution Scope。`execution_scope_id` 由 extension 内部传递，不是模型工具参数。

用户 abort 当前 agent 时，agent_end 的 stopReason=aborted 会 interrupt scope；
session_shutdown 关闭旧 scope。GP 不监测 owner PID，不启动 guardian；
未送达退出事件时，进程生命周期由 Pi/ThinkThread 和 agent 负责。
关闭不恢复、接管或改变 Goal/Search 状态。

ThinkThread 受管 Profile 将 `GOAL_PLUS_ROOT` 放在原生 session 目录中，位于 Root
附着文件树之外。自定义 runtime 路径也必须满足此隔离条件；放在 workspace 内的目录
会在 baseline 捕获前被拒绝，以防 Goal/session 状态进入 Child 的 snapshot。
Scope 按可信 native session 和业务 activation 授权。本地资源日志只记录登记、完成和释放，
不核验进程身份或进程组；session 的操作超时、准确 handle 和 generation gate 保留。
具体合同见 [Host Adapters](agent-host-adapters.md#main-owner-生命周期)。

## 安装与快速开始

### 正常安装

在当前 Muyuan checkout 的 Goal Plus source directory 中运行统一安装器：

```bash
cd <Muyuan checkout>/plugins/goal-plus
./install.sh --pi
```

正常安装注册不可变 Pi package，使用受管 Python 和固定 build。已有正常来源可在安装器
`[y/N]` 提示中确认替换；散落的同名资源仍须处理冲突。完整来源与更新合同见
[开发指南](development.md#安装模式)，安装身份使用 `./install.sh --runtime-info` 查询。

正常安装后可以从目标 workspace 启动交互会话，或直接提交一次 prompt：

```bash
pi -p "/goal-plus inspect this repository"
```

卸载使用 `./uninstall.sh --pi`；release 清理与组件范围见
[卸载说明](development.md#卸载与版本清理)。

### 无模型查询当前 runtime

终端执行 `~/.local/share/goal-plus/bin/goal-plus-info pi`，无需启动 Pi，即可查看该宿主
登记的 Goal Plus build、Python、DEBUG 和 worker sandbox 默认配置；支持 `--json`。
自定义安装目录与查询范围见 [终端查询](development.md#安装模式)。

交互 Pi Main 启动或 reload 时，状态栏自动显示当前 extension 绑定的 build、worker sandbox
默认后端和 DEBUG。输入 `/goal-plus-info` 查看完整信息并刷新摘要，不需要先创建 Goal，
不调用模型，也不写入模型上下文或 Goal 状态。RPC 客户端可通过同名 extension 命令收到
通知；JSON/print 模式没有 UI，使用终端查询。诊断失败显示 unavailable，不阻止普通会话启动。

macOS worker 默认后端为 Seatbelt，Linux 为 bubblewrap；程序缺失会标明 unavailable。
发现程序不证明隔离能执行，`execution_verified=false` 表示未运行探针。
Direct Pi Main 仍使用 launcher 进程权限，不能将 worker 默认后端视为 Main 已受隔离。
ThinkThread Root 显示 provider 边界。该诊断与创建 Goal 时的 `goal-plus-runtime-start`
权限和工具快照分别保留各自用途。
外部终端用 `goal-plus-info pi --thinkthread` 查询 `goal-plus-pi` Profile 的安装绑定；
底层程序显示 `managed by ThinkThread (not inspected)`，不推断该 provider 的内部隔离实现。

### ThinkThread protected Search Root

ThinkThread Runtime 由 ThinkThread/Capsule 独立安装和管理。Goal Plus 不安装、升级或卸载它。
Runtime 和内置 `pi` Profile 已可用时，一条命令安装普通 Pi 集成并生成 Goal Plus Profile：

```bash
cd <Muyuan checkout>/plugins/goal-plus
./install.sh --pi --thinkthread
tt profile show goal-plus-pi
```

安装器校验 Profile 结构、Root direct filesystem 和 Child COW filesystem，然后生成用户级
`goal-plus-pi` Profile。Runtime、Profile schema、SDK 协议版本或 fingerprint 偏离已测基线时仅
警告；可选的 Extension 协议声明可以省略，实际握手由 ThinkThread 校验。Profile 显式加载本
次安装副本的 Goal Plus extension，以只读方式开放该副本及其受管 Python environment，并将
Node/Python runtime prefix 及解释器软链接链目标一并加入 `[environment].read`，
verifier 的裸 `python` / `python3` 直接使用该受管 venv 的绝对解释器路径。
安装器再将 `GOAL_PLUS_ROOT` 延迟解析到每个 Pi native
Session 的 private directory。本 checkout 生成的旧 Profile 会原子更新；已有内容不同且不归本
checkout 所有的同名 Profile 会让安装 fail closed，不会被覆盖。

从 product workspace 启动 protected Root：

```bash
tt run --profile goal-plus-pi --fs <target-workspace>
```

Root 内只有精确 Pi host command 能启动 Goal Plus。显式选择 ThinkThread workspace：

```text
/goal-plus workspace_provider=thinkthread max_parallel=2 <可度量目标>
```

对应 SearchSpec 可省略 `strategy.adaptive_search` 使用默认固定并行，或配置该 policy 对象，并设置
`workspace.provider="thinkthread"`；Pi 身份从 Main 会话解析，不另填 host/profile。
`fork` 另使用共同 fork/session 接口，要求先完成准备回合并恢复同一 Main；已知 receipt
恢复与 retained Child 控制已接线。准备 marker 只证明 completed turn；resume 恢复 Main
授权与控制状态，fresh/fork 的选择和 candidate 启动由 Search/session 工具合同独立处理。
真实 Circle 场景核验共同 native prefix、两个 retained Child 各续轮一次、hard verifier、
audit/Annotation、selection、独立 publication/apply 与资源关闭。每次验收绑定实际安装 build；
SDK exact checkpoint 证明与异常恢复仍有证据边界，见
[Fork 支持范围](agent-host-adapters.md#fork-支持范围)。
fresh/fork 的共同 session、receipt、continuation 和恢复图见
[Candidate Dispatch：fresh 与 fork](candidate-dispatch.md)。
普通 Pi 进程继续使用 `pi + direct + git_worktree`；两条路径不能混用，也不能在 preflight
失败时 fallback。Codex 对应路径见 [Codex](codex.md#支持范围与验证)。

ThinkThread 可独立升级；已测版本或 fingerprint 差异只警告，Root/Child 的实际接口与能力
仍须满足合同。每个 candidate 使用 private Child/Session/branch，公共 Evidence 绑定准确
snapshot。冻结、verifier、inner Git、publication 冲突及同 RequestId 恢复见
[Workspace 架构](workspace-sandbox-arch.md)；独立只读 Annotation Child 见
[Evidence Annotator](evidence-annotator.md#pi)。

Main 使用共同 session 工具。accepted receipt 不等于 verifier 结果；控制器确认准确 Message
静默并完成结算后，typed feedback 才进入调用结果和下一次 wake。controller 丢失后的接管只
观察原调用，不重发模型任务。turn interrupt 要求准确控制确认和本轮静默；stop 保留 Child
供显式 open 重新授权，close 释放资源。详细身份、deadline 与控制边界见
[Agent Host Adapters](agent-host-adapters.md#统一运行模型)。

对称卸载命令是 `./uninstall.sh --pi --thinkthread`：`--thinkthread` 只删除当前 checkout 生成的
`goal-plus-pi` Profile。它绝不删除 ThinkThread Runtime、Root 或 state；这些资源由 ThinkThread
管理员独立管理。需要顺带清理不再被任何 host 引用的受管 release 时，再显式增加
`--unused-runtime`；仍在使用的 release 会保留。

### Checkout 开发安装

```bash
cd <Muyuan checkout>/plugins/goal-plus
./install.sh --pi-dev
source .goal-plus-dev/set_env.sh
cd <target-workspace>
pi-goal-plus
```

editable 环境、launcher、资源冲突与刷新步骤见
[Pi 开发指南](development.md#开发-pi-集成)。`--pi` 与 `--pi-dev` 不能同时使用。
正常和开发安装均不发布 Goal Plus skill；命令由 extension 注册，导航来自共同 MCP instructions。

#### Progress UI 实时展示

实时进度表面只在 Search run 进行期间存在，不做 Goal 全局展示：

- Search run 启动时出现。创建 run 的 `goal_plus_search_create` 返回后立即渲染；Pi 重启恢复、
  显式 resume 或 `/goal-plus` 控制命令时，只要该 run 仍在进行也会渲染。
- 只要 run 仍在进行，每 5 秒重新读取一次只读状态并刷新编辑器上方的 widget：首行是
  `Goal Plus  <状态>  <已用时长> · <metric>`，之后依次是 lanes/verify/best、baseline 提升，
  以及每 lane 一行 `id · 模型 · 状态 · best/last 分数 · 当前轮已跑时长 · 预计下一轮结算剩余
  （由该 lane 平均轮间隔外推）· verify 次数`。全部内容只占这一个 widget：不写状态栏槽，也不
  改模型流式输出期间的 loader 行，避免同一份摘要重复出现。
- run 结束（`promoted`/`aborted`/`failed`，或 Goal 已没有当前 run）后停止刷新，widget 再显示 5 秒
  然后清除；Goal 暂停/阻塞时先渲染红色 `SEARCH BLOCKED`，同样在 5 秒后清除。
- 状态或 monitor 读取失败、或快照里暂时没有该 run 时不算结束：已显示的 widget 保持不变，5 秒轮询
  继续重试，只有权威读取确认 run 已结束才清除。`GOAL_PLUS_DEBUG=1` 会输出
  `goal_plus_progress_read` 与 `goal_plus_progress_phase` 诊断。
- 没有正在进行的 Search run（例如 Goal 只做普通工作、或 run 已结束）时不显示这个 widget；Goal 终态
  由终态结果卡表示。数据来自只读 `goal_plus_status` 与 `goal_plus_monitor_snapshot`（后者用
  `detail=true`，slim summary 不含 `search_tasks`/`candidates`），
  字段见 [API](api.md#handoff-与-observability)。

Main turn 结束而 Goal 仍未终态时，stop gate 会拒绝停止并注入一条 **不在 transcript 显示** 的
`goal-plus-must-continue`。它的正文由共享 runtime（`application/goal_gate.py`）生成，只保留终态与
超时判定规则和当前 next action（约 600 字符，不再重复目标、时间上下文、policy、派发和
reconciliation）；Codex 的 Stop hook 用同一份文本。

完整目标、命令配置、final check policy 与 reconciliation 由 Pi 的 `goal-plus-native-context` 提供：
**只在 session 首轮、压缩后或 revision/control 变化时注入完整版本**，其余每轮只注入精简状态行
（id/phase/revision/next action + 需要时读 `goal_plus_status`）；由 `goal-plus-must-continue`
点火的那一轮已自带 phase/next action，因此跳过这条精简提醒。Codex 在
`SessionStart(source=compact)` 后同样会重注 goal context，两种宿主都保证压缩后目标仍可见。记录真实
终态或 `/goal-plus pause|clear` 后不再注入；candidate 侧只有一次性的
`goal-plus-missing-attempt` 提交提醒，属于另一条独立消息。

也可以在交互式 Pi 中直接使用：

```text
/goal-plus Fix this bug and verify the test suite.
/goal-plus mode=probe Check whether vectorization is viable.
/goal-plus mode=autonomous Deeply optimize the kernel.
```

`/goal-plus edit` 和 `/goal-plus-with-final-check` 与 Codex 使用同一套 goal revision 语义。
required final check 由共同 `goal_plus_run_final_check` 启动独立 Pi RPC reviewer。
启动器继承当前 Main 的模型快照，登记 scope 资源并绑定准确 check/revision/native session。
Final-checker Pi RPC 的工具白名单包含 `read`、`goal_plus_status`、`goal_plus_submit_final_check`；
不开放编辑或 shell。普通 Main 会话只注册 slash command，不注册或注入 Goal Plus MCP 工具；精确执行
`/goal-plus`、完成 host session 绑定后，extension 才按 Main 角色动态注册并激活对应工具。
恢复已有 Goal 时执行相同的 session 校验和动态注册。结构化提交仍由共同 GoalCompletion 验收，
普通正文不能完成 Goal，返回后关闭原生资源。

这些大小写精确的 slash command 是唯一创建/编辑入口。自然语言请求、skill/prompt 自动加载、
直接 MCP 调用和通用 `goal-plus-pi-tool` 不能创建或编辑 Goal Plus。extension 通过未注册为模型 tool 的
host-command control path 调用 runtime，并为每次调用签发 120 秒内有效、原子消费的 one-time
capability；只有 `--host-entrypoint` 标志而没有该 capability 仍会被拒绝。这个一次性 capability
只保护单次 host-control 调用，不是模型 turn 或 Goal 的授权期限。`goal_plus_create` 和
`goal_plus_update_goal` 只保留为 deny-only tombstone。host-control 调用成功后，精确 slash command
会把当前原生 Pi session 绑定到 active Goal Plus 记录。同一 session 内的普通 follow-up、临时
provider error 和自动重试会沿用该绑定；最终异常或用户 abort 会将任务 control 标为 paused。

恢复会话使用 Pi 原生 `--session <id>`、`--continue` 或交互式 `--resume`，不会自动点火。
重启原 Main 使用 `/goal-plus resume`，交互命令与 print/JSON 共用控制入口：
`pi -p --mode json --session <id> '/goal-plus resume'`。原 session 身份必须精确匹配；
不同 session、fork、detached/stale 不继承执行权。摘要、pause、clear 及完整合同见
[运行逻辑](runtime-logic.md)。开发安装的 `pi-goal-plus` 支持相同参数。
可在 `resume` 后附带多行补充信息，与恢复合并为一次输入，例如 `/goal-plus resume 依赖已安装，请继续`。
新的 Pi session 可用 `/goal-plus <完整目标>` 在同一 workspace 创建独立 Goal；其他 session 的
未终态 Goal 不会阻止创建。Search bootstrap 的 `goal_plus_search_freeze_spec` 和 `goal_plus_search_create` 必须传入
当前 session 绑定的 `goal_plus_id`，extension 会在执行前核对该归属。

## Pi 与 Codex 的差异

Pi 使用 extension event，而不是项目 hook 文件。extension 只会在 interactive、RPC、print 和 JSON
模式解析到精确 slash command 后，在模型运行前创建或编辑 Goal Plus，保存 active ID，并绑定当前
原生 session；后续每个 agent turn 都会注入隐藏 context。普通 input 不会自行创建或编辑记录，但在
精确同一 native session 且 control=active 时先处理用户输入再继续任务。中断后的普通消息只聊天，
不恢复任务。`session_start` 核对绑定但不启动 turn，交互和 headless 均须显式恢复已停止 Main。
后续 continuation 在 awaited `agent_end` 中排队；旧 revision/control version 的请求失效。
未绑定 session 的 Goal Plus/Search 变更会被 tool gate 拒绝。它提供原生 turn-level stop gate，
但不是 host process 级 Stop hook。

新建 Goal Plus 记录前，Pi extension 会追加一条不进入模型上下文的
`goal-plus-runtime-start` custom entry。折叠视图显示当前 main session 的 mode、
project trust、active tool 数量，以及 Pi Main 没有内置 sandbox/审批的事实；展开后显示
实际 cwd 和 active builtin/non-builtin tool 名单。这里的 `--approve` 或
`project_trusted=true` 只表示允许加载项目内 Pi 资源，不表示 full access。
filesystem 只报告为 `launcher-process-user`，network 只报告为
`host-policy-unreported`，不会从 settings 或父进程猜测不存在的 Pi permission profile。

Goal Plus 启动的 ordinary、final-checker 和 Search candidate `pi-rpc` 子进程会在
各自 `session_start` 写入同一结构。wrapper 在发送第一条模型 prompt 前从该
native session 读回快照，写入 host event log 的 `goal_plus_runtime_start` 事件、
handle `metadata.runtime_start` 和 `pi_metrics.runtime_start`。
`goal_plus_search_get_agent_observability` 将其投影为 `runtime_start`；快照缺失时保留
`source="unavailable"` 和错误原因，不从 main session 继承或推断。headless worker 不弹出
不可见的 TUI 通知，运行中排障可直接查看对应
`.gp/host-logs/pi-rpc-<agent_session_id>.jsonl`。

Pi Search 的异步 Evidence annotation 也运行在 Pi 上。run-scoped drainer 启动临时
`pi --mode json --no-session --no-tools` 进程。Pi extension 在成功的 worker verifier 返回后通过
one-time host capability 同时唤醒 Annotation 和可选 LLM Verifier queue；这些进程完整继承
当前 Pi host 配置，并携带 launcher 写入的 host provenance。Goal Plus 不保存或转发服务认证与
endpoint 配置。显式限定的 annotator model 只改变 host-native 模型选择。verifier settlement
不等待这些进程，同一次 annotator turn 也会生成 shared-dir Tool View。完整生命周期见
[共享平面](shared-plane.md#global-evidence)。

共同候选宿主补做 final verifier 或复用已结算 Evidence 后，也从 host 收尾路径重扫内部任务，
不依赖已结束的 worker 再调用工具。唤醒错误单独记录，不撤销硬结算或将成功 verifier 改为
失败。Adaptive 的 parent 补验可以生成内部 Annotation/Verifier 输入，但不成为 peer
Global Evidence；monitor snapshot 仍只读，不承担唤醒。

main turn 结束时，仍为 active 的 Goal Plus record 会携带完整 raw goal 和 elapsed-time context
继续执行；用户暂停或清除后不再续跑。单次 worker 返回或执行超时不代表 Goal 完成，
终态仍由领域 gate 核验持久证据。

进入终态时，Pi extension 会在 `agent_settled` 后静默通过只读 monitor 读取持久化结果，
再写入唯一一条可展开的 Goal Plus 完成卡。模型不需要为了最终展示再次调用 monitor；
最终回复只保留一句交接语，避免自然语言结果、可见 tool JSON 和完成卡三重重复。
默认视图只保留四类高价值信息：终态与整个目标耗时、选中结果及相对基线变化、
candidate/verifier/promotion 摘要，以及 main+worker 合计用量。整个目标明确给出时间窗口时，
卡片会同时显示期限和超出量。

完成卡行间不加入额外纵向 padding。展开完成卡可以查看 main/worker 用量拆分、每个 candidate
的模型/分数/verifier/disposition、Goal Plus ID 和报告路径。若模型因过程判断主动调用
`goal_plus_monitor_snapshot`，Pi 的自定义 tool renderer 默认只显示终态、选中结果、verifier 和
promotion 的单行摘要；展开也只增加 run/candidate/phase 摘要，完整 JSON 仍提供给模型但不铺满 TUI。
monitor 不可用时，卡片降级到终态、当前会话耗时和 main 用量；不会从 transcript 猜测缺失数据。
它是 custom entry，不是 LLM message，不会触发另一个 assistant turn，也
不再额外发送一份重复的统计通知。

Search worker 由共同 session 宿主启动受保护的 `pi --mode rpc` 子进程，资源经 scope 登记后放行；
direct child 默认使用 OS wrapper；后端缺失时拒绝启动。Pi Main 沿用宿主权限。
依赖安装、配置副本与完整支持范围见 [Agent sandbox](sandbox.md)。
native session 位于 `.gp/host-sessions/pi/`。旧 Pi pool/driver/worker 与专属 CLI 已删除。
切换前须由旧安装关闭旧活跃 pool；历史文件保留，新版本不接管旧 pool 执行。

## 普通 Goal Mode

普通任务使用 [`goal-plus-ultra-v1`](ultra.md) 轻量 dispatch 记录，不预建 work DAG。Pi binding 保留 Main 在提交
`/goal-plus` 时已经选择的 thinking 档位并记录实际 level，不会强制改档。复杂任务推荐 `xhigh`，
但当前模型不支持 `xhigh` 或用户选择其他档位时仍正常运行。

实际派发普通 subagent 时，Main 把完整任务直接传给共同 `goal_plus_run_work_item`。该工具继承
Main 当前 thinking，懒创建 attempt，启动隔离的 Pi RPC child，仅在 RPC `get_state` 握手和
请求的模型/thinking 设置成功后绑定 session，
等待 child 完成，并用同一个 `attempt_id` 和 `generation` 提交 result。多个相互独立的 tool call
可以并发执行；复用已返回的 id 会记录 rework 并继续当前 native session。新入口不自动接管
断连后的活跃 controller；`result_ready` 的冷恢复须先证明旧准确资源已退出，再恢复同一
native session，详见 [Ultra](ultra.md)。

Pi wrapper 只拥有 native launch/wait。main 仍负责 acceptance、rework、integration、Search routing
和 completion。完整轻量记录协议见 [Ultra Orchestration](ultra.md)。

Search promotion 后，Pi 用 `goal_plus_record_search_result` 登记“搜索完成并验收”，再在原始工作区
检查实际应用的 diff 和整体测试。最终状态 evidence 另行登记“补丁应用完成并验收”。这两项只是
收尾证据边界，不创建 DAG 或额外运行时阶段。

## Search Session 流程

Pi Search 在未指定命令约束时使用 `initial_context="fork"`；完成 Main 准备回合后从
checkpoint 派生初始 lane。需要直接从 baseline 启动 candidate 时显式使用
`spawn_mode=fresh`；命令可用 `spawn_mode=fresh|fork` 约束 SearchSpec 的初始派发方式：

1. 只规划并物化一次初始 candidate；
2. 用 `goal_plus_session_run` 准备候选并执行，保存返回的 session/call identity；
3. 把全部活跃调用放进同一次 `goal_plus_session_wait_any`，在首个完成的调用处读取结果；
4. 检查持久 Evidence，若需继续探索，对同一 candidate 再次 run；
5. controller 重建后，用新的 call_id 继续原 session 和 workspace，详见 [Search 恢复](search-recovery.md)；
6. 结束时调用 `goal_plus_session_close`，再 select 和 promote；默认 promotion mode 必须 apply，
   只有显式 `artifact_only` 才保留 publication；
7. 记录 Search result，完成 raw-goal audit，设置 Goal Plus 终态，并为每个已记录 run 生成一次 final
   report。

本地 Pi 不再注册 `pi_search_pool_*` 模型工具。共同 session 不自动循环；Main 决定继续，
Application 核验预算、当前 generation 与归属。调用返回不代表 verifier 成功或进程已退出。
共同 delivery 用例复用精确 artifact 对应的 Evidence；有实质修改但缺少 Evidence 时由宿主
调用原 verifier 补验，结算后唤醒内部队列，未修改的 workspace 不增加 baseline iteration。
Pi ThinkThread 也使用共同 session 控制，底层单 Child actor 负责准确 Message、retained wake
和资源退出；旧 Child pool 已删除。Codex ThinkThread 已完成真实宿主验收，支持范围见
[Codex 支持范围与验证](codex.md#支持范围与验证)。

## 模型与 Worker Spec

本地 Pi Search 自动绑定 Pi RPC 驱动，并使用 wall-clock budget：

```json
{
  "strategy": {
    "name": "random",
    "initial_context": "fresh",
    "worker_budget": {
      "max_runtime_seconds": 600,
      "max_turns": 8
    }
  }
}
```

共同 session 的每次调用由 Application 按请求额度、准备好的 worker 上限和当前 Goal
deadline 限制。`max_turns` 仍是 prompt 提示；返回后是否继续由 Main 决定，不自动启动下一轮。
`worker_budget` 可省略。没有最低探索时长/次数或提前 closeout；显式上限和 Goal deadline
仍生效，verifier/RPC 的操作超时独立保留。

命令可分别选择 Evidence annotator 和 Search worker 模型：

开头连续的 `key=value` 会被 host control path 持久化为 typed `command_config`；第一个非配置
token 开始就是目标。显式配置会在 ready SearchSpec draft 保存时机械校验，缺省字段才由
Pi main 判断。完整 key 列表见 [API](api.md#host-command-config)。

```text
/goal-plus inner_agent=autoresearch max_parallel=4 annotator=B workers=C,D optimize ...
```

普通 Git Search 可选 `inner_agent=autoresearch` 或 `inner_agent=common`。两者都能使用
Goal Plus MCP；`common` 每次 dispatch 调用一次 verifier 后返回。

已经运行的 Pi session 就是 main agent，`/goal-plus` 不能切换它的模型。应在启动时使用
`pi --model MODEL`，或在提交 Goal Plus prompt 前使用 `/model`。`main=` 不是受支持的
Goal Plus 命令配置；新命令填写它会在创建记录前失败。

`annotator=` 冻结 `strategy.evidence_annotator.model`；`workers=` 将一个或多个模型解析到
`strategy.models`。省略某个 role 时保留 host default 或正常继承，不从其他显式 role 推导。

多个 provider 暴露同一模型 ID 时，使用 `provider/model`：

```text
/goal-plus max_parallel=4 workers=openai-codex/gpt-5.6-terra,openai-codex/gpt-5.6-sol optimize ...
/goal-plus max_parallel=4 workers=openai-codex/gpt-5.6-terra*1,openai-codex/gpt-5.6-sol*3 optimize ...
```

Goal Plus 通过 `goal_plus_list_models(agent_harness="pi", runtime_provider="direct")`
调用 Pi RPC `get_available_models`；默认返回 `provider`/`model_id` 和 reasoning 能力，传 `detail=true` 才返回
完整目录。短 ID
只有在唯一匹配时才会接受。命令未写 count 时不固定各模型数量；spec 也省略 count 时按 round-robin 分配；`A*1,B*3` 会生成显式
count，且总数必须等于 `max_parallel`。`models=` 是 `workers=` 的兼容别名，两者不能同时出现。最终
`selected_models` 对每个 candidate/native session 不可变；同时省略 `workers=` 和 `models=` 时
继承当前 Main 的 exact native model 与 thinking。

`/goal-plus` command 和 `goal_plus_search_freeze_spec` extension gate 只收集显式 worker、annotator
model。匹配当前 Main 的显式引用直接使用 `ctx.model` 对象，其他引用从 native registry
唯一解析；这些检查只读取本地 model catalog，不发送合成 `hi` 请求。extension 直接从
`ctx.model` 和当前 thinking 建立
execution scope 的非敏感 native launch snapshot；candidate、Annotation、普通 subagent 和 final
check 在没有显式 override 时使用该 snapshot，避免新 Pi 进程回落到机器级 default。该 snapshot
不进入 SearchSpec，不使用 Goal Plus model 环境变量，也不包含或解析 API key、endpoint 或独立
provider 配置。catalog 可发现不等于真实推理可用；实际 invocation 失败会以结构化 model failure
写入 session metadata、monitor 和 report。

## Worker 上下文与恢复

candidate worker 初始只注册 `goal_plus_search_get_agent_context`。取得 runtime contract 后再注册
Global Evidence、reference、verifier 和完整 iteration history 工具；只有启用 supplemental
evaluation 或 `shared_dir` 时才分别注册 detail 或共享工具。`goal_plus_search_reference_open` 返回当前
worker 唯一的
reference checkout 路径；worker 直接用普通文件和命令工具查看，再次调用时同一目录切到新的
Evidence commit。

`goal_plus_search_get_agent_context` 返回有界的 best/recent 摘要、权威 ID、精简 `candidate_task` 和
`.gp` ledger 来源；只有 inner binding 请求 workspace projection 时才返回 `results.tsv` 路径。
内置 `inner_agent=autoresearch` 即 AutoResearch-Git：Git outer 使用 candidate worktree
repository，ThinkThread outer 使用 Child-private independent repository；两者都有物理
`results.tsv`。`inner_agent=common` 不启用这套 inner history，也不返回该路径。
只有确实需要旧 verifier 的完整细节时，才调用可能很大的
`goal_plus_search_list_iterations`。

`candidate_task.acceptance` 只包含 FrozenSpec `candidate_acceptance` 中显式声明的稳定验收
条件；objective 和 metric 位于 context 顶层，`constraints` 不会投影给 worker。
`candidate_task.process_environment` 包含从 freeze 设备快照一一分配的 candidate 级资源环境。
Pi supervisor 会把后者注入初始 worker 与 continuation 子进程，worker 启动的本地命令和
runtime 正式 verifier 因而使用同一设备映射。设备快照只能来自 host 预先注册的
`GOAL_PLUS_DEVICE_POOLS`；SearchSpec 仅引用 `pool_id`。每个引用 pool 的设备数必须不少于
`budget.max_parallel`。多个进程或容器共享同一设备时，还必须把
`GOAL_PLUS_VERIFIER_RESOURCE_LOCK_DIR` 映射到同一个 host 目录，供 active run 的 pool lease
和正式 verifier resource lock 共用。

worker 始终把 `context.candidate_task.workspace` 作为文件与 shell 操作的显式 cwd，不依赖
wrapper 启动目录。Pi worker 进程使用 `--no-context-files`，不会额外加载仓库级
`AGENTS.md`/`CLAUDE.md`。任务局部 Skill 无需 spec 配置：runtime 只发现 source Git 已跟踪的
`.agents/skills/<name>/SKILL.md`，并要求该 Skill 目录内所有文件都由 Git 跟踪；发现结果持久化，
通过 `candidate_task.task_skill_paths` 返回当前 worktree 的绝对路径，作为唯一清单。launch prompt
和 instructions 不展开路径；worker 按任务、角色和 provider 选择适用 Skill，选中后完整读取这份
清单中的 `SKILL.md` 并显式遵循，只按需读取同目录资源。同一 session 中已完整加载且内容未变时
复用；上下文被截断、压缩、缺失或文件变化时补读。仓外 Skill 继续由 host 管理；运行时不扫描或复制
parent/global/plugin 或 peer candidate Skill。

manual 模式下，worker 在第一次修改前读取 Global Evidence，之后按配置周期或停滞节点刷新；auto
模式使用 verifier 自动注入的共享快照，independent 模式不提供 peer Evidence。worker 独立选择方向，
交付修改限于 `allowed_files`，并可在 `.tmp/` 保存 scratch 和更新 `handoff.json`。每项实质变更用
一句话 `hypothesis` 调用 verifier；`null` View 只表示 annotator 尚未发布，不要求等待。

同进程 continuation 沿用已经加载的 context 和 Evidence。跨进程恢复同一 native session 时，
extension 从 session 恢复 workspace 和已启用能力的最小投影，不把完整 context 再次注入模型；
完整 worker contract 和已加载 Evidence 仍由持久 session 保留。

模型因上下文限制结束时，host 保存本次调用观测；共同 controller 不自动创建替代 session。
需要新对话时，Main 使用 `goal_plus_session_run(new_session=true)` 接手当前 workspace，
保留未验证修改、durable Evidence 和累计预算。

无法恢复 native session 时，handoff、candidate Git state 和 `.gp` verifier history
是持久恢复依据。main 只发送中性 continuation directive，不替 worker 选择技术方向。

## 工具、Observability 与日志

`goal-plus-pi-tool` 是 Pi 本地 JSON CLI facade，与 MCP 暴露相同的 GoalPlusTools/SearchTools，
共同 session 操作也通过同一 MCP 合同接线，不提供旧 pool 操作。只读 monitor 示例：

```bash
goal-plus-pi-tool goal_plus_monitor_snapshot \
  --root .gp \
  --args-json '{"run_id":"run_..."}' \
  --pretty
```

`goal_plus_monitor_snapshot` 不启动、等待或停止 worker。
`goal_plus_search_get_agent_observability(agent_session_id)` 返回与 Codex 相同的归一化 schema；Pi 将
`pi_metrics` 中的 model、thinking level、duration、usage/cost、context 和 log/session path
投影到该结构。当已产生 token 但 Pi 因缺少单价而返回零费用时，归一化结果将 cost 保留为未知，
不影响运行状态。

普通 Ultra work 可以启用 `orchestration` feature plugin。Pi wrapper 会把
共同 `goal_plus_run_work_item` 保存为 namespaced event metadata；plugin
将其投影成与 Codex 相同的 task/result/main-decision 结构，但不会改变 Pi RPC process ownership。
详见 [Monitor Feature Plugin](adaptive-search.md#monitor-feature-plugin)。

Search state、candidate commit 和 workspace 以 `.gp/` 为准。
默认 worker log 是只含元数据的 event log：

```text
.gp/host-logs/pi-rpc-<agent_session_id>.jsonl
```

它记录 event/tool status、有界 error、timing 和 usage，不含 prompt 或 reasoning。只有短时聚焦调试
确实需要完整 RPC payload 时才设置 `GOAL_PLUS_PI_RAW_LOG=1`；raw stream 可能很大。

已绑定 handle 中的 `metadata.pi_metrics` 保存 model、thinking level、timeout/failure Evidence 和有界
`metadata.progress_handoff`。timeout 表示 deadline enforcement 生效；runner failure 会绑定
synthetic failure metadata，避免 monitor 把 session 误判为仍然存活。

完整 API 见 [API](api.md)，跨 host 诊断见[调试运行时状态](debugging-runtime.md)。

## 支持范围与验证

Pi 当前只支持以下可移植内置策略：

- `agent_guided`、`agent`、`default`
- `random`、`random_mode`

快速测试：

```bash
pytest -m pi -q
```

真实 host smoke test：

```bash
pytest --run-system-tests -n 0 -m "st and st_pi_rpc" \
  tests/st/test_st_session_host.py -v -s -rs
```

该场景通过共同 MCP 验证同 session 两轮候选交付、冷恢复、精确 Evidence 和资源关闭。

2026-10-16 在 `examples/circle-packing-git-apply` 完成了一次 direct、非 ThinkThread
Pi E2E，使用 `gpt-6-luna`、`low` reasoning。`c002` 被选中并 apply，公开 verifier
报告 `valid=true`、恰好 26 个圆、`sum_radii=2.4475165877493406`，最后源码 Git
worktree 保持干净。本次运行的报告保存在 checkout 本地
`.tmp/e2e-circle-pi-gpt6luna-low-hinted-*` 目录；该目录属于运行证据，不纳入提交。
