# Agent Host Adapters

Goal Plus 的 Search runtime 负责持久状态、candidate workspace、verifier 和最终选择。
Codex 或 Pi host 负责实际启动、等待、续跑和停止 worker。Host Adapter 位于两者之间，
把 runtime 的统一请求转换为 host 原生操作；它不改变 Search 的结算语义。

系统整体边界见[共享平面](shared-plane.md)。本文只说明 Host Adapter 的统一契约、Codex/Pi 差异和恢复要求。
Main Goal 的命令、状态和宿主原生 session 恢复设计统一记录在[运行逻辑](runtime-logic.md)，
其中区分当前实现与目标行为；本文的
continuation 主要指 Search candidate worker 的续跑。

普通 Goal Mode 不使用这套 Search Adapter，而是遵循 [`goal-plus-ultra-v1`](ultra.md) 的
轻量 dispatch 合同。Ultra 可以由 Pi、Codex 或外部 harness 实现，不受 `AgentHostKind`
限制；只有进入 verifier-guided Search Mode 后，才使用本文的 candidate pool 契约。

## 统一运行模型

`src/goal_plus/agent_pool.py` 定义 `HostPoolContract` 和终态事件
`WorkerPoolEvent`。每种 host 都必须明确以下能力：

| 字段                | 需要回答的问题                                        |
| ------------------- | ----------------------------------------------------- |
| `launch_mode`       | 启动调用是否立即返回？                                |
| `wait_mode`         | 任意 worker 完成时，parent agent 能否及时被唤醒？     |
| `continuation_mode` | 续跑时复用原 worker，还是把状态派发到 fresh session？ |
| `deadline_mode`     | 哪个 host 组件执行时间上限？                          |
| `recovery_mode`     | 如何重新发现仍在运行的 pool？                         |
| `completion_stage`  | candidate 到什么阶段才可以交给 parent 评估？          |

Adapter 返回的 launch payload 是权威启动说明。main agent 只传递当前 host tool schema
支持的字段；Codex 必须以当前 `spawn_agent` schema 为准，不能自行补造可选 metadata。

Search runtime 不承担 worker supervisor 职责。`search_start_agent_session`
创建的是 context/provenance handle，不代表 worker 已经启动或仍然存活。

同一边界也适用于 Evidence Annotation 和 Adaptive Scheduler。两者继承
`FrozenSpec.native_host` 记录的 Main Agent 身份，不接受独立 host 选择；
Codex hook 或 Pi extension 从当前 native host 进程唤醒 run-scoped queue，
内部子进程完整继承该进程配置。持久 profile 只记录 host/model/reasoning/timeout 和
`configuration_source="host_process"`，不复制外部连接或认证配置。执行器在调用模型前校验
launcher 写入的 host provenance，shared runtime/MCP 不提供备用启动路径。

`internal_agents.py` 是这些 native callback 的共同入口，先通过 typed query 核对冻结 host，
再检查 scope admission。Verifier、Evidence/context 读取和 Search status/select/invalidate/report
的 post-tool 回调都可重扫持久队列；Codex worker-stop 和 TT controller 的既有唤醒继续保留。
Pi 的可信 host command 只接受冻结的 Pi RPC 或 TT driver，TT 的 Root/capability 校验仍在
原 driver 执行，不因此开启 Codex+TT 或 Adaptive+TT。

Search selection 只提交领域终态和 Annotation queue fence。模型关闭由 host pump 和既有
scope cleanup 执行；清理失败保留句柄及错误，后续 host 调用重试。Annotation 完成后只尝试
唤醒 Scheduler，回调失败不回滚已发布结果。`annotation_host.py` 直接装配 typed 用例，
TT annotator 不再通过 candidate `SearchHostService` 构造完整 Runtime 或读取私有 store。
这些模块合同已有受控测试；真实 callback、native 退出联动仍需开发者显式要求的 ST 证据。

## Worktree 与 Skill bootstrap

Git-worktree candidate 由 runtime 物化到独立 worktree。host launch
payload 中的 workspace 只是 provenance：Codex `spawn_agent` 没有 `cwd` 字段，Pi 虽能设置 process
cwd，worker 仍必须先调用 `search_get_agent_context`，并把返回的
`context.candidate_task.workspace`
作为每次文件和 shell 操作的显式 cwd。ThinkThread candidate 已在 private Child cwd 内，
不返回不可访问的 Root 本地
`workspace`；它使用 `workspace_access.logical_root="."` 和 `source_subdir`。parent cwd、其他
candidate workspace 和 `.gp` 内部路径都不是 candidate 工作目录。

任务局部 Skill 采用固定约定，不进入 SearchSpec：只识别 source Git index 中
`.agents/skills/<name>/SKILL.md`，且要求该 Skill 目录内所有文件都由 Git 跟踪。freeze 返回
`task_skill_notice`，明确列出发现的名称和相对路径；没有时明确显示 none。candidate
materialization 通过 `candidate_task.task_skill_paths`
返回该 candidate 自己的绝对路径；`search_start_batch` 为每个 subagent 返回可直接展示的
`task_skill_notice`，launch message 同时输出 `task_local_skills_visible`。worker 完整读取清单中的
`SKILL.md` 并显式遵循，只按需读取同目录资源。仓外 Skill 继续由 host 管理；运行时不扫描或复制
parent/global/plugin 或 peer candidate Skill。

## Inner Agent Binding

`strategy.inner_agent` 是 host-neutral 的 worker 行为 adapter ID，不是 Codex/Pi 原生角色名。
当前 Git 和 ThinkThread outer provider 都支持：

| ID | Dispatch 行为 | MCP 合同 | Workspace ledger projection |
| --- | --- | --- | --- |
| `autoresearch` | 内置 AutoResearch-Git；自主执行多轮 hypothesis/edit/verify，TT 每次 accepted 后结束当前 wake | 每轮调用 `search_run_verifier` | `results_tsv` |
| `common` | 一个 dispatch 完成一个有界 attempt 后返回 | 读取 context，调用一次 `search_run_verifier` | `none` |

runtime 将 ID 解析为持久化的 host binding，再由 Codex/Pi/ThinkThread adapter 选择对应 prompt asset 和
native launch 参数。`.gp` 始终是 canonical iteration ledger；binding 中的
`workspace_ledger_projection` 只决定 trusted runtime/host adapter 是否额外物化 candidate-local
视图。兼容 ID `autoresearch` 的内置版本为 `builtin-autoresearch-git-v1`：Git outer 复用 candidate
worktree repository；TT outer 在 Child 内初始化独立 private Git，提交每次 attempt、restore 和
物理 `results.tsv` ledger。`common` 不启用这些 inner-agent mechanics，即使 outer workspace 是
Git。`worker_agent_type` 仅为旧 Codex state 的 native-role
兼容字段；Pi 不再把它写入无人消费的 launch payload，新 SearchSpec 也不再生成该字段。
unsupported adapter 在
freeze/launch 前 fail closed。

## Codex 与 Pi 的能力对比

| 能力 | Codex | Pi RPC | Pi + ThinkThread |
| --- | --- | --- | --- |
| 启动 | 异步 `spawn_agent`；worker 从 context bootstrap worktree | host-local supervisor 启动前台 Pi 子进程 | Agent POSIX private Child |
| Candidate 进程环境 | `spawn_agent` 无 `env`；worker 对本地命令显式应用 `candidate_task.process_environment` | supervisor 把同一 `process_environment` 注入 Pi worker 进程 | 由 ThinkThread Profile/Runtime 控制，不接受 candidate process environment |
| 等待 | `wait_agent` + `list_agents` | `pi_search_pool_wait_any` | `thinkthread_search_pool_wait_any` |
| 续跑 | `followup_task` 复用 worker | 新进程恢复 native session | wake 同一 Child/Session/branch |
| Deadline | parent watchdog | pool lease + 进程 watchdog | parent/controller watchdog |
| 恢复 | Codex registry + `.gp` | `.gp/host-pools/pi/` | `.gp/host-pools/thinkthread/` |
| Goal gate | 精确 `$goal-plus...` + hooks | 精确 `/goal-plus...` + extension events | 同 Pi gate + exact Profile preflight |
| 模型发现 | app-server `model/list` | Pi RPC model catalog | Profile/default 或显式 provider/model |
| Observability | native JSONL + metadata | `pi_metrics` + metadata | TT IDs + durable snapshot/request IDs |
| Workspace | `copy`、`git_worktree` | `copy`、`git_worktree` | `thinkthread` |

当前可移植 planner 为 `agent_guided`（含 `agent`、`default`）和 `random`（含
`random_mode`）。Codex/Pi RPC 的普通运行使用固定初始并行循环；Git worktree 可选
`adaptive_search`。ThinkThread 只支持 Pi Profile 和 `parallel_loops`；Codex
仍不声明 ThinkThread capability。host/backend 不匹配或 Profile preflight 失败时 runtime 在
freeze/create 边界 fail closed。

Codex Goal Plus startup 仍只接受精确 `$goal-plus...` 的 `UserPromptSubmit` 授权，并在
`PreToolUse`、`Stop` 和 `SubagentStop` checkpoint fail closed；ThinkThread 支持不会改变该
Codex hook contract。

资源分配由 runtime 在 freeze 时解析并固定，不由 Adapter 各自推断。Codex/Pi 的正式 verifier
都使用 CandidateTask 中持久化的进程环境和解析后的物理资源锁；Pi 还能在 worker 进程边界直接
强制该环境。Codex 原生 `spawn_agent` 当前不支持 `env`，所以它的本地 profile 命令必须按
candidate contract 显式应用环境变量，不能把 parent 的逻辑设备 `:0` 当作已隔离设备。

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

## Candidate 循环

新的 Codex/Pi spec 使用 `orchestration_mode="parallel_loops"`。一次 Search
run 只做一次初始规划，创建固定数量的 candidate lane：

1. 启动全部初始 candidate；
2. 以 wait-any 方式处理最先完成的 worker；
3. 复用该 worker 已经落盘的准确 Evidence，缺失时才由 parent 补做 verifier；
4. 如果全局停止条件尚未满足，继续同一 candidate、workspace 和 native session；
5. selection 前停止或 drain 所有活跃 worker。

`autoresearch` continuation 继续同一自主循环；`common` continuation 只下发下一个有界
attempt。两者都复用同一 candidate 和 settled outer workspace，main 不为 common worker
手工创建 checkpoint。

低分、同分或单次没有提升都不会触发 candidate 替换，Adapter 也不会自动 refill。较慢 lane 不会阻塞其他 lane 的 verifier 结算。

Codex 通过 `search_continue_agent_session` 和 `followup_task` 原地续跑。Pi 的 `candidate_ready`
表示 minimum lease 已释放且 durable Evidence 已存在；main 随后调用
`pi_search_pool_continue`，由新进程恢复同一 native Pi session。`timed_out`
表示 lease 耗尽但未满足，不能作为已验证完成的 ready 事件；原生 session 续跑仍需
满足当前 Goal/run、scope、执行归属与剩余预算。

ThinkThread Child 的 `search_run_verifier` 先返回带稳定 `attempt_id` 的 typed accepted receipt；
Child 随即结束 turn。host 在 quiescent boundary 持久化 `attempt_verification` recovery intent，
再捕获 snapshot 和运行 verifier。完整 typed feedback 同时进入 `candidate_ready`，并在 branch reset
完成后强制注入同一 Child 的下一次 wake。无 accepted attempt 的 completed turn fail closed。
`needs_recovery` 按 `spawn`、`wake` 或 `attempt_verification` stage 分派；attempt recovery 重放相同 id
与 provider RequestId，branch reset 则用目标 snapshot 和 generation CAS 确认。一次 wait 的
deadline 只限制本次 recovery 查询，未确认的事实继续保留为 active，不能作为 ready event 或
drain 完成。一个 lane 的 recovery 不阻止 controller 推进其他 lane。

当前 TT pool 的正常 continuation 要求 job 为 `ready` 且有正式 attempt feedback。
未提交正式 attempt 就耗尽 lease 的 job 会标为 `timed_out`，该入口拒绝直接 wake；
通用 candidate checkpoint recovery 也不支持 TT。正常多轮 continuation 与中途超时后的
session resume 是不同路径，保留 session 或 snapshot 本身不代表后一条路径已经接通。

TT `autoresearch` binding 显式加载 Pi Search worker prompt，并由 trusted extension 在每次 verifier
request 前封存 Child-private Git attempt。Root 对 quiescent attempt snapshot 验证后，在
`fs.run(writes="snapshot")` 中恢复 best（如需要）、追加 `results.tsv` 并提交 ledger，然后把 Child
branch reset 到该 post-settlement snapshot。同一自主循环跨 retained Child wake 延续：每次 wake
最多提交一个 accepted attempt，下一次 wake 先接收完整 settled feedback，再继续下一轮。如果 Pi
worker 在正常 `agent_end` 前没有提交 verifier，host extension 在同一 wake 内只
追加一次纠偏 follow-up；第二次仍为空则照常结束，由 pool 报 `MissingCandidateAttempt`。这里的
inner commit 只用于 Child-local history；Evidence 始终是 exact outer `fs_snapshot`。

Agent POSIX snapshot pagination 和 `pread` 在 adapter 边界严格校验 item、cursor、offset、实际解码
长度与 EOF。Durable provider mutation 的业务 outcome 和 request close 分开持久化；close
completion-unknown 不等于已关闭，后续 reconciliation 只重试 close，不重放已确认的 mutation。

## 模型与 Observability

`goal_plus_list_models` 提供只读模型发现。用户把选择写入
`strategy.models`，Adapter 在派发时将已解析模型投影到实际 host
schema；没有指定模型时，保留 host 原生继承行为。模型目录只说明当前可发现的模型，不保证旧版 host
tool 能转发所有可选字段。

模型目录也不证明真实推理可用。现有 Goal command 与 `search_freeze_spec` host gate 只收集显式
worker、annotator 和 scheduler override，按模型去重后并发发送最小 `hi` 请求；任一请求失败就
在启动 agent 前 fail closed。没有显式 override 时不 probe，因为 Main 当前 turn 已经是当前模型
可用的证据。Codex probe 只通过原生 `codex exec -m MODEL` 切换模型；Pi probe 使用 extension
持有的 native model 对象和 `modelRegistry.complete()`。Pi 未显式覆盖的 child 从 host-local
execution scope 继承 Main 的 exact native model/thinking，而不是重新选择机器级 default。两条路径
都继承 host 配置，不建立 provider、credential 或 endpoint plumbing。

所有 Adapter 都实现只读 `collect_observability`，并通过 `search_get_agent_observability`
暴露。该数据只用于 provenance、报告和诊断，不会把 host lifecycle 写入 Search record。

Codex rollout JSONL 提供 input、cached-input、cache-write、output 和 reasoning
token，但不提供实际账单。Goal Plus 使用 `src/goal_plus/codex_pricing.py`
中带版本、与 Pi 兼容的模型目录估算 `usage.cost_usd`；长上下文分层和 `flex`/`priority`
倍率遵循 Pi 语义。`usage.cost_estimate` 会注明目录版本和覆盖率，未知模型不套用猜测价格。

## Worker 预算

| Host   | 必需字段                            | 执行方式                                     |
| ------ | ----------------------------------- | -------------------------------------------- |
| Codex  | `worker_budget.max_runtime_seconds` | 首次等待、一次 closeout、最后等待、interrupt |
| Pi RPC | `worker_budget.max_runtime_seconds` | closeout steer + 进程硬 watchdog             |

`budget.max_parallel` 同时决定初始 candidate 数和活跃 worker 数。`max_turns`
对 Codex 和 Pi 都只是 prompt 提示。

可选的 `min_runtime_seconds` 和 `min_verifier_runs` 构成单 worker 的 AutoResearch minimum
lease。Codex 由 `SubagentStop` hook 在同一 child turn 内继续；Pi 由 worker `agent_end`
handler 和 pool supervisor 累计执行。lease 必须在外层软收尾前释放，`max_runtime_seconds`
仍是独立硬上限。基础设施故障、pool close 或外层 closeout 会立即终止 lease。

`strategy.worker_launch` 只保存 host 启动偏好。Codex 可映射 `model`、`reasoning_effort` 和
`service_tier`；Pi 通过可信进程配置映射 model 和 thinking level。这些字段不属于 Search state。

## Main owner 生命周期

Goal Plus host adapter 为实际启动的活计算附加 host-local Execution Scope。Scope 不属于
Search record，也不改变 `AgentSessionRecord` 的 provenance 语义。Pi extension 内部注入
`execution_scope_id`；Codex hook 使用 native session 和 collaboration task identity，模型不能覆盖
scope 归属。

Scope、本地 Annotation/Scheduler drainer 和 Pi pool wrapper 的身份校验目前要求可读的 Linux `/proc`。
Scope 使用启动 tick token；drainer 核对精确 module、root、run 和 generation 参数，并在
读取命令前后复核启动 token。进程存在但观测不可读、缺失或损坏时返回
`ProcessInspectionUnavailable`，不退回 PID-only 归属。Scope cleanup 保留 `stop_failed`
句柄和错误，guardian 对未知 owner 继续观测，不把空值、异常或不可读身份当成 owner 退出。
Recovery 仍按原记录读取 `ps lstart`，不把它与 procfs token 互相转换。进程组后代与启动
登记的责任关系见[架构图集](architecture.md#04-恢复与提交)。

Scope/drainer/Pi pool wrapper/Pi RPC 对专用进程组按成员观测清理，不以组长退出代替整组静默。当前句柄已核验
的成员可以继续作为归属依据；重新读取记录时组长已消失且没有成员身份依据，则保留 unknown
错误和句柄，不仅凭历史 PGID 发信号。组信号被拒绝时不降为只停止组长后报告成功。
Scope 已登记的 Pi RPC 和内部推理额外保存 boot identity 与已核验成员的 PID/start token。
Guardian 持续补充观察，执行器在 reap 前保存成员，重试关闭从原 scope token 恢复这些事实；
已观察成员即使离开原组也按精确身份停止，系统重启后不向新 boot 的进程发信号。窄 callback
由 Execution 的 ScopeProcessRegistry 经 State 保存/读取，具体文件 adapter 在 bootstrap 注入。
Execution 不构造 store。未知组不从历史 PGID 猜测归属，
没有 witness 的历史记录不原地补造身份。独立 drainer/pool wrapper 同样在放行前保存启动
witness，kick/reconciliation/close 的已核验观察由各自 worker/pool 锁保护并合并保存。
Annotation/Scheduler drainer 在同一 admission 锁内额外登记 scope 的 internal process；该登记
先于目标命令放行。空队列退场只移除 single-flight worker 记录，不移除 scope 资源。
Guardian 继续观测其成员，确认静默后才完成资源；scope close 能清理已退出队列的 drainer。
无 scope 的独立调用不创建或猜测 Goal/scope；注册 callback 离开 admission 后失效。
未被观察过的逃逸后代仍未覆盖，不能声称任意进程树的跨重启清理已完成。

Pi pool 使用既有 root/pool/job identity 核验完整 worker argv，随后保留启动 token 执行停止，
不会向碰巧占用历史 PID 的无关进程发信号。Worker 在 launcher 使用的同一把 pool lock 下
核验 `running`、自身 PID、pool/run 和 request 归属后才执行 candidate；未提交的启动或已经
interrupt 的 pool 不获得执行权。关闭先保存 fence，再有界等待或停止所有可确认归属的
wrapper 资源，包括已经写入终态 job 的进程；`active_count=0` 不代替 `state=closed`。
不可读身份或未知旧组保留未关闭状态，其他已核验资源仍执行停止。Worker/job 仅增加可选的
操作性 witness，不进入公开 pool snapshot 或 drainer stop result；没有从时间戳猜测历史
启动 token。损坏或不可读的 worker 登记不当作不存在，不能借此覆盖旧资源。Pi RPC 在工作 admission 前通过 `ManagedProcess` 绑定
独立组，沿用 5 秒 TERM/5 秒 KILL 预算；清理失败不退回只停组长，也不完成 scope token。
模型目录查询也共用直接子进程停止，保留原 2 秒预算，不改变 RPC 或模型配置。

本地推理的 active query、monitor 或 pipe 读取异常仍由执行 owner 停止并回收直接子进程；
停止失败时保留当前 driver 的句柄和临时合同，拒绝覆盖它，并允许重试 `terminate`。Drainer
在 worker lock 内完成启动登记，失败时先停止尚未通过 claim 的子进程。Pi RPC 的观察、scope
登记、RPC 初始化和收尾统计异常均保留停止路径；只有停止成功后才完成已知 scope token。
这些覆盖包含 `KeyboardInterrupt`，但不等于启动者 SIGKILL、机器重启或逃逸后代已可恢复。

Pi RPC、Pi pool wrapper、两类 drainer、scope 内的一次性推理及 guardian 共用 `ProcessLaunchGate`：仅向
隔离的 Python 放行程序传入读管道，持久登记完成后写入放行字节，再以同一 PID exec
原始命令。登记前启动者退出（包括 SIGKILL）使管道 EOF，目标命令不会执行。放行程序
不加载工作区 Python 启动脚本，并恢复原 Popen 的 SIGPIPE 等信号语义；目标命令仍继承
原 native environment。Guardian 也在 scope 文件写入后才开始读取，不再抢先发现文件缺失。

Bootstrap 为已有 scope 的 Annotation/Scheduler 装配窄 `ProcessRegistration`。推理拥有
独立组，登记成功才启动；正常退出也清理同组后代，组长 reap 前先保存成员身份见证，只有
资源关闭后才完成 scope token。Scope process 记录增加可选 `kind=internal`，只用于资源
选择；原 candidate 记录不变。Candidate-only fence 不拒绝或停止内部推理；内部队列关闭
先停 drainer，再清理该 run 的内部组；完整 scope close 清理全部资源。无 scope 的独立
调用保留既有组继承，不为其猜测或创建 Goal/scope。Guardian 自身丢失由既有 host 恢复入口
补启动。真实宿主验证按具体行为选择最小场景，本地进程回归不替代原生生命周期证据。

Annotation/Scheduler 的共同一次性 CLI 路径使用有界 pipe collector：stdout/stderr 各最多
8 MiB 原始字节，每次读取最多 64 KiB。超过边界会停止并回收直接子进程，返回
`ProcessOutputLimitExceeded`，不将截断 transcript 解析为完整 usage 或接受已有结果文件。
结构化结果按 2 MiB 字节上限读取，超过时返回 `InferenceOutputLimitExceeded`；拒绝
FIFO、目录和符号链接，按同一已打开 fd 检查类型/大小并有界读取，防止检查后增长绕过。
两角色继续使用原有 output-error/retry 政策，失败不修改已结算 Evidence。预算内保留完整
event/usage 与结果语义。这是本地读取预算，不是磁盘配额；不覆盖 Pi 长期 RPC、TT Child
或 host 自己的 transcript 存储；进程组和启动失败的清理按上述登记与 witness 规则处理。

Guardian 通过 typed owner/cleanup observation 持续处理 `closing` 和 `cleanup_failed`，直到
持久状态变为 `closed` 或 scope 记录已移除；关闭回调返回成功本身不代替持久状态。失败重试
从既有 0.25 秒观测间隔退避到最多 5 秒，资源关闭仍使用各自的有界预算。已有显式关闭请求
可以在 owner 观测未知时继续清理已登记资源，但只有确认 owner 退出才采用对应 Codex 原生
退出记账。已确认的退出原因不会被普通 retry 覆盖。

完整 scope close 和 candidate-only close 先持久化各自 fence，再争用同一 scope 的
`cleanup.lock`；等待资源清理时不持有 `scope.lock`，新工作 admission 仍可及时读取 fence。
清理动作串行，重复 close 读取已关闭状态，不再次执行资源停止。既有 `run_guardian` 入口可以
继续处理未关闭记录；所属 host 恢复/启动入口复核 guardian 身份并在缺失时补启动。不提供
独立的外部常驻重启服务，也不重新打开旧 scope。

ScopeProcessRegistry 统一登记、witness 合并/读取、资源观测和停止；ScopeCleanup 负责上述
fence、关闭顺序及 cleanup 汇总。Host 经窄接口提供 Pi/TT pool、内部 drainer 的关闭和
Codex session 静默查询，保留原具体宿主入口。Scope 文件格式、锁顺序、失败重试和公开
返回结构不变。ScopeActivation 在 owner 锁内复用或替换 scope，旧资源未关闭时拒绝新建；
ScopeGuardianLauncher 在 scope 锁内核验身份、持久登记后放行，并为 host 恢复入口补启动。
二者通过 bootstrap 注入 State、Clock、原源码路径和 cleanup owner，host 不保留旧实现。
内部激活、admission、native/process 登记、followup 和 cleanup 使用 ScopeAttachment 区分
Goal 与 Search run；磁盘仍保存原 run_ids/run_id，公开宿主参数保持不变。既有 Goal ID
命名空间只用于解码类型，不授予执行权；GoalAuthorization 仍核验当前 Goal 和持久链接。
普通 Goal 关闭自身 scope 资源，不进入 Search drainer；candidate-only fence 和内部角色
登记要求 Search attachment。

- `search_select` 前 interrupt/drain scope-owned candidate，并要求 `active_count=0`；
- selection 关闭 Annotation queue 和 publication generation 后立即终止 annotator；required
  supplemental evaluation 未完成时 selection 保持 blocked 且 queue 保持 open；
- Pi `agent_end(aborted)`、`session_shutdown`、owner PID 消失和 Codex interrupted Stop 关闭 scope；
- Pi 进程 cleanup 从 TERM 升级到 KILL，ThinkThread cleanup 使用 interrupt/destroy；provider
  completion-unknown 和未解析的 mutation intent 保留恢复证据；
- scope 关闭后拒绝迟到的 pool continuation 和 Annotation drainer launch。

`search_select` 前的 run fence 只禁止新的 candidate work。若 required Annotation 尚未完成，
同一 open owner scope 仍允许 host 启动内部 Annotation/Scheduler backlog；完整 scope close 才同时
禁止两类 launch。这样 selection 可以安全重试，而不会为了补 Annotation 重新开放 candidate lane。

Owner cleanup 只停止活计算，故意不更新 Goal/Search 状态。本合同不定义 resume、scope 接管或
被取消 Annotation 的补算。Codex 正常或手动 closeout 由 main 使用原生 `list_agents` 和
`interrupt_agent` 完成；hook 在初次 spawn 放行前登记 task/parent/native 调用 ID，返回丢失时
保留清理责任，后续原生 registry 状态可补齐观察。`followup_task` 放行前记录本地 wake，
同样关联原生调用 ID；迟到回执不通过同名 target 选择最新 scope。
再次唤醒后只以 wake 返回后开始的 registry 读取或当前 wake 的成功 interrupt 确认静默，
无关联的迟到 `SubagentStop` 不再关闭该 wake；当前约束见 [Codex](codex.md#execution-scope)。
仍有活跃 target 时阻止 selection 或 main Stop。当前 CLI 不把另行启动的 app-server 当作 TUI
lifecycle controller。owner PID 消失时，Codex child 随同一 native runtime/process 退出；guardian
只把这些 target 记为 `owner_process_exited`，并继续清理 scope 内的外部 Annotation 资源。

## 恢复与 Handoff

正常续跑优先复用 host 的 native session。工作区保持完整、仅需要 fresh context 时，使用
可移植的 state redispatch：

1. 对原 candidate 调用 `search_redispatch_candidate`；
2. 在同一 workspace 创建新的 `agent_session_id`；
3. worker 调用 `search_get_agent_context`；
4. 根据 candidate-local artifact、verifier iterations、`research_summary`、Global Evidence 和
   `.tmp/handoff.json` 恢复。

Pi 支持跨进程的 native session 续轮：PID 会变化，但 native session、runtime
`agent_session_id`、candidate 和 workspace 保持不变。只有 native
session 无法恢复但工作区完整时，才退回上述 state redispatch。

Worker 丢失且留下半次修改或结算时，使用 [Search 一致性恢复](search-recovery.md)：共享
runtime 固定 checkpoint 并 fence 旧 generation；host 提供停止事实；provider 恢复
`ArtifactRef`；随后启动 fresh worker。普通 redispatch 不回滚工作区，不能代替这个流程。

每份 handoff 应包含关键工作、verifier-backed feature entries、blockers、next
steps，以及最多五条带适用范围和条件的 pitfalls。`candidate_local`
pitfall 只约束当前 candidate；`feature_family`
pitfall 只有在机制和条件匹配时才可迁移。worker 提出的 verifier concern 在 main
agent 确认前始终只是 advisory。

## Verifier 失效与静默

main agent 确认 verifier 不再可信后，先 fence run，再由 host 停止全部 worker：

| 步骤     | Codex                                       | Pi RPC                                   |
| -------- | ------------------------------------------- | ---------------------------------------- |
| Fence    | `search_invalidate_run`                     | `search_invalidate_run`                  |
| 停止     | 对每个活跃 candidate 调用 `interrupt_agent` | `pi_search_pool_close(mode="interrupt")` |
| 确认静默 | `list_agents` / `wait_agent` 直到全部终止   | snapshot / wait 直到 `active_count=0`    |
| 重建     | 静默后修复并重新 freeze                     | 静默后修复并重新 freeze                  |
| 后继 run | `search_create(..., source_run_id=old)`     | 相同                                     |

失效 run 不再接受新 candidate、Pi pool 提交或 fence 后才完成的 verifier
result。旧 run 仍可提供有界研究上下文，但旧分数不能被 successor run 复用。

## Adapter 维护要求

Adapter 可以构建 launch/continuation payload、发现模型、校验 host
budget、声明 pool 能力，并保存原生 handle 和有界 host metadata。它不得创建 candidate
workspace、执行或排序 verifier、规划技术方向、生成报告或导出 promotion patch。

修改 Host Adapter 时，需要同步更新 host
asset、本文和对应测试。快速测试只验证 schema 和映射；真实 host 行为由以下 smoke path 覆盖：

| 路径                           | 验证内容                                                                 |
| ------------------------------ | ------------------------------------------------------------------------ |
| `codex_parallel_loop_cycle`    | 初始 candidate、wait-any、同 worker 续跑、best 更新、selection 和 report |
| `pi_rpc_managed_pool_wait_any` | 两个真实 Pi worker、pool 恢复、candidate-ready event 和 drain            |
| `pi_rpc_parallel_loop_cycle`   | 单次初始 plan、同 candidate 续跑、best 更新、selection 和 report         |
