# Codex Adaptive Search

只在准备冻结或恢复的 `FrozenSpec` 明确设置
`strategy.orchestration_mode="adaptive_search"` 时读取并执行本文件。该模式还要求
`strategy.search_scheduler` 非 `null`。`parallel_loops` 不允许配置 scheduler，不读取本文件、
不创建 `ScheduleTask`，也不执行 allocation 工具或创建质量驱动的替代 candidate。

## 配置

```yaml
budget:
  max_parallel: 4
  max_candidates: 8
strategy:
  orchestration_mode: adaptive_search
  search_scheduler:
    host: pi-rpc
    model: bench-openai/gpt-5.6-sol
    reasoning_effort: low
    timeout_seconds: 180
    reward: "evidence_llm_value/v3 @prompt rubric要关注本轮新增价值"
    allocation: value_guided_replace/v2
workspace:
  backend: git_worktree
```

`budget.max_parallel` 是实时 live worker 上限 `K`，由 runtime 自动推导给 scheduler。可选的
`budget.max_candidates` 是整个 run 的累计唯一 candidate 上限；`null` 表示不限制，有界值必须
大于等于 `K`。scheduler 的 host/model/reasoning/timeout 参数不继承 worker profile，超时和重试
使用内部策略。不设置单份 Decision 的 replacement 或派生深度配置项；scheduler 自主选择字段和
实际派生数量，但仍受当时可用并发槽位约束，并在有界时受累计剩余额度约束。

`reward` 和 `allocation` 使用 `name/vN [@prompt 补充说明]` 引用 runtime 注册的准确策略版本。
runtime 在 search 开始前把解析后的完整策略快照写入 FrozenSpec。配置不接受函数名或脚本路径，
runtime 和 scheduler 都不执行外部策略代码。

内置策略仅保留 `evidence_llm_value/v3` 和 `value_guided_replace/v2`。前者按公开结果增量、
新增 Evidence 信息和新增方向计算边际 reward；后者在当前回退、连续无新增价值或 peer 方向重复时
请求替换，并在确认阶段从高价值历史节点派生尚未覆盖的方向。删除的旧版本号不会复用。
每个策略版本的 `build_context` 通过统一入口提供 scheduler 所需上下文；普通调用只注入 allocation
pruning 规则，确认调用才注入 derivation 规则。

## Scheduler 契约

每次 verifier 完成 hard settlement 后，runtime 先持久化 `AnnotationTask`，等其 completed 或
terminal_error 后再把精简 Evidence View 投影到 `ScheduleTask`。随后可恢复的异步 scheduler
worker 做一次无 session 推理，按 reward -> pruning -> optional derivation 返回 reward、UCT
分解、布尔 `allocation_flag`，以及确认 task 再次返回 `true` 时的独立 Decision 草案。

scheduler 只读取准确 commit/diff、公开 verifier Evidence、当前 candidate 的 ScheduleTask reward
历史和冻结策略。模型输入分为 `scheduler_context`、`reward_context`、`allocation_context`；前者
由 scheduler 维护，后两者由所选 policy 的 `build_context` 经统一入口生成。普通调用只读取策略
所需的候选统计或精简 peer frontier；只有确认 task 额外读取全 candidate reward-bound 节点。

`ScheduleTask` 按 candidate iteration 有序消费，不同 candidate 可并发。超时和重试采用内部
策略；进程中断后，pending、running 和 retry_wait task 可由新的 worker 恢复。
`ScheduleTask.output` 是 reward/flag 的唯一权威记录；runtime 通过只读 API 从 completed
ScheduleTask 索引 reward，普通调用读取当前 candidate 历史，确认 allocation 时读取全 candidate
历史。不保存 value tree 或 candidate/report reward 副本，也不存在 run-root tree、graph、backup
event、action edge 或 hindsight 投影。

lazy allocation 使用上一已结算节点的 flag：若上一 flag 是 `false` 或尚未完成，当前 worker
不等待 annotation/scheduler 链；若上一 flag 是 `true`，runtime 直接把当前 settlement task 视为
确认任务并等待 AnnotationTask 和 scheduler 完成。只有当前 flag 再次为 `true` 且 plan 合法时，
runtime 才持久化 `AllocationDecision`。plan 来自独立的 `SchedulerOutput.decision`，包含全部退休
candidate、依据、预留派生 candidate id、精确来源节点、探索方向和可选 worker budget。
`allocation_flag`、lazy confirmation 和 Decision 输出条件属于 runtime 公共协议，不属于
allocation policy。scheduler 自主选择这些字段；worker 和父 agent 不计算 reward，不补写或改写
Decision。

AnnotationTask 是 ScheduleTask 创建的弱依赖：必须先终态，但 terminal_error 仍以 `view=null`
和历史补偿提示创建 ScheduleTask。任一侧失败不改变硬 score、已完成 settlement、最终选择或
promotion。`scheduler_status=pending` 且 `allocation_decision=null` 不是等待条件，worker 继续当前
循环且不轮询；此时 `schedule_task_id` 可以为空。`search_select` 和 promotion 始终只使用硬
verifier score；reward、UCT 和 flag 只服务 scheduler 调度。

## Decision 执行

每个终态 worker 完成公共 Search skill 的绑定和 verifier 检查后，先检查报告中的
`allocation_decision`：

- decision 为空时，执行公共 skill 的全局停止和同候选 continuation 流程。
- decision 非空时，其中的退休 candidate 已被 runtime fence，只禁止下一轮 iteration。不要恢复
  任一 retire action 对应的 worker。先结束其中仍 live 的 worker，等待全部 replacement 所需 live
  slots 释放，再调用 `search_list_allocation_decisions(status="pending")` 和幂等的
  `search_apply_allocation_decision`。
- apply 从 Decision 固定的 verifier-backed source commit 创建 workspace，继承 results ledger、
  全局 Evidence 可见性和 selected model，并为每个 replacement 返回 agent session 与 host-native
  launch payload。使用 `spawn_agent` 逐一启动并绑定所有返回 payload，不能只启动第一项；所有
  live worker 合计不得超过 `max_parallel`。只执行 runtime 返回的 Decision，不自行改写剪枝、
  来源、模型或预算。

decision 对外可见前，触发 iteration 的 Git/results ledger 已完成结算。runtime 已持久化的 scheduler
allocation retirement 会释放候选的最低 lease。reward/policy 错误
保持 fail-open，只记录 iteration；最终选择仍按硬 verifier score。完成本分支后返回公共 Search
skill 的等待、验证和最终 drain 流程。
