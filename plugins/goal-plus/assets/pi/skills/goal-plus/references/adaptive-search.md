# Pi Adaptive Search

只在 SearchSpec 设置 `strategy.orchestration_mode="adaptive_search"` 时使用。该模式要求
`workspace.backend="git_worktree"` 和 `strategy.allocation`，并自动启用 LLM-Verifier。Policy 版本
同时管理 allocation 规则和追加的评分提示词。`budget.max_parallel` 限制所有 pool 中的实时 active
candidate，`budget.max_candidates` 可限制累计 candidate。

Runtime 在 hard settlement 后异步生成不可变 Verifier records，再用确定性 policy 生成
AllocationDecision。LLM 不选择 winner、不返回 allocation flag，也不产生可执行 Decision。

## Main 流程

1. 按公共 Pi Search 流程启动初始 pool。
2. `candidate_ready` 后调用 `search_list_allocation_decisions(status="pending")`；没有 Decision 时按公共
   continuation 继续；Verifier backlog 不阻塞 worker。
3. 有 Decision 时立即调用幂等的 `search_apply_allocation_decision`，不要改写 source、direction 或预算。
   Pi extension 会先向 Decision 中每个 active candidate 发送一次中止请求，收到宿主接受回执后才把 apply
   转发给 Runtime；不等待 worker 自然结束，也不关闭 sibling 或 pool。
4. 用 apply 返回的全部 candidate ids 创建新 pool，启动并绑定整批 derived worker；不要拆分一份
   Decision，所有 pool 的 live 总数不得超过 `max_parallel`。
5. 保存新 pool id，回到公共 wait/snapshot/close 和 final selection。Verifier 或 Annotation 失败不
   撤销 hard Evidence；最终选择严格使用冻结的 `ranking_keys` 与容差，不做运行时降级。
