---
name: goal-plus-with-final-check
description: Internal Goal Plus plugin skill for an already-bound Goal Plus Main Agent to run an independent final check. Not for Search candidate workers; do not invoke manually.
---

# 带最终检查的 Goal Plus

对 `$goal-plus-with-final-check` 使用此 skill。Codex host 会在模型轮次前创建 Goal Plus 记录并将
`policy.final_check.mode` 设为 `required`；从当前 session 绑定的 `goal_plus_id` 继续。

遵循完整的 `goal-plus` skill。实现和原始目标审计完成后：

1. 调用 `goal_plus_prepare_final_check(checker_host="codex")`。
2. 将返回的 launch payload 原样映射到前台 `spawn_agent`，保持 `fork_turns="none"`；使用 schema
   支持的 checker agent type，否则使用返回的 fallback。
3. 等待 checker 自行调用 `goal_plus_submit_final_check`。Main 不代交结论；失败或中断时处理发现
   并申请新检查。
4. 读取 `goal_plus_status`，并遵循停止 gate。通过的 required check 会原子完成当前 revision。

`$goal-plus edit` 会创建新 revision 并使旧检查失效；原生 Codex session 恢复后，用
`$goal-plus resume` 继续当前 revision。
