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
宿主 launch payload，不表示进程是否存活。共同 session controller 管理 Codex app-server、
Pi RPC 或 ThinkThread Child；真实资源在 host-local scope 中登记。

宿主为当前 main-agent owner 建立独立的 Execution Scope，状态位于
`.gp/host-scopes/`。它只绑定已登记进程、native session、hook 已观察到的 Codex native task target 和
ThinkThread Child：

```text
main native session
  +-- host Execution Scope
        +-- candidate / ordinary / final-check workers
        +-- Evidence Annotation
        +-- ThinkThread candidate and Annotation Children
```

正常 selection 前，host 先停止 candidate 并确认静默；runtime 随后关闭 Annotation admission、
递增 publication generation 并撤销后台消费者。Main 的 abort/shutdown 事件
会 fence 新 launch 并关闭宿主资源；GP 不监测进程退出，也不清理进程树。该操作不修改 Goal 或 Search 的 durable 状态，
也不提供 scope 接管、Annotation 重开或 resume 语义。

Main 通过共同 session 工具完成 candidate closeout；hook 继续观察普通 native collaboration，
共同 launcher 不控制当前 TUI turn。Session 资源和 provider mutation 在 scope admission 下
先登记为可发现；无法确认完成的 ThinkThread request 或 Child spawn 保持
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
构造或选择 verifier，并调用 `goal_plus_search_freeze_spec`。Runtime 会：

- 使用 Pydantic 校验 SearchSpec 结构；
- hash-pin verifier artifact；
- 对 Git source 在 origin 执行一次 process-verifier suite，并固定 clean source commit；
- 在 run 的首个 Git worktree 中重复一次相同 suite，证明 relocation 后的路径合同有效；
- 检查退出码、数值 metric、超时、verifier hash 和 source identity。

`copy` 仍保留旧的 disposable-copy ranking preflight。ThinkThread workspace_provider 在 origin 验证后
固定 Root-owned provider snapshot，不重复 Git relocation；受支持的 Pi Profile 从该 snapshot
派生 private Children。Codex 对 TT candidate 路径仍会 fail closed。

Runtime 不理解具体 benchmark 的语义，也不会自动证明本地 verifier 与官方 Judge
等价。公开评分公式、有效性条件、每例资源限制和聚合方式是否完整进入 verifier，仍由
SearchSpec 的创建者负责。隐藏数据只能作为明确声明的近似项，不能被无提示地替换为
另一套权威指标。

SearchSpec 不包含预设软 rubric。这样 Spec Discovery 阶段不会因为 MainAgent 过早猜测
任务维度而固定后续注意力、假设和代码基线。

Annotation 跟随冻结的 Main 宿主身份。补充评价开关、目标 revision/hash、比较基线和
上下文隔离见 [Evidence Annotator](evidence-annotator.md#比较基线与目标上下文)。

冻结后的合同不能原地修改。确认 verifier 合同有误时，应先使当前 run 失效并停止其
worker，再冻结修正后的 spec，创建 successor run。旧分数不能跨合同复用。

## 一次 Search Run

一次正常的 Search run 按以下顺序执行：

1. main agent 提交 SearchSpec；runtime 在 origin 验证 verifier 并冻结 source artifact。
2. `goal_plus_search_create` 确认 source identity 未变化，一次初始 `SearchPlan` 分配固定 lane。
3. Git workspace_provider 先物化 `c001` 并执行 relocation gate；通过后才物化其余 candidate。
4. 每个 candidate 获得独立工作区，并按 frozen `strategy.inner_agent` 启动
   `autoresearch` 或 `common` worker。
5. Main 使用共同 session wait 读取准确调用结果；较慢 lane 不阻塞其他 lane 发布 Evidence。
6. Main 审查 Evidence 和用户上限后决定是否显式 wake 同一个 candidate、原生 session 和工作区。
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

`strategy.orchestration_mode="adaptive_search"` 在 hard settlement 主流程上增加 LLM Verifier 评估和
确定性资源分配。它要求 `workspace.provider="git_worktree"` 和非空的 `strategy.allocation`，并自动
启用 LLM-Verifier。非 Adaptive 模式则由 `selection.ranking_keys` 是否包含 `quality_score` 决定是否
启用。`budget.max_parallel` 是实时活跃 lane
上限，可选的 `budget.max_candidates` 是累计物化上限。

每个精确 settlement 幂等创建一个 `VerifierTask`。一次结构化 LLM 调用同时生成 `quality_score`
以及 policy 版本定义的 `progress_reward` 和 `allocation_utility`；allocation 指标统一为
`[0,1]`、越高越优的 normalized utility。Task 使用
`pending -> running -> retry_wait/completed/terminal_error` 的可靠队列状态；lease 过期可恢复，输出
record 一旦写入就不可变。Evidence Annotation 是独立弱依赖，不参与 Verifier task 身份和 Allocation
前态。

Adaptive reconcile 只读取已经提交的权威事实，构建不可变 `SearchSnapshot`：

```text
settled nodes + VerifierRecord + FrozenSpec + decision history
  -> SearchSnapshot
  -> deterministic FeatureView
  -> priority_replace/v1
  -> AllocationPlan
  -> durable AllocationDecision
```

只有已经完成并持久化的 VerifierRecord 才形成 Snapshot 评分行，缺失值不会折算为零。FeatureProvider
只派生数值事实；当前 `priority_replace/v1` 独立拥有 feature 组合和 `switch_margin` 语义。
Snapshot、FeatureView 和 AllocationPlan 都带内容哈希；Application 会从冻结配置重算 plan 后再记录
Decision。Plan 固定精确 source node、退休目标、稳定 direction key 和理由，但不预留 candidate id、
workspace 或 session。

`goal_plus_search_apply_allocation_decision` 在 run lock 内再次校验 snapshot revision、source
candidate/iteration/node/commit/artifact、retire eligibility、capacity 和 budget，然后才分配新
candidate id、退休 lane、物化 workspace 并创建 `AgentSessionRecord`。中断后的重复 apply 复用持久
Decision 和已完成的子步骤，不重新调用 LLM 或重新计算 policy。Host 只按返回的 launch payload
启动派生 worker，不解释质量分值或改写计划。

Verifier task 只进入 run-scoped 可靠队列，并由单 drainer 顺序消费；未完成任务不参与 candidate
admission，也不回写 candidate Evidence。只有 AllocationPolicy 基于 VerifierRecord 生成 pending
retirement Decision 后，admission 才禁止对应 lane 的新 wake。

最终 Selection 与 Adaptive allocation 相互独立。Selection 在 hard-settled options 上按冻结的
`ranking_keys` 和 `ranking_tolerance` 做逐级确定性过滤，且每个 score 必须绑定同一精确 subject。
Allocation score 或 Decision 失败不改变 hard score、keep/retain/discard/failure、`best.json`、
selection 或 promotion。隐藏答案 benchmark 不能向 Verifier/worker 暴露 gold correctness、score
或其派生决策。

## Candidate 循环

Outer workspace 与 inner-agent 行为是两个独立选择。Git outer provider 使用 worktree 和
commit Evidence，ThinkThread 使用 private Child 和 snapshot Evidence；两者都支持
`strategy.inner_agent="autoresearch"` 和 `strategy.inner_agent="common"`。兼容 ID
`autoresearch` 明确解析为内置 AutoResearch-Git：它自主执行多轮，并维护 candidate-private Git
attempt history 与物理 `results.tsv`；TT 中该 repository 位于 Child 内。`common` 每次 dispatch
只完成一个有界 attempt，不维护 inner-agent Git history 或 `results.tsv`。两者都必须调用同一 Goal
Plus MCP，并由 runtime 在 `goal_plus_search_run_verifier` 边界形成 authoritative outer artifact。

每条 candidate lane 使用以下协议：

1. 首次启动或 fresh session 恢复时调用 `goal_plus_search_get_agent_context`，读取自己的权威状态和
   `candidate_task` contract；同一 session continuation 沿用已加载上下文。
2. 按冻结模式使用 Evidence：`manual` 在首次修改前读取并周期刷新 Global Evidence；`auto`
   使用 verifier 返回时自动注入的共享快照；`independent` 不向 worker 注入 peer Evidence。
3. 独立选择方向，交付修改限于自己的 `allowed_files`；`.tmp/` 可用于 candidate-local 临时状态。
4. 启用 `shared_dir` 时回顾本轮及此前 iteration 的命令序列、临时代码片段和 scratch
   scripts。命中正向工具化信号时，从 `.tmp/tool-drafts/` 显式选择源文件并调用
   `goal_plus_search_stage_shared_tool`；否则记录具体排除项。
5. 调用 `goal_plus_search_run_verifier`，用一句 `hypothesis` 描述实际完成的尝试，并在启用
   `shared_dir` 时传入 `toolization_decision`。
6. 本地 Codex/Pi 收到 `ScoreReport`，或在 pending 后通过 `goal_plus_search_wait_verifier`
   等待准确结果；等待期间保持工作区不变。ThinkThread 先收到 accepted receipt，并在同一 retained
   Child 的下一次 wake 中收到 authoritative feedback。收到 feedback 后，`autoresearch` 从已结算
   工作区继续下一轮；`common` 返回 handoff，由 main 决定是否 continuation 同一
   candidate/session。

工作区内的文件都对 worker 可读，可用于导入、profile 和本地诊断；交付修改限于
`candidate_task.allowed_files`，`denied_files` 与冻结 verifier 资产保持只读。Runtime 提供的
`.tmp/` 是 edit surface 之外的 candidate-local 可写临时区，用于 scratch、恢复 handoff，以及
启用 `shared_dir` 时的工具草稿。本地命令输出只是分析材料，只有 `goal_plus_search_run_verifier` 返回的
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
和有界 handoff。`adaptive_search` 只按 runtime 已持久化的 AllocationDecision 退休 candidate；
worker、Verifier 和 host 都不能自行推断质量剪枝。

## Global Evidence

`goal_plus_search_get_global_evidence` 将当前 run 中已结算的 worker iteration 投影为窄表：

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
durable shared plane，不会随 cleanup 删除。面向 main 的 monitor 通过 `host_cleanup` 投影 session、
annotation、snapshot 和 final artifact 状态，不暴露 session-private 物理路径。原始 host 状态
用于 lifecycle 调试；`host-pools/thinkthread-annotations` 仍是 Annotation 专属存储，不是候选 pool。

冻结 spec 显式启用 `shared_dir` 时，已发布工具还会以 `shared_tools` 出现在同一条
Global Evidence 中。每项包含 runtime 绑定的 `tool_view`、`tool_id`、`snapshot_hash` 与
`source_commit`。候选不会看到共享目录路径，也不能在 Tool View 生成前发现工具；它只能通过
`goal_plus_search_copy_shared_tool` 按精确 id/hash 复制到本地临时 inbox，阅读 manifest 与源码后自行决定
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

完整评价不在窄视图中重复返回。worker 仅在 `goal_plus_search_get_agent_context` 的
`supplemental_evaluation_enabled=true` 且目标行
`supplemental_available=true` 时按需调用：

```text
goal_plus_search_get_evidence_detail(
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

View 是精确 settled Evidence 的异步客观描述；可选 supplemental evaluation 提供有证据的
观察维度与非定向 peer 比较，不产生总分或推荐。`view=null` 不影响 Evidence 有效性，candidate
无需等待。只有显式 required supplemental evaluation 会阻止尚未满足要求的 selection。

输入、完整 Artifact View、bounded diff、比较基线、读取审计与 queue/retry/publication fence
见 [Evidence Annotator](evidence-annotator.md)。Shared Plane 只共享已发布结果，不暴露内部
推理、peer 活动工作区或 Annotation task 的执行状态。

## Workspace 与不可变产物

Git 保留完整 candidate-private attempt/restore 历史，ThinkThread 以精确 snapshot 作为公共
Evidence identity。`keep`/`retain` 推进候选工作基线，`discard`/`failure` 恢复此前最佳；
canonical `.gp` ledger 保留全部尝试，`autoresearch` 额外维护 `results.tsv` 投影。

Source clean gate、relocation、Git 字段、容量估算、task-local Skill、reference 和 filtered
publication 的完整合同见 [Workspace 架构](workspace-sandbox-arch.md)。宿主启动和准确
session 归属见 [Agent Host Adapters](agent-host-adapters.md)。

## 选择、Promotion 与失效

run-wide best 从有效 iteration record 的硬 score 中计算，与各 candidate-local best 分开。
`goal_plus_search_select` 不读取补充评价或 peer 比较来改变排名；硬分并列按原有稳定顺序选择。它将
结果绑定到一个精确、通过验证的 worker Evidence commit。只有旧状态
或当前产物没有对应 durable Evidence 时，parent 才补做 process verifier。

promotion 是独立的验收 gate。Runtime 会检出选中的不可变 revision，在
`GOAL_PLUS_VERIFIER_PHASE=promotion` 下重跑配置的 promotion verifier，并把结果绑定
到 Git head 和 artifact hash。只有 promotion 成功，才生成可被 Git 应用的 patch；
source workspace 不会被静默修改。

worker 报告的 verifier concern 只是建议。main agent 确认缺陷后，Runtime 先原子
invalidate 并 fence 当前 run，宿主再停止所有 worker。修复后的合同必须进入 successor
run。可以继承有界研究上下文，但不能继承旧分数或把旧 Evidence 当作新合同下的结果。

## 持久化事实

完整目录和排障入口见 [Runtime 调试](debugging-runtime.md)。

`candidate.json` 中的 iteration record 是 Evidence 的事实来源。Annotation task 保存
可选 View 及其执行状态。Global Evidence 在读取时即时投影，不是第二份可写共享账本。
`best.json` 是当前 run 最优 verifier commit 的权威原子指针；严格改善或同分时更新，
退化和失败不会覆盖它。同分 candidate 按最后完成结算的有效 commit 决定。

## 核心不变量

- 并行工作开始前，先冻结 verifier 与编辑策略。
- 隔离可写 candidate 工作区，只共享持久化事实和 Git object。
- verifier-backed 严格改善或同分最新版本才能成为 candidate-local best。
- 回滚、选择和 promotion 后，所有精确 attempt 仍可审计。
- 客观 View 和补充评价不改变硬结算；显式 required supplemental 与 Adaptive confirmation 各有自己的 gate。
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
