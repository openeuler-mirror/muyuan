---
name: search
description: 仅供 Goal Plus Main Agent 使用 goal-plus MCP 的内部 Search Mode 编排引擎；不适用于 Search candidate worker。
---

# Codex Search Mode

只在当前 Goal 已准备好高置信度 SearchSpec 时使用。Search runtime 拥有 FrozenSpec、candidate
workspace、verifier Evidence、selection 和 promotion；Codex host 拥有原生 worker 的启动、
等待、续跑和中断。直接调用当前 registry 中的原生 MCP tools。

## 建立 Run

1. 调用 `search_freeze_spec`，检查返回的 source、task skill 和 workspace capacity 结果；失败时
   修正合同，不绕过 gate。
2. 调用 `search_create`，并立即用准确的 `frozen_spec_id`、`run_id` 执行
   `goal_plus_link_search_run`。
3. 调用一次 `search_plan_next(requested_k=budget.max_parallel)` 和一次 `search_start_batch`，
   创建初始 candidate。普通 `parallel_loops` 不补 lane 或重新规划。

新 Codex run 使用 `worker_host="codex"`，通常使用 `git_worktree` 和 manual Global Evidence；
显式 command config 优先。只有目标明确要求 runtime reward/allocation，或恢复的 FrozenSpec 已
使用 `adaptive_search` 时，才完整读取
`../goal-plus/references/adaptive-search.md` 并执行其中的额外 host 流程。

## 驱动 Worker

对每个 candidate：

1. 调用 `search_start_agent_session`，把同一次响应中的 launch payload 映射到当前
   `spawn_agent` schema；使用返回的 `task_name`、`message` 和 `fork_turns`，不合成字段。
2. `spawn_agent` 成功并返回真实 handle 后，调用
   `search_bind_agent_handle(host="codex", ...)`。启动并绑定全部初始 worker 后再等待。
3. 使用无 target 的 `wait_agent` 等待事件，并用 `list_agents` 核对真实终态。按 launch 返回的
   budget control 执行 wait、soft closeout 和必要的 `interrupt_agent`；没有 live worker 时不等待。
4. worker 结束后优先复用与当前 artifact 精确匹配的 worker Evidence；仅在缺失时由 Main 调用
   `search_run_verifier`。需要诊断 host 行为时调用 `search_get_agent_observability`。
5. 全局停止条件未满足时，调用 `search_continue_agent_session`，再用 `followup_task` 恢复同一
   native worker，不附加 Main 偏好的技术方向。continuation lease 使用冻结上限与“外层剩余时间
   减收尾预留”的较小值；只有 native worker 无法继续时才用 `search_redispatch_candidate` 恢复
   同一 candidate/workspace。

worker 首次启动或 fresh session 恢复时会调用 `search_get_agent_context`，并以 runtime 返回的
`candidate_task` 为完整合同。Main 不复制 workspace、Evidence、shared_dir、supplemental、
Adaptive、回滚或 verifier 规则。普通低分、一次退化或 peer 领先都不是替换 worker 的理由。

## 收尾

达到原始目标的全局停止条件或时间收尾点后：

1. drain 或中断所有 live worker，确认全部终态。
2. 调用 `search_select` 和 `search_promote`。任务授权写回且 promotion 要求 apply 时，调用
   `search_apply_promotion`；artifact-only 结果保持 prepared。
3. 将结果交回 Goal Plus 主流程，由其调用 `goal_plus_record_search_result`。链接到 active Goal
   的 run 此时不调用 `search_report`。

如果 Evidence 证明 verifier 的覆盖、确定性、目标对齐或基础设施合同失效，调用
`search_invalidate_run`，停止 worker，修复合同并创建后继 run。新的技术假设、停滞或性能差异
仍属于当前 run。
