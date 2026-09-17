# Agent Host Adapters

Goal Plus 的 Search runtime 负责持久状态、candidate workspace、verifier 和最终选择。
Codex 或 Pi host 负责实际启动、等待、续跑和停止 worker。Host Adapter 位于两者之间，
把 runtime 的统一请求转换为 host 原生操作；它不改变 Search 的结算语义。

系统整体边界见[共享平面](shared-plane.md)。本文只说明 Host Adapter 的统一契约、Codex/Pi 差异和恢复要求。
Main Goal 的命令、状态和宿主原生 session 恢复设计统一记录在[运行逻辑](runtime-logic.md)，
其中区分当前实现与目标行为；本文的
continuation 主要指 Search candidate worker 的续跑。

普通 Goal Mode 不使用这套 Search Adapter，而是遵循 [`goal-plus-ultra-v1`](ultra.md) 的
轻量 dispatch 合同。Ultra 可以由 Pi、Codex 或外部 harness 实现，不受 `AgentHarnessKind`
限制；进入 verifier-guided Search Mode 后，使用本文的 candidate session 合同。

## 统一运行模型

`agent_harness` 表示 Codex/Pi 身份，`runtime_provider` 表示 direct/ThinkThread 执行托管，
`workspace.provider` 表示 Copy/Git worktree/ThinkThread 文件空间。传输名 `pi-rpc` 只在
具体 adapter 和进程标签中使用。装配先选择 runtime provider，direct 内再选择 harness；
ThinkThread 的 Child/Message、controller lease 和延后结算保留在原执行路径。
静态组合配置与工作区能力均通过窄接口消费，不把完整 Runtime 或 store 传给执行器。
Goal Plus 任务编排运行时负责业务状态，ThinkThread Agent Runtime 负责原生托管，
受管 Python 环境只负责安装依赖与解释器，三者职责不同。

[Ports session 合同](../src/goal_plus/ports/session.py)定义单轮请求、结果、控制操作和
共同 `SessionControlTools`；[Ports host 合同](../src/goal_plus/ports/agent_hosts.py)定义
`AgentExecutionCapabilities`、`AgentExecutionBinding`、model catalog 和 candidate launch 的窄接口。
[静态宿主绑定](../src/goal_plus/infrastructure/host_bindings.py)提供 capability 与 inner-agent
资产映射，不加载执行器；`agent_hosts.py` 保留具体 model discovery、launch 转换和 observation。
Worker policy 的 `session_control` 发布同一套 run/wait/message/interrupt/stop/close
工具名称。MCP 的可信 launcher 驱动实际 adapter；声明本身不代表完成了模型执行或资源关闭。
同一 session 的调用使用独立 `call_id`，结果含准确 native invocation。wait 不取消执行，
interrupt 作用于当前轮，stop 终止执行，close 释放资源；未完成清理保留可重试身份。
TT stop 后通过显式 open 重新授权，确认旧执行和回调退出后复用原 Child；close 后拒绝重开。
TT turn interrupt 经精确 Message 控制请求调用原生 abort；准确确认与本轮静默共同决定
interrupted 结果，平台将原生 abort 回报为 completed 时也不触发 verifier settlement。

Adapter 返回的 launch payload 由可信 launcher 消费。Main 使用共同控制工具，
不解释 native spawn 参数或补造身份 metadata。旧 pool DTO、派发队列和模型工具已删除。

Search runtime 不承担 worker supervisor 职责。session run 内部创建的领域 session
是 context/provenance handle，不代表 worker 已经启动或仍然存活。

同一边界也适用于 Evidence Annotation 和 LLM Verifier。两者继承
`FrozenSpec.agent_harness` 记录的 Main Agent 身份，Annotation 同时使用冻结的 runtime provider；
LLM Verifier 使用 direct 执行，不接受独立身份选择；
Codex hook 或 Pi extension 从当前 native host 进程唤醒 run-scoped queue，
内部子进程完整继承该进程配置。持久 profile 只记录 agent_harness/runtime_provider/model/reasoning/timeout 和
`configuration_source="host_process"`，不复制外部连接或认证配置。执行器在调用模型前校验
launcher 写入的 host provenance，shared runtime/MCP 不提供备用启动路径。

`internal_agents.py` 是这些 native callback 的共同入口，先通过 typed query 核对冻结 host，
再检查 scope admission。Verifier、Evidence/context 读取和 Search status/select/invalidate/report
的 post-tool 回调都可重扫持久队列；Codex worker-stop 和 TT controller 的既有唤醒继续保留。
Pi 的可信 host command 只接受冻结的 Pi RPC 或 TT driver，TT 的 Root/capability 校验仍在
原 driver 执行，不因此开启 Codex+TT 或 Adaptive+TT。

Search selection 只提交领域终态和内部 queue fence。模型关闭由 host pump 和既有
scope cleanup 执行；清理失败保留句柄及错误，后续 host 调用重试。Annotation 与 Verifier
分别消费自己的可靠队列，任一回调失败都不回滚已提交的 hard settlement。`annotation_host.py` 直接装配 typed 用例，
TT annotator 不再通过 candidate `SearchHostService` 构造完整 Runtime 或读取私有 store。
这些模块合同已有受控测试；真实 callback、native 退出联动仍需开发者显式要求的 ST 证据。

## Worktree 与 Skill bootstrap

Git-worktree candidate 由 runtime 物化到独立 worktree，可信 session launcher 以该工作区
启动 Codex app-server 或 Pi RPC。worker 仍须调用 `goal_plus_search_get_agent_context`，并把返回的
`context.candidate_task.workspace`
作为每次文件和 shell 操作的显式 cwd。ThinkThread candidate 已在 private Child cwd 内，
不返回不可访问的 Root 本地
`workspace`；它使用 `workspace_access.logical_root="."` 和 `source_subdir`。parent cwd、其他
candidate workspace 和 `.gp` 内部路径都不是 candidate 工作目录。

任务局部 Skill 采用固定约定，不进入 SearchSpec：只识别 source Git index 中
`.agents/skills/<name>/SKILL.md`，且要求该 Skill 目录内所有文件都由 Git 跟踪。freeze 持久化
发现结果；candidate context 通过 `candidate_task.task_skill_paths` 返回该 candidate 自己的
绝对路径，launch message 同时输出 `task_local_skills_visible`。freeze/batch 的 Main 回执不
重复技能清单或 candidate instructions。worker 完整读取清单中的
`SKILL.md` 并显式遵循，只按需读取同目录资源。仓外 Skill 继续由 host 管理；运行时不扫描或复制
parent/global/plugin 或 peer candidate Skill。

## Inner Agent Binding

`strategy.inner_agent` 是 host-neutral 的 worker 行为 adapter ID，不是 Codex/Pi 原生角色名。
当前 Git 和 ThinkThread outer provider 都支持：

| ID | Dispatch 行为 | MCP 合同 | Workspace ledger projection |
| --- | --- | --- | --- |
| `autoresearch` | 内置 AutoResearch-Git；自主执行多轮 hypothesis/edit/verify，TT 每次 accepted 后结束当前 wake | 每轮调用 `goal_plus_search_run_verifier` | `results_tsv` |
| `common` | 一个 dispatch 完成一个有界 attempt 后返回 | 读取 context，调用一次 `goal_plus_search_run_verifier` | `none` |

runtime 将 ID 解析为持久化的 host binding，再由 Codex/Pi/ThinkThread adapter 选择对应 prompt asset 和
native launch 参数。`.gp` 始终是 canonical iteration ledger；binding 中的
`workspace_ledger_projection` 只决定 trusted runtime/host adapter 是否额外物化 candidate-local
视图。兼容 ID `autoresearch` 的内置版本为 `builtin-autoresearch-git-v1`：Git outer 复用 candidate
worktree repository；TT outer 在 Child 内初始化独立 private Git，提交每次 attempt、restore 和
物理 `results.tsv` ledger。`common` 不启用这些 inner-agent mechanics，即使 outer workspace 是
Git。旧 `worker_agent_type` native-role 字段已删除，包含该字段的 SearchSpec 会被拒绝。
候选 launch 仅包含宿主内部 session 输入，native handle 使用准确 `external_id`。
unsupported adapter 在
freeze/launch 前 fail closed。

## Codex 与 Pi 的能力对比

| 能力 | Codex | Pi RPC | Pi + ThinkThread |
| --- | --- | --- | --- |
| 启动 | 共同 session run → app-server | 共同 session run → Pi RPC | 共同 session run 登记并创建 private Child |
| Candidate 进程环境 | 宿主把冻结 `process_environment` 注入候选进程 | 同一宿主输入接线 | 由 ThinkThread Profile/Runtime 控制，不接受 candidate process environment |
| 等待 | `goal_plus_session_wait` | 同一工具 | 同一工具 |
| 续跑 | `goal_plus_session_run` 复用 native session | 同一工具 | 同一工具，复用 Child/Session/branch |
| Deadline | Application 预算 gate + 宿主执行限时 | 同一接线 | parent/controller watchdog |
| 恢复 | `.gp` checkpoint/generation + scope 资源核验 | 同一恢复用例 | scope 资源日志、原 call_id/Message、独占 controller lease |
| Goal gate | 精确 `$goal-plus...` + hooks | 精确 `/goal-plus...` + extension events | 同 Pi gate + exact Profile preflight |
| 模型发现 | app-server `model/list` | Pi RPC model catalog | Profile/default 或显式 provider/model |
| Observability | native JSONL + metadata | `pi_metrics` + metadata | TT IDs + durable snapshot/request IDs |
| Workspace | `copy`、`git_worktree` | `copy`、`git_worktree` | `thinkthread` |

本地候选的进程环境和工作目录由 `native_candidate_input` 从已授权 session/task 构造，
不在 Pi launch 中另存 worker 交接副本。Pi entries 增量由存活的原生 session actor 采集，
准确 invocation 观测写入既有 provenance，续跑 launch 不传递 metrics baseline。

当前可移植 planner 为 `agent_guided`（含 `agent`、`default`）和 `random`（含
`random_mode`）。Codex/Pi RPC 的普通运行使用固定初始并行循环；Git worktree 可选
`adaptive_search`。ThinkThread 只支持 Pi Profile 和 `parallel_loops`；Codex
仍不声明 ThinkThread capability。harness/runtime/workspace 组合不匹配或 Profile preflight 失败时 runtime 在
freeze/create 边界 fail closed。

Codex Goal Plus startup 仍只接受精确 `$goal-plus...` 的 `UserPromptSubmit` 授权，并在
`PreToolUse`、`Stop` 和 `SubagentStop` checkpoint fail closed；ThinkThread 支持不会改变该
Codex hook contract。

资源分配由 runtime 在 freeze 时解析并固定，不由 Adapter 各自推断。Codex/Pi 的正式 verifier
都使用 CandidateTask 中持久化的进程环境和解析后的物理资源锁；共同候选宿主还把冻结环境
注入两端 native 进程，不能把 parent 的逻辑设备 `:0` 当作已隔离设备。

host 通过 `GOAL_PLUS_DEVICE_POOLS` 注册受信任的硬件无关映射：

```json
{
  "cuda":{"visibility_env":"CUDA_VISIBLE_DEVICES","devices":["0","1"]},
  "ascend-npu":{"visibility_env":"ASCEND_RT_VISIBLE_DEVICES","devices":["4","5"]}
}
```

SearchSpec 只能用 `{"device_pools":[{"pool_id":"cuda"}]}` 引用已注册 pool，对应 verifier
使用 `<pool_id>:0` 作为 `resource_lock`。它不能提供环境变量名或设备值。未声明 device pool
的普通 host resource lock 保持原有精确字符串语义。freeze preflight 使用第一条 candidate
lane 的单设备环境；后续宿主配置变化不会改写已冻结 run 的设备分配。每个引用 pool 必须
至少包含 `budget.max_parallel` 个设备，lane 按顺序一一分配，不会轮转复用；同一 spec 中
多个 pool 也不能使用相同的 `visibility_env`。

runtime 从 run 创建到 promoted、aborted 或 failed 期间独占每个引用 pool，避免不同 run
同时把 worker 放到同一组设备。pool lease 和 verifier resource lock 共用
`GOAL_PLUS_VERIFIER_RESOURCE_LOCK_DIR`；多个 runtime 进程或容器共享设备时，该变量必须指向
同一个 host 共享目录，否则只能保证单个文件系统范围内不超售。

ThinkThread verifier 使用相同锁目录和冻结 `resource_lock` 的精确字符串身份；相同资源串行，
不同资源并行，不从 Root 的逻辑设备编号推断远端设备。执行结果未知时保留原 request 的持久
占用，其他请求收到 `VerifierResourceNeedsRecovery`，原请求确认完成后释放。只有共享该目录
且使用同一实际资源身份的控制器参与互斥；不约束外部任务或独立机器的资源使用。

## Candidate 循环

新的 Codex/Pi spec 使用 `orchestration_mode="parallel_loops"`。一次 Search
run 只做一次初始规划，创建固定数量的 candidate lane：

1. 启动全部初始 candidate；
2. 使用共同 session wait 读取各 invocation 结果；
3. 复用该 worker 已经落盘的准确 Evidence，缺失时才由 parent 补做 verifier；
4. 如果全局停止条件尚未满足，继续同一 candidate、workspace 和 native session；
5. selection 前停止或 drain 所有活跃 worker。

`autoresearch` continuation 继续同一自主循环；`common` continuation 只下发下一个有界
attempt。两者都复用同一 candidate 和 settled outer workspace，main 不为 common worker
手工创建 checkpoint。

低分、同分或单次没有提升都不会触发 candidate 替换，Adapter 也不会自动 refill。较慢 lane 不会阻塞其他 lane 的 verifier 结算。

本地 Codex/Pi 通过共同 `goal_plus_session_run` 原地续跑；宿主内部调用共同 session
准备用例。调用返回与 durable Evidence 分别核验，原生 session 续跑仍需满足当前
Goal/run、scope、执行归属与剩余预算。本地 Pi 与 TT 的旧 pool 模型入口和内部编排均已删除。

ThinkThread Child 的 `goal_plus_search_run_verifier` 先返回带稳定 `attempt_id` 的 typed accepted receipt；
Child 随即结束 turn。共同 session actor 确认准确 Message 已静默后，由候选验收 owner
捕获 snapshot、运行 verifier 并完成 branch reset。完整 typed feedback 写入准确 invocation
结果，并注入同一 Child 的下一次 wake。Main 的 wait 默认只返回状态和结果可用标记，
`detail=true` 才展开完整 invocation。无 accepted attempt 的 completed turn 拒绝结算。
控制器退出后，从 scope-local resource journal 恢复原 call、Message 和 deadline，观察原调用，
不重复 spawn/wake；provider mutation 仍按原 request identity 恢复。一次 wait 的 timeout
只限制等待，不取消执行，也不自动续跑。各 candidate 的控制器独立推进。

正常双轮、同轮消息、运行中控制器恢复，以及 execution stop 后显式 open 复用 retained
Child 的继续路径和 interrupt/stop/close 差异均已有 Pi TT 真实证据。通用 candidate checkpoint recovery
不支持 TT，保留 session 或 snapshot 本身不授予恢复执行权限。

TT `autoresearch` binding 显式加载 Pi Search worker prompt，并由 trusted extension 在每次 verifier
request 前封存 Child-private Git attempt。Root 对 quiescent attempt snapshot 验证后，在
`fs.run(writes="snapshot")` 中恢复 best（如需要）、追加 `results.tsv` 并提交 ledger，然后把 Child
branch reset 到该 post-settlement snapshot。同一自主循环跨 retained Child wake 延续：每次 wake
最多提交一个 accepted attempt，下一次 wake 先接收完整 settled feedback，再继续下一轮。如果 Pi
worker 在正常 `agent_end` 前没有提交 verifier，host extension 在同一 wake 内只
追加一次纠偏 follow-up；第二次仍为空则照常结束，共同 session 验收因缺少 accepted attempt 失败。这里的
inner commit 只用于 Child-local history；Evidence 始终是 exact outer `fs_snapshot`。

Agent POSIX snapshot pagination 和 `pread` 在 adapter 边界严格校验 item、cursor、offset、实际解码
长度与 EOF。Durable provider mutation 的业务 outcome 和 request close 分开持久化；close
completion-unknown 不等于已关闭，后续 reconciliation 只重试 close，不重放已确认的 mutation。

## 模型与 Observability

`goal_plus_list_models` 提供只读模型发现。用户把选择写入
`strategy.models`，Adapter 在派发时将已解析模型投影到实际 host
schema；没有指定模型时，保留 host 原生继承行为。模型目录只说明当前可发现的模型，不保证旧版 host
tool 能转发所有可选字段。

模型目录也不证明真实推理可用。现有 Goal command 与 `goal_plus_search_freeze_spec` host gate 只收集显式
worker 和 annotator override，按模型去重后并发发送最小 `hi` 请求；任一请求失败就
在启动 agent 前 fail closed。没有显式 override 时不 probe，因为 Main 当前 turn 已经是当前模型
可用的证据。Codex probe 只通过原生 `codex exec -m MODEL` 切换模型；Pi probe 使用 extension
持有的 native model 对象和 `modelRegistry.complete()`。Pi 未显式覆盖的 child 从 host-local
execution scope 继承 Main 的 exact native model/thinking，而不是重新选择机器级 default。两条路径
都继承 host 配置，不建立 provider、credential 或 endpoint plumbing。

Codex hook 在宿主提供模型事实时，也把 Main 的 model/reasoning_effort 写入 execution
scope 的可信快照。产品 candidate launcher 已通过共同 session 接到 app-server 执行器，
从该快照继承 Main 模型；显式 worker override 仍优先于继承值。

所有 Adapter 都实现只读 `collect_observability`，并通过 `goal_plus_search_get_agent_observability`
暴露。该数据只用于 provenance、报告和诊断，不会把 host lifecycle 写入 Search record。

Codex rollout JSONL 提供 input、cached-input、cache-write、output 和 reasoning
token，但不提供实际账单。Goal Plus 使用 `src/goal_plus/codex_pricing.py`
中带版本、与 Pi 兼容的模型目录估算 `usage.cost_usd`；长上下文分层和 `flex`/`priority`
倍率遵循 Pi 语义。`usage.cost_estimate` 会注明目录版本和覆盖率，未知模型不套用猜测价格。

## Worker 预算

两端共同调用 `CandidateSessions.invocation_timeout`，按请求额度、准备好的 worker
`max_runtime_seconds` 和当前 Goal deadline 限制本次执行。Native session/TT Child actor
执行有界等待和超时停止；Search Runtime 不监督 worker。显式继续不会重置 Goal deadline。

`budget.max_parallel` 同时决定初始 candidate 数和活跃 worker 数。`max_turns`
对 Codex 和 Pi 都只是 prompt 提示。

`strategy.worker_budget` 可省略。没有最低探索时长/次数或提前 closeout；三个 launch
adapter 只投影同一份 session 上限。共同 session 不自动重派任务，是否再次 wake 由 Main
决定。候选返回不授予 Evidence 或 promotion 成功，交付验收仍由共同 Application 执行。

Main 通过 `strategy.models[]` 分配模型席位，`strategy.worker_launch` 设置全局
`model`、`reasoning_effort`、`service_tier` 默认值，各席位的非空设置优先。Codex 映射模型与启动偏好；
Pi 通过可信进程配置映射模型和 thinking level。

## Main owner 生命周期

Goal Plus host adapter 为实际启动的活计算附加 host-local Execution Scope。Scope 不属于
Search record，也不改变 `AgentSessionRecord` 的 provenance 语义。Pi extension 内部注入
`execution_scope_id`；Codex hook 使用 native session 和 collaboration task identity，模型不能覆盖
scope 归属。

### 宿主负责进程生命周期

GP 不读取操作系统进程表，不核验 PID、启动时间、boot identity、进程组或成员，
不区分 Linux/macOS 的进程实现，不启动 guardian，也不根据 Main 进程消失接管资源。
原生 session、TT Child 及其子进程的生命周期由 agent/宿主负责。
GP 只保留自己已持有的直接子进程句柄的等待、超时和关闭；不会从历史 PID 恢复控制权，
不会发送进程组信号或承诺遗留进程、逃逸后代已经退出。

Scope 由可信 harness、native session 和业务 activation 标识。开放 scope 可复用；
关闭的同一 activation 不可重开，新的显式授权可创建下一代 scope。
PID 仅作为启动回执中的诊断值，不参与 admission、恢复授权或关闭 gate。
原生 session_start、reload 和 compact 不因 PID 变化自动产生 execution_lost；
执行丢失由宿主事件报告，是否继续仍由既有 Goal control 决定。

### Session 资源与关闭

共同 session 仍校验 Goal/run/scope、native handle、invocation 和 candidate generation。
Main 通过七个 session 控制工具操作准确调用；TT 继续通过原生 Child/Message API 关闭资源。
Scope close 先持久化 fence，再串行处理 host session 关闭、内部 generation 撤销和登记释放。
未完成的 TT mutation 或原生 collaboration 回执仍保留其原有业务 gate。

本地 launch journal 的 registered 只表示已登记，finished 表示持有句柄的调用方报告完成，
released 表示 scope 已释放登记；后者不证明操作系统进程退出。
Scope cleanup 的 active_count 仅汇总受控 native/provider 资源，不是系统进程计数。
关闭失败通过显式 host 入口重试，没有后台进程监测或自动 cleanup 重试服务。
控制器丢失后的遗留进程由 agent/宿主处理；scope 可以释放旧登记，不探测对应 PID。

Annotation/LLM Verifier drainer 保留 single-flight worker 记录、锁和 generation 归属。
显式 close 撤销 generation，不按 PID 停止服务；旧消费者在下一次领取前退出。
TT Annotation 的本地 RPC service 使用同样的显式 generation 释放，TT Child 生命周期仍走 SDK。
异常退出后尚未释放的 generation 不自动按存活猜测重启，由 agent/宿主显式关闭后重新启动。
已领取任务的完成验收继续由 Annotation/LLM Verifier owner 负责，操作性释放不撤销 Evidence。

### 登记与有界输出

Pi RPC、drainer 和 scope 内的一次性推理通过 ProcessLaunchGate 在登记后放行。
放行管道 EOF 时目标命令不执行；该机制不读取进程表，也不管理后代。
Bootstrap 只装配登记和完成回调，不提供进程 witness 或跨进程停止接口。

Annotation/LLM Verifier CLI 的 stdout/stderr 各最多 8 MiB，每次读取最多 64 KiB；
超限不解析截断 transcript，结构化结果按 2 MiB 上限有界读取。
本地 verifier 和 Git capture 在直接子进程退出后有界收集输出，不等待其他进程关闭继承管道。
推理、verifier 和 RPC 的操作超时继续生效，直接句柄关闭不代表进程树已清空。

### Selection 与 Owner 收尾

goal_plus_search_select 前保留 candidate scope fence 和原生 session 控制合同。
Required Annotation 未完成时保持 blocked，open scope 仍允许内部角色处理 backlog。
Selection 关闭 Annotation admission 和 publication generation，撤销后台消费者的 generation。
Pi abort/shutdown 和 Codex 明确的 interrupted Stop 仍通过宿主事件关闭 scope；
普通 active Goal Stop 继续由 continuation gate 决定，不增加自动续跑。

这些操作不改写 Goal/Search 终态，不授予 resume，不影响原生 collaboration 的准确调用归属。
共同 launcher 仅操作自己持有的 app-server/RPC 句柄，不接管另一个 TUI 的 active turn。
完整 scope close 和 candidate-only close 共用 cleanup 锁，等待时不阻塞读取 admission fence。
Goal/Search attachment、原 run_ids/run_id 布局与领域授权继续保留。

## 恢复与 Handoff

正常续跑优先复用 host 的 native session。工作区保持完整、仅需要 fresh context 时，使用
可移植的 state redispatch：

1. 对原 candidate 调用 `goal_plus_session_run(new_session=true)`；
2. host 确认空闲旧资源已清理后，在同一 workspace 创建新 generation 的 session；
3. worker 调用 `goal_plus_search_get_agent_context`；
4. 根据 candidate-local artifact、verifier iterations、`research_summary`、Global Evidence 和
   `.tmp/handoff.json` 恢复。

Codex/Pi 支持跨进程的 native session 续轮：调用超时、stop 或 MCP 重建后，宿主在
下一次新 call 的 run 中取得由 native 进程持有的独占执行 lease 并重新授权，分别通过 `thread/resume` 或 `--session`
恢复准确的原生会话。PID 会变化，但 native session、runtime `agent_session_id`、candidate
generation、workspace、Evidence 和累计预算保持不变。新 call 不重放旧调用，用量沿用最后
成功观测的基线。只有 native session 无法恢复、Main 明确决定换上下文时，才使用上述
state redispatch；恢复失败不能隐式创建新会话。

控制器重建读取持久 native handle 和最后成功观测的用量基线。旧 call 的完整结果不可用时
明确报错，Main 检查领域状态后以新 call_id 继续，不进入 checkpoint recovery。原对话无法加载时，显式
`new_session=true` 可直接接手现场，无需先恢复旧对话。

普通未验证修改保留在 workspace；pending verifier/settlement 须先处理完。
显式恢复 checkpoint 时，使用 [Search 一致性恢复](search-recovery.md)：共享
runtime 固定 checkpoint 并 fence 旧 generation；host 核验旧 session 已关闭或释放；provider 恢复
`ArtifactRef`；随后启动 fresh worker。普通 redispatch 不回滚工作区。

每份 handoff 应包含关键工作、verifier-backed feature entries、blockers、next
steps，以及最多五条带适用范围和条件的 pitfalls。`candidate_local`
pitfall 只约束当前 candidate；`feature_family`
pitfall 只有在机制和条件匹配时才可迁移。worker 提出的 verifier concern 在 main
agent 确认前始终只是 advisory。

## Verifier 失效与静默

main agent 确认 verifier 不再可信后，先 fence run，再由 host 停止全部 worker：

| 步骤 | Codex / Pi RPC |
| --- | --- |
| Fence | `goal_plus_search_invalidate_run` |
| 停止 | 对每个活跃 candidate 调用 `goal_plus_session_stop/close` |
| 确认静默 | scope 核验准确资源已退出；调用返回本身不代表退出 |
| 重建 | 静默后修复并重新 freeze |
| 后继 run | `goal_plus_search_create(..., source_run_id=old)` |

失效 run 不再接受新 candidate、session 执行或 fence 后才完成的 verifier
result。旧 run 仍可提供有界研究上下文，但旧分数不能被 successor run 复用。

## Adapter 维护要求

Adapter 可以构建 launch/continuation payload、发现模型、校验 host
budget、声明 session 控制能力，并保存原生 handle 和有界 host metadata。它不得创建 candidate
workspace、执行或排序 verifier、规划技术方向、生成报告或导出 promotion patch。

修改 Host Adapter 时，需要同步更新 host
asset、本文和对应测试。快速测试只验证 schema 和映射；真实 host 行为由以下 smoke path 覆盖：

| 路径 | 验证内容 |
| ------------------------------ | ------------------------------------------------------------------------ |
| [session host ST](../tests/st/test_st_session_host.py) | Codex/Pi 同 session 双轮、准确 Evidence、冷恢复和资源关闭 |
| [Main ST](../tests/st/test_st_unified_main.py) | 无旧控制 skill 的两端 Main、共同工具及 Search 流程 |
| [TT ST](../tests/st_thinkthread/test_linux_v3.py) | retained Child、准确 Message、工具桥、snapshot 和清理；各场景进度见统一重构记录 |
