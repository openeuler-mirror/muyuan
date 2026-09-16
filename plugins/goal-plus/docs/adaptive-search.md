# Adaptive Search

Goal Plus 在同一份 [LLM Verifier](llm-verifier.md) 局部评估记录上支持两种搜索模式：

- **极简模式**：`orchestration_mode: parallel_loops`。固定预算执行候选搜索，最终由确定性
  Selection 选择 winner。
- **Adaptive 模式**：`orchestration_mode: adaptive_search`。同一次 LLM 调用额外生成冻结的
  `scores`，确定性 AllocationPolicy 消费其中所需指标，退休和派生 candidate。

两种模式共享 `quality_score` 和最终 Selection。Adaptive 只增加资源分配，不改变质量分数语义、
hard gate 或 promotion 原则。旧 Search Scheduler、`ScheduleTask`、`allocation_flag`、二次确认及其兼容 fallback 均不属于当前方案。

```text
SettlementReceipt
  -> one VerifierTask -> one LLM call -> one VerifierRecord
  -> deterministic Selection
  -> optional SearchSnapshot -> AllocationPolicy -> Policy Algorithms
  -> optional AllocationDecision -> AllocationExecutor
```

## SearchSpec

### 极简模式

```yaml
strategy:
  orchestration_mode: parallel_loops
  selection:
    ranking_keys: [quality_score, hard_score]
    ranking_tolerance: [0.05, 0.0]
```

极简模式不配置 `allocation`，但可通过 `strategy.verifier.scores` 独立启用额外评分。
未选择额外指标时 `VerifierRecord.scores=null`。它不启用动态 retirement、derive、progress trend、
AllocationPolicy 或 AllocationExecutor。

`selection.ranking_keys` 包含 `quality_score` 时，runtime 自动启用 LLM-Verifier；只配置
`hard_score` 且未配置 verifier 时不会创建 VerifierTask。`verifier: {}` 可显式启用基础质量评分。

### Adaptive 模式

```yaml
workspace:
  provider: git_worktree
strategy:
  orchestration_mode: adaptive_search
  verifier:
    scores: [progress_reward, allocation_utility]
  selection:
    ranking_keys: [quality_score, hard_score]
    ranking_tolerance: [0.05, 0.0]
  allocation:
    policy: swe_search_uct/v1
    parameters:
      exploration_weight: 0.01
      early_depth_bonus_weight: 2.0
      early_depth_bonus_decay: 0.8
      late_depth_penalty_weight: 0.008
      low_reward_threshold: 0.0
      low_reward_streak: 2
      max_children_per_node: 3
      max_depth: 20
```

Adaptive 模式必须分别配置 `allocation` 和 `verifier`，当前实现要求 Git worktree backend。
`allocation.policy` 确定资源分配算法，`verifier.scores` 独立选择评估指标 key。
`allocation.parameters` 的字段和校验也由该 policy 版本独立定义，不存在跨 policy 固定的参数名。

Freeze 固化 Selection key/tolerance/direction 以及具体 AllocationPolicy spec。运行中的 run 不接受
这些配置变化。相同 FrozenSpec、权威输入和 policy 版本
必须回放出相同的 Snapshot、Selection trace、plan hash 和 Decision identity。

## 实现位置、版本策略与接口

Adaptive Search 只消费终态 `VerifierRecord`，不拥有模型评分规则。评分 key 拥有唯一语义，Policy
通过 `required_score_keys` 对齐输入要求：

| owner | 位置 | 输入与输出 |
| --- | --- | --- |
| Verifier score contract | [`src/goal_plus/domain/verifier.py`](../src/goal_plus/domain/verifier.py) | 注册指标 key；`verifier_score_contract(score_keys)` 决定字段、范围、提示词和 contract hash |
| Policy declaration and dispatch | [`src/goal_plus/domain/adaptive/policy.py`](../src/goal_plus/domain/adaptive/policy.py) | Pydantic `AllocationPolicySpec` discriminator 集合、版本专有参数及 `evaluate_allocation_policy(snapshot, policy) -> AllocationPlan` |
| Policy / numeric algorithms | [`src/goal_plus/domain/adaptive/`](../src/goal_plus/domain/adaptive/) 与 [`policy_algorithms/`](../src/goal_plus/domain/adaptive/policy_algorithms/) | Snapshot 到 Plan 的纯确定性计算；不访问 I/O、候选或宿主 |
| Snapshot and decision application | [`src/goal_plus/application/adaptive_snapshot.py`](../src/goal_plus/application/adaptive_snapshot.py)、[`allocation.py`](../src/goal_plus/application/allocation.py)、[`allocation_apply.py`](../src/goal_plus/application/allocation_apply.py) | Record 追加、Plan/Decision 持久化、Decision 执行与恢复 |
| Public orchestration | [`src/goal_plus/runtime.py`](../src/goal_plus/runtime.py) | `accept_verifier_record()`、`recover_allocation()`、`apply_allocation_decision()` |

`FrozenSpec.strategy.verifier` 固化指标 key 列表，`strategy.allocation` 固化 policy identity 和参数。
Settlement/Verifier 只读取前者并将完整规则冻结到 task；Record 用同样的 key 输出 `scores`，
并用 `score_contract_hash` 绑定冻结规则。新增评分与是否启用 allocation 无关。

`SweSearchUctPolicySpec.required_score_keys` 声明 `progress_reward` 与
`allocation_utility`。配置校验确认已选择这些指标；Snapshot 入口核对成功 Record 的评分 key，
仅将这些指标原样投影到节点的 `allocation_scores`。其他指标仍保存在 Record，供其他消费者使用。
缺失必需 key 时拒绝该输入。Record 不携带 policy identity 或评分版本；
Snapshot/Plan/Decision 的 policy identity 来自冻结的 allocation spec。

新增策略时在 `policy.py` 声明输入指标 key、参数与分派，再实现纯确定性 evaluator；评分含义
变化需注册不同语义的新 key，决策语义变化升级 policy 版本。注册步骤见
[新增评分或策略](llm-verifier.md#新增评分或策略)。Verifier 不 import Adaptive internals，
Adaptive 不 import rubric 或调用模型。

可信 host 的接线只依赖 runtime 的公开接口：VerifierQueue 终态 consumer 调用
`accept_verifier_record(record, execute=...)`；host 唤醒时独立调用 `recover_allocation(run_id, ...)`。
不会由 Snapshot、Policy 或 Executor 反向驱动队列。

## Deterministic Selection

### 极简模式流程

```text
start fixed candidates
  -> candidate iterations and hard settlements
  -> enqueue Verifier tasks asynchronously
  -> continue under original candidate budget
  -> run stop condition
  -> wait for required ranking tasks to reach terminal state
  -> resolve exact candidate pool
  -> deterministic tolerance Selection
  -> exact artifact/evidence validation
  -> promotion
```

Selection 复用 hard-settled options。Application 从 `FrozenSpec + SettlementReceipt` 生成完整
`VerifierSubject` 摘要，并把 candidate、iteration、settlement、artifact hash 和
`verifier_subject_hash` 组成 `SelectionSubjectKey`。Domain 只比较该身份键，不解释 settlement。

启用 `quality_score` 时，ScoreSource 只接受完整权威链：

1. option 的 `verifier_subject_hash` 精确匹配 task subject；
2. task 已 `completed`，且 `task.output_record_id == record.record_id`；
3. output id 只命中一条 record；
4. record 的 `task_id` 和完整 subject 均与 task 一致。

ScoreSource 的运行期状态为：

```text
pending | unavailable | value(finite number)
```

- 任一必要 source 为 `pending` 时，收尾进入 readiness wait；
- source 已终态但缺失、非有限或 identity 不匹配时，排除该精确 option；
- 必要 task 为 `abandoned` 时，对应 source 不可用；
- 过滤后候选池为空时进入 `selection_blocked`，不改变 FrozenSpec 降级。

### 逐级容差

`ranking_keys` 与 `ranking_tolerance` 必须非空、等长，key 不重复，容差为有限非负数。算法按
配置顺序执行：

```text
survivors = candidate_pool - rejected_subjects
for key, tolerance in configured_order:
  best = directional_best(survivors, key)
  survivors = values within inclusive tolerance of best
winner = prefer previous hard best, then preserve original option order
```

`quality_score` 固定 maximize；`hard_score` 使用 SearchSpec 的 `metric_direction`。Selection 不调用
LLM，只保存 winner、每级 threshold、survivor count、排除原因和 evidence source，不伪造完整全局
排名，也不覆盖 `best_*` 的观测语义。

Winner 恢复后仍校验 exact artifact 与 hard evidence。必要时由 parent hard verifier 重验；失败则
排除该 subject，并对剩余候选重放同一算法。

## Deterministic Allocation

串联入口为 `FileSearchRuntime.accept_verifier_record(record, execute=...)`，它委托
`AdaptiveSearch.accept_verifier_record()`。可信 host 把 runtime 入口绑定为 Verifier 队列的同步
终态 consumer；查询、monitor/report 和 candidate admission 不触发决策。

```text
settlement: 单次事实、身份、diff -> VerifierTask
LLM-Verifier: FIFO 推理与内部重试 -> 完整得分或最终失败 VerifierRecord
AdaptiveSearch: append Snapshot -> AllocationPolicy -> Plan / no-op
AllocationExecutor: 持久化 Decision -> run.lock -> 淘汰 -> 按序派生及派发
```

### Snapshot 与评分边界

Snapshot 只从已有 Snapshot 和当前终态 VerifierRecord 追加，不扫描 candidate、allocation
执行结果或历史工作区。record 携带指标 key、queue sequence、父节点与初始 artifact 身份。
相同 record 返回原 Snapshot；跳号拒绝。历史得分不被重算或覆盖。

失败终态也追加到 Snapshot，缺失分数保持缺失并切断未知链路。后续完整得分可建立独立评分根，
不能越过缺口连接旧节点。回滚后的 continuation 根据精确恢复节点连接，不连接被丢弃的 attempt。
完整得分在基线未知时仍追加为独立根，不丢分；晚到的历史提交不回退当前端点。
Snapshot 不包含 eligibility 或候选总数，也不从派生评分到达推断淘汰是否成功。

### AllocationPolicy 与算法库

AllocationPolicy 是唯一解释 Snapshot 领域事实并生成 AllocationPlan 的无 I/O owner。Policy 固定
调用的 Policy Algorithm 只处理数值、序列和父索引，不理解 candidate、retirement、capacity 或
artifact。Application 不配置、解析或直接调用算法。

当前唯一交付的 `swe_search_uct/v1` 从 Snapshot 临时重建树，将每个 node 的
`allocation_utility` 离线回传为子树
访问数、utility 总和与均值，然后按下式选择可扩展 source：

```text
UCT = mean_allocation_utility
    + C * sqrt(ln(parent_visits) / node_visits)
    + alpha * exp(-beta * (depth - 1))
    - gamma * sqrt(depth)
```

`progress_reward` 使用 `[-1,1]`，只用于根到 node 路径的尾部低 reward 止损；达到 patience 后的
路径即使 utility 很高也不能成为 source。Policy 同时执行 `max_depth`、每 node child 上限、稳定
tie-break 和 Goal Plus 并行 lane retirement。UCT view、visits、均值、排名和 abandoned 状态都只在
一次 evaluation 内存在，从不持久化。

### Plan、Decision 与执行

Plan 包含按 Policy 优先级排列的淘汰备选和派生备选，以及每个派生备选允许替换的候选名单。
Executor 最多选中一个淘汰目标、成功派生一个候选。Algorithm 只参与数值计算；
Policy 是唯一决策者。计算失败记录错误并拒绝决策，不回到 LLM 重新评分。

本版本使用 Snapshot schema 6、Plan/Decision schema 5，不隐式升级旧运行记录。
Decision 按 plan hash 幂等持久化，保留每个 expansion 的完整 source、稳定 expansion id、
预留 child id、`selected_retirement`、Verifier sequence 与执行结果。Executor 不重算策略、不重验 Snapshot 或得分，只核对执行授权、
候选当前身份和真实容量。

Executor 的一次执行在同一个 `run.lock` 内完成：

1. 按 Verifier sequence 恢复未完成 Decision；前项结果未知时不执行后项。按排名跳过已淘汰目标，
   检查可用派生选项及真实容量，再持久化唯一 `selected_retirement`。选定后不改换淘汰目标。
2. 宿主按 candidate/session/generation/call 的精确身份执行中断，保存原调用回执。
   确认拒绝则 `terminal_error`；未知中断保持 `interrupted`，不能假定已淘汰。
3. 淘汰成功后，按 Plan 顺序尝试派生；其他候选已占用的 source 跳过。
4. 在物化前持久化 child id 和完整 ExpansionSource；同 Decision 恢复复用原身份。
5. 物化失败或宿主确认未提交调用时，将 partial child 退役后尝试下一节点。未知派发在同锁内
   按原身份再观察一次；仍未知则保留 `dispatching` 和 `interrupted`，不启动备用 child。
6. 成功派发后写 `applied`；全部选项失败写 `terminal_error`，保留已发生的淘汰和失败事实。

宿主调用原生 session 控制接口负责真实中断与派发，Main 无需再次启动返回的 session。
正常执行及即时重试始终持有 run.lock；宿主不可用或进程中断时只能依赖持久 Decision 保留
执行顺序，恢复重新取得锁。host wake/start 调用 Executor 自身的 recovery 入口，显式 apply
也会先恢复较早的未完成 Decision，不重新推理或计算 Plan。已终态 Decision 不再次执行。
Direct native host 丢失 controller 后不能仅凭 run request 意图证明派发结果；这种冷恢复保持
`interrupted` 并明确要求宿主恢复，不猜测派发成功、不创建替代身份。ThinkThread 使用其准确调用恢复接口。
旧 prepare/ack 接口、reconciliation journal、reconciler 和独立调度锁已移除。

## Queue 与恢复

Verifier 队列归可信 candidate host 所有，每个 run 一个消费者。任务重试和不完整得分保持
队首所有权；只有完整评估或最终失败才写不可变 Record。终态同步交付 AdaptiveSearch，
确定性决策结束并完成持久交接后即可确认 delivered，再领取下一项。
Snapshot/Decision 持久化失败只重放 Record，不重新推理；Decision 已存在则不重算 Policy。
执行失败不回传给 Verifier，Executor 独立恢复原 Decision。

Settlement 将不可变单次提交输入与 iteration 一起保存，正常提交只入队当前一项；队列首次
启动或显式恢复时重放已提交输入，补齐入队失败的任务，
不重新从历史构造评估上下文。队列内部存储互斥只保护入队/claim，单消费者保护完整交付；
Snapshot 和确定性计算没有独立锁。Executor 独占 run 执行锁，不把锁传回评估器。

Verifier backlog 不阻塞 candidate 自主迭代。Executor 已选定的未完成淘汰目标由 admission
拒绝新执行；非目标候选正常继续。关闭 host/run 会停止对应队列与推理。

## 模块所有权与验证

- Settlement：当前迭代事实与不可变提交，不评估历史。
- LLM-Verifier：规则、FIFO、推理、重试与终态评分表；只通过 consumer 接口交付。
- AdaptiveSearch：Snapshot、Policy 调用、持久化 Plan/Decision；无模型调用或 reconciliation。
- Policy Algorithm：纯数值算法，不理解流程、存储和宿主。
- AllocationExecutor：精确身份执行、恢复、备用节点与 run 锁；不新增评估。
- Monitor/report：只读投影，不启动队列或生成决策。

测试覆盖 FIFO 终态交付、评分缺口与回滚、重复 record、确定性排序、备用派生、
部分物化、未知派发和同身份恢复。真实宿主生命周期仍需显式运行对应 ST。

## Monitor Feature Plugin

Monitor feature plugin 基于持久化 Goal record 和只追加 event log 生成只读投影。它属于共享
observability，不参与 Adaptive 算法。启用方式、Orchestration View、host metadata 和确定性
协议对比见 [Monitor Feature Plugin 使用说明](feature-plugins.md)。
