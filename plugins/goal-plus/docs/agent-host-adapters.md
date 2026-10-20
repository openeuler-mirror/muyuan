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

Candidate 的 fresh/fork 总体生命周期、模式差异和持久状态图见
[Candidate Dispatch：fresh 与 fork](candidate-dispatch.md)。本页只维护宿主 adapter 的
provider 差异、能力矩阵和 native session 事实。

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

<a id="fork-adapter"></a>

### Candidate dispatch adapter（fresh / fork）

`strategy.initial_context=fork` 的语义由 Search 层统一定义，不随 Codex、Pi 或
ThinkThread 改变；`fresh` 使用同一 adapter contract，只是不携带 parent checkpoint。
公共流程是 frozen spec/preflight、准备 session target、provider operation、exact receipt、
共同 session control、hard settlement、可选 LLM audit、selection、promotion 和 apply。
Fork 模式还要求完整父 checkpoint 和 N 个隔离分支。
父 Main 持有最终 apply 权限，candidate branch 只能提交结果和证据。

Provider-neutral `ProviderDispatchAdapter` 提供 prepare、start 与 receipt recovery；内部 coordinator
通过 `start_agent_session(..., session_mode="fresh"|"fork")` 复用 candidate session 生命周期。
Receipt 关联 provider、父/分支
opaque handle、checkpoint provenance、workspace 或 Fs attachment、capability/model/profile、
request identity 和 cleanup 状态；Runtime 只做归属、代际、幂等和状态转换，不解析 provider
payload。Codex app-server、Pi SessionManager 和 ThinkThread `thinkthread.fork` 是三个实现，
不是三套 Search 状态机。缺少完整 checkpoint 或 receipt provenance 时 adapter 必须拒绝，不能
退回 fresh `spawn` 并伪装成上下文 fork。

当前 direct Codex/Pi 与 ThinkThread Codex/Pi adapter 都已接入该流程。仍处于 prepared 且没有
越过 provider fence 的 group 会继续启动；submitted/unknown target 只重放完整持久 receipt，
没有准确 outcome 时保持 fenced。共同 owner 保存不可变 launch provenance，
并在全部 started branches 收到可信关闭事实后提交 group closed；provider 物理关闭语义不混同。
Direct Pi 可从确定性的候选 session 文件恢复 provider receipt；Direct Codex 没有 provider 查询或
journal 时保持 fail-closed，禁止通过重发创建未知分支。
TT journal 重建、close callback 重放与冻结工具投影由无模型回归覆盖。Pi/Codex 的真实 TT
Circle 场景核验完整派生、续轮、交付与关闭；验收记录绑定实际安装 build。
ThinkThread Child 的 stop/destroy、Fs branch 清理和 Message delivery 细节留在 adapter 内部。

### Fork 分支的启动消息与工作区

Fork 继承父 Main 的完整 checkpoint，候选启动仍使用自己的工作合同。
`goal_plus_search_fork` 只派生并绑定分支；Main 随后调用 `goal_plus_session_run` 时，host
在继承的历史之后提交新的执行消息，内容为 **candidate launch 消息 + Main 本次 prompt**。
它不会仅凭父历史中的角色和路径让分支开始工作，也不改写原有历史。

Launch 消息只携带用于追踪的 `agent_session_id`、`candidate_id`、一句候选 intent，并要求先调用
绑定的 `goal_plus_search_get_agent_context()`。worker budget 仍作为 host 控制字段传递，不重复放进
模型消息；proposal 的 hypothesis、tradeoff 和 instructions 由 context 的
`candidate_task` 统一返回。Direct 使用所选 inner-agent 的 prompt 资产说明候选角色；ThinkThread
的 `common` 使用最小引导，完整行为要求放在返回的 `candidate_task.instructions`。因此没有一份
跨宿主统一的 fork 专属 system prompt。
Launch 拼接由 `infrastructure/artifacts/candidate_launch.py` 与 `agent_hosts.py` 负责，
实际提交在 `session_host.py` 和 `infrastructure/thinkthread_sessions.py`。

`get_agent_context` 的响应是当前工作合同，包含候选身份、目标、可交付修改范围、只读文件、
instructions 和已启用能力。工作区按 provider 表达：

| Provider | 模型获得的工作区说明 | Host 实际绑定 |
| --- | --- | --- |
| Direct Copy/Git | `candidate_task.workspace` 是候选本地路径，文件和 shell 操作显式使用该路径 | Codex fork 的 `cwd`、Pi fork 的 target cwd 及后续进程 cwd 均绑定候选 workspace |
| ThinkThread | `workspace_access.logical_root='.'` 与 `source_subdir`，不暴露 runtime 的本地候选路径 | 从固定 baseline 派生独立 private Fs branch，并核对 Child/branch/baseline 归属 |

ThinkThread 中路径字符串与父历史相同，不代表访问同一棵 live tree；文件操作落在当前
Child 的 private 文件空间。Root live tree 和 sibling Child 不属于当前候选可见范围。
候选合同由 `application/candidate_creation.py` 生成，公开响应由
`application/candidate_context.py` 投影。

工作区和执行权限还由可信 host 绑定与共同入口校验，不只依赖提示词。ThinkThread adapter
在首次 wake 前发送 `wake=false` 的身份/能力绑定消息，不触发模型执行；正式 dispatch
再携带 session identity 与 nonce，Child 工具桥从准确 Parent Message 取得绑定。
继承历史中的 Main 工具调用或路径不授予当前候选 Main 权限。
后续 dispatch 沿原分支执行，ThinkThread 将上一 attempt 的权威 verifier feedback 与
continuation 指令一起送入同一 Child，不重新 fork。

### Fork 完成边界与恢复

Main 完成 freeze/create/plan/batch 后结束准备回合。共同 gate 要求准备事件晚于真实 batch
完成；时间精度为秒，同秒不能证明先后时拒绝。已有 started plan 缺少完成事实时不补造，
重放不改变实际 started_at。

- Codex direct 核验同一 completed turn，原生 completedAt 必须晚于准备事件，不能选择旧 turn。
- Pi direct 要求同 run marker，沿活动祖先物化选定 ancestry，排除后续 Main 消息与 inactive
  branch，再使用 SessionManager.forkFrom。
- TT 使用 fork_current_checkpoint/private_branch；Pi 以 final assistant stop 为 provider
  边界，工具组 entry 不能证明 checkpoint 推进。整组派生前后的两次读取必须保持边界一致。

所有分支固定共同 checkpoint、初始 FrozenSpec 与源码 baseline。共同 owner 严格核验
Child/Fs、effective model/grants、预算与身份后才放行整组；派生与执行分开，首轮复用已派生的
Child。已知 Child/Fs 后保存 journal；后续身份、grant 或领域提交失败时清理准确资源。
未知 outcome 保持 fenced，不能重发 fork/spawn/wake 来伪造重放成功。

Close 先 fence，再确认原调用静默和真实资源关闭。Launch receipt 不可变，close facts 独立；
全部 started branches 有可信关闭事实后 group 才 closed。Partial/failed group 不因清理转成功。
Direct 的 controller/RPC 收尾与 TT 的 retained turn 静默、Fs cleanup、Child destroy 各自记录。
原 fork Child 丢失、被替换或 native context 无法证明保全时，不能从 snapshot fresh spawn 并
沿用旧 fork provenance；当前由 Main 决定 successor work。

TT SDK 没有 exact checkpoint selector/receipt；Root digest 只证明读取结果一致，JSONL leaf
不证明 live active leaf。相同持久历史前缀不证明首模型请求的 system/tool projection 或
compaction 后窗口相同，也不证明缓存命中、成本降低或求解质量改善。
历史 Main 路径与角色不授予当前权限；Child 的工具、cwd、workspace、预算和可信 identity
按 candidate 合同重绑，Main 保留编排与 apply 权限。

Fresh/fork 决定首次上下文来源。Goal resume 恢复执行许可，native resume 打开已有会话；
后续 session run 用新 call_id 沿原分支执行，不重新派生。恢复 artifact 不证明 native context 恢复。
Host command 可用 `spawn_mode=fresh|fork` 显式约束 SearchSpec 的初始上下文；省略该命令配置时，
Main 仍负责在 SearchSpec 中选择模式。

### Fork 支持范围

Harness、runtime provider、workspace provider、inner agent、首次上下文和执行版本是独立维度。
`common` 是 worker 行为，不能指代 direct provider。当前配置以 `initial_context=fork` 选择 fork，
`fork` 与 `adaptive_search` 当前互斥；`fresh` 可以叠加 `adaptive_search`。

| Harness / runtime | Workspace | Inner agent | Fork | 显式 worker model |
| --- | --- | --- | --- | --- |
| Codex / direct | Copy、Git worktree | common、autoresearch | 共同 adapter | 支持 |
| Pi / direct | Copy、Git worktree | common、autoresearch | 共同 adapter | 支持 |
| Codex / TT | TT private Fs | common | 真实 Circle 核验 Child 历史与 Fs | Profile allowlist 内 exact provider/model |
| Pi / TT | TT private Fs | common、autoresearch | 真实 Circle 使用 common | Profile 默认模型，任务级 override 关闭 |

TT 两端均不支持任务级 reasoning/service-tier override。Pi TT 的 launch/continue 使用同一
能力准入。Copy 仅支持 artifact_only，Git worktree 与 TT 使用原 publication/apply。
TT candidate Reference 尚无 provider 物化能力，目录隐藏 reference_open，调用以
ReferenceUnavailable 拒绝；Annotation Artifact View 可用不代表 candidate Reference 可用。
TT Shared Tool/shared_dir 的 freeze 继续拒绝；冻结 capability 统一投影 Codex MCP、TT bridge、
Pi 激活与 context reload，不能通过目录伪报支持。

TT Root Ultra delegation、独立 final check、Pi worker model override、fork + adaptive，
以及可替换 Child 的 native-context restore 均未开放。两端 Circle 真实验收覆盖完成边界、
两个独立 Child/Fs、原 Child 各两轮、hard settlement、required audit/Annotation、selection、
独立 publication verifier、Root apply、Goal complete、报告和 cleanup；成功场景不证明全部
unknown intent、丢回包或故障恢复。宿主特有范围见 [Codex](codex.md#支持范围与验证) 和 [Pi](pi.md)。

### 执行与内部角色边界

Direct native child 的 `SessionProcessInput` 默认携带 `ProcessSandbox` 合同；Codex 使用
原生策略，Pi 使用 OS wrapper。文件写入范围、共享 runtime 例外、内部推理接线与支持表
统一见 [Agent sandbox](sandbox.md)。该边界不替代 host callback 的领域授权。

Search runtime 不承担 worker supervisor 职责。session run 内部创建的领域 session
是 context/provenance handle，不代表 worker 已经启动或仍然存活。

同一边界也适用于 Evidence Annotation 和 LLM Verifier。两者继承
`FrozenSpec.agent_harness` 记录的 Main Agent 身份，Annotation 同时使用冻结的 runtime provider；
LLM Verifier 使用冻结 runtime provider 对应的执行器（direct 使用 structured inference，ThinkThread
使用受限的 fresh private Child），不接受独立身份选择；
Codex hook 或 Pi extension 从当前 native host 进程唤醒 run-scoped queue，
内部子进程完整继承该进程配置。持久 profile 只记录 agent_harness/runtime_provider/model/reasoning/timeout 和
`configuration_source="host_process"`，不复制外部连接或认证配置。执行器在调用模型前校验
launcher 写入的 host provenance，shared runtime/MCP 不提供备用启动路径。

`internal_agents.py` 是这些 native callback 的共同入口，先通过 typed query 核对冻结 host，
再检查 scope admission。Verifier、Evidence/context 读取和 Search status/select/invalidate/report
的 post-tool 回调都可重扫持久队列；Codex worker-stop 和 TT controller 的既有唤醒继续保留。
Pi 的可信 host command 只接受冻结的 Pi RPC 或 TT driver，TT 的 Root/capability 校验仍在
原 driver 执行；Codex+TT 与 Adaptive+TT 的接线和真实宿主证据以支持矩阵和专项文档为准，
不从 Codex+TT fork Circle 的通过推导其它 orchestration/host 组合。

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
绝对路径，作为唯一清单。launch message 和 instructions 不展开路径；worker 按当前任务、角色
和 provider 选择适用 Skill，选中后完整读取 `SKILL.md` 并显式遵循，只按需读取同目录资源。
同一 session 中已完整加载且内容未变时复用；上下文被截断、压缩、缺失或文件变化时补读。
仓外 Skill 继续由 host 管理；运行时不扫描或复制 parent/global/plugin 或 peer candidate Skill。

## Inner Agent Binding

`strategy.inner_agent` 是 host-neutral 的 worker 行为 adapter ID，不是 Codex/Pi 原生角色名。
已接入的 inner-agent 行为如下；各 harness/provider 组合以[支持范围](#fork-支持范围)为准：

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

| 能力 | Codex | Pi RPC | Pi + ThinkThread | Codex + ThinkThread |
| --- | --- | --- | --- | --- |
| 启动 | 共同 session run → app-server | 共同 session run → Pi RPC | 共同 session run 登记并创建 private Child | 同 Pi + ThinkThread |
| Candidate 进程环境 | 宿主把冻结 `process_environment` 注入候选进程 | 同一宿主输入接线 | 由 ThinkThread Profile/Runtime 控制，不接受 candidate process environment | 同 Pi + ThinkThread |
| 等待 | `goal_plus_session_wait_any` | 同一工具 | 同一工具 | 同一工具 |
| 续跑 | `goal_plus_session_run` 复用 native session | 同一工具 | 同一工具，复用 Child/Session/branch | 同一工具，复用原 Child；丢失原上下文时拒绝冒充续跑 |
| Deadline | Application 预算 gate + 宿主执行限时 | 同一接线 | parent/controller watchdog | 同 Pi + ThinkThread |
| 恢复 | `.gp` checkpoint/generation + scope 资源核验 | 同一恢复用例 | scope 资源日志、原 call_id/Message、独占 controller lease | 同 Pi + ThinkThread，保留原 Child/context 归属 |
| Goal gate | 精确 `$goal-plus...` + hooks | 精确 `/goal-plus...` + extension events | 同 Pi gate + exact Profile preflight | Codex gate + exact Profile preflight；managed config 无 hooks，TT Root 无 Stop-hook 续跑 |
| 模型发现 | app-server `model/list` | Pi RPC model catalog | Profile 默认模型，任务级 override 关闭 | TT Profile allowlist；默认或显式 exact provider/model |
| Observability | native JSONL + metadata | `pi_metrics` + metadata | TT IDs + durable snapshot/request IDs | 同 Pi + ThinkThread |
| Workspace | `copy`、`git_worktree` | `copy`、`git_worktree` | `thinkthread` | `thinkthread` |

本地候选的进程环境和工作目录由 `native_candidate_input` 从已授权 session/task 构造，
不在 Pi launch 中另存 worker 交接副本。Pi entries 增量由存活的原生 session actor 采集，
准确 invocation 观测写入既有 provenance，续跑 launch 不传递 metrics baseline。

当前可移植 planner 为 `agent_guided`（含 `agent`、`default`）和 `random`（含
`random_mode`）。Codex/Pi RPC 的普通运行使用固定初始并行循环；Git worktree 可选
`adaptive_search`。ThinkThread（Pi 与 Codex Profile）支持固定并行和
`adaptive_search`，Codex
ThinkThread 默认继承 Profile 模型；显式模型必须来自 Profile allowlist，并以 exact
`provider/model` 传给 Child。harness/runtime/workspace 组合不匹配或 Profile preflight 失败时 runtime 在
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
`GOAL_PLUS_VERIFIER_RESOURCE_LOCK_DIR`。可信本地 candidate host 在启动 worker 前解析该 host
目录，并覆盖投影到 candidate 进程；worker 的临时目录或 candidate 环境不能改变锁身份。多个
容器或 sandbox namespace 共享设备时，宿主接线还必须把这个目录映射为同一个可写 host 目录；
未共享该目录的执行者不参与互斥。

ThinkThread verifier 使用相同锁目录和冻结 `resource_lock` 的精确字符串身份；相同资源串行，
不同资源并行，不从 Root 的逻辑设备编号推断远端设备。执行结果未知时保留原 request 的持久
占用，其他请求收到 `VerifierResourceNeedsRecovery`，原请求确认完成后释放。只有共享该目录
且使用同一实际资源身份的控制器参与互斥；不约束外部任务或独立机器的资源使用。

## Candidate 循环

新的 Codex/Pi spec 默认使用 `initial_context="fork"`。需要从冻结 baseline 创建初始 candidate
时显式使用 `spawn_mode=fresh` 和 `initial_context="fresh"`。Fork Search 先完成 Main 准备回合，
再从完成 checkpoint 创建固定数量的 candidate lane：

1. 启动全部初始 candidate；
2. 用共同 session wait-any 等待首个完成的 invocation，并按 roster 处理其余调用；
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
schema；没有指定模型时，保留 host 原生继承行为。默认 `detail=false` 只返回
`provider`/`model_id` 和 reasoning 选择字段，减少模型选择时的上下文开销；`detail=true` 才返回 display name、模态、限制
和 catalog provenance。runtime freeze 始终使用完整目录做精确解析。模型目录只说明当前可发现的模型，
不保证旧版 host tool 能转发所有可选字段。

模型目录只证明模型可解析。Goal command 与 `goal_plus_search_freeze_spec` 不额外发送
推理请求，provider 的实际可用性在对应 agent invocation 中检验，失败原因写入执行记录。
Pi 未显式覆盖的 child 从 host-local execution scope 继承 Main 的 exact native model/thinking。
Codex/Pi 都继承 host 配置，不建立 provider、credential 或 endpoint plumbing。

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
ThinkThread process verifier 的 `timeout_seconds` 会原样转换为 `fs.run.limits.timeoutMs`；当前
alpha9 Agent POSIX 基线支持 1 至 1800 秒，超过 300 秒时必须同步升级 ThinkThread Runtime 与 SDK。

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

共同 candidate host 在派发缺少直接 scope attachment 的 linked run 前核对 Main session、Goal
revision 与 run 归属。匹配当前 activation 的 open scope 直接幂等追加 run；干净关闭的 scope
在同一 activation 内只有显式 resume 后才创建下一 generation，并继承原 scope 的 native agent
配置。ThinkThread 的新 Goal activation 可在同一 Main session 的历史 Goal scope 干净关闭后创建
独立 scope；正在关闭或清理失败的 scope 保持 fenced。该过程不删除旧 run、fence 或进程记录，
也不把 Goal 改为 `needs_user`。replacement run 经过同一合同，没有专用恢复分支。

正常续跑优先复用 host 的 native session。工作区保持完整、仅需要 fresh context 时，使用
可移植的 state redispatch。以下 fresh context 流程仅适用于非 fork candidate；fork 的
`new_session=true` 仍复用原绑定，原 native context 丢失时拒绝以 fresh session 冒用 fork provenance。

非 fork candidate 的步骤为：

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
明确报错，Main 检查领域状态后以新 call_id 继续，不进入 checkpoint recovery。原对话无法加载时，
非 fork candidate 可显式使用 `new_session=true` 接手现场，无需先恢复旧对话。

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
| [Main ST](../tests/st/test_st_unified_main.py) | 无旧控制 skill 的两端 Main、共同 Search/session 流程、连续评价修订与版本化 Global Evidence |
| [TT ST](../tests/st_thinkthread/test_linux_v3.py) | retained Child、准确 Message、工具桥、snapshot 和清理；完整场景及证据边界见[支持范围](#fork-支持范围) |
