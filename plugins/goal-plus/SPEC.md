# Goal Plus 系统规格

状态：当前实现合同。本文描述 Goal Plus 必须保持的稳定行为；精确字段以 Pydantic model 和
tool schema 为准。文中的“必须”和“不得”表示不变量，“当前”表示可以通过显式设计继续演进
的实现。改变合同需要同时修改实现、行为测试和相关用户文档。

## 目标

Goal Plus 为长时间 agent 工作提供可恢复、可审计的执行记录。普通任务由 Main Agent 在
Goal Mode 完成；具备明确 metric 和 verifier 的优化任务进入 Search Mode，在隔离 candidate
中探索，并只提升经过冻结 verifier 验证的结果。

系统必须做到：

- 用户目标、执行状态和证据在会话退出后仍可恢复；
- Codex 与 Pi 共用同一套领域规则；
- candidate 可自主探索，但不能改变评价合同或访问 peer 私有工作区；
- 最佳结果绑定精确 artifact，而不是模型自述或当前目录的偶然状态；
- 未启用的能力不占用 worker context，也不暗示不存在的工具或数据。

系统不负责理解 benchmark 的隐藏语义，也不保证公开 verifier 等价于外部官方评测。它也
不是通用 worker supervisor；进程启动、等待、续跑和中断由 host 管理。

## 核心对象

| 对象 | 含义 |
| --- | --- |
| Goal Plus record | 一个完整用户目标及其 revision、实际 dispatch、Search links 和终态 |
| Dispatch attempt | Main 实际发起的一次原生 subagent 派发及其 attempt/generation |
| SearchSpec | Main Agent 准备的可执行评价合同 |
| FrozenSpec | 已校验、固定 source 和 verifier artifact 的不可变合同 |
| Search run | 在一份 FrozenSpec 上执行的一次搜索 |
| SearchPlan | 为 run 一次性分配初始 candidate lanes 的计划 |
| Candidate | 一条长期探索 lane 及其隔离 workspace、iteration 和 best artifact |
| AgentSessionRecord | candidate 与 host-native session 的上下文/来源绑定，不表示进程存活 |
| Evidence | 一次 verifier settlement 及其精确 artifact、分数和状态 |
| ScheduleTask | Adaptive Search 对已结算节点的一次持久化 scheduler 调用 |
| AllocationDecision | Adaptive Search 已校验并可幂等执行的 candidate 替换计划 |
| Promotion | 对已选择 artifact 的独立最终验证与 publication/apply 结果 |

## 架构与所有权

```text
host command
    |
    v
Goal runtime ---- dispatch records / Search links / final audit
    |
    v
Search runtime -- FrozenSpec -- candidate workspaces -- Evidence -- promotion
    ^                                      |
    |                                      v
Codex/Pi host <---- launch / wait / continue / stop ---- candidate worker
```

| 所有者 | 权限与职责 |
| --- | --- |
| Goal runtime | intake、host 授权、triage、dispatch identity、Search 归属、final check 和终态 |
| Search runtime | freeze、workspace、verifier、Evidence、回滚、selection、promotion 和报告 |
| Adaptive Search | settlement 后的 ScheduleTask、reward、AllocationDecision 和派生 candidate |
| Main Agent | 理解目标、设计验收合同、决定 delegation/Search、审查结果和全局收尾 |
| Candidate worker | 在自己的合同和 workspace 内选择方向、修改、验证和提交 handoff |
| Host adapter/supervisor | worker launch、native session、wait、budget、continuation、interrupt 和日志 |
| Evidence annotator | 为已结算 Evidence 生成客观 View；不改变硬分和 selection |

防御性规则按其可信信息来源落到不同的可执行边界：

| 边界 | 执行内容 |
| --- | --- |
| Model/schema | 单字段与跨字段结构不变量 |
| Goal/Search state owner | phase、归属、artifact、workspace、Evidence 与终态转换 |
| Shared tool facade | Codex/Pi 共用的 caller role 和调用前置条件 |
| Host hook/extension/adapter | command transport、session identity 与真实 lifecycle 事实 |
| Candidate contract projection | edit surface、process environment 和已启用能力的动态可见性 |
| Skill/prompt | 只能由模型完成的意图理解、合同设计和探索判断 |
| AGENTS/SPEC/docs | 开发约束与设计解释，不承担运行时拒绝 |

“Runtime enforcement”指以上可执行边界共同形成的防御，不要求所有逻辑进入同一个
`runtime.py`。同一领域规则只在共享 owner 中实现；Host adapter 只处理 host 独有事实，skill
只描述模型判断和正常调用顺序。

## 授权边界

只有以下精确 host command 可以创建、编辑或恢复 Goal Plus：

- Codex：`$goal-plus ...`、`$goal-plus edit ...`、`$goal-plus resume`
- Pi：`/goal-plus ...`、`/goal-plus edit ...`、`/goal-plus resume`
- 独立最终检查：`$goal-plus-with-final-check ...` 或 `/goal-plus-with-final-check ...`

自然语言提及、skill 自动发现、SessionStart、普通 child、candidate worker、直接 MCP 调用和
generic CLI 都不构成授权。Search bootstrap、Main 后续操作、candidate 操作和终态 report
分别使用其可验证的归属依据；run 完成链接后，Main 对该 run 的操作必须与当前 Goal 的持久化
链接一致。拒绝时不得改变领域状态。

## Goal 生命周期

一条 Goal 的正常流程是：

```text
                  /-> goal -----------------\
intake -> triage                            -> final_audit -> [final_check] -> terminal
                  \-> spec_discovery -> search /
```

`active` 表示仍可推进；`needs_user` 只用于确实缺少用户决定；`blocked`、`complete` 和
`abandoned` 是终态。Runtime 的 `next_action` 是当前 revision 的权威下一步，skill 不应自行
维护第二套状态机。

Main Agent 自主拆解普通任务并决定是否使用原生 subagent。Runtime 不要求预建执行计划或
work DAG，只记录实际发生的 dispatch。Main 在调用 host 原生 spawn 前创建 dispatch attempt，
启动成功后绑定 handle；后续 message、result、rework 和终态必须携带当前 attempt identity。
迟到的旧 generation 结果保留为 stale Evidence，但不能覆盖当前 attempt。具有确定 metric、
可执行 verifier、隔离 edit surface，且并行探索有价值的任务适合 Search；合同不完整时先进入
Spec Discovery。

## SearchSpec 与冻结

SearchSpec 合同覆盖：

- `objective`、`metric_name` 和 `metric_direction`；
- `source_path` 与 `edit_surface`；
- `process_verifiers`，以及需要时的 `promotion_verifiers`；
- `budget.max_parallel`；
- `strategy`、`workspace`；
- 需要时才设置的 `candidate_acceptance`、`device_pools` 和 `shared_dir`。

`constraints` 是 runtime 私有控制数据，不会整体投影给 candidate。Main Agent 必须让最初
目标、candidate 验收、ranking 和最终验收使用同一标准；不能先按一套标准搜索，再用未经
冻结的另一套标准选择结果。

Freeze 与 run bootstrap 必须：

1. 保存 draft 时校验 SearchSpec 与显式 host command config；
2. freeze 时固定 clean source identity 和 verifier artifact hash；
3. freeze 时在 origin 执行 process verifier baseline；
4. 创建 run 时确认 source identity 未变化；
5. 首次物化 Git worktree 时执行 workspace relocation gate；
6. 全程校验 metric、退出码、timeout、edit surface 和可选 device pool。

FrozenSpec 创建后不可原地改写。评价合同有误时，使旧 run invalid，冻结新合同并创建
successor run；不同合同的分数不得直接比较。

## Search 生命周期

一次标准 run 按以下顺序执行：

1. `search_freeze_spec`
2. `search_create` 并链接当前 Goal revision
3. 创建一个 SearchPlan，一次性物化初始 candidate lanes
4. Host 为每个 candidate 启动并绑定 native session
5. Candidate 循环修改并调用权威 verifier
6. Host 在预算允许时 continuation 同一 candidate、session 和 workspace
7. 所有 live worker 静止后执行 selection
8. 对选中 artifact 执行 promotion verifier
9. 在用户授权时 apply，否则输出 artifact-only publication
10. Goal 记录 Search result，完成 final audit/可选 final check，进入终态
11. 终态后生成一次 report

`parallel_loops` 使用固定 lanes：只做初始分配，不因低分补建 candidate；后续优先继续同一
native session，必要时才在相同 candidate/workspace 上 redispatch。

`adaptive_search` 保留相同 hard verifier settlement，并在之后异步创建 AnnotationTask 和
ScheduleTask。`runtime.py` 只选择该模式并调用 `AdaptiveSearch` facade；facade 协调 settlement、
barrier、Scheduler 与 Allocation，`scheduler.py` 管理 ScheduleTask 和调用上下文，
`allocation.py` 管理 Decision 与派生 workspace/session，`policies.py` 管理版本化策略及其
context builder。`budget.max_parallel` 是实时活跃 candidate 上限；可选 `max_candidates` 是
累计物化上限。

Scheduler 输出是不可信提案。Model 校验其结构；Allocation/state owner 在持久化或执行前校验
settled source、artifact、candidate 状态、容量、预留 identity 和幂等前态。Prompt 只承担价值与
探索方向等模型判断，不承担这些确定性 gate。Scheduler、annotation 或 allocation 失败可以更新
自身 task 和审计状态，但不得撤销 verifier settlement，也不改变 hard score、selection 或
promotion。具体合同见 [Adaptive Search](docs/adaptive-search.md)。

## Candidate 合同

Candidate 首次启动或 fresh session 恢复时，通过 `search_get_agent_context` 得到顶层目标、
metric 和 `candidate_task`；这些内容共同构成 worker 的完整运行合同，包括：

- 当前 `run_id`、`candidate_id`、objective 和 metric；
- candidate workspace、允许/禁止交付的文件；
- 对 worker 可见的 `candidate_acceptance`；
- candidate 专属 process environment；
- 仅针对当前已启用能力动态生成的 instructions；
- task-local、Git-tracked skill 路径。

Candidate 可以读取 workspace 内所有任务文件并进行本地诊断，但交付修改只能落在
`allowed_files`。冻结 verifier 和 `denied_files` 保持只读。`.tmp/` 是 candidate-local scratch，
不属于交付 artifact。

Candidate 不应获得：

- Main Agent 的私有编排上下文或完整 raw goal；
- `constraints` 整包内容；
- peer 的真实 workspace、transcript 或私有推理；
- hidden evaluation、credential 或任意 host environment variable。

本地命令只提供分析信息。`search_run_verifier(agent_session_id=...)` 是唯一形成正式 Evidence
并参与共享搜索的验证路径。Verifier settlement 后，runtime 以已结算 workspace 状态作为下一
轮起点。

## Evidence、选择与提升

每条 Evidence 必须绑定 candidate、iteration、精确 commit/artifact hash、verifier result 和
metric。Global Evidence 可以共享这些已结算事实及其客观 View，但不共享 peer 私有推理。

Evidence 模式：

- `manual`：worker 主动读取 Global Evidence；
- `auto`：verifier 返回时注入最新共享快照；
- `independent`：只向 worker 暴露自身 Evidence，不暴露 peer Evidence。

Supplemental evaluation 和 shared_dir 都是可选能力。启用时由 runtime 把必要入口动态写入
candidate contract；关闭时不出现相关说明。它们不改变 verifier 硬分、selection 或 promotion
gate。

Runtime 根据 `metric_direction` 从有效 Evidence 中维护 candidate-local best 和 global best。
Selection 必须固定某条已验证 Evidence 的精确 artifact。Promotion 恢复该 artifact，并独立
执行已配置的 promotion verifier；不得用缓存分、当前 workspace 或模型声明代替所需的
promotion evidence。失败的 promotion 不生成可应用结果。

## Host 与资源合同

Codex 和 Pi 共用 Goal/Search 领域规则，但 lifecycle 实现不同：

- Codex 使用 native subagent、hook、wait/followup/interrupt；
- Pi 使用 RPC worker 和 durable host-local pool。

Host 返回的 launch payload 是权威映射输入。Runtime 不轮询 host 进程，不把 host liveness
写进 Search 领域状态。`AgentSessionRecord` 只保存绑定、上下文和 provenance。

设备资源由 host 通过受信任的 `GOAL_PLUS_DEVICE_POOLS` 注册。SearchSpec 只能引用 pool ID；
runtime 为 lane 固定非敏感可见变量和设备，并通过共享 lease/resource lock 防止超售。不能让
SearchSpec 读取任意环境变量。

## 持久化与恢复

目标 workspace 下的 `.gp/` 保存 Goal、FrozenSpec、run、candidate、Evidence、promotion、
host metadata 和最终报告。Adaptive Search 另外持久化 ScheduleTask 与 AllocationDecision。
状态文件是恢复依据，prompt 记忆不是。

恢复时优先使用 `goal_plus_monitor_snapshot`，再根据持久状态执行 `next_action`。底层文件可
补充 monitor 未投影的字段，transcript 用于排查 host 行为；二者都不能取代持久领域状态。
不得手工修改 `.gp/` 来跨过 gate。

## 变更验收

涉及不变量或状态转换的变更，应在实现或评审材料中回答：

1. 哪个持久状态所有者负责这条不变量？
2. 正常前态和非法前态分别是什么？
3. 哪些领域状态必须不变，哪些操作性副作用允许存在？
4. 这是保持现有行为、修复 bug，还是明确删除行为？
5. 哪个行为测试能在不比较 prompt 措辞的情况下证明它？
6. 是否改变 Codex/Pi 共同合同，还是仅改变某个 host lifecycle？

共同规则在共享 facade/runtime 层实现和验证。Host 的真实启动、hook、context、操作性 prompt
或 continuation 合同变化时，同时修改对应 adapter 测试并运行真实 host ST；环境不具备时明确
记录尚未验证的行为。
