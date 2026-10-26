# Adaptive Search

Adaptive Search 是 VerifierRecord 驱动的综合搜索决策模块。它不调用模型、不定义评分语义，也不
重新验证 settlement；它把可信评分追加到历史 Snapshot，按冻结的确定性策略生成 Decision，
再调用相应 Executor 影响正在进行的搜索。

~~~text
terminal VerifierRecord
  -> AdaptiveSearch.accept_verifier_record()
  -> if Allocation enabled: append SearchSnapshot -> AllocationPolicy
       Plan     -> AllocationDecision -> AllocationExecutor
       no plan  -> optional SteeringPolicy
                     action -> SteeringDecision -> SteeringExecutor
                     no-op  -> return
       error    -> record error and return
     otherwise: optional SteeringPolicy -> SteeringDecision -> SteeringExecutor
~~~

Allocation 优先于 Steering。同一次 Record 消费中两类决策不会同时执行；已有当前 Snapshot 的
AllocationDecision、Allocation 计算错误或 Allocation 执行失败都不会回退到 Steering。
最终 winner Selection 是独立评分消费者，见 [LLM Verifier](llm-verifier.md#selection-设计)。

## 基于 Verifier 评分调度、分配和引导 worker

Adaptive Search 是叠加在固定初始并行 lanes 上的可选策略层。省略
`strategy.adaptive_search` 时使用固定并行；配置该对象后才启用它包含的两个正交动作域：

- Allocation：根据完整 Snapshot 选择要淘汰的 candidate 和要派生的 source node；
- Steering：独立启用，或在 Allocation 本次明确没有 Plan 时，根据当前 Record 的诊断事实对现有活动调用
  发送一次受限建议。

两者都消费已经冻结和校验的 score key，但不共享 Decision、Executor 或恢复机制。Allocation
改变搜索拓扑并具有持久化执行恢复；Steering 不改变拓扑，是一次性发送许可，没有待发送队列。

Verifier key 拥有唯一语义，policy identity 和参数按决策语义版本化。Adaptive Search 支持 Git
worktree 和 ThinkThread private workspace；最简只需指定 Allocation policy：

~~~yaml
workspace:
  provider: git_worktree
strategy:
  initial_context: fresh
  adaptive_search:
    allocation:
      policy: swe_search_uct/v1
    steering:
      policy: search_diagnostic_guidance/v1
~~~

公共 Freeze 输入和内部 SearchSpec 共用 StrategyOptions，在配置解析时完成以下补全，然后
将结果写入 FrozenSpec 并参与 spec hash：

- `strategy.adaptive_search` 至少配置 allocation 或 steering；省略该对象表示固定并行。
- Adaptive Search 必须显式使用 `initial_context: fresh`（或命令配置 `spawn_mode=fresh`），因为
  `initial_context: fork` 与 `adaptive_search` 当前互斥。
- `initial_context: fork` 与 `adaptive_search` 当前拒绝，因为 fork successor dispatch 尚未接线。
- verifier 缺省、scores 为空或不完整时，合并已启用 Allocation/Steering 的 required_score_keys，
  保留用户额外选择的 key。未知 key 和重复配置仍拒绝。
- parameters 可以省略或只覆盖部分字段，其余使用该 policy 版本已有默认值；没有默认值的必填
  参数仍必须提供，不会为其猜测取值。

这些规则位于 [domain/search.py](../src/goal_plus/domain/search.py)；公共输入转换见
[domain/search_input.py](../src/goal_plus/domain/search_input.py)。补全只组合配置，Verifier 仍按自己
的注册表解释独立 score key。两项 policy 都未配置时使用固定并行。Selection 参数、完整
评分 key 与版本策略一并冻结；运行中的 run 不接受配置变化。

## VerifierRecord 耦合设计

Adaptive Search 对 Verifier 的唯一依赖是公共 VerifierRecord schema。稳定交接入口为：

~~~text
FileSearchRuntime.accept_verifier_record(record, execute=..., steering_host=...)
  -> AdaptiveSearch.accept_verifier_record(...)
~~~

Record 不携带 policy identity 或 score version。Adaptive 从 FrozenSpec 取得 policy spec，并只核对
该 policy 的 required_score_keys 是否存在；不读取 rubric、prompt、score instructions、task 重试
状态或模型输出草稿。

启用 Allocation 时，AdaptiveSnapshotBuilder 从旧 Snapshot 与当前终态 Record 追加新 revision；
仅启用 Steering 时直接消费 Record，不创建 Allocation Snapshot：

- queue_sequence 必须连续；同一 Record 重放返回原 Snapshot；
- 历史得分只追加，不重算、不覆盖；
- 成功 Record 只把 AllocationPolicy 需要的 key 投影为 allocation_scores；
- 失败 Record 也追加 lineage gap，但没有分数，不能跨过缺口连接旧节点；
- rollback continuation 连接精确恢复节点，不连接已丢弃 attempt；
- 完整得分在基线未知时建立独立根，不猜测父子关系；
- Snapshot 不扫描 candidate、workspace 或 Allocation 执行结果，也不推断 eligibility 和容量。

Snapshot/Decision 持久交接失败可以重放同一个 Record；已存在同 Snapshot 的 Decision 时不重算
Policy。Executor 的执行结果与恢复状态不会反向修改 Record 或 Snapshot。

## Adaptive Search 流程和架构设计

公共装配位于 [adaptive](../src/goal_plus/adaptive/__init__.py)，入口编排位于
[adaptive/search.py](../src/goal_plus/adaptive/search.py)。一次 Record 消费按固定顺序执行：

1. 校验终态 Record；启用 Allocation 时追加或复用 Snapshot，否则跳到 Steering 分支；
2. failed Record 到此结束，不生成决策；
3. 若当前 Snapshot 已有 AllocationDecision，结束本次消费；
4. 调用冻结 AllocationPolicy；异常写入 policy error 并结束；
5. 有 Plan 时幂等持久化 AllocationDecision，并尝试调用 AllocationExecutor；
6. 未启用 Allocation 或 Plan 为 null 时，把当前成功 Record 交给可选 Steering；
7. 交付入口返回后，Verifier 队列可以确认 delivered 并领取下一项。

确定性计算没有额外 reconciler、drainer、scheduler 或决策锁。AllocationExecutor 对拓扑修改使用
run.lock；SteeringExecutor 只在发送准入与来源复核的短临界区使用同一 run lock，消息 I/O 不持锁。

主要 owner 为：

| owner | 位置 | 职责 |
| --- | --- | --- |
| Snapshot | [application/adaptive_snapshot.py](../src/goal_plus/application/adaptive_snapshot.py) | Record 到历史评分和 lineage 的只追加投影 |
| Policy specs | [domain/adaptive/policy.py](../src/goal_plus/domain/adaptive/policy.py) | 版本 discriminator、参数、required score keys 和纯分派 |
| AdaptiveSearch | [adaptive/search.py](../src/goal_plus/adaptive/search.py) | 单一 Record 入口及 Allocation/Steering 顺序 |
| Runtime facade | [runtime.py](../src/goal_plus/runtime.py) | host 可调用的交付、apply 和 recovery 接口 |

## Allocation

Allocation 是唯一改变 candidate 拓扑的决策域。Policy 解释 Snapshot 并产生 Plan；Application 将
Plan 固化为 Decision；Executor 只执行该 Decision。

### 确定性 Allocation 版本策略架构

AllocationPolicySpec 是以 policy 为 discriminator 的 Pydantic union。每个版本声明：

- 唯一 policy identity；
- 该版本的完整参数 schema；
- required_score_keys；
- 从 SearchSnapshot 到 AllocationPlan 或 null 的纯确定性 evaluator。

FrozenSpec 固化完整 spec。相同 FrozenSpec、Snapshot 与 policy 版本必须产生相同 Plan 和 plan
hash。新增策略时，在 domain/adaptive/policy.py 增加 spec 与分派，再实现纯 evaluator；改变决策
语义时增加 policy 版本。评分语义不随 policy 版本改变，新评分仍在
[Verifier 注册表](llm-verifier.md#新增评分接口和配置)中使用新的唯一 key。

Application 不配置或直接调用底层数值算法。计算异常表示拒绝决策：记录错误并结束，不请求模型
重评，也不构造 fallback Plan。

### Algorithms 算法库

通用算法位于
[domain/adaptive/policy_algorithms](../src/goal_plus/domain/adaptive/policy_algorithms/)；策略组合位于
[domain/adaptive](../src/goal_plus/domain/adaptive/)。算法只处理数字、序列、父索引和稳定
tie-break，不理解 candidate、artifact、retirement、capacity、store、host 或 VerifierRecord。

Policy 是算法库的唯一业务调用者。它负责：

- 从 Snapshot 建立算法输入；
- 组合 UCT/UCB、深度项、止损和容量规则；
- 把纯计算结果解释为有业务身份的 AllocationPlan；
- 确保相同输入得到相同排序。

算法库不生成 Decision、不做 I/O，也不自行注册评分语义。一个 policy 可以组合多个算法；同一
算法也可被不同版本 policy 复用。

### AllocationDecision

AllocationPlan 包含按策略优先级排列的 retirement candidates、expansion candidates，以及每个
派生选项允许替换的 candidate 列表。Plan 是纯计算结果，Decision 是可恢复执行意图。

Application 按 plan hash 幂等持久化 AllocationDecision，保存：

- based_on_snapshot_revision 与 Verifier sequence；
- 完整 policy identity 和 plan hash；
- 每个 expansion 的 source、稳定 expansion id 和预留 child id；
- selected_retirement；
- 中断、物化、派发和最终执行结果。

当前 Snapshot schema 为 6，Plan/Decision schema 为 5；旧记录不隐式升级。Decision 一旦持久化，
Executor 不重算策略、不重验分数，也不因执行环境变化改换 policy。

### AllocationExecutor

一次执行在同一个 run.lock 内完成：

1. 按 Verifier sequence 恢复较早的未完成 Decision；前项结果未知时不执行后项。
2. 按排名跳过已淘汰目标，检查真实容量与派生选项，持久化唯一 selected_retirement；选定后不
   改换淘汰目标。
3. 按 candidate/session/generation/call 精确身份请求中断并保存回执。明确拒绝进入
   terminal_error；未知结果保持 interrupted，不能假定淘汰成功。
4. 淘汰成功后按 Plan 顺序选择 expansion；已经被其他 candidate 占用的 source 跳过。
5. 物化前保存 child id 与完整 ExpansionSource，恢复时复用原身份。
6. 物化失败或 host 明确未提交调用时，退休 partial child 并尝试下一节点。未知派发只按原身份
   再观察；仍未知则保留 dispatching/interrupted，不启动备用 child。
7. 成功派发写 applied；全部选项失败写 terminal_error，并保留已经发生的淘汰和失败事实。

正常执行及即时重试始终持有 run.lock。host wake/start 调用 Executor recovery，显式 apply 也先
恢复较早 Decision。Direct native host 丢失 controller 后不能用 request intent 证明派发成功；
冷恢复保持 interrupted。ThinkThread 使用准确调用恢复接口。旧 prepare/ack、reconciliation
journal、reconciler 和独立调度锁不属于当前架构。

### 示例：swe_search_uct/v1

当前交付的 Allocation policy 是 swe_search_uct/v1，自动补齐 progress_reward 与
allocation_utility。parameters 缺省使用该版本默认值，也可仅设置例如 max_depth: 7。
它从 Snapshot 临时重建树，把 allocation_utility 离线回传为子树 visits、
utility sum 和 mean，再对可扩展 source 排序：

~~~text
UCT = mean_allocation_utility
    + C * sqrt(ln(parent_visits) / node_visits)
    + alpha * exp(-beta * (depth - 1))
    - gamma * sqrt(depth)
~~~

progress_reward 使用 [-1,1]，只用于根到 node 路径尾部的低 reward 止损；达到配置 streak 后，
即使 utility 很高也不再扩展。Policy 还应用 max_depth、每节点 child 上限、稳定 tie-break 和
并行 lane retirement。UCT view、visits、均值、排名和 abandoned 状态都只在一次 evaluation
内存在，不持久化。

## Steering

Steering 是可选软引导域。它在未启用 Allocation 或 Allocation 返回 null 时消费当前成功 Record，根据诊断
分数、结构化 findings、当前 settlement 之前的本 candidate 历史和尝试额度选择一个动作或 no-op。
它不读取 peer 排名、Allocation utility、rationale 或未来事实。

### 确定性 Steering 版本策略架构

SteeringPolicySpec 与 AllocationPolicySpec 分离，但采用相同的版本化模式：policy discriminator、
版本专有参数、required_score_keys 和纯 evaluator。当前策略 search_diagnostic_guidance/v1
要求五项诊断 score，并要求冻结 findings contract。

两类 Policy 都只从 FrozenSpec 进入 `domain/adaptive/policy.py` 的版本分派，再调用各自独立实现：
Allocation 使用 `swe_search_uct.py`，Steering 使用 `search_diagnostic_guidance.py`。Steering 不提供
构造时注入任意 Policy 对象的第二入口，也没有单独的 `deterministic.py` 注册层。

启用配置示例：

~~~yaml
strategy:
  steering:
    policy: search_diagnostic_guidance/v1
~~~

五项诊断 score 随 steering 自动补齐；parameters 与 delivery 可以省略或只覆盖部分字段。
v1 默认沿用原诊断示例：correctness/validation 阈值 0.6、confounder/test gap 阈值 0.7、
boundary probe/confirmed 阈值 0.4/0.9、冷却 30 秒、每 candidate 最多 4 次、每 issue 最多 2 次、
history_limit 16。投递默认 apply，RPC 超时 2 秒，来源最长 60 秒。这些默认值尚未经收益标定。

需要仅观察时配置 delivery.mode: shadow；它计算并保存诊断、检查投递 gate，但不投递、不占用
额度，也不形成可升级的消息待办。变更冻结策略必须创建新 run。与 Allocation 同时配置时，仍
遵守 Allocation 优先的互斥流程。

### SteeringDecision

纯策略返回具名 no-op 或单个动作。存在动作时，Adaptive 建立 SteeringDecision，绑定：

- source_record_id、完整 subject hash、candidate_id；
- reason_code、issue_key、evidence_revision 与 input_hash；
- 由可信有限模板生成的消息，不直接发送模型自由文本。

Decision 不允许更改目标、验收、预算、edit surface、正常 settlement 或 retirement。它也不透露
阈值、排名和淘汰倒计时。引导后候选仍按正常规则结算和淘汰。

### SteeringExecutor

SteeringExecutor 检查当前 settlement、来源年龄、rollback、session/generation、retirement fence
和准确活动 call。没有活动调用时跳过；它不启动、唤醒或转投其他 candidate。

发送前在 run.lock 内持久登记 steering-attempts/<attempt_key>.json，并重核冷却、candidate/issue
次数和来源身份；锁外执行一次有界 Host 消息调用。相同来源与配置最多取得一次发送许可，消息
变化不产生新许可。登记后崩溃允许丢失；调用失败、无回执或 observation 保存失败均不重发，
也不阻塞 Verifier 交付。

Host 完成自身 admission 后、交给 controller 前，调用 Executor 提供的 before_send 来源复核。
复核短暂取得 run.lock，消息 I/O 不持锁；若来源、代际或执行许可已经失效，则记录 not_sent，
但不退还已消耗机会。该检查不与异步传输或 candidate 执行构成原子事务。消息携带来源
settlement，晚到时只作为历史建议，不要求 worker 回到旧 artifact。

同一 trace 独立保存首次 observation：status、reason_code、target 和 observed_at，不改写纯策略
结果。returned 只表示调用返回；unknown 或缺失不能解释为未收到。already_consumed 重放不覆盖
原 observation。没有 pending queue、ack wait、Exposure 跟踪或 recover_steering。

### 示例：search_diagnostic_guidance/v1

策略先拒绝合同或公开引用不匹配、缺主要假设、同范围高置信 confounder、充分边界证据冲突，
以及冷却、次数、同问题同证据限制；然后按固定顺序选择第一项：

| 条件 | 决策 |
| --- | --- |
| 充分且限定范围的边界证据 | boundary_established no-op |
| 完整身份相同且无需随机复测的实验 | deduplicate |
| correctness gap 与局部 confounder 达阈值 | minimal_repair |
| 部分边界证据和明确可判别问题，无高置信 confounder | boundary_probe |
| validation gap 和可判别问题达阈值，无高置信 confounder | hypothesis_probe |
| 其他情况 | no_supported_action |

[application/steering.py](../src/goal_plus/application/steering.py) 从当前 iteration 原始 submission
核对 Record，只读取当前 settlement 之前的本 candidate 历史。诊断输入和纯结果持久化到
steering-diagnostics，以 input/result hash 保证重放不加入晚到事实；该目录不是执行队列。

问题 identity 保守地使用规范化主要 hypothesis 与 scope。evidence revision 只来自结算已有的
attempt_artifact_hash、公开 hard outcome 和完整实验身份；hard outcome 使用 process_passed 与
hard_score。相邻 iteration diff、Record、settlement/session id、耗时、diff 基准推进、
ledger-only commit，以及同分复测的 keep/retain 差异都不构成新证据。

严格实验去重只接受冻结 hard verifier 在
SettlementEvidence.metrics.<verifier_name>.steering_experiment 中声明的完整 artifact、test scope、
input、environment、seed、determinism 和 replication identity。身份缺失、非法、非确定性或需要
重复测量时均为 unknown，不推断等价。停止重复实验不等于退休 candidate，也不允许跳过验证或
发布未验证 artifact。

## Host

Host 只负责装配和准确执行能力，不拥有策略：

- 把 VerifierQueue 的终态 consumer 绑定到 accept_verifier_record()；
- 为 Allocation 提供按准确 session/call 身份的 cancel、dispatch 和恢复能力；
- 为 Steering 提供 active_call()、message() 以及发送前 guard 调用点；
- 在 wake/start 时调用 recover_allocation()，但不扫描评分或重新计算 Policy；
- 在 run 关闭时停止对应 Verifier consumer 和 native worker 生命周期。

Host 不解释 score、选择 Plan、生成 Steering action、修改 Decision 或从进程存活推断业务终态。
native session 的授权与生命周期合同见
[Agent Host Adapters](agent-host-adapters.md)。Monitor/report 只读投影持久状态；使用方式见
[Feature Plugins](feature-plugins.md)。

### Monitor Feature Plugin

Monitor 是独立的只读 observability plugin，不参与评分、决策、执行或恢复。本节只保留旧链接
锚点；启用方式、视图和 host metadata 以 [Feature Plugins](feature-plugins.md) 为准。

## 模块边界和验证

| 模块 | 核心职责 | 不拥有 |
| --- | --- | --- |
| AdaptiveSearch | Record 入口、Snapshot 追加和决策分支顺序 | LLM、评分语义、执行恢复 |
| AllocationPolicy | Snapshot 到 Plan 的确定性业务计算 | I/O、candidate 修改 |
| Policy Algorithms | 通用纯数值算法 | 流程和领域对象 |
| AllocationExecutor | 持久 Decision 的拓扑执行与恢复 | 评分和 Policy 重算 |
| SteeringPolicy | 当前诊断输入到 action/no-op | 消息发送和 peer 排名 |
| SteeringExecutor | 一次性准入、来源复核与发送 | 重发队列、搜索拓扑 |
| Host | 能力装配和真实 native 调用 | 评估与决策 |

测试以模块主合同为中心：Record/Snapshot 顺序与 lineage、确定性 Plan、Decision 幂等与恢复、
备用派生、Steering no-op/一次性准入、来源复核和 Host 能力边界。真实 host 的消息消费效果、
诊断语义正确率和策略收益需要单独 benchmark 或 ST；固定输入单元测试不证明这些效果。
