# 共享平面

Goal Plus 是面向长时间 agent 任务的宿主中立运行时。普通任务停留在 Goal
Mode；具有明确可度量目标的优化任务进入 Search Mode。Search Mode 会先冻结评价
合同，再让多个隔离、长期运行的 candidate 并行探索。

Search Mode 的核心是持久化的共享平面。共享平面不共享 agent 的私有推理，也不共享
可写工作区。它只共享冻结合同、精确 Git revision、verifier 支持的 Evidence、异步
生成的客观 View 与可选开放式补充评价，以及选择和提升结果所需的持久化状态。

## 总体架构

```text
main agent
  |  发现并冻结 SearchSpec，分配初始 candidate
  |  观察持久化结果，执行全局停止策略，选择并提升结果
  v
+------------------------- 共享运行时平面 -------------------------+
| Goal 记录与冻结 SearchSpec                                      |
| candidate 工作区与 Git revision                                 |
| verifier-backed Global Evidence 与异步 View                     |
| candidate-local best、全局 best、报告与 promotion               |
+------------------------------------------------------------------+
       ^                         ^                         ^
       |                         |                         |
 candidate c001             candidate c002             candidate c003
 隔离工作区                 隔离工作区                 隔离工作区
 持久 lane                  持久 lane                  持久 lane

宿主平面：启动、wait-any、continuation、deadline、interrupt、原生日志
```

各组件的职责边界如下：

| 所有者 | 职责 |
| ------ | ---- |
| Search runtime | 冻结合同、物化工作区、提交 Git 和 verifier Evidence、回滚、选择、报告与 promotion |
| candidate worker | 选择自己的技术方向、修改自己的工作区、调用 verifier、提交 handoff |
| main agent | triage、spec discovery、初始 candidate 分配、全局停止、最终收尾、确认 verifier 失效 |
| Codex 或 Pi 宿主 | 实际 worker 启动、等待、续跑、deadline、interrupt 和原生 transcript |
| Evidence annotator | 对每个已结算尝试生成客观描述，并基于动态 Evidence 快照生成开放式补充评价与 peer 比较 |

Search runtime 不是 worker supervisor。`AgentSessionRecord` 只记录上下文、来源和
宿主 launch payload，不表示进程是否存活。Pi 的 pool 状态因此单独保存在
`.gp/host-pools/pi/`；Codex 生命周期由原生 agent registry 管理。

宿主为当前 main-agent owner 建立独立的 Execution Scope，状态位于
`.gp/host-scopes/`。它只绑定活跃进程、pool、hook 已观察到的 Codex native task target 和
ThinkThread Child：

```text
main native session
  +-- host Execution Scope guardian
        +-- candidate / ordinary / final-check workers
        +-- Evidence Annotation
        +-- ThinkThread candidate and Annotation Children
```

正常 selection 前，host 先停止 candidate 并确认静默；runtime 随后关闭 Annotation admission、
递增 publication generation 并终止 annotator。main 被 abort、shutdown 或进程退出时，guardian
立即 fence 新 launch 并清理 scope-owned 活计算。该操作不修改 Goal 或 Search 的 durable 状态，
也不提供 scope 接管、Annotation 重开或 resume 语义。

Codex 的正常 closeout 由 main 使用原生 `list_agents`/`interrupt_agent` 完成，hook 只记账并强制
静默门禁；另行启动的 app-server 不控制当前 TUI turn。Pi/ThinkThread 的 pool 和 provider mutation
在 scope admission 下先变为可发现；无法确认完成的 ThinkThread request 或 Child spawn 保持
`needs_recovery`，不能被报告为已清理。

Goal Plus intake 将 host command 开头连续的 typed `key=value` 与自然语言目标分离。
`GoalPlusRecord.command_config` 保存显式配置，`raw_goal` 只保存目标和探索模式指导；
host-command invocation 继续保存原始输入 hash。ready SearchSpec draft 必须匹配显式
配置，只有缺省字段才由 main agent 在 Spec Discovery 中决定。因此 config precedence
由 runtime 校验，不依赖 prompt 是否正确复述。

## SearchSpec 与评价合同

一个 `run_id` 绑定一份不可变的 `SearchSpec`。它至少包含：

- 可度量的目标、`metric_name` 和方向；
- `source_path` 与允许修改的文件范围；
- process verifier 与 promotion verifier；
- 初始并发宽度、工作区后端和 worker 执行策略；
- 可选的用户模型选择 `strategy.models`；
- verifier 所依赖的源码内 artifact。

当前逻辑由 main agent 在 Spec Discovery 中读取任务说明、公开数据和评分工具，然后
构造或选择 verifier，并调用 `search_freeze_spec`。Runtime 会：

- 使用 Pydantic 校验 SearchSpec 结构；
- hash-pin verifier artifact；
- 对 Git source 在 origin 执行一次 process-verifier suite，并固定 clean source commit；
- 在 run 的首个 Git worktree 中重复一次相同 suite，证明 relocation 后的路径合同有效；
- 检查退出码、数值 metric、超时、verifier hash 和 source identity。

`copy` 仍保留旧的 disposable-copy ranking preflight。ThinkThread backend 在 origin 验证后
固定 Root-owned provider snapshot，不重复 Git relocation；受支持的 Pi Profile 从该 snapshot
派生 private Children。Codex 对 TT candidate 路径仍会 fail closed。

Runtime 不理解具体 benchmark 的语义，也不会自动证明本地 verifier 与官方 Judge
等价。公开评分公式、有效性条件、每例资源限制和聚合方式是否完整进入 verifier，仍由
SearchSpec 的创建者负责。隐藏数据只能作为明确声明的近似项，不能被无提示地替换为
另一套权威指标。

SearchSpec 不包含预设软 rubric。这样 Spec Discovery 阶段不会因为 MainAgent 过早猜测
任务维度而固定后续注意力、假设和代码基线。

Benchmark 机制消融由 controller 设置
`GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED` 和
`GOAL_PLUS_SUPPLEMENTAL_EVALUATION_REQUIRED`。它们只决定 annotation task 是否要求开放式
补充评价，不向 FrozenSpec 注入 criterion。ON/OFF 都不改变硬分结算或评价内容；required 模式
会在所有 task 成功发布补充评价前阻止 selection，不能把缺失或失败的 Annotation 当成通过。

Evidence annotator 继承 `FrozenSpec.native_host` 记录的 Main Agent 身份，不接受 `host` 配置。
角色执行驱动由该身份和工作区后端确定。模型和 reasoning 可以作为实验变量独立
冻结，外部连接与认证配置始终来自启动它的 native host 进程，不进入 Goal Plus 配置或状态。

每个 annotation context 同时提供本轮有界 `actual_diff`、从候选初始基线到当前提交的有界
`candidate_diff`，以及 target/peer 的完整 immutable Artifact View。description 只描述本轮增量；
开放式补充评价直接核对完整候选 workspace，diff 只用于定位，超限时记录 omitted 原因。
annotation task 创建时还会快照最多 8 个其他 candidate 的当前硬分最佳结算版本，每个
candidate 最多一个，固定其 candidate、iteration 和 commit，后续推理基于这份可审计
比较基线。

为判断代码变化与原始请求是否相关，annotation task 记录与当前 Search run 绑定的准确
Goal revision 引用和 SHA-256，而不复制 `raw_goal`。annotator 启动时临时解析该 revision
并校验 hash；没有绑定 Goal 时才退回 FrozenSpec objective。原始上下文只进入隔离的
annotation prompt，不进入 Global Evidence、candidate task 或报告正文；报告只可保留来源、
引用和 hash。benchmark adapter 只能提供公开任务字段，不能把 hidden judge、答案 patch
或私有评测数据写入 Goal。

冻结后的合同不能原地修改。确认 verifier 合同有误时，应先使当前 run 失效并停止其
worker，再冻结修正后的 spec，创建 successor run。旧分数不能跨合同复用。

## 一次 Search Run

一次正常的 Search run 按以下顺序执行：

1. main agent 提交 SearchSpec；runtime 在 origin 验证 verifier 并冻结 source artifact。
2. `search_create` 确认 source identity 未变化，一次初始 `SearchPlan` 分配固定 lane。
3. Git backend 先物化 `c001` 并执行 relocation gate；通过后才物化其余 candidate。
4. 每个 candidate 获得独立工作区，并按 frozen `strategy.inner_agent` 启动
   `autoresearch` 或 `common` worker。
5. 宿主以 wait-any 方式返回完成事件；较慢 lane 不阻塞其他 lane 发布 Evidence。
6. 只要全局停止条件未满足，宿主续跑同一个 candidate、原生 session 和工作区。
7. 收尾时先让所有 live worker 静止，再选择精确 commit，执行 promotion gate 并生成报告。

origin/relocation bootstrap result 单独保存在 FrozenSpec 和 RunRecord 中，并由 monitor/report
展示。它不是 candidate iteration，不进入 Global Evidence、`results.tsv`、worker verifier
计数或候选排名。relocation 失败会以 `verifier_target_mismatch` 自动 abort run，保留 `c001`
供诊断，且不会启动 worker。

`budget.max_parallel` 是 spec 的初始 candidate 数和实时 live-worker 上限 `K`。`parallel_loops`
只做一次初始分配，不按分数创建替代 candidate，也没有后续
规划轮次。用户提供 `strategy.models` 时，runtime 在冻结前通过 host adapter 验证
名称，并在初始 plan 中生成与 lane 一一对应的 `selected_models`：未写数量时轮转，
全部写数量时按数量展开。每个 selected model 随 candidate 和原生 session 的续跑保持
不变，但所有 lane 仍共享同一份 Global Evidence。

### Adaptive Search allocation

`strategy.orchestration_mode="adaptive_search"` 是独立于固定候选 `parallel_loops` 的调度流程，
并要求非 `null` 的 `strategy.search_scheduler`。它保留 Search 的 hard settlement 主流程，只在
settlement 后增加 scheduler 分支。`adaptive_search.py` 是核心 runtime 的唯一 Adaptive Search facade 并协调
barrier、scheduler 结果和 Allocation；`scheduler.py` 负责配置、`ScheduleTask`、reward 列表、可恢复
异步 worker、公共 scheduler context 及一次性 Codex/Pi 调用；`allocation.py` 独立验证、持久化并执行
Decision；`policies.py` 管理版本化策略及其 reward/allocation context builder。ScheduleTask 和
Decision 分别由所属模块持久化，不共享 Store。

Adaptive Search 要求 `workspace.backend="git_worktree"`、`budget.max_parallel` 和非空
`search_scheduler`。可选的 `budget.max_candidates` 限制累计唯一 candidate；`null` 表示不限制，
有界值必须大于等于 `max_parallel`。`search_scheduler` 直接包含
model/reasoning/timeout 模型调用参数和 `reward`/`allocation` 策略引用。策略使用
`name/vN [@prompt 补充说明]` 格式，runtime 在 search 开始前解析已注册的准确版本并把策略快照
写入 FrozenSpec；runtime 根据冻结版本调用注册的 context builder，异步 agent 不查询注册表。三项
模型调用参数均为必填项，不继承 worker profile；host 从冻结的 Main 身份解析。
resolved profile 标记 `configuration_source="host_process"`，不保存外部连接或认证配置。
配置不接受函数名或脚本路径。并发由
`budget.max_parallel` 推导，重试采用内部策略。
backup 和 expansion 分别归入 reward 与 allocation，不再暴露独立参数。

每次 verifier settlement 先写入 `AnnotationTask`；其 completed 或 terminal_error 后，runtime 才把
精简 Evidence View 投影到 candidate-local `ScheduleTask`。annotation 失败时 View 为 `null`，scheduler
只收到“参考历史观测自行决定补偿方案”的提示，不采用固定补偿值。一次无 session scheduler 调用按
reward -> pruning -> optional derivation 顺序同时返回 reward、rubric/UCT/exploration 分量和 allocation
flag。`scheduler.py` 构造当前结算事实的 `scheduler_context`，再通过 `policies.py` 的统一入口取得
与准确策略版本匹配的 `reward_context` 和 `allocation_context`。screen 阶段只获得对应 pruning
所需的统计或精简 peer frontier，只有确认 task 才获得全 candidate reward-bound 节点和 derivation
规则，并可返回独立的 Decision 草案。task 按 candidate iteration 有序消费，不同
candidate 可并发，失效进程留下的 pending、running 或 retry_wait task 可以恢复。
`ScheduleTask.output` 是 reward/flag 的唯一权威记录；runtime 只在读取时从 completed task 索引生成
reward 列表，不保存 value tree 或 candidate/report reward 副本。reward 只读取当前 candidate 历史；
screen allocation 读取精简 peer frontier，确认 allocation 才读取全 candidate 节点。reward 记录通过
settlement id、ScheduleTask id 和 node id 显式绑定到 settled node。系统不维护 run-root tree、
graph、transition、backup event、action edge 或 hindsight。

lazy allocation 读取上一已完成节点的布尔 flag。上一 flag 为 `false` 时，当前 settlement 不等待
annotation/scheduler 链；上一 flag 为 `true` 时，runtime 直接把当前 task 视为确认任务。native host
异步完成 AnnotationTask 和 scheduler；runtime 本身不启动或等待模型，并在确认任务终态前拒绝该
lane 的下一次 continuation、redispatch 和 verifier。只有当前 flag 再次
为 `true` 且独立 Decision 草案通过 candidate 容量、retiree 和精确 settled source 校验，
runtime 才在
`.gp/runs/<run_id>/allocation-decisions/*.json` 持久化 `AllocationDecision`。Decision 固定退休
candidate、依据、预留 derived id、source candidate/iteration/node/commit/artifact、探索方向和可选
worker budget，
同时 fence 全部 retirees 的下一轮 iteration。

`search_apply_allocation_decision` 幂等预检并物化整份计划：从 Decision 的 source commit 创建
workspace，继承 results ledger、run-global Evidence 可见性和 selected model，再为每个 replacement
创建 native `AgentSessionRecord` 并返回 launch payload。host 必须先释放全部 retired live handles，
再启动全部 payload；它不计算 reward、不选择来源、不改写计划，也不自动 refill。

Evidence annotation 是 ScheduleTask 创建的弱依赖：必须先终态，但 terminal_error 不阻断 scheduler。
任一侧失败不改变 hard score、
keep/retain/discard/failure、`best.json`、selection 或 promotion。`parallel_loops` 不创建
ScheduleTask 或质量驱动的替代 candidate。隐藏答案 benchmark 不能向 scheduler/worker 暴露 gold
correctness、score 或其派生决策；这类任务继续使用公开格式 verifier 和 gold-independent 聚合。

## Candidate 循环

Outer workspace 与 inner-agent 行为是两个独立选择。Git outer provider 使用 worktree 和
commit Evidence，ThinkThread 使用 private Child 和 snapshot Evidence；两者都支持
`strategy.inner_agent="autoresearch"` 和 `strategy.inner_agent="common"`。兼容 ID
`autoresearch` 明确解析为内置 AutoResearch-Git：它自主执行多轮，并维护 candidate-private Git
attempt history 与物理 `results.tsv`；TT 中该 repository 位于 Child 内。`common` 每次 dispatch
只完成一个有界 attempt，不维护 inner-agent Git history 或 `results.tsv`。两者都必须调用同一 Goal
Plus MCP，并由 runtime 在 `search_run_verifier` 边界形成 authoritative outer artifact。

每条 candidate lane 使用以下协议：

1. 首次启动或 fresh session 恢复时调用 `search_get_agent_context`，读取自己的权威状态和
   `candidate_task` contract；同一 session continuation 沿用已加载上下文。
2. 按冻结模式使用 Evidence：`manual` 在首次修改前读取并周期刷新 Global Evidence；`auto`
   使用 verifier 返回时自动注入的共享快照；`independent` 不向 worker 注入 peer Evidence。
3. 独立选择方向，交付修改限于自己的 `allowed_files`；`.tmp/` 可用于 candidate-local 临时状态。
4. 启用 `shared_dir` 时回顾本轮及此前 iteration 的命令序列、临时代码片段和 scratch
   scripts。命中正向工具化信号时，从 `.tmp/tool-drafts/` 显式选择源文件并调用
   `search_stage_shared_tool`；否则记录具体排除项。
5. 调用 `search_run_verifier`，用一句 `hypothesis` 描述实际完成的尝试，并在启用
   `shared_dir` 时传入 `toolization_decision`。
6. Git/Pi RPC 同步收到 `ScoreReport`；ThinkThread 先收到 accepted receipt，并在同一 retained
   Child 的下一次 wake 中收到 authoritative feedback。收到 feedback 后，`autoresearch` 从已结算
   工作区继续下一轮；`common` 返回 handoff，由 main 决定是否 continuation 同一
   candidate/session。

工作区内的文件都对 worker 可读，可用于导入、profile 和本地诊断；交付修改限于
`candidate_task.allowed_files`，`denied_files` 与冻结 verifier 资产保持只读。Runtime 提供的
`.tmp/` 是 edit surface 之外的 candidate-local 可写临时区，用于 scratch、恢复 handoff，以及
启用 `shared_dir` 时的工具草稿。本地命令输出只是分析材料，只有 `search_run_verifier` 返回的
结果进入正式 Evidence。Runtime 根据该 run 的实际 Evidence、supplemental evaluation 与
`shared_dir` 开关生成 `candidate_task.instructions`；静态 Codex/Pi worker prompt 不描述未启用的能力。

`candidate_task.acceptance` 只包含 FrozenSpec `candidate_acceptance` 中显式声明、受界且
对 worker 可见的验收条件；objective 和 metric 位于 agent context 顶层，`constraints` 保持
runtime 私有。`candidate_task.process_environment` 是该 lane 的进程环境。host 在
`GOAL_PLUS_DEVICE_POOLS` 中注册受信任的 pool、可见变量和非敏感设备清单；SearchSpec 的
`device_pools` 只能引用 `pool_id`，不能选择任意宿主环境变量。若 verifier 以
`resource_lock="<pool_id>:0"` 声明单卡资源，runtime 会在 freeze 时固定该 pool 并按 candidate
一一分配不同设备；pool 容量不足或多个 pool 共用同一可见变量时 freeze 直接失败。正式
verifier 使用对应环境和物理设备锁，freeze preflight 使用第一条 lane 的单设备环境，
continuation 沿用同一个 CandidateTask，不重新分配资源。active run 还会在 host 共享的
`GOAL_PLUS_VERIFIER_RESOURCE_LOCK_DIR` 下持有 pool lease，防止其他 run 超售同一 pool。

candidate 不需要在修改前提交 iteration plan。`hypothesis` 是完成尝试后的事实性
自述，与 verifier 结果一起保存；它不是 pending plan，也不形成协调锁。多个
candidate 可以同时读取同一版 Evidence 并并发工作。

当前工具化信号按能力分为六类：`repeated_workflow`（重复或等价的多步命令/操作流程）、
`domain_construction_or_probe`（非显然领域对象/状态构造、边界、配置或兼容性探测）、
`behavior_or_invariant_checker`（功能验证、断言或正确性、性能、资源及状态不变量检查）、
`reproducer_fixture_or_case_generator`（最小复现、fixture、输入、corpus 或 case 生成）、
`parser_trace_or_comparator`（解析/转换、trace/日志归一化、差分/状态比较、mutation 或失败分析）和
`peer_setup_or_feedback_reduction`（setup/cleanup、环境 harness 或缩短 peer 本地反馈循环）。复用范围
只要求同一 run 内的其他 candidate 有用，不要求跨项目通用；短小、任务专属、来自临时代码片段或只输出退出码都不能单独
作为低价值理由。

测试代码按用途而不是路径、文件名或框架分类。搜索期间创建的测试，以及 candidate 自己编写并纳入
最终交付或主补丁的正式回归测试、配套 fixture/case 生成器、功能验证函数、差分或不变量检查都可以
作为工具；`test_` 文件名、使用测试框架、属于正式测试或验证同一目标行为都不是
`restricted_artifact` 理由。worker 仍须把显式选择的测试及依赖复制到 `.tmp/tool-drafts/`，并确保
它能在 peer workspace 运行且不依赖 candidate 私有临时状态。该排除项只覆盖 candidate 产品实现、
冻结 verifier/runner/grader、隐藏答案或评分逻辑，以及日志、原始数据、凭据和构建输出。共享测试或
诊断工具不得复制、代理或近似重建隐藏反馈。其他具体排除项仍包括单条普通命令、无逻辑 wrapper、
依赖 candidate 私有状态或与已发布快照完全相同。

首次工具发布保持低门槛，短小 inline probe 仍可进入共享面。每个不可变 `SharedToolRecord`
同时绑定 runtime 分配的 `family_id` 与递增 `version`。worker 仅在 catalog 的
`revision_allowed=true` 时引用唯一的 `revision_head.tool_id` 更新已有 family。
`capability_extension` 必须新增稳定 capability/coverage key；`adoption_fix` 必须有同 family 的真实
copy/adoption 事实和具体缺陷；`contract_change` 必须为有价值的入口、输入、输出或依赖变化新增稳定
契约键。键是机器可比较的语义标识，纯改名、重排、同义断言或改写同义键不构成新版本。
`no_toolizable_material` 表示 worker 回顾本轮及此前尚未发布的材料后确实没有工具。它与已有 family
没有实质 revision 增量不同。`revision_allowed=false` 时 worker 使用 catalog 的 `revision_blocker`（例如
`max_published_versions_reached`）作为具体排除事实。`existing_family_sufficient` 仅表示
`revision_head` 已覆盖
需求；`no_material_tool_delta` 仅用于已有 shared-tool family，表示没有上述实质增量。runtime 在
catalog 尚无 family 时拒绝这三个 revision-only 排除项。`draft_not_ready` 仅表示具体的安全性、
完整性、可移植性或 peer 可运行性阻塞。

`SharedToolFamily` 保存一个 `pending_head` 和一个 `discoverable_head`。新 revision 等待 Tool View 时，
Global Evidence 继续暴露旧 discoverable head；Tool View 成功后 runtime 原子切换 head，失败则清空
pending 并保留旧 head。更新的 pending revision 会取代尚未生成 Tool View 的旧 pending revision，
旧 iteration 仍生成普通 View，但不再请求过期 Tool View。跨 candidate 更新在 run transaction 内
仲裁。worker 通过不含路径和源码的 `tool_family_catalog` 发现 family 身份、pending/discoverable
head、唯一 `revision_head`、累计能力/覆盖键及版本容量；复制仍必须使用 Global Evidence 中
discoverable head 的精确 `tool_id`/`snapshot_hash`。
默认配置允许每个 family 一个 pending revision、最多两个不可变发布记录（pending 也计入）；提高
版本上限后仍可用新 pending 取代尚未完成 Tool View 的旧 pending。该限制按 family 生效，不使用
run-wide 工具总数限制。

`ToolizationDecision` 只是 `IterationRecord` 的普通事实。缺失、声明 staged 但 staging 为空，
或声明 not_applicable 但 staging 非空会分别产生 advisory；这些字段只供 monitor/report，
不改变 score、verifier disposition、selection 或 promotion。实际 staging inventory 与现有
publication settlement 始终是权威。

main agent 不向 worker 提供后续技术方向。`parallel_loops` 优先续跑同一个原生 session；
redispatch 只用于恢复，并继续使用同一个 candidate 工作区、Git 历史、verifier 历史
和有界 handoff。`adaptive_search` 中的 Search Scheduler 只有在 verifier 返回 runtime 已持久化的 allocation decision
时才停止该 candidate；worker 和 host 都不能自行推断质量剪枝。

## Global Evidence

`search_get_global_evidence` 将当前 run 中已结算的 worker iteration 投影为窄表：

```json
{
  "candidate_id": "c001",
  "iteration": 3,
  "artifact_ref": {
    "kind": "git_commit",
    "id": "<exact-attempt-commit>",
    "provider": "git_worktree"
  },
  "commit": "<exact-attempt-commit>",
  "reference_available": true,
  "artifact_availability": "available",
  "annotation_result_available": true,
  "score": 13350,
  "disposition": "keep",
  "view": "将调度逻辑改为按依赖深度分组。",
  "view_created_at": "2026-08-06T12:00:00Z",
  "supplemental_available": true
}
```

`artifact_availability` 区分 durable identity 与 provider 内容可访问性：`available`、
`removal_pending`、`removed` 或 `unknown`。ThinkThread terminal cleanup 后仍保留 `artifact_ref` 和
已发布 annotation，但 `reference_available=false`、`artifact_availability=removed`，不再承诺重开
完整 Artifact View。

cleanup 只释放 provider 临时资源：candidate/annotation Child、private branch 和不再被 pin 的
snapshot。score、diff、annotation、provider journal、selection、promotion 和最终 report 属于
durable shared plane，不会随 cleanup 删除。面向 main 的 monitor 通过 `host_cleanup` 投影 pool、
annotation、snapshot 和 final artifact 状态，不暴露 session-private 物理路径；原始 host-pool
文件只用于 provider lifecycle 调试和真实 ThinkThread 系统验收。

冻结 spec 显式启用 `shared_dir` 时，已发布工具还会以 `shared_tools` 出现在同一条
Global Evidence 中。每项包含 runtime 绑定的 `tool_view`、`tool_id`、`snapshot_hash` 与
`source_commit`。候选不会看到共享目录路径，也不能在 Tool View 生成前发现工具；它只能通过
`search_copy_shared_tool` 按精确 id/hash 复制到本地临时 inbox，阅读 manifest 与源码后自行决定
直接执行或导入、提取并改写局部逻辑、复用诊断方法、作为实现对照，或不采用。复制本身不要求
调用原工具。只有直接执行、导入或把原快照作为运行时依赖时，才先在当前 workspace 验证原工具；
只读分析或改写源码不要求运行原工具，最终候选修改仍由正常 process verifier 验证。下一次
worker verifier 原子消费 copy receipt。当前 schema 中的 `adopted_tools` 仅证明快照曾进入本轮上下文，
不证明原工具被执行或其代码被保留。工具描述与复制结算事实仅是后续搜索的参考，不单独聚合收益，
也不改变硬分、retain/discard、选择或 promotion。
工具化决策本身不进入 Global Evidence。完整状态流保持为：
`ToolizationDecision -> IterationRecord`，以及独立的
`staging -> passing verifier -> SharedToolRecord -> Tool View -> Global Evidence`
`-> copy receipt -> adopted_tools`。

完整评价不在窄视图中重复返回。worker 仅在 `search_get_agent_context` 的
`supplemental_evaluation_enabled=true` 且目标行
`supplemental_available=true` 时按需调用：

```text
search_get_evidence_detail(
  agent_session_id="<current-session>",
  candidate_id="c001",
  iteration=3
)
```

工具只解析调用方当前 run 的已结算 Evidence identity；`independent` 模式只允许读取自己
candidate。返回完整 `supplemental_evaluation`，但不暴露 annotation 文件路径、peer
workspace、transcript 或推理。

verifier 会同步发布 `candidate_id`、`iteration`、`commit`、`score` 和
`disposition`。`disposition` 取值为：

- `keep`：尝试有效，并且严格改善该 candidate 的历史最佳；
- `retain`：尝试有效、硬分同分，并成为该 candidate 的最新工作基线；
- `discard`：尝试有效，但分数更差；
- `failure`：本次尝试验证未通过、缺少有效分数或 artifact 不符合约束。尝试记录和 Evidence
  仍然保留，不更新 best，也不表示整个 Goal/Search 失败；缺分数时原因是 `MissingNumericMetric`。

Global Evidence 只包含 worker 的 process-verifier 尝试。parent fallback verification
与 promotion verification 不会成为 peer Evidence。视图也不会暴露 peer transcript、
私有推理、annotation 内部任务状态或 peer 工作区路径。

EdgeBench 的 Goal Plus 模式默认通过 `GOAL_PLUS_EXTERNAL_EVIDENCE_DIR` 提供按轮保存
的外部评测附件；将该变量显式设为空字符串可关闭。Runtime 仅在其中的 `run_id`、
`candidate_id`、`iteration` 和 `commit` 与既有
process-verifier Evidence 完全一致时，将记录附加到该条目的 `external_evaluations`；
它不创建 iteration、不参与本地排名，也不成为第二份历史账本。未配置、记录不完整或
身份不匹配时，Global Evidence 行为不变。附件仍服从 `manual`、`auto` 和 `independent`
三种可见性模式。

## 客观 View 与开放式补充评价

`view` 是绑定到准确 Evidence identity 的异步、不可变、best-effort annotation。后台
Codex hook 或 Pi extension 在 verifier post-tool callback 中按冻结的 Main 宿主身份启动
drainer，再选择 Codex 的一次性只读执行或 Pi 的一次性受限工具执行；不会为了 annotation 跨用
另一个 host。共享 MCP 只管理 task，不启动模型进程。Annotator 可读取：

完整的角色边界、host 路由、配置继承、重试状态和排障流程见
[Evidence Annotator](evidence-annotator.md)。

- candidate 的一句 `hypothesis`；
- 从本轮 settled base 到 attempt commit 的有界 diff；
- target 与 comparison basis peer 的完整 immutable Artifact View；
- 精确 attempt commit；
- 本轮 changed files；
- 冻结的 process verifier 合同；
- verifier 结果与相关 metrics。

Annotator 只通过授权入口读取这些内容，不运行项目代码、测试、verifier 或执行只读查看之外的
命令。Host 只激活领取任务、读取 Artifact View 和提交 annotation 所需的精确工具集合；不为
annotation 派生或修改 snapshot。

View 只用一句中文客观描述实际做了什么，不评价好坏、不推断动机、不排名，也不推荐下一
步。事实来源是 immutable target Artifact View 与 actual diff，而不是 candidate 的自述。Changed files、verifier command
和 metrics 只提供验证上下文；命令名称本身或失败的测试不能证明目标行为已经实现。

`supplemental_evaluation` 不读取 FrozenSpec 软标准。annotator 只依据当前候选和 peer 的完整
Artifact View、公开 verifier Evidence 和 annotation task 创建时固定的 peer identity，自行提出 1–8 个与当前
任务实际相关的观察维度。它逐项给出 finding、证据与置信度，并对比较基线中的每个 peer
返回非定向的 `similar`、`different`、`tradeoff`、`complementary` 或 `unknown`，不能借此
选择赢家。没有 peer 时 comparisons 为空。limitations 明确列出公开 Evidence 无法判断的
事项。

这种评价发生在提交结算之后，因此不会在搜索开始前固定注意力方向。worker 可以把第三方
观察作为下一轮假设来源，但必须独立核对；评价不产生总分或最终推荐，不能改变硬 score、
PASS/FAIL、candidate-local 基线、run-wide 排名或 promotion gate。

annotator 收到的累计 diff 使用 Git 函数级上下文和至少 10 行普通上下文，并继续受字节
上限约束。上下文中未出现某个定义，不代表该定义不存在；这类判断必须降低置信度并写入
`limitations`。每次 worker 调用 `search_get_global_evidence`，runtime 都会在对应
`agent_sessions/*.json` 的 `global_evidence_reads` 中记录读取时间、当时 Evidence 数量、
已完成 View 的 candidate/iteration/commit 引用以及其中是否含 supplemental evaluation。
该读取记录只用于审计 View 是否在后续 verifier 之前可见，不参与候选结算或最终验收。

`view=null` 只表示 annotation 尚未发布，Evidence 本身已经有效。candidate 可以先按
自己的方向继续，不应等待、sleep 或轮询 View。

verifier settlement 是 task admission 的唯一权威来源；Evidence 读取只执行非阻塞 wake，不创建
新 task。持久 queue revision 对每个新 settlement 单调递增，一个 drainer 串行处理 backlog，
但 verifier、selection 和 promotion 都不等待它。`search_select` 在进入 `ready_to_promote` 前关闭
queue 并递增 generation，pending/retry task 变为 `cancelled`，正在推理的旧 generation 结果也会被
publication fence 拒绝。模型、host provenance、deadline、usage、task 和已完成 View 都持久化保存。

## Git 与 Candidate-Local Best

新 `git_worktree` run 不再删除 source 的 `.git` 后重新初始化。Runtime 对真实 Git source
创建 run-local bare mirror，保留完整 refs、commit history 和对象，再从启动 HEAD 建立
baseline ref 与各 candidate worktree。`source_path` 位于 repository 子目录时，worktree 仍是
完整 repository，worker cwd 和 edit surface 则保持相对于该 source 子目录。

Git-backed source 的规范输入是 clean `HEAD`。`search_freeze_spec` 在 origin verifier 前检查
整个 repository，验证后再次检查并把 HEAD 固定为 `source_artifact_ref`；`search_create` 在写入
run 前复检 clean 状态和准确 commit。staged、unstaged 或 non-ignored untracked 任一存在都会
以 `DirtySourceWorkspace` 失败，clean 但 HEAD 已变化则以 `FrozenSourceChanged` 失败。Runtime
不自动 stash、clean 或提交 source。`.gp`、`.search`、`.tmp` 等既有 runtime-noise 排除项不参与
判脏。确实要把当前目录状态作为输入时，显式使用 `copy + artifact_only`。

run-local mirror 以 frozen commit 建立 baseline。`search_start_batch` 先物化 `c001`，确认其 HEAD
与 frozen artifact 一致，并在该 logical workspace 重跑 process verifier；只有通过后才创建
剩余 candidate。该 relocation gate 专门阻止 origin 路径能运行、worktree 路径却失效的合同。

Evidence 中的 commit 表示 verifier 实际读取的完整 Git tree，不是相对于初始源码的
patch。Runtime 分别记录：

- `attempt_base_git_head`：尝试前的 settled HEAD；
- `git_head`：verifier 实际验证的 attempt commit；
- `attempt_changed_files`：用于 annotation 的 `base..attempt` 净变化；
- `changed_files`：相对于原始 source 的最终 artifact 变化，用于策略检查和 promotion。

Runtime 会在验证前提交所有 candidate-controlled 修改，并要求 artifact worktree
干净。candidate 可以包含多个手工 commit；annotation 使用完整
`settled-base..attempt` 范围，而不是只查看最后一个 commit。

每次 process-verifier 尝试都会永久记录在 canonical `.gp` ledger。严格改善和同分分别以
`keep`、`retain` 更新 candidate-local 最新最佳。只有 `discard` 或 `failure` 时，Runtime 才把
代码恢复到此前最佳版本。内置 `autoresearch` binding 还请求 trusted adapter 管理的
`results.tsv` 兼容投影，因此它的物理历史为：

```text
keep:     settled -> attempt -> ledger
retain:   settled -> equal-score attempt -> ledger
discard:  settled -> attempt -> restore-best -> ledger
```

attempt、恢复和 ledger commit 都保持在 candidate-private Git 历史中。因此下一轮始终从
candidate-local best
规划，而 Global Evidence 仍保留好、差和失败的全部尝试。`common` binding 不请求该投影：
keep/retain 后直接以 attempt commit 继续，discard/failure 后以 restore commit 继续，不创建
`results.tsv` 或额外 ledger commit。一个 candidate 的回滚不会改变 peer 工作区，也不会修改
peer 的判断。

在 ThinkThread 中，public `IterationRecord.artifact_ref` 仍是 verifier 实际读取的 quiescent
attempt snapshot，public Git compatibility fields 保持 `null`。private attempt commit 和
post-settlement ledger commit 只保存在 Child snapshot 与 `inner_agent_state` 中；settled Child
branch 指向 post-settlement snapshot。Annotation Artifact View、changed-file validation 和
publication 一律过滤 `.git`、`.gp`、`.search`、`.tmp` 与任意层级的 `results.tsv`。

最终 promotion 不改写或压缩 candidate branch。Runtime 以 run baseline 为 parent、以 selected
artifact 的允许产品文件为 tree，另行创建一个 squash publication commit；`results.tsv`、`.gp`、
`.tmp` 和 Git metadata 不进入该提交。`search_apply_promotion` 把这一个提交 cherry-pick
回原 repository，因此产品历史保持单提交，而完整 autoresearch running history 仍留在 run-local refs。
`workspace.promotion_mode` 默认是 `apply`，Goal Plus 在 apply 完成前拒绝记录 Search result；
只有受保护 source、只读 template 或测试 fixture 显式使用 `artifact_only`。

freeze 还会持久化 `workspace_capacity_estimate`。它同时区分当前 source scope、有效
materialized source、整个 repository 的每-node checkout、`max_parallel` 个 node 合计和共享
object store；因此 Git 子目录任务不会错误地只按子目录估算，也不会简单使用 `du cwd × K`。
保守上界和目标文件系统可用空间共同产生 `fits_available_space`，并进入 monitor/report。

`git_worktree` 后端下，各 candidate 共享一个 Git common directory。窄、单文件证据可以
从自己的工作区渐进式查看 peer Evidence commit：

任务局部 Skill 仍属于 source baseline，而不是共享平面。runtime 自动发现 source Git
index 中的 `.agents/skills/<name>/SKILL.md`，并要求该 Skill 目录内所有文件都由 Git 跟踪；
freeze 返回 `task_skill_notice`，每个 candidate
通过 `task_skill_paths` 获得自己 worktree 的绝对路径。无需额外 spec 配置或逐文件 Skill
哈希；Git baseline 决定内容。worker 只加载明确列出的 task-local Skill，不扫描
parent/global/plugin 或 peer worktree。

```bash
git show <commit>:<allowed-file>
git diff HEAD <commit> -- <allowed-file>
```

改动较大或需要浏览完整实现时，candidate 调用 `search_reference_open`。runtime 把 root-owned
Evidence identity checkout 到当前 worker 唯一、稳定的 detached reference worktree：

```text
candidate workspace/       # 当前 worker 的实现工作区
ref-workspace/             # 当前 worker 的参考 checkout
```

worker 直接使用普通文件和命令工具查看返回路径。再次调用会丢弃 ref-workspace 的本地变化，
并把同一目录切到新的 Evidence commit；不会创建第二个 reference 目录，也不要求 worker
close。candidate 的实现修改始终留在自己的 workspace，不访问 peer 活动 workspace。
ThinkThread 的完整 target/peer 阅读由独立 retained Annotation Child 的 immutable Artifact View
工具提供；candidate 不获得 sibling live branch 或 Annotation 工具。

最终 promotion artifact 虽然是
相对于 source 生成的 patch，但这不会改变 Evidence commit 代表完整代码树的语义。

## 选择、Promotion 与失效

run-wide best 从有效 iteration record 的硬 score 中计算，与各 candidate-local best 分开。
`search_select` 不读取补充评价或 peer 比较来改变排名；硬分并列按原有稳定顺序选择。它将
结果绑定到一个精确、通过验证的 worker Evidence commit。只有旧状态
或当前产物没有对应 durable Evidence 时，parent 才补做 process verifier。

promotion 是独立的验收 gate。Runtime 会检出选中的不可变 revision，在
`GOAL_PLUS_VERIFIER_PHASE=promotion` 下重跑配置的 promotion verifier，并把结果绑定
到 Git head 和 artifact hash。只有 promotion 成功，才生成可被 Git 应用的 patch；
source workspace 不会被静默修改。

worker 报告的 verifier concern 只是建议。main agent 确认缺陷后，Runtime 先原子
invalidate 并 fence 当前 run，宿主再停止所有 worker。修复后的合同必须进入 successor
run。可以继承有界研究上下文，但不能继承旧分数或把旧 Evidence 当作新合同下的结果。

## 持久化目录

```text
.gp/
  goal-plus/<goal_plus_id>/
  specs/<frozen_spec_id>/
  runs/<run_id>/
    run.json
    best.json
    plans/<initial-plan-id>.json
    candidates/<candidate_id>/
      candidate.json
      evidence-annotations/iteration-<n>.json
    evidence-annotator/
      queue.json
      artifact-views/<task>/<view>/workspace/
    agent_sessions/<agent_session_id>.json
    workspace/<candidate_id>/
    report.md
    report.html
    promotion/<candidate_id>.patch
  host-pools/pi/
```

`candidate.json` 中的 iteration record 是 Evidence 的事实来源。Annotation task 保存
可选 View 及其执行状态。Global Evidence 在读取时即时投影，不是第二份可写共享账本。
`best.json` 是当前 run 最优 verifier commit 的权威原子指针；严格改善或同分时更新，
退化和失败不会覆盖它。同分 candidate 按最后完成结算的有效 commit 决定。

## 核心不变量

- 并行工作开始前，先冻结 verifier 与编辑策略。
- 隔离可写 candidate 工作区，只共享持久化事实和 Git object。
- verifier-backed 严格改善或同分最新版本才能成为 candidate-local best。
- 回滚、选择和 promotion 后，所有精确 attempt 仍可审计。
- 客观 View 和开放式补充评价都可以延迟，但不能阻塞优化或改变最终硬验收。
- worker 生命周期和原生 transcript 不进入 Search runtime 状态。
- 一份有效合同对应一个 run，successor run 不复用旧分数。
- 全局停止依据持久化状态，而不是单个 worker 的意见或 lease 到期。

当前框架没有逐 iteration Global Plan、AtomicPlan admission、Search Space schema、
共享 chain of thought、peer-history feed，也没有 runtime-owned agent scheduler。
benchmark 专用任务、evaluator、campaign 和对比结果属于 `bench-goal-plus`；
本仓只维护可复用运行时。

具体工具见 [API](api.md)。宿主机制见
[Agent Host Adapters](agent-host-adapters.md)、[Codex](codex.md) 和 [Pi](pi.md)。
持久化状态排查见 [Debugging Runtime State](debugging-runtime.md)。
