---
name: goal-plus-with-final-check
description: 运行 Goal Plus，并要求在完成前由独立 Codex 最终检查员进行检查。
---

# 带最终检查的 Goal Plus

对 `$goal-plus-with-final-check` 使用此 skill。
Codex 的 `UserPromptSubmit` hook 会在模型轮次开始前创建 Goal Plus 记录，并设置
`policy.final_check.mode="required"`。

它与 `$goal-plus` 使用同一角色模型语法：真正可选择的角色是
`annotator=B workers=C,D`。Codex 当前 session 本身就是 main；`main=` 只保留为
必须匹配当前 session model 的兼容断言。`models=` 是 `workers=` 的兼容别名。
具体解析和冻结规则由完整的 `goal-plus` skill 定义。

遵循完整的 `goal-plus` skill 工作流，并额外遵守以下强制终态契约：

1. 不要自行调用 `goal_plus_set_status(status="complete")`。
2. 完成实现和原始目标审计后，调用
   `goal_plus_prepare_final_check(goal_plus_id, checker_host="codex")`.
3. 将返回的 launch payload 映射到可用的前台 `spawn_agent` 工具。始终使用返回的
   `task_name`、`message` 和 `fork_turns`。只有当前 `spawn_agent` schema 明确列出
   `goal_plus_final_checker` 时才使用 `launch.agent_type`；安装版插件未发布该自定义角色时，
   必须使用 `launch.fallback_agent_type="default"`，不得自行换成其他角色。两条路径都必须
   保持 `fork_turns="none"` 和 launch message 中的独立只读审查契约。
4. 等待检查员返回。通过的检查会原子地完成 Goal Plus 记录。检查失败时必须修复每一项发现，
   然后申请新的检查。检查被中断时目标保持 active，也必须申请新的检查。
5. 读取 `goal_plus_status`，然后在停止前调用 `goal_plus_gate(event="stop")`。

`$goal-plus edit <完整的修订目标>` 会保留同一个 Goal Plus id，创建新的目标修订版，
并使所有旧检查失效。host 中断后，`$goal-plus resume` 会继续当前持久化修订版。
