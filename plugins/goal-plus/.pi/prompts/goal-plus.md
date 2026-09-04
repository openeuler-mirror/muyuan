# Goal Plus Prompt Compatibility

此文件只是 Pi prompt 兼容入口。只有原生 extension 已通过精确 `/goal-plus`、
`/goal-plus edit`、`/goal-plus resume` 或 `/goal-plus-with-final-check` 将当前 session 绑定到
active `goal_plus_id` 时才继续。`summary/pause/resume/clear` 只能由精确 `/goal-plus` host control 执行；
每轮读取 `goal_plus_status` 的 `control` 与 `reconciliation`。

加载 `/skill:goal-plus`，以 runtime 注入的当前 Goal、revision、`raw_goal`、`command_config`
和 `next_action` 为权威状态：

$ARGUMENTS

遵循 skill 中的 Main 成功路径和工具 schema；确定性边界由 runtime 执行。
