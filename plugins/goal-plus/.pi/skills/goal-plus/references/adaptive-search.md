# Pi Adaptive Search

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

`budget.max_parallel` 是所有 pool 合计的实时 live worker 上限 `K`；runtime 自动从
`budget.max_parallel` 推导 scheduler 并发上限。可选的 `budget.max_candidates` 是整个 run 的累计
唯一 candidate 上限；`null` 表示不限制，有界值必须大于等于 `K`。scheduler 的
host/model/reasoning/timeout 参数不继承 worker profile，超时和重试使用内部策略。不设置单份
Decision 的 replacement 或派生深度配置项；
scheduler 自主选择字段和实际派生数量，但仍受当时可用并发槽位和累计剩余额度约束。

`reward` 和 `allocation` 使用 `name/vN [@prompt 补充说明]` 引用 runtime 注册的准确策略版本。
runtime 在 search 开始前把解析后的完整策略快照写入 FrozenSpec。配置不接受函数名或脚本路径，
runtime 和 scheduler 都不执行外部策略代码。

内置策略仅保留 `evidence_llm_value/v3` 和 `value_guided_replace/v2`。前者按本轮公开结果、Evidence
信息和探索方向的新增价值评分；后者按回退、停滞和 peer 重复提前释放 lane，并在确认阶段派生未覆盖
方向。删除的旧版本号不会复用。每个策略版本的 `build_context` 通过统一入口提供 scheduler 所需
上下文；普通调用只注入 allocation pruning 规则，确认调用才注入 derivation 规则。
backup/expansion 分别归入 reward/allocation。

## Scheduler 契约

每个 verifier hard settlement 先持久化 `AnnotationTask`，其终态后才把精简 Evidence View 投影到
`ScheduleTask`。随后由可恢复、可并发、一次性无 session 的 scheduler 调用按 reward -> pruning ->
optional derivation 计算 reward、更新所需 UCT 分解并生成布尔 `allocation_flag`。

任务按 candidate iteration 有序消费，不同 candidate 可以并发；worker 重启后继续
pending/running/retry_wait task。`ScheduleTask.output` 是 reward/flag 的唯一权威记录；runtime
通过只读 API 从 completed ScheduleTask 索引 reward，普通调用读取当前 candidate 历史，确认
allocation 时读取全 candidate 历史。系统不保存 value tree 或 candidate/report reward 副本，也不
维护 run-root tree、graph、backup event、action edge 或 hindsight。

lazy allocation 根据上一已完成节点的 flag 决定 runtime 是否阻塞当前 verifier response：上一 flag
为 `false` 时不阻塞；上一 flag 为 `true` 时，runtime 直接把当前 task 视为确认任务并等待。只有
当前 flag 再次为 `true` 且独立的 `SchedulerOutput.decision` 合法时，runtime 才持久化标准
`AllocationDecision`，其中包含全部退休 candidate、依据、预留派生 candidate id、精确 settled
source node、探索方向和可选 worker budget。普通异步 task 不读取全 candidate reward 历史，也不
生成 Decision 草案；只有确认 task 获得全 candidate allocation context。reward、flag 和 Decision
只由 scheduler 产出，runtime 负责校验和持久化，host 只消费持久化结果。

AnnotationTask 是 ScheduleTask 创建的弱依赖：必须先终态，但 terminal_error 仍以 `view=null` 和
历史补偿提示创建 ScheduleTask。任一侧失败不改变硬 score、settlement、selection 或 promotion。
`scheduler_status=pending` 且 `allocation_decision=null` 表示本次 response 没有调度 Decision；
此时 `schedule_task_id` 可以为空。

## Decision 执行

每个 `candidate_ready` 完成公共 Goal Plus skill 的 verifier 检查后，调用
`search_list_allocation_decisions(status="pending")`：

- 没有 allocation decision 时，执行公共 skill 的全局停止 policy；若其为 false，则使用适合剩余
  时间的预算对准确 `candidate_id` 调用 `pi_search_pool_continue`。
- 有 decision 时，不得 continue 任一 `retire_candidate` action 对应的 worker。其中尚有 active pool
  job 时，不得提前启动 replacement；继续通过准确 pool snapshot/event 等待 host-owned slot 释放。
- slot 释放后，对该 decision 调用一次幂等的 `search_apply_allocation_decision`。只执行返回内容，
  不自行改写剪枝、来源、模型或预算。apply 从固定的 verifier-backed source commit 物化 workspace，
  继承 results ledger、run-global Evidence 可见性和 selected model，并创建 native session
  provenance。
- 统计所有已记录 pool 的 active jobs。仅当可用 slot 足以容纳本 decision 返回的全部 candidate 时，
  使用一次
  `pi_search_pool_open(candidate_ids=[...all returned candidate ids...], max_parallel=<returned count>, final_verify=true)`
  原样启动整批，不能只启动第一项或把一份原子 decision 拆成选择性子集。保存新 `pool_id` 并同等
  wait/snapshot/close。

decision 对外可见前，触发 iteration 的 Git/results ledger 已完成结算，retirement 只禁止下一轮
iteration。不得恢复已退休 candidate，也不得增加手动 pool submit 工具或让 supervisor 自动补位。
reward/policy 错误保持 fail-open，只记录 iteration；`search_select` 始终按硬 verifier score，而不是
reward。完成本分支后返回公共 Goal Plus skill 的 pool 等待、验证和最终 drain 流程。
