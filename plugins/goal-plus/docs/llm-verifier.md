# LLM Verifier

本文定义 Goal Plus 当前 LLM Verifier 的正式协议。LLM Verifier 对一个已经完成硬结算的
subject 做一次局部、结构化评估，并产生一个不可变 `VerifierRecord`。它不负责选择 winner、
分配搜索资源或修改 candidate 状态。

固定并发的 `parallel_loops` 与动态分配的 `adaptive_search` 共用本协议；两种模式的配置、
Selection 和确定性 Allocation 见 [Adaptive Search](adaptive-search.md)。旧 Search Scheduler、
`ScheduleTask`、`allocation_flag`、二次确认及其兼容 fallback 均不属于当前协议。

## 职责边界

LLM Verifier 的输入和输出链路为：

```text
candidate iteration
  -> hard verifier
  -> exact SettlementReceipt
       attempt artifact identity + hard evidence + disposition
  -> idempotent VerifierTask admission
  -> host-owned run-scoped FIFO queue
  -> one structured LLM call
  -> immutable VerifierRecord
       quality_score
       scores | null
  -> terminal record delivery
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

一个已结算 subject 对应一个 `VerifierTask` 和一个不可变终态 `VerifierRecord`。
一次成功结构化调用同时完成全部得分；失败调用和缺分在该 task 内重试。`quality_score` 与可选的 `scores` 在同一次调用中产生；Adaptive
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

LLM Verifier prompt projection = B identity + hard evidence
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

Candidate 完成 hard settlement 后提交本轮持久化的事实、身份与 diff，幂等入队当前任务。
历史 submission 仅在队列首次启动或显式恢复时重放，正常提交不扫描历史。`pending`、`running`、`retry_wait` 或
`abandoned` task 不阻塞 candidate 启动、续轮、hard settlement 或恢复，也不回写 candidate
Evidence。最终 Selection 对必要分数的 readiness wait 只发生在搜索收尾阶段，不属于 candidate
backpressure。

Adaptive 模式中，只有 Executor 已选定的未完成 retirement 目标可以 fence 对应 lane。具体规则见
[Retirement 执行边界](adaptive-search.md)。

## 启用与冻结

`strategy.verifier` 独立于 `strategy.allocation` 配置：

- `verifier: {}` 显式启用基础 `quality_score` 评估，即使 Selection 只使用 hard score；
- `verifier.scores` 选择已注册的额外指标 key，可以用于 `parallel_loops` 或 `adaptive_search`；
- 未配置 verifier 时，Selection 使用 `quality_score` 仍自动启用基础质量评分，否则不创建任务；
- Adaptive 必须显式配置 verifier，所选指标 key 必须覆盖 AllocationPolicy 的输入要求。

例如，普通搜索也可以评估局部进展，完全不配置 allocation：

```yaml
strategy:
  orchestration_mode: parallel_loops
  verifier:
    scores: [progress_reward]
```

基础质量 rubric 固定为 `quality_score`；额外指标从 Verifier 自己的注册表选择。一次调用生成
`quality_score` 和通用 `scores`，字段与评估语义不再以决策消费者命名。
Adapter 只把固定输入投影明确列出的字段放进不可信 Evidence 数据区，rubric criteria 和执行协议
则进入高优先级 verifier instructions。当前投影包含 objective、
公开 `candidate_acceptance`、candidate hypothesis、当前 attempt 的精简身份、显式 settlement
事实、直接 comparison base 的精简身份、attempt manifest 和 bounded diff。完整 SettlementReceipt、
历史 VerifierRecord、旧分数与 rationale、调用与 usage 信息以及整包 runtime `constraints` 均不投影。

FrozenSpec 保存独立的 verifier 指标 key 列表和 allocation policy 声明。Settlement admission
只根据前者解析并冻结 score contract；后续推理和重试直接使用 task 中的完整规则，不重新查找
当前注册表。未选择额外指标时 `scores=null`；否则输出所选指标的精确字段集合。

完整 SearchSpec 示例见 [Adaptive Search 配置](adaptive-search.md#searchspec)。

## 评分语义、注册与程序集接口

[`src/goal_plus/domain/verifier.py`](../src/goal_plus/domain/verifier.py) 拥有注册表
`VERIFIER_SCORE_RULES`、配置 `VerifierSpec` 和解析接口 `verifier_score_contract(score_keys)`。
`VerifierScoreRule.name` 就是唯一得分 key，每个 key 对应唯一语义；规则还定义范围、评估指令
及是否需要直接 comparison base。指标没有版本字段。当前注册项为：

- `progress_reward`：`[-1,1]`，评价相对直接基线的已发生进展，需要 comparison base；
- `allocation_utility`：`[0,1]`，评价从 exact attempt 再探索一步的价值。

解析器拒绝未知 key 和重复指标，按 key 排序，使配置顺序不影响 contract hash。
`SettlementVerifier.prepare()` 在
[`src/goal_plus/application/settlement_verifier.py`](../src/goal_plus/application/settlement_verifier.py)
完成解析并冻结 contract。Record 的 `scores` 直接以这些 key 标识得分，不另存身份映射。
`score_contract_hash` 只绑定完整冻结规则，不是得分身份或得分版本。

```text
FrozenSpec.strategy.verifier.scores (unique metric keys)
  -> SettlementVerifier.prepare()
  -> verifier_score_contract(score_keys)
  -> frozen VerifierTask.score_contract
  -> immutable VerifierRecord(scores, score_contract_hash)
  -> consumers, including optional AdaptiveSearch
```

同一评分 key 的含义和量纲不可改变；需要不同语义时注册一个描述该语义的新 key。Policy 仍按
决策语义版本化，在自己的 spec 中声明 `required_score_keys`。配置层核对选择是否完整，
Adaptive 消费成功 Record 时核对所需 key，仅投影自己需要的指标；失败 Record 保持无分数。
Verifier 不 import Adaptive 内部实现；Adaptive 不解释 rubric，也不重建评分契约。

### 新增评分或策略

1. 在 `VERIFIER_SCORE_RULES` 的规则序列中加入一个 `VerifierScoreRule`，例如
   `name="maintainability", minimum=0, maximum=1, instructions="..."`。
   基础 `quality_score` 为保留名；不要为已有 key 赋予另一种语义。
2. 在 `strategy.verifier.scores` 中选择 `maintainability`。Python、MCP 和 Pi 使用同一配置
   schema，自动展示注册 key；不需要修改 allocation、队列或模型调用代码。
3. 只有新增决策消费者时，才在
   [`domain/adaptive/policy.py`](../src/goal_plus/domain/adaptive/policy.py) 声明所需指标 key 并实现策略。
   同一评分可供多个 policy 使用，一个策略也可只消费评分表的一部分。

当前新增注册项是插件源码扩展；运行配置只选择注册 key，不接收任意评分提示词或动态 Python
导入。注册后应覆盖一次无 allocation 的完整评估，以及对应消费方的 key 匹配测试。

组件间的稳定程序集入口为：

- `SettlementVerifier.prepare()`、`submit()` 与 `replay()`：冻结当前 settlement input 并提交或恢复入队；
- `VerifierWorkflow.ensure_assessment()`、claim/complete/fail：拥有 task 生命周期、重试和最终放弃；
- host-owned `VerifierQueue`：串行运行模型，并将终态 record 交给一个 consumer；
- `FileSearchRuntime.accept_verifier_record(record, execute=...)`：Verifier 到 AdaptiveSearch 的唯一业务交付入口。

这些接口只传递标准化的 submission、task 或 record；它们不让 Verifier 读取 Snapshot、候选状态、
AllocationDecision 或执行锁。

Verifier 的调用观测保存 `agent_harness=codex|pi` 和 `runtime_provider=direct`。模型与 reasoning
继承可信当前宿主配置，Pi 使用 scope 中记录的 Main model/thinking；没有独立 model override。
执行复用 StructuredInference、角色工具白名单和 ProcessRegistration。
Settlement 保存单次不可变输入并入队；可信 candidate host 唤醒其持有的串行队列。
Selection readiness wait 不关闭内部队列；已完成 record 消费失败只重放消费，不重新评分。

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

Settlement owner 只完成 disposition、必要 rollback、ledger 与完整结算单持久化。Settlement 随后
用当前 committed submission 幂等调用 `ensure_assessment`，因此 `VerifierTask` 不会绑定未完成的结算状态。

### VerifierSubject

```text
VerifierSubject
  frozen_spec_id
  settlement: SettlementReceipt
```

Subject 不复制 commit、artifact 或 hard evidence 字段。Subject identity 是 `frozen_spec_id` 与整张
结算单 canonical payload 的 hash；attempt、rollback 或 evidence 任一字段变化都会改变身份。

Verifier 输入包含 objective、公开 candidate acceptance、candidate hypothesis，以及三个明确投影：

- `attempt_identity`：candidate、iteration、settlement 和 attempt artifact 身份；
- `hard_evidence`：disposition、hard score、process 结果和 failure class；
- `comparison_base_identity`：本次 bounded diff 的直接基线 artifact 身份。

`artifact_manifest` 只列本次 attempt 的改动文件；`bounded_artifact_diff` 比较直接 parent/base 与
exact attempt。完整 SettlementReceipt、历史 VerifierRecord、旧分数/rationale、invocation/usage、
FrozenSpec identity 与整包 runtime `constraints` 不进入 LLM 输入。直接浏览 candidate 代码与 Git
历史暂不属于当前协议；未来如需支持，应作为 Verifier 的受限只读能力扩展。

### VerifierTask

```text
VerifierTask
  schema_version
  task_id / queue_sequence
  subject / input / delivered
  score_contract:
    schema_version / contract_hash
    score_rules: name / minimum / maximum / instructions / requires_comparison_base
  state
  attempts / max_attempts / next_attempt_at
  lease
  output_record_id
  invocation / usage / error
  created_at / updated_at
```

Task identity 由 run、settlement 和冻结 contract hash 共同确定。
`ensure_assessment` 是幂等操作，相同身份只返回已有 task。

状态转换为：

```text
pending -> running -> completed
                  -> retry_wait -> running
                  -> abandoned
```

Lease 过期后可以重新 claim。Completion 和 failure 必须提交 claim 时取得的 `worker_id + attempts`，
并在 task 锁内匹配当前 lease；重领后的旧 claim 不得写 record 或改变 task。若 record 已写入而 task
尚未提交为 `completed`，当前 claim 的重复 completion 必须复用同一个不可变 record 并修复 task 状态。

### VerifierRecord

```text
VerifierRecord
  schema_version
  record_id / task_id / queue_sequence
  outcome: completed | failed
  error | null
  initial_artifact_ref / parent_node_id / allocation_depth / replaces_candidate_id
  subject
  score_contract_hash
  quality_score
  scores | null
  evidence_refs
  score_rationales
  invocation / usage
  created_at
```

成功 Record 的 `quality_score` 是 `[0,1]` 内的有限数。失败 Record 无分数和 rationale，
必须带 error。未注册额外指标时 `scores` 必须为 `null`；否则
必须提供对象，其字段集合与 task 冻结的 score rules 完全一致，每个值分别满足该 rule 的
上下界。`score_rationales` 必须与 `quality_score` 加全部额外 score 严格一一对应。
Record 保存 `score_contract_hash` 并通过 `task_id` 绑定生成它的完整冻结契约，不复制整份规则。
Task/Record 使用 schema 7，score contract 使用 schema 2；旧 policy 绑定记录不隐式迁移。

当前 record 不包含 `quality_uncertainty`、`criterion_scores` 或 `VerifierMetricRecord`。Criteria
属于 rubric 内部评估结构，不作为长期数据合同。未来的 Steering 与 Allocation 同属
Adaptive Search；其所需新指标必须通过 Verifier 独立注册并配置，从通用 `scores` 读取，
只声明自己需要的指标 key，不能从 rationale 推造 criterion scores。当前未实现 Steering。

## Allocation 指标

Verifier 按唯一指标 key 定义固定的语义、范围及 LLM 提示词；具体 contract 的
source of truth 是 `domain/verifier.py`，而不是策略算法：

- `progress_reward`：exact attempt 中新增的局部价值，包括粗粒度 hard score 未表达的进展；
- `allocation_utility`：无论 disposition 为何，从 exact attempt 继续搜索的局部价值。

评估语义遵守以下规则：

- 指标名称匹配 `^[a-z][a-z0-9_]*$` 且同一评分表内不能重复；
- FrozenSpec 分别固化指标 key 选择和策略身份/参数，不包含 Verifier score rules；
- Verifier admission 用指标 key 选择规则并把完整 contract 固化到 task；
- 每个评分 key 拥有唯一语义；policy 单独保留决策版本；
- `swe_search_uct/v1` 要求 `progress_reward` 和 `allocation_utility`，不限制其他消费者的指标；
- 动态 JSON Schema 精确列出冻结字段并禁止额外字段；
- 少字段、多字段、非有限值或越界值都会使 completion 失败，不会静默填 0。

Snapshot 原样中转 Policy 需要的已校验分数。AllocationPolicy 与纯算法库的组合规则见
[Deterministic Allocation](adaptive-search.md#deterministic-allocation)。

## 结构化输出

Verifier adapter 根据 task 的冻结字段动态生成严格 JSON Schema。Adaptive 输出示例：

```json
{
  "quality_score": 0.82,
  "scores": {
    "progress_reward": 0.31,
    "allocation_utility": 0.74
  },
  "evidence_refs": ["diff:src/example.py"],
  "score_rationales": {
    "quality_score": "The current artifact partially satisfies the objective.",
    "progress_reward": "The attempt made measurable progress over its parent.",
    "allocation_utility": "A concrete high-value follow-up remains."
  }
}
```

极简模式示例：

```json
{
  "quality_score": 0.82,
  "scores": null,
  "evidence_refs": ["diff:src/example.py"],
  "score_rationales": {
    "quality_score": "The settled artifact improves the target."
  }
}
```

可信写入链路为：

1. Adapter 严格按固定输入投影，从 FrozenSpec 公开验收合同、当前 attempt 的精简事实与身份、
   comparison base 身份和 bounded diff 构造不可信 Evidence 数据区；
   固定质量 rubric 与执行协议单独组成 verifier instructions，task 中的 score contract 规则按需拼接。
2. Structured inference backend 强制模型返回动态 schema 对象；Pi 路径要求最后且唯一动作调用受限
   Verifier 输出工具。
3. Adapter 直接读取 inference payload，不扫描普通 stdout，也不读取 worker 自建 JSON。
4. `VerifierResult` 校验有限性、每项冻结范围以及 score/rationale key 的精确集合。
5. `complete_task` 先写共享 `VerifierRecord`，再把 task 标记为 `completed`。

## 队列与恢复

每个 run 入队分配单调 `queue_sequence`，队首按次序独占消费。模型异常、字段缺失和重试
属于任务内部状态；只有完整评分或最终放弃才写不可变 VerifierRecord，分别对应
`completed` 与 `abandoned`。失败 Record 没有部分分数，也不填零。

Settlement 的不可变 submission 与 iteration 一起提交。入队中断可重放已提交输入，不重新
读取工作区或用历史评分补上下文。Verifier 仅读取 task.input 与规则，不读取 candidate 历史。

终态 Record 经注入的单一 consumer 同步交付，消费返回后标记 delivered，再领取下一项。
Snapshot/Decision 的持久化交付失败仅重放输出，不重试模型。Decision 已持久化后，执行异常
由 Executor 独立恢复，不阻塞后续评分，也不触发 Policy 重算。Record 已写而 task 未更新的中断，
通过同一 task 身份恢复。
运行中断通过 lease 到期重新领取；旧 claim 不得覆盖新 claim 或既有终态。

## 模块所有权

| 组件 | 职责 | 禁止 |
| --- | --- | --- |
| Settlement | 当前事实、精确身份、diff 和持久化 submission | 等待模型或读取旧评分 |
| Verifier workflow/queue | 入队、串行推理、重试、终态与输出交付 | 修改 candidate 或作 allocation 决策 |
| LLM Verifier | 注册指标 key，根据冻结 rubric 评估单次输入 | 导入 Adaptive 内部策略或存储 |
| Structured inference | 一次受限模型调用、结构化输出 | 写业务权威状态 |
| AdaptiveSearch consumer | 追加 Snapshot、计算 Plan、持久化 Decision 后调用 Executor | 重新评分、修改历史 Record 或回传执行恢复 |
| Monitor/report | 只读投影 | claim、恢复或驱动队列 |

## 验证要求

实现与回归测试至少覆盖：

- 一次调用同时生成 `quality_score` 与所有冻结 `scores`；
- 未配置额外指标时 `scores=null`；非 allocation 搜索也能独立评估新增指标；
- 动态 schema 拒绝缺失、额外、非有限和越界字段；
- task identity 幂等、`queue_sequence` 单调、同 run 严格串行 claim，且重领后旧 claim 无法完成或失败 task；
- record 先写而 task 未提交时的 completion 恢复；
- 四类 disposition 都绑定 exact attempt，不投影 rollback receipt；
- failure/discard diff 以 attempt base 和 attempt artifact 为端点；
- 共享 record 不依赖 candidate workspace；
- Verifier backlog 不阻塞 candidate execution；
- 完整 subject、task output pointer、冻结 score rules 和唯一 record identity 在消费前得到校验。
