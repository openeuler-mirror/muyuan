# LLM Verifier

本文定义 Goal Plus 当前 LLM Verifier 的正式协议。LLM Verifier 对一个已经完成硬结算的
subject 做一次局部、结构化评估，并产生一个不可变 `VerifierRecord`。它不负责选择 winner、
分配搜索资源或修改 candidate 状态。

固定并发的 `parallel_loops` 与动态分配的 `adaptive_search` 共用本协议；两种模式的配置、
Selection 和确定性 Allocation 见 [Adaptive Search](adaptive-search.md)。旧 Search Scheduler、
`ScheduleTask`、`allocation_flag`、二次确认、candidate id 预留及其兼容 fallback 均不属于当前协议。

## 职责边界

LLM Verifier 的输入和输出链路为：

```text
candidate iteration
  -> hard verifier
  -> exact SettlementReceipt
       attempt artifact + hard evidence + disposition
       rollback receipt when disposition is discard/failure
  -> idempotent VerifierTask admission
  -> run-scoped FIFO drainer
  -> one structured LLM call
  -> immutable VerifierRecord
       quality_score
       allocation_scores | null
  -> VerifierRecordCompleted
  -> deterministic consumers
       final Selection
       optional Adaptive allocation
```

LLM Verifier 只拥有局部评估。它不能：

- 修改 hard evidence、settlement 或 candidate eligibility；
- 停止搜索、退休 candidate、创建或应用 `AllocationDecision`；
- 选择 winner 或绕过 promotion gate；
- 从自由文本、candidate 自建文件或 peer workspace 猜测权威分数；
- 将 `hard_score`、`quality_score` 和 allocation 指标压缩成隐式总分。

## 核心不变量

### 一次调用和一个完整记录

一个已结算 subject 对应一个 `VerifierTask`、一次结构化 LLM 调用和一个不可变
`VerifierRecord`。`quality_score` 与可选的 `allocation_scores` 在同一次调用中产生；Adaptive
模式不会为 allocation 指标启动第二个 task。

两种模式使用同一个质量 rubric 和完全相同的 `quality_score` 语义。`quality_score` 是供
Selection 使用的局部排序信号，不是 winner 或执行意图。

### 局部评估与全局决策分离

`VerifierRecord` 只描述一个精确 settlement。LLM 输入不包含其他 candidate 的 VerifierRecord，
避免局部分数随 peer 完成顺序变化。全局 capacity、peer、历史、切换成本和 winner 只由确定性
Selection 或 AllocationPolicy 解释。

### Attempt 与回滚身份分离

Settlement 同时保存两类不同事实：

- `attempt_artifact_ref/hash` 是 hard verifier 和 LLM Verifier 实际评估的修改；
- `post_settlement_artifact_ref` 与 `rollback` 是结算后 workspace 的恢复结果。

`keep/retain` 不带 rollback；`discard/failure` 必须带完整 rollback 回执。回滚不会改变被评估
attempt 的身份，也不会删除其 Verifier 价值。

```text
base A
  -> attempt B
       hard verifier -> discard/failure
       SettlementReceipt.attempt = B
  -> rollback result R
  -> ledger projection L

LLM Verifier subject = B + hard evidence + rollback(A -> R)
```

对四类 disposition，评分对象始终是 exact attempt B。后续 Adaptive allocation 若选择该评分行，
派生来源仍是 B，而不是 R 或 ledger commit。

### Hard gate 与排序信号分离

Hard evidence 同时包含不可覆盖 gate 与排序信号：

- artifact identity、edit surface、validity、process、anti-cheat 和 promotion 是不可覆盖 gate；
- `hard_score` 及公开 metric 是排序信号。

`hard_score=0` 和负数仍是有效数值。LLM Verifier 可以补充目标匹配度和局部价值，但不能让违反
gate 的 artifact 进入 promotion。

### 共享权威记录

Verifier task 和 record 由 Search run 集中持久化：

```text
.gp/runs/<run_id>/verifier/tasks/<task_id>.json
.gp/runs/<run_id>/verifier/records/<record_id>.json
```

Candidate workspace 只提供被评估 artifact，不拥有评分数据库。结构化推理结果由可信 Verifier
adapter 解析、校验并写入共享状态；candidate worker 和 LLM 都不能直接写权威 record。

### Verifier backlog 不形成 candidate 背压

Candidate 完成 hard settlement 后只负责幂等入队。`pending`、`running`、`retry_wait` 或
`terminal_error` task 不阻塞 candidate 启动、续轮、hard settlement 或恢复，也不回写 candidate
Evidence。最终 Selection 对必要分数的 readiness wait 只发生在搜索收尾阶段，不属于 candidate
backpressure。

Adaptive 模式中，只有已经持久化的 pending retirement Decision 可以 fence 对应 lane。具体规则见
[Retirement fence](adaptive-search.md#retirement-fence-与同步淘汰)。

## 启用与冻结

SearchSpec 不提供独立的 LLM-Verifier 配置或开关。Runtime 根据消费者声明自动启用：

- `selection.ranking_keys` 包含 `quality_score` 时启用；
- `strategy.orchestration_mode="adaptive_search"` 时启用，以生成 policy 所需的 allocation 指标；
- 默认 `parallel_loops` 只按 `hard_score` 排名，不创建 VerifierTask。

LLM-Verifier 内部维护唯一的质量 rubric、输入投影和评分语义，不向 SearchSpec 暴露可选规则集。
Adapter 只把固定输入投影明确列出的字段放进不可信 Evidence 数据区，rubric criteria 和执行协议
则进入高优先级 verifier instructions。当前投影包含 objective、
公开 `candidate_acceptance`、candidate hypothesis、完整 SettlementReceipt、artifact manifest、
bounded attempt diff 和当前 candidate 的有界历史评分，不投影整包 runtime `constraints`。

启用 LLM-Verifier 的 `parallel_loops` 输出必须包含 `allocation_scores: null`。`adaptive_search` 根据冻结的
`AllocationPolicy` 版本动态拼接 allocation 评分提示词与输出 Schema；相同 FrozenSpec、完整
subject 和 policy 必须生成相同 task identity。

- `parallel_loops` 不配置 AllocationPolicy，并由 Selection 是否消费 `quality_score` 决定是否评估；
- `adaptive_search` 必须冻结 AllocationPolicy，并输出该 policy 版本定义的精确字段集合。

完整 SearchSpec 示例见 [Adaptive Search 配置](adaptive-search.md#searchspec)。

## 数据合同

### SettlementReceipt

`SettlementReceipt` 是 settlement owner 对一次 immutable attempt 的权威结算单：

```text
SettlementReceipt
  settlement_id / run_id / candidate_id / iteration
  attempt_base_artifact_ref | null
  attempt_artifact_ref / attempt_artifact_hash
  evidence:
    metric_name / hard_score / process_passed
    changed files / edit-surface facts / failure_class
    verifier metrics / log paths
  disposition: keep | retain | discard | failure
  rollback | null:
    target_artifact_ref
    restored_artifact_ref / restored_artifact_hash
    restored_to_iteration | null
  post_settlement_artifact_ref
  ledger_artifact_ref
  created_at
```

Settlement owner 先完成 disposition、必要 rollback 和 ledger 写入，再持久化完整结算单，最后调用
`ensure_assessment`。因此 `VerifierTask` 不会绑定未完成的结算状态。

### VerifierSubject

```text
VerifierSubject
  frozen_spec_id
  settlement: SettlementReceipt
```

Subject 不复制 commit、artifact 或 hard evidence 字段。Subject identity 是 `frozen_spec_id` 与整张
结算单 canonical payload 的 hash；attempt、rollback 或 evidence 任一字段变化都会改变身份。

Verifier 输入包含 objective、公开 candidate acceptance、candidate hypothesis、结算单、artifact
manifest、`attempt_base_artifact_ref -> attempt_artifact_ref` 的 bounded diff 和当前 candidate
自身的有界历史评分。Peer 历史、FrozenSpec identity 与整包 runtime `constraints` 不进入 LLM
输入。直接浏览 candidate 代码与 Git 历史暂不属于当前协议；未来如需支持，应作为 Verifier 的
受限只读能力扩展。

### VerifierTask

```text
VerifierTask
  schema_version
  task_id / queue_sequence
  subject
  allocation_policy_id | null
  allocation_score_names
  state
  attempts / max_attempts / next_attempt_at
  lease
  output_record_id
  invocation / usage / error
  created_at / updated_at
```

Task identity 由 run、settlement、allocation policy 和冻结 score 名称共同确定。
`ensure_assessment` 是幂等操作，相同身份只返回已有 task。

状态转换为：

```text
pending -> running -> completed
                  -> retry_wait -> running
                  -> terminal_error
```

Lease 过期后可以重新 claim。若 record 已写入而 task 尚未提交为 `completed`，重复 completion 必须
复用同一个不可变 record 并修复 task 状态。

### VerifierRecord

```text
VerifierRecord
  schema_version
  record_id / task_id
  subject
  allocation_policy_id | null
  quality_score
  allocation_scores | null
  evidence_refs
  rationale
  invocation / usage
  created_at
```

`quality_score` 是 `[0,1]` 内的有限数。极简模式的 `allocation_scores` 必须为 `null`；Adaptive
模式必须提供对象，其字段集合与 task 冻结的 `allocation_score_names` 完全一致，所有值均为
`[0,1]` 内有限数。

当前 record 不包含 `quality_uncertainty`、`criterion_scores` 或 `VerifierMetricRecord`。Criteria
属于 rubric 内部评估结构，不作为长期数据合同。未来 Intervention 必须使用独立的
`intervention_scores`、规则、schema 和 consumer，不能复用 allocation namespace。

## Allocation 指标

`priority_replace/v1` policy 固定定义两个指标及其 LLM 提示词：

- `progress_reward`：exact attempt 中新增的局部价值，包括粗粒度 hard score 未表达的进展；
- `allocation_utility`：无论 disposition 为何，从 exact attempt 继续搜索的局部价值。

Policy 与冻结遵守以下规则：

- 指标名称匹配 `^[a-z][a-z0-9_]*$` 且在 policy 内不能重复；
- Freeze 通过完整 AllocationPolicy spec 固化 policy 版本；
- 所有 allocation 指标共享 `[0,1]`、越高越优的 normalized utility 合同；
- 动态 JSON Schema 精确列出冻结字段并禁止额外字段；
- 少字段、多字段、非有限值或越界值都会使 completion 失败，不会静默填 0。

Snapshot 原样中转已经校验的分数。FeatureProvider 的派生事实和 AllocationPolicy 的组合规则见
[Deterministic Allocation](adaptive-search.md#deterministic-allocation)。

## 结构化输出

Verifier adapter 根据 task 的冻结字段动态生成严格 JSON Schema。Adaptive 输出示例：

```json
{
  "quality_score": 0.82,
  "allocation_scores": {
    "progress_reward": 0.31,
    "allocation_utility": 0.74
  },
  "evidence_refs": ["diff:src/example.py"],
  "rationale": "The settled artifact improves the target while preserving the acceptance criteria."
}
```

极简模式示例：

```json
{
  "quality_score": 0.82,
  "allocation_scores": null,
  "evidence_refs": ["diff:src/example.py"],
  "rationale": "The settled artifact improves the target while preserving the acceptance criteria."
}
```

可信写入链路为：

1. Adapter 严格按固定输入投影，从 FrozenSpec 公开验收合同、完整
   SettlementReceipt、bounded attempt diff 和当前 candidate 的有界历史构造不可信 Evidence 数据区；
   固定质量 rubric 与执行协议单独组成 verifier instructions，Adaptive policy 规则按需拼接。
2. Structured inference backend 强制模型返回动态 schema 对象；Pi 路径要求最后且唯一动作调用受限
   Verifier 输出工具。
3. Adapter 直接读取 inference payload，不扫描普通 stdout，也不读取 worker 自建 JSON。
4. `VerifierResult` 校验范围、有限性和 allocation key 的精确集合。
5. `complete_task` 先写共享 `VerifierRecord`，再把 task 标记为 `completed`。

## 队列与恢复

每个 run 在入队时分配单调递增的 `queue_sequence`。Loader 按
`run_id, queue_sequence, task_id` 排序；同一 run 存在未过期 running lease 时不 claim 下一项，
因此 run 内严格串行，不同 run 可以独立推进。

- settlement 提交后和 candidate recovery 都可以幂等调用 `ensure_assessment`；
- retryable failure 进入 `retry_wait`，到达 `next_attempt_at` 后重领；
- non-retryable failure 或 attempts 耗尽进入 `terminal_error`；
- 进程崩溃后通过 lease expiry 重领，不创建第二份权威 record；
- task 失败不撤销 hard settlement，也不改变 candidate eligibility；
- completed task 的 record 是 durable event source，恢复下游消费不再次调用 LLM。

Verifier timeout、无效 JSON、字段不匹配和 backend 错误只改变 task。Record 一旦完成便保持不可变；
Selection 或 Adaptive reconciliation 的失败不能反向修改 task 或 record。

## 模块所有权

| 组件 | 拥有的职责 | 明确禁止 |
| --- | --- | --- |
| Settlement owner | 记录 attempt、evidence、disposition、rollback 与 ledger 事实，提交完整结算单后入队 | 等待 LLM、解释隐藏价值或决定 allocation |
| Verifier application | task identity、FIFO、claim、retry、completion 和 immutable record | 修改 candidate、选择 winner、创建或应用 Decision |
| Verifier host/drainer | 执行单次结构化推理、推进 task，并把 completed record 交给窄 consumer | 导入或实现 Adaptive Snapshot、policy、Decision 和状态存储 |
| Structured inference adapter | 执行一次受限模型调用并返回结构化 payload | 自行写权威 JSON 或扩展冻结 schema |
| Verifier state | 原子保存 task、lease、record 和 completion 状态 | 解释评分或驱动 candidate 生命周期 |
| Deterministic consumer | 通过完整 subject identity 消费 record | 修改 VerifierRecord 或把 LLM 输出当作执行命令 |
| Monitor/report | 只读投影 task、record 和使用量 | claim、恢复或驱动业务状态 |

## 验证要求

实现与回归测试至少覆盖：

- 一次调用同时生成 `quality_score` 与所有冻结 `allocation_scores`；
- 极简模式严格要求 `allocation_scores=null`；
- 动态 schema 拒绝缺失、额外、非有限和越界字段；
- task identity 幂等、`queue_sequence` 单调且同 run 严格串行 claim；
- record 先写而 task 未提交时的 completion 恢复；
- 四类 disposition 都绑定 exact attempt 和必要 rollback 上下文；
- failure/discard diff 以 attempt base 和 attempt artifact 为端点；
- 共享 record 不依赖 candidate workspace；
- Verifier backlog 不阻塞 candidate execution；
- 完整 subject、task output pointer、allocation policy 和唯一 record identity 在消费前得到校验。
