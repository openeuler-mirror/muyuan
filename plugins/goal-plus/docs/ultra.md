# Ultra 编排

Goal Plus 在 Codex 中通过 `$goal-plus`、在 Pi 中通过 `/goal-plus` 使用同一套
host-neutral 执行合同：

```text
host-selected main reasoning + proactive delegation + automatic Search routing + until terminal
```

Main agent 自己负责拆解任务、决定并行、等待、追问、重试、Search 路由、结果审查和最终集成。
host 负责原生 worker 的启动、等待、通信、中断和日志。Goal Plus 不要求执行计划或 work DAG，
只记录实际发生的 subagent 操作，并保护 attempt identity。

## Subagent 记录

Main 使用共同 `goal_plus_run_work_item` 派发普通任务。启动器通过 GoalDispatch 记录
`dispatch`；首次使用某个 `work_item_id` 时，从任务 summary 创建轻量记录：

```text
dispatch -> launching -> bind -> active -> result -> result_ready
```

`accepted` 表示 Main 已检查并采用结果；`rework` 表示继续要求同一个已绑定 worker 修正，
并产生新的 attempt/generation。这些记录不是全局计划，也不会阻塞无关工作。没有派发
subagent 的 Main-only 任务可以没有任何记录。

MCP controller 丢失后，已处于 `result_ready` 的工作项可再次调用同一工具返工。
启动器先核验准确 scope 资源已 finished/stopped，再 resume 原生 session 并新建 attempt；
资源仍活跃、记录缺失或原生会话无法恢复时明确拒绝或记录启动失败，不接管未知执行。

`dispatch` 只生成 attempt identity 和 launch TTL，不代表 worker 已启动。原生启动成功后
才能记录 `bind`；`bind`、`message`、`result` 和 `failed` 必须携带当前 `attempt_id` 与
`generation`。过期的 `launching` 可以重新派发；旧 generation 的迟到终态结果保留为
`stale_result` Evidence，但不能覆盖当前 attempt。

final check 和 `complete` 只会被当前仍处于 `launching` 或 `active` 的派发阻止。Main 仍需
审查 subagent 返回并运行整体测试。

## Search 路由

Search 继续使用自己的 SearchSpec、run、candidate、verifier 和 promotion 状态，不映射成
普通 subagent 记录。只有任务具备数值 metric、确定性 verifier、隔离 edit surface、多个
有价值 hypothesis 和足够 budget 时，Main 才进入 Search。

## Host 合同

Ultra host 使用 `goal-plus-ultra-v1` protocol。`spawn`、`wait` 和 `observe` 是必需能力，
`send` 和 `interrupt` 是可选的原生能力。每个 host 记录实际 reasoning 档位和约束责任方。

Codex/Pi 的共同 MCP 入口继承当前 Main 的模型与 reasoning/thinking 快照，分别启动
app-server/RPC session。工具等待本次结果，保留 session 供同一 work_item_id 的返工复用，
并按新 attempt/generation 记录结果；Main 检查后记录 accepted。MCP 关闭或 scope cleanup
负责关闭资源。新入口尚不自动接管断连后不可用的 native controller。
`deepseek-harness` 或其他 harness 使用自己的 worker API，并上报同样的轻量生命周期事件。

可选的 `orchestration` monitor 只是这些记录和事件的只读投影。它不启动、不等待、不通信、
不重试，也不监督 worker。字段见
[Monitor Feature Plugin](adaptive-search.md#monitor-feature-plugin)，host 细节见
[Codex](codex.md) 和 [Pi](pi.md)，工具合同见 [API](api.md)。
