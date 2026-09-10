# Pi

Pi 以可安装 package 的形式提供一个面向用户的 `goal-plus` skill 和一个 extension，底层复用共享 Goal
Plus runtime。它不修改 Pi core，也不暴露独立的用户级 `search` skill；开发 checkout
可以通过隔离的开发启动器直接加载同一组 asset。

## Execution Scope

Pi main 在第一次启动 Goal Plus worker、Search pool 或 final checker 时惰性创建 host-local
Execution Scope。`execution_scope_id` 由 extension 内部传递，不是模型工具参数。

用户 abort 当前 agent 时，`agent_end` 的 `stopReason=aborted` 会 interrupt scope；quit、reload、
new、resume 或 fork 导致 extension teardown 时，`session_shutdown` 关闭旧 scope。独立 guardian
同时监视 owner PID，因此 SIGTERM/SIGKILL 不依赖 shutdown handler。关闭只停止活计算，不恢复、
接管或改变 `.gp` 中的 Goal/Search 状态。
运行中的 guardian 会对 `closing/cleanup_failed` 持续重试，直到持久化关闭；Main 未退出但
已经请求关闭时也适用。它不解除 scope fence，不自动重启已丢失的 guardian 进程。

本地 pool wrapper 与 scope/drainer 共用 Linux `/proc` 身份检查和有界组停止。Pool close
须返回 `state=closed` 才表示已核验 wrapper 资源退出，job 的 `active_count=0` 不替代这一点；
不可读身份或无归属依据的旧进程组会保留关闭失败。RPC 独立组也通过共同驱动核验和停止，
仅清理成功后完成 scope token；跨重启资源责任仍有未完成项，具体边界见
[Host Adapters](agent-host-adapters.md#main-owner-生命周期)。

## 安装与快速开始

### 正常安装

在当前 Muyuan checkout 的 Goal Plus source directory 中运行统一安装器：

```bash
cd <Muyuan checkout>/plugins/goal-plus
./install.sh --pi
```

安装器通过 uv 准备受管 Python 3.12 和锁定依赖，验证 build 后注册不可变 Pi package 副本。
Extension 从自己的包目录读取解释器与 build，不从 Conda、`PATH` 或目标 workspace 加载代码。
同一来源的更新创建新版本；其他来源或散落的同名 extension/skill 会报出冲突并停止。
`./install.sh --runtime-info` 可检查安装身份；正常安装后不依赖原 checkout 持续存在。

正常安装后可以从目标 workspace 启动交互会话，或直接提交一次 prompt：

```bash
pi -p "/goal-plus inspect this repository"
```

移除 Pi 集成：

```bash
./uninstall.sh --pi
```

关闭旧会话且其他宿主不再引用运行时后，运行 `./uninstall.sh --runtime` 清理未引用的受管版本。
旧 pip/Conda 安装不被自动修改。

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
次安装副本的 Goal Plus extension/skill，以只读方式开放该副本及其受管 Python environment，并将
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
/goal-plus workspace_backend=thinkthread max_parallel=2 <可度量目标>
```

对应 SearchSpec 使用 `orchestration_mode="parallel_loops"` 和
`workspace.backend="thinkthread"`；Pi 身份从 Main 会话解析，不另填 host/profile。
普通 Pi 进程继续使用 `pi-rpc + git_worktree`；两条路径不能混用，也不能在 preflight
失败时 fallback。Codex 尚不支持 ThinkThread worker host。

ThinkThread 可独立升级，接口兼容时无需重装 Goal Plus 或 SDK。Runtime 在启动 candidate 前
校验 Root direct filesystem 和 delegated Child capability；SDK 协议版本与 contract fingerprint
差异只给出 warning，接口调用和结果仍须满足实际合同。每个 initial
candidate 从同一个 Root-owned `S0` 启动独立 Pi Child/Session/private branch；Child 只拿到
逻辑 `source_subdir`，不会得到可访问的 Root 本地路径。worker turn 结束且 Agent ready 后，
controller 固定 private branch snapshot，并让 process verifier 对同一个 immutable snapshot
执行 `fs.run(writes="discard")`。只有该 verifier-backed snapshot 能进入 Evidence 和 selection。
Child 调用 `search_run_verifier` 后先收到带稳定 `attempt_id` 的 `accepted` receipt，并立即结束
当前 turn；Root 完成 snapshot、`ScoreReport` 记录和必要的 branch reset 后，把同一份 typed
feedback 同时放入 `candidate_ready` 和该 retained Child 的下一次 wake。accepted 本身不是
verifier 结果；没有 accepted attempt 就结束的 runtime-integrated turn 会 fail closed。

ThinkThread Evidence annotator 是每个 run 保留的独立 Child。它只激活 annotation RPC 和
`list/stat/pread/diff` Artifact View 工具；candidate 工具与 annotation 工具双向隔离。Annotation
不运行命令或 verifier，也不为读取创建、patch 或删除 snapshot。
超限 diff 不进入 prompt，annotator 必须从 target/peer immutable snapshot 读取完整内容；
selection 会先推进 queue generation，再 interrupt annotator，因此迟到结果不能发布。

`search_promote` 从 `S0` 与 selected snapshot 生成只包含允许路径的 publication snapshot，
`search_apply_promotion` 使用稳定 RequestId 和 `safe_content_v1` 乐观 `fs.apply`。Root 只修改
publication delta 外路径时保留该修改并 apply；changed path 或声明的 dependency 变化会原子
转为 `awaiting_main_integration` 并生成 durable pending artifact。响应状态不确定时保持
`recovering/needs_recovery`，不发送成功或普通失败通知；重启后仍以同一 RequestId 查询并收敛。
报告持久化后，cleanup 只删除本 run 所有且无 Evidence/publication/recovery pin 的资源。

对称卸载命令是 `./uninstall.sh --pi --thinkthread`：`--thinkthread` 只删除当前 checkout 生成的
`goal-plus-pi` Profile。它绝不删除 ThinkThread Runtime、Root 或 state；这些资源由 ThinkThread
管理员独立管理。需要同时移除不再被其他 host 使用的 Goal Plus Python package 时，再显式增加
`--runtime`。

### Checkout 开发安装

```bash
cd <Muyuan checkout>/plugins/goal-plus
./install.sh --pi-dev
source .goal-plus-dev/set_env.sh
cd <target-workspace>
pi-goal-plus
```

开发安装在 `.goal-plus-dev/venv` 中 editable 安装当前 checkout，并生成
`.goal-plus-dev/bin/pi-goal-plus`。`set_env.sh` 只把这个 launcher 目录加入 `PATH`，不会修改
`PI_CODING_AGENT_DIR`、`CODEX_HOME` 或认证信息；launcher 自己临时加入 venv 的 `bin`，再用
`--extension` 和 `--skill` 显式加载当前 checkout。

`./install.sh --env-show` 会只读列出安装器已登记及当前 Git worktree 中的 Pi 开发环境、
源码 commit、状态、单项与总空间占用。

开发态不注册全局 Pi package。若当前用户或目标项目显式引用同一 `assets/pi/` extension/skill，
Pi 会按绝对资源路径去重。旧式源码根目录 package 注册需先移除；来自其他 checkout 或复制
路径的 Goal Plus 仍会让安装和每次启动停止并列出冲突。
断链的旧式 loose-resource symlink 不会被 Pi 加载，也不会阻塞开发态。遇到真实冲突时先显式
`pi remove <reported-source>`，再运行 `pi-goal-plus`。`--pi` 和 `--pi-dev` 不能同时使用。

正常安装和开发安装都只暴露一个面向用户的 `goal-plus` Pi skill。

#### Progress UI 开发预览

extension 可以在不创建 Goal Plus record、Search run、candidate workspace 或模型请求的情况下显示
fixture-backed Search progress。这只是开发预览，不是 benchmark Evidence：

```bash
GOAL_PLUS_UI_PREVIEW=active \
  pi --offline --no-session --no-extensions \
  -e assets/pi/extensions/goal-plus.ts
```

在该 session 中使用 `/goal-plus-ui-preview active`、`/goal-plus-ui-preview complete`、
`/goal-plus-ui-preview result` 或 `/goal-plus-ui-preview clear` 切换状态。`result` 会用 Circle Packing
的真实样例数值追加一张终态完成卡，不需要运行模型、verifier 或完整 Goal Plus 流程；普通 session
不注册该命令。体验目标和字段语义见 [Lightweight Search Progress UX](usability-progress.md)。

也可以在交互式 Pi 中直接使用：

```text
/goal-plus Fix this bug and verify the test suite.
/goal-plus mode=probe Check whether vectorization is viable.
/goal-plus mode=autonomous Deeply optimize the kernel.
```

`/goal-plus edit` 和 `/goal-plus-with-final-check` 与 Codex 使用同一套 goal revision 语义。
required final check 由独立、只读的 Pi RPC reviewer 执行。

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
新的 Pi session 可用 `/goal-plus <完整目标>` 在同一 workspace 创建独立 Goal；其他 session 的
未终态 Goal 不会阻止创建。Search bootstrap 的 `search_freeze_spec` 和 `search_create` 必须传入
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
project trust、active tool 数量，以及 Pi 没有内置 sandbox/审批的事实；展开后显示
实际 cwd 和 active builtin/non-builtin tool 名单。这里的 `--approve` 或
`project_trusted=true` 只表示允许加载项目内 Pi 资源，不表示 full access。
filesystem 只报告为 `launcher-process-user`，network 只报告为
`host-policy-unreported`，不会从 settings 或父进程猜测不存在的 Pi permission profile。

Goal Plus 启动的 ordinary、final-checker 和 Search candidate `pi-rpc` 子进程会在
各自 `session_start` 写入同一结构。wrapper 在发送第一条模型 prompt 前从该
native session 读回快照，写入 host event log 的 `goal_plus_runtime_start` 事件、
handle `metadata.runtime_start` 和 `pi_metrics.runtime_start`。
`search_get_agent_observability` 将其投影为 `runtime_start`；快照缺失时保留
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

Pi driver 补做 final verifier 或复用已结算 Evidence 后，也从 host 收尾路径重扫内部任务，
不依赖已结束的 worker 再调用工具。唤醒错误单独记录，不撤销硬结算或将成功 verifier 改为
失败。Adaptive 的 parent 补验可以生成内部 Annotation/Verifier 输入，但不成为 peer
Global Evidence；monitor snapshot 仍只读，不承担唤醒。

main turn 结束时，仍为 active 的 Goal Plus record 会携带完整 raw goal 和 elapsed-time context
继续执行。Pi 只有在 agent 写入终态后才停止；worker watchdog 到期只是一次 dispatch event，
不代表 goal 完成。

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

Search worker 是由 host-local durable pool 监督的 Pi RPC 进程。每个 wrapper 通过
`goal-plus-pi-worker` 启动前台 `pi --mode rpc` 子进程；native session 位于
`.gp/host-sessions/pi/`，pool 状态位于 `.gp/host-pools/pi/`。进程可以重启，但 candidate workspace
和 native session 提供连续性。

## 普通 Goal Mode

普通任务使用 [`goal-plus-ultra-v1`](ultra.md) 轻量 dispatch 记录，不预建 work DAG。Pi binding 保留 Main 在提交
`/goal-plus` 时已经选择的 thinking 档位并记录实际 level，不会强制改档。复杂任务推荐 `xhigh`，
但当前模型不支持 `xhigh` 或用户选择其他档位时仍正常运行。

实际派发普通 subagent 时，Main 把完整任务直接传给 `pi_goal_plus_run_work_item`。该工具继承
Main 当前 thinking，懒创建 attempt，启动隔离的 Pi RPC child，仅在 RPC `get_state` 握手和
请求的模型/thinking 设置成功后绑定 session，
等待 child 完成，并用同一个 `attempt_id` 和 `generation` 提交 result。多个相互独立的 tool call
可以并发执行；复用已返回的 id 会记录 rework 并恢复原 session。

Pi wrapper 只拥有 native launch/wait。main 仍负责 acceptance、rework、integration、Search routing
和 completion。完整轻量记录协议见 [Ultra Orchestration](ultra.md)。

Search promotion 后，Pi 用 `goal_plus_record_search_result` 登记“搜索完成并验收”，再在原始工作区
检查实际应用的 diff 和整体测试。最终状态 evidence 另行登记“补丁应用完成并验收”。这两项只是
收尾证据边界，不创建 DAG 或额外运行时阶段。

## Search Pool 流程

Pi Search 使用 `strategy.orchestration_mode="parallel_loops"` 和固定的初始 lane：

1. 只规划并物化一次初始 candidate；
2. 调用 `pi_search_pool_open(..., max_parallel=<frozen limit>)`；
3. 用 `pi_search_pool_wait_any` 等待任意终态 event；
4. 收到 `candidate_ready` 后结算 verifier-backed best；若全局停止条件尚未满足，对同一 candidate
   调用 `pi_search_pool_continue`；
5. main turn 中断后，用 `pi_search_pool_snapshot(run_id=...)` 重新发现 pool；
6. 结束时调用 `pi_search_pool_close`，再 select 和 promote；默认 promotion mode 必须 apply，
   只有显式 `artifact_only` 才保留 publication；
7. 记录 Search result，完成 raw-goal audit，设置 Goal Plus 终态，并为每个已记录 run 生成一次 final
   report。

supervisor 只维护初始固定 candidate，不提供 submit/refill，也不会因为低分或没有提升替换 candidate。
`candidate_ready` 必须同时满足 minimum lease 已释放、handle 已绑定且 durable Evidence 已落盘；
`timed_out`、`interrupted` 和 `failed` 都不能作为 ready candidate 继续。

driver 复用匹配的 worker Evidence，不创建重复的 parent process iteration。只有 workspace 已改变但缺少
worker Evidence 时，parent 才补做 final verifier；未改变的 workspace 不会增加 baseline iteration。

## 模型与 Worker Spec

本地 Pi Search 自动绑定 Pi RPC 驱动，并使用 wall-clock budget：

```json
{
  "strategy": {
    "name": "random",
    "orchestration_mode": "parallel_loops",
    "worker_budget": {
      "min_runtime_seconds": 500,
      "min_verifier_runs": 1,
      "max_runtime_seconds": 600,
      "max_turns": 8,
      "on_exceed": "interrupt"
    }
  }
}
```

`max_runtime_seconds` 必填。`max_turns` 只是 prompt 提示。可选 `min_runtime_seconds` 和
`min_verifier_runs` 由 pool wrapper 累计执行：Pi turn 提前结束时，会在同一 slot/worktree 恢复相同
native session，并只获得剩余上限预算。达到 hard deadline 前，runner 发送一次 closeout steer；
基础设施故障、pool close 或外层 closeout 会停止自动续跑。

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

Goal Plus 通过 `goal_plus_list_models(host="pi-rpc")` 调用 Pi RPC `get_available_models`。短 ID
只有在唯一匹配时才会接受。没有 count 的条目按 round-robin 分配；`A*1,B*3` 会生成显式
count，且总数必须等于 `max_parallel`。`models=` 是 `workers=` 的兼容别名，两者不能同时出现。最终
`selected_models` 对每个 candidate/native session 不可变；同时省略 `workers=` 和 `models=` 时
继承当前 Main 的 exact native model 与 thinking。

`/goal-plus` command 和 `search_freeze_spec` extension gate 只收集显式 worker、annotator
model。匹配当前 Main 的显式引用直接使用 `ctx.model` 对象，其他引用从 native registry
唯一解析；随后按模型去重，通过一次 `Promise.all` 并发调用 `ctx.modelRegistry.complete()` 发送
最小 `hi` 请求。任一失败时在 agent 启动前拒绝创建或 freeze。没有显式 model 时完全不 probe，
正在运行的 Main 已经证明其当前模型可用。extension 直接从 `ctx.model` 和当前 thinking 建立
execution scope 的非敏感 native launch snapshot；candidate、Annotation、普通 subagent 和 final
check 在没有显式 override 时使用该 snapshot，避免新 Pi 进程回落到机器级 default。该 snapshot
不进入 SearchSpec，不使用 Goal Plus model 环境变量，也不包含或解析 API key、endpoint 或独立
provider 配置。catalog 可发现不等于真实推理可用。

## Worker 上下文与恢复

candidate worker 初始只注册 `search_get_agent_context`。取得 runtime contract 后再注册
Global Evidence、reference、verifier 和完整 iteration history 工具；只有启用 supplemental
evaluation 或 `shared_dir` 时才分别注册 detail 或共享工具。`search_reference_open` 返回当前
worker 唯一的
reference checkout 路径；worker 直接用普通文件和命令工具查看，再次调用时同一目录切到新的
Evidence commit。

`search_get_agent_context` 返回有界的 best/recent 摘要、权威 ID、精简 `candidate_task` 和
`.gp` ledger 来源；只有 inner binding 请求 workspace projection 时才返回 `results.tsv` 路径。
内置 `inner_agent=autoresearch` 即 AutoResearch-Git：Git outer 使用 candidate worktree
repository，ThinkThread outer 使用 Child-private independent repository；两者都有物理
`results.tsv`。`inner_agent=common` 不启用这套 inner history，也不返回该路径。
只有确实需要旧 verifier 的完整细节时，才调用可能很大的
`search_list_iterations`。

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
`.agents/skills/<name>/SKILL.md`，并要求该 Skill 目录内所有文件都由 Git 跟踪；在 freeze 的
`task_skill_notice` 和 launch prompt 中明确列出，
并通过 `candidate_task.task_skill_paths` 返回当前 worktree 的绝对路径。worker 完整读取这份
清单，不扫描 parent/global/plugin 或其他 candidate 中的 Skill。

manual 模式下，worker 在第一次修改前读取 Global Evidence，之后按配置周期或停滞节点刷新；auto
模式使用 verifier 自动注入的共享快照，independent 模式不提供 peer Evidence。worker 独立选择方向，
交付修改限于 `allowed_files`，并可在 `.tmp/` 保存 scratch 和更新 `handoff.json`。每项实质变更用
一句话 `hypothesis` 调用 verifier；`null` View 只表示 annotator 尚未发布，不要求等待。

同进程 continuation 沿用已经加载的 context 和 Evidence。跨进程恢复同一 native session 时，
extension 从 session 恢复 workspace 和已启用能力的最小投影，不把完整 context 再次注入模型；
完整 worker contract 和已加载 Evidence 仍由持久 session 保留。

如果 assistant response 以 `stopReason="length"` 结束且没有 tool call，worker-local extension
不再续接该上下文。driver 会返回 refresh signal，由 pool supervisor 为同一 candidate 和 workspace
创建 fresh native session 与新的 `agent_session_id`；Git 修改和 durable Evidence 继续保留。

无法恢复 native session 时，handoff、candidate Git state 和 `.gp` verifier history
是持久恢复依据。main 只发送中性 continuation directive，不替 worker 选择技术方向。

## 工具、Observability 与日志

`goal-plus-pi-tool` 是 Pi 本地 JSON CLI facade，与 MCP 暴露相同的 GoalPlusTools/SearchTools，
并提供 host-local pool 操作。只读 monitor 示例：

```bash
goal-plus-pi-tool goal_plus_monitor_snapshot \
  --root .gp \
  --args-json '{"run_id":"run_..."}' \
  --pretty
```

`goal_plus_monitor_snapshot` 不启动、等待或停止 worker。
`search_get_agent_observability(agent_session_id)` 返回与 Codex 相同的归一化 schema；Pi 将
`pi_metrics` 中的 model、thinking level、duration、usage/cost、context 和 log/session path
投影到该结构。当已产生 token 但 Pi 因缺少单价而返回零费用时，归一化结果将 cost 保留为未知，
不影响运行状态。

普通 Ultra work 可以启用 `orchestration` feature plugin。Pi wrapper 会把
`pi_goal_plus_run_work_item` 和 `goal-plus-pi-worker` 保存为 namespaced event metadata；plugin
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
ST_PI_CYCLE_WORKER_SECONDS=120 \
  pytest --run-system-tests -n 0 -m "st and st_pi_rpc" -k managed_pool_wait_any -v -s -rs
```

该场景启动两个独立 Pi RPC worker，通过 `run_id` 重新发现 pool，观察 wait-any completion，并正常 drain。
