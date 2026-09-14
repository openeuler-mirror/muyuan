# Codex Adaptive Search

只在 SearchSpec 设置 `strategy.orchestration_mode="adaptive_search"` 时使用。该模式要求
`workspace.backend="git_worktree"` 和 `strategy.allocation`，并自动启用 LLM-Verifier。Policy 版本
同时管理 allocation 规则和追加的评分提示词。`budget.max_parallel` 限制实时 active candidate，
`budget.max_candidates` 可限制累计 candidate。

Runtime 在 hard settlement 后异步生成不可变 Verifier records，再用确定性 policy 生成
AllocationDecision。LLM 不选择 winner、不返回 allocation flag，也不产生可执行 Decision。

## Main 流程

1. 按公共 Search 流程启动并绑定初始 worker。
2. candidate 返回后调用 `search_list_allocation_decisions(status="pending")`；没有 Decision 时按公共
   continuation 继续；Verifier backlog 不阻塞 worker。
3. 有 Decision 时，立即对其中仍 live 的 candidate 调用原生 `interrupt_agent`；成功回执即为中止
   请求已被宿主接受，不发送 soft closeout，也不等待或复查 worker 是否已经退出。
4. 对每份 Decision 调用幂等的 `search_apply_allocation_decision`，不要改写 source、direction 或预算。
   Codex hook 会按 candidate 精确拒绝尚未收到中止接受回执的 apply，并返回必须中止的 target。
5. 对 apply 返回的全部派生 session，按 host-native launch payload 启动 worker，并用真实 handle
   完成 `search_bind_agent_handle`；live 总数不得超过 `max_parallel`。
6. 回到公共等待、continuation 和 final selection。Verifier 或 Annotation 失败不撤销 hard Evidence；
   最终选择严格使用冻结的 `ranking_keys` 与容差，不做运行时降级。
