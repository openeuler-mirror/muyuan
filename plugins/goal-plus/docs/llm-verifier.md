# LLM Verifier

本文定义 Goal Plus 从一次硬结算事实生成可信评分，并由确定性 Selection 消费评分的正式合同。
LLM Verifier 是局部专业评估器：它只评估一个已经完成硬结算的 subject，产生不可变
VerifierRecord；不修改事实、不读取全局搜索历史，也不决定资源分配、worker 引导或 winner。

~~~text
candidate iteration
  -> hard verifier
  -> SettlementReceipt + bounded diff
  -> VerifierTask
  -> run-scoped FIFO queue
  -> one structured LLM call
  -> terminal VerifierRecord
       -> deterministic Selection
       -> optional AdaptiveSearch
~~~

固定并发与可选 adaptive_search 共用同一套结算、评分和 Selection
合同。搜索过程中的 Allocation 与 Steering 见 [Adaptive Search](adaptive-search.md)。

## 评估验证功能和架构

一次已结算 subject 对应一个幂等 VerifierTask 和一个不可变终态 VerifierRecord。成功调用必须
同时返回 quality_score、全部已配置附加分数及各自 rationale；模型异常、字段缺失和重试都是
task 内部状态，不向下游暴露部分结果。

LLM Verifier 只拥有局部评估，不能：

- 修改 hard evidence、settlement、artifact 或 candidate eligibility；
- 读取其他 candidate 的 Record、Allocation 状态或搜索排名；
- 停止搜索、退休或派生 candidate，或创建任何 Decision；
- 选择 winner、绕过 promotion gate，或把 hard score 与 LLM 分数压缩为隐式总分；
- 从自由文本、candidate 自建文件或 rationale 猜测权威指标。

Hard evidence 中的 artifact identity、edit surface、process、anti-cheat 与 promotion 条件是不可覆盖
gate；hard_score 和 LLM 分数只是显式排序或策略输入。模型分数不能让违反 gate 的 artifact
进入 promotion。

Task 和 Record 集中存储在 Search run 中：

~~~text
.gp/runs/<run_id>/verifier/tasks/<task_id>.json
.gp/runs/<run_id>/verifier/records/<record_id>.json
~~~

candidate workspace 只提供被评估 artifact，不拥有评分数据库。可信 adapter 解析、校验并写入
共享状态；candidate worker 和模型都不能直接写 Record。

## Settlement 流程

Settlement owner 只结算当前 attempt，不聚合历史。SettlementReceipt 是 disposition、hard
evidence、artifact 身份和必要 rollback 的权威事实清单：

~~~text
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
  post_settlement_artifact_ref / ledger_artifact_ref
~~~

keep/retain 不带 rollback；discard/failure 必须保存完整 rollback 回执。被评估对象始终是
attempt_artifact_ref，回滚后的 artifact 与 ledger projection 只描述结算后的 workspace 状态，
不能替换评分对象或把两个身份连接起来。

~~~text
base A -> attempt B -> hard settlement -> rollback R -> ledger L
                       |
                       +-> Verifier evaluates B
~~~

Settlement 完成 disposition、必要 rollback、ledger 和 Receipt 持久化后，生成不可变 submission，
再幂等调用 SettlementVerifier.submit()。正常提交只处理当前 iteration；只有队列首次启动或显式
恢复才由 replay() 重放已持久化 submission，不重新扫描工作区或从历史 Record 重建输入。

VerifierSubject 只包含 frozen_spec_id 与完整 SettlementReceipt。subject identity 由两者的
canonical payload 计算；attempt、rollback 或 evidence 任一字段变化都会改变身份。

模型输入只投影当前 subject 的精简事实：

- objective、candidate_acceptance、candidate_hypothesis：冻结目标、显式验收要求和本次假设；
- metric_target：冻结指标方向、有效数值 success_threshold 和 hard score 是否达到该阈值；
- attempt_identity：candidate、iteration、settlement 和 exact attempt artifact 身份；
- hard_evidence：disposition、hard score、process 结果、edit-surface 事实和 failure class，
  以及有界 verifier metrics 摘要；
- comparison_base_identity：bounded diff 的直接基线身份；
- artifact_manifest 与 bounded_artifact_diff：当前 attempt 的改动文件和直接 diff。

完整 Receipt、历史 Record、旧分数和 rationale、调用与 usage、整包 runtime constraints、rollback
artifact 内容都不进入模型输入。直接浏览 candidate 代码与 Git 历史也不属于当前协议。

metric_target 只比较数值，不替代 process 或 edit-surface gate，也不表示达到全局最优。
阈值与方向在 settlement submission 中冻结；旧记录缺少字段时保持 null，不从当前配置补取。
metrics 摘要最多 8 KiB，每个字段最多 1 KiB，保留完整 JSON 值；内部元数据和 stdout/stderr
tail 不进入摘要。超限、非法数值或其他被省略的验证内容通过 metrics_omitted 明示，
不截断字段值，也不把缺失理解成验证未执行或失败。

## 异步任务队列设计

每个 run 使用一个由可信 candidate host 持有的 FIFO consumer。入队时分配单调
queue_sequence，队首 task 独占模型执行权；只有它进入不可逆终态并完成 Record 交付后，
队列才领取下一项。

启用 Verifier 时，host 在派发 candidate 调用时启动持久 consumer，worker 尚未返回也能消费
后续结算任务；结算与 Selection 回调可再次唤醒同一队列。内部评分仍校验 Main 授权、run 归属、
当前 activation 和开放的 execution scope，但不受关闭候选时的 run fence 阻断，也不重新打开
已关闭的 scope。Allocation 与 Steering 的候选操作仍受原有执行准入约束。

~~~text
pending -> running -> completed
                  -> retry_wait -> running
                  -> abandoned
~~~

completed 表示完整评分已经写入成功 Record；abandoned 表示内部重试耗尽并写入无分数的失败
Record。模型调用失败、结构不完整、暂时等待重试都不是任务终态。失败 Record 不保留部分分数，
也不以零代替未知值。

VerifierTask 保存 subject/input、冻结 score contract、state、attempts、retry time、lease、
output pointer、调用观测和错误。Task identity 由 run、settlement 与 contract hash 共同确定；
ensure_assessment() 对相同身份幂等。

claim 用 worker_id + attempts 绑定当前 lease，过期后可以重领；旧 claim 不得写 Record 或改变
新 claim。若 Record 已写而 task 尚未提交为 completed，同一 task 的恢复必须复用已有 Record，
不能再次调用模型。

终态 Record 经注入的单一 consumer 同步交付。consumer 返回后设置 delivered，然后队列处理
下一项。交付失败只重放同一个 Record，不重新评分。Verifier backlog 不阻塞 candidate 继续迭代；
Selection 需要评分时的等待只发生在搜索收尾阶段。

## LLM 模型调用

SettlementVerifier.prepare() 根据 FrozenSpec.strategy.verifier.scores 解析已注册 score key，
把完整规则冻结到 VerifierTask.score_contract。运行和重试只读取 task 中的冻结输入与规则，
不重新查询当前注册表，也不读取 Adaptive Search 状态。

Adapter 将固定质量 rubric、执行协议和附加 score instructions 放入可信指令，将 Settlement 的
精简投影放入不可信 evidence 区。Structured inference backend 强制返回动态严格 JSON Schema；
direct Pi 路径要求最后且唯一动作调用受限 Verifier 输出工具。Adapter 直接读取 inference payload，
不扫描普通 stdout 或 worker 自建 JSON。

质量评分区分达到用户阈值与继续优化，不引用未提供的最优纪录或从 diff 推断实测提升。
固定锚点为 0（核心目标已证实失败）、0.5（部分完成或存在实质缺口）、1（目标和验收有充分
证据支持，且无实质正确性或集成问题）；中间分值应说明具体缺口，不自行增加要求。
每项 rationale 建议少于 800 字符，严格上限仍为 1000 字符。Pi 在模型请求上下文中将 verifier
校验失败回执精简为有界的字段错误与重提提示，不回显整份参数；原始记录、成功提交和其他
工具错误保持原样。

结构化结果必须满足：

- quality_score 是 [0,1] 内有限数；
- scores 的字段集合与冻结规则完全一致，每项有限且在自己的范围内；
- score_rationales 与 quality_score 加全部附加 score 一一对应；
- diagnostics 启用时，findings 的结构与公开引用合法；
- 不允许缺字段、额外字段、非有限值或静默默认值。

执行 adapter 核对 task 所属 run、FrozenSpec、harness 和 artifact provider，再按冻结的
runtime_provider 分流，不从环境变量或模型输入推断后端。
Git/Copy 的 direct 调用保留 Codex native / Pi outer 隔离；direct Pi 使用 scope 中记录的
Main model/thinking，没有独立 model override。

ThinkThread 每次评估启动独立的 fresh private Child，使用 runtime-owned S0 与 self Profile，
继承 Root 模型配置，仅授予 Message capability。工具限定为
`goal_plus_thinkthread_verifier_input` 和 `goal_plus_thinkthread_verifier_submit`；
没有 candidate、Annotation、文件访问或命令工具。输入通过分块 Message RPC 返回，评分仍由
同一 owner 按冻结 schema、score contract 和当前 lease 验收，不解析普通模型文本。
重复请求复用原回包，冲突提交拒绝；spawn 回包丢失时观察原 Child，不重新 spawn。
完成、超时或取消后确认原 Child 停止并释放 private branch；清理结果未知时拒绝后续模型调用。
执行 journal 位于 `runs/<run_id>/verifier/executions/`，保留准确 Child/Message 和 attempt 身份。
进程中断后的未释放资源不会自动重开新评估；必须由宿主确认清理后再开始新 run。
沙箱依赖缺失、身份不符或清理失败不是可自动重试的模型错误；不改变 hard settlement。

调用观测记录 agent_harness=codex|pi、runtime_provider=direct|thinkthread；ThinkThread
另外记录 native_session_id 和 native_invocation_id，不将 Child 调用标记为 direct。

direct Verifier 每次调用另存独立的 no-session 推理记录，按 run、candidate、iteration、
settlement、task、attempt 和 invocation 关联到 worker 结算；记录完整输入、可观测事件、
原始输出和验收结果。Pi 额外保存每轮 provider 请求快照；Codex CLI 无法提供该快照，
页面明确标注观测边界。与 Annotator 共用 `host-logs/inference-transcripts/` 采集格式，
由 agent-transcript 导出器合入 `index.html`；诊断失败不改变评分或重试。
Pi provider 请求先写入本次推理的 sandbox 临时目录，由宿主归档，不增加 runtime 写权限。
完整内容仅在宿主进程设置 `GOAL_PLUS_DEBUG=1` 时采集；仅把变量写进
prompt 不会改变宿主环境。
ThinkThread Verifier 同样记录本次 prepared input、结果与候选/迭代关联，但明确标记它们不等于
完整 provider 请求。Pi Child 的实际上下文由原生 session Message 归档保留，并按 Verifier 角色
关联到同一 task/attempt，在 `index.html` 的候选评估分组中展示；删除 Child 前补收归档。
缺少原生归档时不能宣称完整上下文已采集；Codex Child 的原生归档尚未覆盖。
先前 schema 8 Task 的摘要字段只在读取时兼容，后续写入回到 schema 7；旧摘要不补造
历史完整上下文。

## VerifierRecord 设计和 Adaptive Search 接口

~~~text
VerifierRecord
  schema_version
  record_id / task_id / queue_sequence
  outcome: completed | failed
  error | null
  initial_artifact_ref / parent_node_id / allocation_depth / replaces_candidate_id
  subject / score_contract_hash
  quality_score / scores | null
  evidence_refs / score_rationales / steering_findings | null
  invocation / usage / created_at
~~~

成功 Record 带完整评分；失败 Record 无评分、rationale 和 findings，并带 error。未选择附加指标时
scores=null。Record 通过 task_id 绑定冻结 contract，只保存 score_contract_hash 做完整性校验；
它不携带 Allocation/Steering policy identity，也没有 score version。Task/Record 当前使用
schema 7，score contract 使用 schema 2，旧 policy 绑定记录不隐式迁移。

Verifier 到 Adaptive Search 的唯一业务交付入口是：

~~~text
FileSearchRuntime.accept_verifier_record(record, execute=..., steering_host=...)
~~~

接口只传不可变终态 Record。Verifier 不导入 Adaptive 内部模块，不读取 Snapshot、Decision、
candidate 状态或执行锁。Adaptive 验证自己需要的 key、更新 Snapshot 并产生确定性决策；执行
结果不回写 Record，也不会触发重新评分。具体见
[VerifierRecord 耦合设计](adaptive-search.md#verifierrecord-耦合设计)。

## 新增评分接口和配置

[domain/verifier.py](../src/goal_plus/domain/verifier.py) 是评分语义的唯一 owner：

- VERIFIER_SCORE_RULES 注册附加 score；
- VerifierSpec.scores 配置要启用的唯一 score key；
- verifier_score_contract(score_keys) 解析、排序并冻结完整合同。

每个 score key 对应唯一、稳定的语义和量纲。指标本身没有版本；需要改变语义时必须注册新 key。
score_contract_hash 绑定 task 使用的整份规则，不是 score 身份或版本。Policy 可以版本化，
但只能声明所需 key，不能改写评分语义。

| key | 范围 | 语义 |
| --- | --- | --- |
| progress_reward | [-1,1] | exact attempt 相对直接 comparison base 已经取得的进展 |
| allocation_utility | [0,1] | 从 exact attempt 继续探索一步的局部价值 |
| correctness_gap | [0,1] | 从没有已显示缺口到明确违反核心验收 |
| validation_gap | [0,1] | 从验证覆盖声明到核心声明缺少相关验证 |
| local_confounder_evidence | [0,1] | 局部实现或测量错误妨碍判断的证据强度 |
| discriminating_test_gap | [0,1] | 尚未有效检验的具体可证伪问题强度 |
| boundary_evidence_strength | [0,1] | 支持明确适用边界的实验或反例证据强度 |

基础 quality_score 是保留 key，不出现在 VerifierSpec.scores 中。典型配置为：

~~~yaml
# 只用 hard_score，不运行 LLM Verifier
strategy:
  initial_context: fresh
  selection:
    ranking_keys: [hard_score]
    ranking_tolerance: [0.0]
~~~

~~~yaml
# 显式启用 Verifier，并选择与 Allocation 无关的附加评分
strategy:
  initial_context: fresh
  verifier:
    scores: [progress_reward]
~~~

~~~yaml
# 在 Git worktree SearchSpec 中，policy 自动补齐所需评分
strategy:
  initial_context: fresh
  adaptive_search:
    allocation:
      policy: swe_search_uct/v1
~~~

若 Selection 包含 quality_score，配置层会自动补齐 verifier: {} 并冻结；即使未启用任何 Adaptive
policy，也会产生 Selection 所需的质量评分。hard_score 来自硬验证，不由 LLM 重复评分；仅用
hard_score 且没有 policy/verifier 配置时，LLM-Verifier 关闭。显式 verifier: {} 始终启用基础评估。
附加 score 可独立用于两种搜索模式，启用评分不等于启用 Allocation
或 Steering。选择 Allocation/Steering policy 后，公共配置层自动合并其 required_score_keys，
保留额外配置的评分；verifier 或 scores 缺省也能补齐。补全后的 key 列表写入 FrozenSpec，
Verifier 仍只读取独立评分配置。policy 参数沿用该版本默认值，用户可以只填写需要覆盖的字段。

因此 LLM-Verifier 的启用条件为：已配置 Allocation、Steering、需要质量评分的 Selection，
或显式 Verifier，任一满足即可；关闭时无需创建评估任务。Selection 的 quality_score 保留在
Record 顶层，不重复加入附加 scores。

新增评分时：

1. 在 VERIFIER_SCORE_RULES 中增加 VerifierScoreRule，使用新的唯一 name，定义范围、
   instructions 和是否要求 comparison base；不要改变已有 key 的含义。
2. 用户在 strategy.verifier.scores 中选择该 key。Python、MCP 与 Pi 使用同一 schema，不需要
   修改队列、模型调用或 Allocation。
3. 只有新增决策消费者时，才在对应 policy spec 中声明 required_score_keys 并实现确定性策略。

选择任一诊断指标时，contract 还会冻结 search_diagnostics/v1 findings schema 与 instructions。
steering_findings 最多各包含一个 hypothesis、confounder、open question 和 boundary，引用只能
来自当前输入的闭合集合。诊断仍是评估事实，不是 action proposal；消费方式见
[Steering](adaptive-search.md#steering)。

当前注册扩展是插件源码扩展；运行配置只能选择已注册 key，不接受任意提示词或动态 Python
导入。新增 key 应测试无 Allocation 的完整评估、结构化输出校验和消费者 key 匹配。

## Selection 设计

Selection 是独立的确定性评分消费者，不属于 LLM 推理，也不属于 Adaptive Search。它在搜索
收尾阶段从 hard-settled options 中选择 winner，随后仍需 exact artifact/evidence 验证和 promotion。

~~~text
run stop condition
  -> wait for required score sources to become terminal
  -> resolve exact candidate pool
  -> deterministic tolerance Selection
  -> exact artifact/evidence validation
  -> promotion
~~~

Application 从 FrozenSpec + SettlementReceipt 生成 SelectionSubjectKey。启用 quality_score 时，
ScoreSource 只接受完整权威链：option 的 subject hash 匹配 task；task 已完成且 output pointer
指向唯一 Record；Record 的 task_id 和完整 subject 再次匹配。source 只有：

~~~text
pending | unavailable | value(finite number)
~~~

必要 source 为 pending 时进入 readiness wait；task 最终失败、分数缺失或身份不匹配时，精确
option 被排除。过滤后为空则进入 selection_blocked，不会修改 FrozenSpec 或临时降级。hard-only
Selection 没有 Verifier readiness wait。

ranking_keys 与 ranking_tolerance 必须非空、等长、key 不重复，容差为有限非负数。算法按配置
顺序逐级收窄：

~~~text
survivors = candidate_pool - rejected_subjects
for key, tolerance in configured_order:
  best = directional_best(survivors, key)
  survivors = values within inclusive tolerance of best
winner = prefer previous hard best, then preserve original option order
~~~

外部 host 若需要使用 Jev 或 LLM-as-a-Verifier 在 hard-score 并列项中点名候选，
只能通过可信 runtime 的 `select_for_controller()` 提交带 receipt hash 的精确
`ControllerSelectionDirective`。它不会改变 ranking、hard evidence 或 promotion
gate；指定 subject 若不在冻结 ranking survivors 中，或 artifact 重验失败，Selection
直接进入 `selection_blocked`。该接口不作为 MCP/Pi 模型工具发布。

quality_score 固定 maximize；hard_score 使用 SearchSpec 的 metric_direction。Selection 只保存
winner、每级 threshold、survivor count、排除原因和 evidence source。Winner 恢复后仍校验 exact
artifact 与 hard evidence；必要重验失败时排除该 subject，并在剩余候选上重放同一算法。

实现位于 [application/selection.py](../src/goal_plus/application/selection.py) 与
[domain/selection.py](../src/goal_plus/domain/selection.py)。评估队列、Selection 等待和
promotion 各自保持独立状态，不用一把协调锁串联。

## 模块边界和验证

| 组件 | 核心职责 | 不拥有 |
| --- | --- | --- |
| Settlement | 当前 attempt 的事实、身份、diff 与 submission | 历史评分、模型重试 |
| Verifier workflow/queue | FIFO、推理、重试、终态 Record 与交付 | 搜索决策、candidate 修改 |
| LLM Verifier | 按冻结 rubric 评估局部输入 | Adaptive internals、全局历史 |
| Selection | 终态评分 readiness 与确定性 winner 选择 | 模型调用、Allocation |
| AdaptiveSearch | 消费 Record 并作搜索过程决策 | 评分规则和 Selection |
| Monitor/report | 只读投影 | claim、恢复或驱动队列 |

测试应围绕完整主流程和边界：一次调用产生完整评分、FIFO 只交付终态、失败不泄露部分分数、
rollback 不改变 attempt 身份、Record 交付幂等、Selection 使用精确权威评分链。低概率组合只在
出现真实风险时增加针对性测试，不建立额外协调框架。
