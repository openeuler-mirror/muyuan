# Adaptive Search

Goal Plus 在同一份 [LLM Verifier](llm-verifier.md) 局部评估记录上支持两种搜索模式：

- **极简模式**：`orchestration_mode: parallel_loops`。固定预算执行候选搜索，最终由确定性
  Selection 选择 winner。
- **Adaptive 模式**：`orchestration_mode: adaptive_search`。同一次 LLM 调用额外生成冻结的
  `allocation_scores`，确定性 AllocationPolicy 据此退休和派生 candidate。

两种模式共享 `quality_score` 和最终 Selection。Adaptive 只增加资源分配，不改变质量分数语义、
hard gate 或 promotion 原则。旧 Search Scheduler、`ScheduleTask`、`allocation_flag`、二次确认、
candidate id 预留及其兼容 fallback 均不属于当前方案。

```text
SettlementReceipt
  -> one VerifierTask -> one LLM call -> one VerifierRecord
  -> deterministic Selection
  -> optional SearchSnapshot -> FeatureView -> AllocationPolicy
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

极简模式不配置 `allocation`，`VerifierRecord.allocation_scores` 必须为
`null`。它只解决固定预算下的 Best-of-N 选择，不启用动态 retirement、derive、progress trend、
AllocationPolicy 或 AllocationExecutor。

`selection.ranking_keys` 包含 `quality_score` 时，runtime 自动启用 LLM-Verifier；只配置
`hard_score` 时不会创建 VerifierTask。SearchSpec 不提供独立的 verifier 开关。

### Adaptive 模式

```yaml
workspace:
  provider: git_worktree
strategy:
  orchestration_mode: adaptive_search
  selection:
    ranking_keys: [quality_score, hard_score]
    ranking_tolerance: [0.05, 0.0]
  allocation:
    features:
      - latest_record_values/v1
      - progress_trend/v1
    switch_margin: 0.15
```

Adaptive 模式必须配置 `allocation`，当前实现要求 Git worktree backend，并自动启用 LLM-Verifier。
Runtime 固定 `allocation.policy=priority_replace/v1`，同时确定资源分配算法和
LLM-Verifier 动态追加的 allocation 评分规则；Main 不填写 policy 版本。

Freeze 固化 Selection key/tolerance/direction 以及具体 AllocationPolicy spec。运行中的 run 不接受
这些配置变化。相同 FrozenSpec、权威输入和 policy 版本
必须回放出相同的 Snapshot、Selection trace、plan hash 和 Decision identity。

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
4. record 的 task、allocation policy 和完整 subject 均与 task 一致。

ScoreSource 的运行期状态为：

```text
pending | unavailable | value(finite number)
```

- 任一必要 source 为 `pending` 时，收尾进入 readiness wait；
- source 已终态但缺失、非有限或 identity 不匹配时，排除该精确 option；
- 必要 task 为 `terminal_error` 时，对应 source 不可用；
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

Adaptive 模式复用相同 Verifier 和最终 Selection，只增加以下副作用链：

```text
VerifierRecordCompleted
  -> validate task + record + SettlementReceipt
  -> immutable SearchSnapshot score-table relay
  -> pure FeaturePipeline
  -> deterministic AllocationPolicy
  -> AllocationPlan | no-op
  -> durable AllocationDecision(pending)
  -> logical lane fence
  -> one native cancel per retirement target
  -> accepted receipt
  -> AllocationExecutor apply
  -> retire old candidate + materialize child
  -> Host starts child worker
```

### 唯一 Decision 生产事件

`AdaptiveDecisionReconciliation.on_verifier_record_completed(record)` 是唯一允许进入
Snapshot -> FeatureView -> AllocationPolicy -> AllocationDecision 链路的入口。它先验证：

1. record 的 allocation policy 和 score 字段集合与 FrozenSpec 一致；
2. 对应 task 已 `completed`，且 `output_record_id` 指向该 record；
3. record 已写入 run-scoped VerifierRecord store；
4. candidate iteration 中的 SettlementReceipt 与 record subject 完全一致。

Hard verifier 返回、candidate 启动/续轮/恢复、Decision 查询以及 monitor/report 都不能生成
Decision。外部 API 不暴露 `reconcile()` 或 `snapshot()` 形式的隐式生产入口。

Completed record 是 durable event source。Verifier drainer 优先消费尚无 completed reconciliation
receipt 的 record，恢复时不再次调用 LLM。Reconciliation 先持久化固定 Snapshot revision 与 plan
hash 的 prepared journal，再提交 Decision/no-op 和 completed receipt；prepared 事件始终从同一
Snapshot 重算同一 plan。下游失败不能反向修改已完成的 task 或 record。

### Snapshot 评分行

Snapshot 中每个 node 是一条已完成的不可变评分行：

```text
SnapshotNode
  candidate_id / iteration / settlement_id / node_id
  verifier_record_id / verifier_subject_hash
  quality_score
  allocation_scores
```

Snapshot builder 中转与冻结 policy 匹配的全部 completed records，并附带 candidate lane 元数据。
它不读取 task 状态，不复制 disposition、rollback 或 artifact，也不按 `keep/retain`、failure 类型或
`artifact_iteration_eligible` 过滤。没有 record 就没有评分行，缺失值不会折算为 0。

退休对象只来自当前 eligible lane；派生来源可以来自任意已有评分行，包括旧 iteration、已退休
candidate 和 `discard/failure` attempt。`verifier_subject_hash` 只绑定 provenance，不参与 policy
计算。Snapshot 与 FeatureView 是可重建中转视图，不形成第二份搜索状态树。

### FeaturePipeline 与 AllocationPolicy

FeatureProvider 和 AllocationPolicy 是无 I/O 纯计算，不得读取 store、时钟、环境、workspace 或
host。FeatureProvider 只输出版本化事实，不生成 priority，也不解释 policy 参数。

当前 `priority_replace/v1` 执行以下规则：

1. 每个 active lane 必须至少有一个由已启用 FeatureProvider 产生的可比较值，否则 no-op；
2. 对 active lane 的最新 `allocation_utility` 与最近三次 `progress_reward` 均值再次取算术平均，
   得到 `[0,1]` lane priority；
3. 从 Snapshot 全部评分行中选择 `allocation_utility` 最高的 source attempt；
4. 当 `source utility - weakest active lane priority >= switch_margin` 且 capacity 允许时，产生
   replacement plan。

该规则同样适用于 `max_parallel=1`。`switch_margin` 是 `priority_replace/v1` 的专有参数，范围为
`[0,1]`；Snapshot 和 FeatureProvider 不理解它。缺少 `allocation_utility` 的评分行不能使用
`quality_score` fallback。当前版本不执行价值回传、探索配额或广度/深度奖励；未来 policy 必须
用独立版本化 spec 定义 feature 组合、缺失值和参数语义。

### AllocationPlan 与 Decision

AllocationPlan 只描述逻辑意图：

```text
based_on_snapshot_revision
policy_id / feature_hash / plan_hash
retire_candidate_ids
expansions:
  source candidate/iteration/settlement/node/VerifierRecord identity
  direction_key / rationale
  optional worker_budget
reason_codes
```

Plan 不携带 artifact，不分配 candidate id、workspace 或 session。Application 使用同一 Snapshot 和
冻结 policy 重算结果，再根据 source identity 找回 SettlementReceipt，验证 subject hash，并将
receipt 中的 `attempt_artifact_ref/hash` 与 `ledger_artifact_ref` 固化进
`AllocationDecision.ExpansionSource`。

`AllocationDecisions.record()` 在 run lock 内完成重算、相同 identity 幂等检查和 pending 检查。
相同 plan 返回原 Decision；已有另一 pending Decision 时本次 no-op。因此每个 run 最多一个 pending
Decision 是原子持久化不变量。

### Retirement fence 与同步淘汰

Decision 状态为：

```text
pending -> applied
        -> superseded
        -> terminal_error
```

Pending Decision 一旦指定 `retire_candidate_ids`，admission 立即 fence 目标 lane，禁止 `recover`、
`redispatch`、`start_session`、`continue_session` 和 `run_verifier` 继续进入 candidate 执行。

公共 `goal_plus_search_apply_allocation_decision` 通过可信 MCP session host 对准确目标发起
原生取消。scope 中的取消 intent/receipt 绑定 Decision、candidate、session、generation、native
handle 和 call，不接受模型提供取消事实。准确 controller 的已完成调用可记为 accepted；
尚未启动的 session 无需取消。缺失 controller 不能作为 not_found 成功处理。

- `accepted`：取消已接受或准确调用已完成，不证明资源退出；重复请求复用回执；
- `rejected`：宿主明确拒绝，Decision 保持 pending，允许重试；
- `requested` / `unknown`：尚无准确结果，必须恢复，不能重新发送不确定的取消或直接 apply。
  同一 controller 后续确认原 call 为 completed/interrupted 时，重试 apply 只观察该准确终态并
  保存 accepted 回执；仍在运行、失败或 controller 缺失时保持恢复要求。

收到可接受回执后，AllocationExecutor 在 run lock 内重新校验 Decision、Snapshot、SettlementReceipt、
ExpansionSource、eligibility、capacity 和 budget，然后退休旧 candidate、分配 child id，并从
`ExpansionSource.attempt_artifact_ref` 物化派生 candidate。实际启动 child worker 仍归 host。
派生 session 通过 `goal_plus_session_run` 启动。旧 session 的 close、资源退出和容量检查独立于
取消接受；清理失败保留资源状态，不因 Decision applied 而伪报清理成功。

重复 apply 使用持久 Decision、candidate 和 session provenance 恢复，不重新调用 LLM、不重复发送
已记录的 cancel，也不重新解释 policy。过期输入进入 `superseded`；不可恢复的协议错误进入
`terminal_error`。

### Admission 边界

Verifier queue 不是 candidate admission 输入。Adaptive admission 只检查：

1. candidate 的 `allocation_eligibility` 仍为 `eligible`；
2. 是否存在将其列为 retirement target 的 pending AllocationDecision。

Apply 前由 pending Decision fence 消除续轮竞态；apply 后由持久化的 `retired` 状态继续拒绝执行。
Verifier task 的 pending、running、retry 或 error 状态不参与 admission。

因此 worker 的动态 `candidate_task.instructions` 只要求正常继续 candidate 循环，不轮询 Verifier
task 或 AllocationDecision。只有 runtime 或 host 明确拒绝、fence 或中断当前 lane 时才收尾；
Decision 的发现、物理中止和应用始终属于 Main、host 与 Allocation runtime。

## Queue 与 Host

Verifier task 的 FIFO、lease 和 retry 由 [LLM Verifier](llm-verifier.md#队列与恢复) 拥有。Candidate
只负责 hard settlement 后幂等入队，Verifier backlog 不影响其原始迭代预算。

Codex 与 Pi 的可信 host callback 负责唤醒 Annotation 和 Verifier drainer。对带 allocation policy
的 completed record，drainer 消费 durable event 并恢复 reconciliation；查询、admission、monitor
和 report 保持只读。关闭 run 会关闭内部 drainer，失败不会撤销 hard settlement 或 record。

Host/session owner 只负责执行一次 cancel、返回 receipt 和启动 child。它不能推断 candidate 价值、
绕过 admission 或重新计算 policy。

## 故障与恢复

- 没有 completed record 的 task 不进入 Snapshot，也不会被折算成 0；
- 只有 `VerifierRecordCompleted` 可以驱动 Decision 生产；
- reconciliation 失败不改变 task 或 record；
- rollback 不删除 `discard/failure` attempt 或其评分；
- Snapshot 后若 receipt、attempt ref/hash 或 subject identity 被替换，Allocation 必须拒绝解析；
- cancel receipt 丢失时按同一持久 identity 查询或重放；
- apply 中断后从 Decision 和 materialization provenance 幂等恢复；
- Selection 的必要分值永久缺失时排除精确 option，候选池为空时显式 blocked；
- winner 的 hard evidence 或 artifact identity 重验失败时，排除该 subject 后确定性重选。

## 模块所有权

| 组件 | 拥有的职责 | 明确禁止 |
| --- | --- | --- |
| Selection application | 候选池、readiness、逐级容差、trace 和重验 | 调用 LLM 或修改 VerifierRecord |
| Snapshot builder | 中转 completed score rows 和 lane 元数据 | 解释 settlement、过滤 disposition 或填造缺失值 |
| FeatureProvider / AllocationPolicy | 对数值评分表执行纯计算 | 读取 artifact/state/host 或执行副作用 |
| Adaptive reconciliation | 消费 completed record，校验身份并编排 Snapshot、policy 和 Decision record | 补建 task、从查询生成 Decision 或执行 Decision |
| Allocation owner | 重算 plan，绑定 exact attempt，持久化并幂等 apply | 接受 LLM 直接生成的 Decision 或从 rollback artifact 派生 |
| Adaptive facade | Decision 查询和显式 apply | 暴露 Verifier task/record、隐式 reconcile、构建 Snapshot 或监督 worker |
| Host/session owner | cancel、receipt 与 child launch | 推断价值或绕过 admission |
| Monitor/report | 只读投影 task、record、trace 和 Decision | 恢复或驱动业务状态 |

## 验证要求

Adaptive 与 Selection 的核心测试应覆盖：

- pending/running/retry Verifier task 不阻塞 candidate execution；
- pending retirement Decision 在 apply 前 fence 全部目标操作；
- 每个目标只 cancel 一次，接受型 receipt 不等待物理退出；
- Snapshot 中转全部 completed score rows，不读取 task 状态或过滤 disposition；
- 未结算 iteration、未完成 task、未持久化 record 或 policy 不匹配不能触发 Decision；
- Decision 查询和 admission 是只读路径；
- competing plans 在 run lock 内最多产生一个 pending Decision；
- retired candidate、旧 iteration 和 failure/discard attempt 可作为 allocation source；
- score row 通过 subject hash 绑定同一 SettlementReceipt，身份重绑定被拒绝；
- Decision 与 workspace materialization 使用 attempt artifact，不使用 rollback 或 ledger commit；
- 相同 Snapshot、features 和 policy 产生相同 plan/Decision identity；
- Selection 覆盖 readiness、容差边界、完整身份链和 winner 重验失败后的重选。

## Monitor Feature Plugin

Monitor feature plugin 基于持久化 Goal record 和只追加 event log 生成只读投影。它属于共享
observability，不参与 Adaptive 算法。启用方式、Orchestration View、host metadata 和确定性
协议对比见 [Monitor Feature Plugin 使用说明](feature-plugins.md)。
