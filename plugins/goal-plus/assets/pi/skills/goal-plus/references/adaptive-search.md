# Pi Adaptive Search

只在 SearchSpec 设置 `strategy.orchestration_mode="adaptive_search"` 时使用。该模式要求非空
`strategy.search_scheduler`、`workspace.backend="git_worktree"`，以及 runtime 已注册的 reward
和 allocation policy；具体字段以 SearchSpec schema 为准。`budget.max_parallel` 限制所有 pool
中的实时 live candidate，`budget.max_candidates` 可限制累计 candidate。

Runtime 在 hard verifier settlement 后负责 annotation、ScheduleTask、reward、两轮 lazy
allocation 确认、Decision 校验和 candidate fence。worker prompt 与 Pi supervisor 不承载这些
内部状态机；最终 selection 和 promotion 始终只使用 hard verifier Evidence。

## Main 流程

1. 按公共 Pi Search 流程启动初始 pool。
2. `candidate_ready` 后调用 `search_list_allocation_decisions(status="pending")`。没有 Decision 时，
   继续公共停止/continuation 流程；pending scheduler task 不需要等待或轮询。
3. 有 Decision 时，不再 continue 其中已退休的 candidate；等待对应 active job 终止，并通过
   pool snapshot 确认 replacement 所需 slot 已释放。
4. 对每份 Decision 调用一次幂等的 `search_apply_allocation_decision`。只执行 runtime 返回的
   来源、方向、模型与预算，不改写计划。
5. 用 apply 返回的全部 candidate ids 创建新 `pi_search_pool_open`，让 pool 启动并绑定整批
   derived worker；不要拆分一份 Decision，所有 pool 的 live 总数不得超过 `max_parallel`。
6. 保存新 pool id，返回公共 wait/snapshot/close 流程。scheduler 或 annotation 失败不撤销已
   结算 Evidence，也不改变 hard score 排名。
