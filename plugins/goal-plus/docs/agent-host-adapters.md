# Agent Host Adapters

适配器负责把 runtime 的启动和续轮请求转换为 host 原生的 worker 操作。Search runtime 本身保持不变；[共享平面](shared-plane.md)定义共享循环和所有权边界。

## 通用契约

`src/goal_plus/agent_pool.py` 定义了 `HostPoolContract` 和终态 `WorkerPoolEvent`。每种 host 都需要声明：

| 字段 | 契约问题 |
|---|---|
| `launch_mode` | 启动是否立即返回？ |
| `wait_mode` | parent agent 能否在任意 worker 完成时被唤醒？ |
| `continuation_mode` | 复用同一 worker，还是把状态重新派发到 fresh session？ |
| `deadline_mode` | 哪个 host 组件负责执行运行时限？ |
| `recovery_mode` | 如何重新发现仍在运行的 pool？ |
| `completion_stage` | candidate 在什么阶段可以安全地交给 parent agent 评估？ |

适配器还会返回权威的启动字段。main agent 只投影当前 host 工具 schema 支持的字段；对 Codex 而言，应以当前 `spawn_agent` schema 为准，不能假设可选元数据一定存在。

## 维护中的能力矩阵

| 能力 | Codex | Pi RPC |
|---|---|---|
| 启动 | 异步 `spawn_agent` | 分离的本地 supervisor + 前台 Pi 子进程 |
| 等待模式 | `wait_agent` 任意事件唤醒 + `list_agents` | `pi_search_pool_wait_any` |
| 续轮 | 通过 `followup_task` 复用同一 worker | 在新进程中恢复同一 native session |
| Deadline | 每次派发由 parent watchdog 管理 | 累计 pool lease + Pi 进程 watchdog |
| 恢复 | 原生 agent 注册表 + `.gp` | 持久化 `.gp/host-pools/pi/` + `.gp` |
| Goal gate | `UserPromptSubmit`、`SessionStart`、`PreToolUse`、`PostToolUse`、`Stop`、`SubagentStop` | extension 的 input/tool/turn 事件 |
| 策略覆盖 | 初始并行循环 | 初始并行循环 |
| 模型发现 | Codex app-server `model/list` | Pi RPC `get_available_models` |
| 归一化 observability | native session JSONL + 已绑定元数据 | `pi_metrics` + 已绑定元数据 |

所有适配器都实现只读的 `collect_observability` 契约，并通过 `search_get_agent_observability` 暴露。它只用于 provenance 和诊断，不会把 worker lifecycle 状态写入 Search record，也不会让 runtime 承担 supervisor 职责。

适配器还通过 `goal_plus_list_models` 提供只读模型发现。发现结果只表示 host 当前公布了哪些模型，不保证每个目录项都能通过所有版本的 host launch tool 转发。用户仍然通过 `strategy.models` 选择模型；派发时，权威 launch payload 会投影到实际 host tool schema。没有请求模型时，Goal Plus 跳过发现并保留 host 原生的默认模型继承行为。

Codex rollout JSONL 会提供每次响应的 input、cached-input、cache-write、output 和 reasoning token 用量，但不提供实际计费美元金额。因此，Goal Plus 使用 `src/goal_plus/codex_pricing.py` 中带版本的、与 Pi 兼容的 OpenAI Codex 模型目录，根据每个 `last_token_usage` 事件计算 `usage.cost_usd`。长上下文分层以及 `flex`/`priority` 倍率遵循 Pi 语义。该结果是按 API 模型费率得到的估算，不是观测到的 ChatGPT 订阅费用；`usage.cost_estimate` 会保留目录版本、覆盖率和计费说明。未知模型的 `cost_usd` 保持不可用，不套用猜测费率。

当前接受的初始 planner 为 `agent_guided`/`agent`/`default` 和 `random`/`random_mode`。

## 并行循环

Codex 和 Pi 都满足异步 wait-any 语义：

- **Codex**：只启动一次初始 candidate 集合，等待任意 mailbox 更新，再通过 `list_agents` 找到所有新进入终态的 worker。它复用准确的 worker Evidence；只有缺少匹配 Evidence 时才运行 parent verifier。只要全局停止条件尚未满足，就通过 `search_continue_agent_session` 和 `followup_task` 继续同一 worker。
- **Pi**：持久化 pool/job 状态；存在最小 lease 时，会在同一个 job 内自动恢复提前结束的 native turn，并且绝不自动补充新 candidate。只有最小 lease 已满足且 durable Evidence 已落盘时才返回 `candidate_ready`；lease 耗尽但未满足时返回 `timed_out`。每次收到 candidate-ready event 后，只要全局停止条件尚未满足，main agent 都会对同一 candidate 调用 `continue`。Pi 会在新进程中重新加载同一 native session。

新的 Pi/Codex spec 使用 `orchestration_mode="parallel_loops"`；唯一一次初始规划会创建持久 candidate loop。两种适配器都不会把这次规划变成 completion barrier。低分或没有提升都不会触发 candidate 替换。

## Worker 预算

| Host | 必需控制项 | 执行方式 |
|---|---|---|
| Codex | `worker_budget.max_runtime_seconds` | 首次等待、一次收尾消息、最后等待、interrupt |
| Pi RPC | `worker_budget.max_runtime_seconds` | 收尾 steer + 进程硬 watchdog |

对 Codex 和 Pi 而言，`max_turns` 只是一项 prompt 提示。`max_parallel` 唯一决定初始 candidate 数和活跃 worker 数，因为后续工作会继续这些既有 candidate。

Codex 通过 `worker_budget.min_runtime_seconds` 和 `min_verifier_runs` 支持单 worker 的 AutoResearch 最小 lease。`SubagentStop` hook 会继续同一 child agent turn，直到达到下限；`max_runtime_seconds` 仍是独立的 parent watchdog 上限。适配器要求 lease 在 parent 软收尾前释放，避免两项控制发生竞态。Pi 暴露相同字段，其 worker role 的 `agent_end` handler 会在收尾前继续同一 Pi 进程和 native session。本地 host pool supervisor 仍是崩溃恢复边界，并跨进程恢复累计执行 lease。每次恢复只获得剩余的最大运行时间；基础设施故障以及 pool/外层收尾都会终止 lease。

`strategy.worker_launch` 携带可选的 host 启动偏好。Codex 在 host 支持时映射 `model`、`reasoning_effort` 和 `service_tier`；Pi 通过可信进程配置映射模型和 thinking level。这些值不属于 Search 状态。

## 恢复与 Handoff

状态级重新派发是可移植的恢复路径：

1. 为现有 candidate 调用 `search_redispatch_candidate`；
2. 在同一 workspace 启动新的 `agent_session_id`；
3. worker 重新读取 `search_get_agent_context`；
4. 使用 candidate-local Git 状态、verifier iterations、`research_summary` 和窄 `search_get_global_evidence` 视图恢复，不再依赖旧 transcript。

Codex 原生支持同 worker 续轮。Pi 支持跨进程的 native session 续轮：每次派发使用新 PID，但保留相同的 native session、runtime `agent_session_id`、candidate 和 workspace。对于不支持恢复 native session 的 host 或旧 record，状态级重新派发仍是可移植的后备方案。

每份 worker handoff 都应说明最重要的工作、verifier-backed feature entries、blockers、next steps，以及最多五条带适用范围和条件的 pitfalls。Candidate-local pitfalls 只在当前 candidate 内有效；feature-family pitfalls 只有在机制和条件匹配时才可迁移。main agent 确认前，verifier concerns 始终只是 advisory。

## 已确认的 Verifier 失效

Runtime fence 与 host 无关；如何证明静默状态则由适配器决定：

| 步骤 | Codex | Pi RPC |
|---|---|---|
| Fence | `search_invalidate_run` | `search_invalidate_run` |
| 停止活跃 worker | 对每个活跃 candidate 调用 `interrupt_agent` | `pi_search_pool_close(mode="interrupt")` |
| 证明已静默 | 通过 `list_agents`/`wait_agent` 等到全部终止 | 通过 snapshot/wait 等到 `active_count=0` |
| 重建 | 仅在静默后修复并重新 freeze | 仅在静默后修复并重新 freeze |
| 后继 run | `search_create(..., source_run_id=old)` | 相同 |

适配器不得尝试向已经失效的 run 补充 candidate。Runtime 还会拒绝打开或提交 Pi pool，并拒绝记录 fence 之后才完成的 verifier 结果。旧 run 仍可用于诊断和继承研究信息，但其分数不能被 successor run promotion 或复用。

## 验证证据

| 路径 | 仓库证据 |
|---|---|
| Codex 并行循环 | `codex_parallel_loop_cycle`：两个初始 candidate、一个 plan、同一原生 worker 续轮、best 更新、final selection 和 report |
| Pi 托管 pool | `pi_rpc_managed_pool_wait_any`：两个分离的真实 Pi worker、pool 重新发现、candidate-ready 事件、drain |
| Pi 并行循环 | `pi_rpc_parallel_loop_cycle`：一个初始 plan、使用新 session 重新派发同一 candidate、最佳项更新、最终选择和报告 |

快速测试只证明 schema 和适配器映射。只有选择性运行的真实 host 测试才能证明原生启动、等待、续轮、hook 和 provider 行为。

## 适配器职责

适配器可以：

- 构建启动和续轮 payload；
- 在不启动 Search run 的情况下列出 host 当前公布的模型；
- 校验 host 专属预算字段；
- 声明 pool 能力；
- 保存原生 handle 和有界 host 元数据。

适配器不得创建 candidate workspace、执行或排序 verifier、规划下一个 hypothesis、生成报告或导出 promotion patch。

Host integration 仅限 Codex 和 Pi。修改任一适配器时，都必须同步更新本地资产和文档，在单元测试中覆盖 launch/bind/budget/continuation，并保留一条真实的多轮 smoke 路径。
