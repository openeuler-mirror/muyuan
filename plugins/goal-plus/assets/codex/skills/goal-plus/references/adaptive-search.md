# Codex Adaptive Search

只在 SearchSpec 设置 `strategy.orchestration_mode="adaptive_search"` 时使用。该模式要求非空
`strategy.search_scheduler`、`workspace.backend="git_worktree"`，以及 runtime 已注册的 reward
和 allocation policy；具体字段以 SearchSpec schema 为准。`budget.max_parallel` 限制实时 live
candidate，`budget.max_candidates` 可限制累计 candidate。

Runtime 在 hard verifier settlement 后负责 annotation、ScheduleTask、reward、两轮 lazy
allocation 确认、Decision 校验和 candidate fence。worker prompt 不承载这些内部状态机；最终
selection 和 promotion 始终只使用 hard verifier Evidence。

## Main 流程

1. 按公共 Search 流程启动并绑定初始 worker。
2. worker 返回的 `allocation_decision` 为空时，继续公共停止/continuation 流程；
   `scheduler_status=pending` 不需要等待或轮询。
3. Decision 非空时，不再 continue 其中已退休的 candidate；结束仍 live 的对应 worker，并等待
   replacement 所需 slot 释放。
4. 调用 `search_list_allocation_decisions(status="pending")`，再对每份 Decision 调用一次幂等的
   `search_apply_allocation_decision`。只执行 runtime 返回的来源、方向、模型与预算，不改写计划。
5. 对 apply 返回的每个派生 session，使用其 host-native launch payload 启动 worker，并用真实
   handle 完成 `search_bind_agent_handle`。必须启动全部返回项，且 live 总数不超过
   `max_parallel`。
6. 返回公共等待、continuation 和最终 drain 流程。scheduler 或 annotation 失败不撤销已结算
   Evidence，也不改变 hard score 排名。
