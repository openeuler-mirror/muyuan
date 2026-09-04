# Agent Host Adapters

Goal Plus 的 Search runtime 负责持久状态、candidate workspace、verifier 和最终选择。
Codex 或 Pi host 负责实际启动、等待、续跑和停止 worker。Host Adapter 位于两者之间，
把 runtime 的统一请求转换为 host 原生操作；它不改变 Search 的结算语义。

系统整体边界见[共享平面](shared-plane.md)。本文只说明 Host Adapter 的统一契约、Codex/Pi 差异和恢复要求。
Main Goal 的宿主原生 session 恢复语义独立记录在[原生 Session Resume](resume.md)；本文的
continuation 主要指 Search candidate worker 的续跑。

普通 Goal Mode 不使用这套 Search Adapter，而是遵循 [`goal-plus-ultra-v1`](ultra.md) 的
轻量 dispatch 合同。Ultra 可以由 Pi、Codex 或外部 harness 实现，不受 `AgentHostKind`
限制；只有进入 verifier-guided Search Mode 后，才使用本文的 candidate pool 契约。

## 统一运行模型

`src/goal_plus/agent_pool.py` 定义 `HostPoolContract` 和终态事件
`WorkerPoolEvent`。每种 host 都必须明确以下能力：

| 字段                | 需要回答的问题                                        |
| ------------------- | ----------------------------------------------------- |
| `launch_mode`       | 启动调用是否立即返回？                                |
| `wait_mode`         | 任意 worker 完成时，parent agent 能否及时被唤醒？     |
| `continuation_mode` | 续跑时复用原 worker，还是把状态派发到 fresh session？ |
| `deadline_mode`     | 哪个 host 组件执行时间上限？                          |
| `recovery_mode`     | 如何重新发现仍在运行的 pool？                         |
| `completion_stage`  | candidate 到什么阶段才可以交给 parent 评估？          |

Adapter 返回的 launch payload 是权威启动说明。main agent 只传递当前 host tool schema
支持的字段；Codex 必须以当前 `spawn_agent` schema 为准，不能自行补造可选 metadata。

Search runtime 不承担 worker supervisor 职责。`search_start_agent_session`
创建的是 context/provenance handle，不代表 worker 已经启动或仍然存活。

## Worktree 与 Skill bootstrap

每个 candidate 由 runtime 物化到独立 Git worktree。host launch
payload 中的 workspace 只是 provenance：Codex `spawn_agent` 没有 `cwd` 字段，Pi 虽能设置 process
cwd，worker 仍必须先调用 `search_get_agent_context`，并把返回的
`context.candidate_task.workspace`
作为每次文件和 shell 操作的显式 cwd。parent cwd、其他 candidate workspace 和 `.gp`
内部路径都不是 candidate 工作目录。

任务局部 Skill 采用固定约定，不进入 SearchSpec：只识别 source Git index 中
`.agents/skills/<name>/SKILL.md`，且要求该 Skill 目录内所有文件都由 Git 跟踪。freeze 返回
`task_skill_notice`，明确列出发现的名称和相对路径；没有时明确显示 none。candidate
materialization 通过 `candidate_task.task_skill_paths`
返回该 candidate 自己的绝对路径；`search_start_batch` 为每个 subagent 返回可直接展示的
`task_skill_notice`，launch message 同时输出 `task_local_skills_visible`。worker 完整读取清单中的
`SKILL.md` 并显式遵循，只按需读取同目录资源。仓外 Skill 继续由 host 管理；运行时不扫描或复制
parent/global/plugin 或 peer candidate Skill。

## Codex 与 Pi 的能力对比

| 能力 | Codex | Pi RPC |
| --- | --- | --- |
| 启动 | 异步 `spawn_agent`；无 `cwd`，worker 从 context bootstrap worktree | host-local supervisor 启动前台 Pi 子进程；worker仍核对 context workspace |
| Candidate 进程环境 | `spawn_agent` 无 `env`；worker 对本地命令显式应用 `candidate_task.process_environment` | supervisor 把同一 `process_environment` 注入 Pi worker 进程 |
| 等待 | `wait_agent` 任意事件唤醒，再用 `list_agents` 收集终态 | `pi_search_pool_wait_any` |
| 续跑 | `followup_task` 复用同一 worker | 新进程恢复同一 native session |
| Deadline | parent watchdog 管理每次派发 | 累计 pool lease + Pi 进程 watchdog |
| 恢复 | Codex agent registry + `.gp` | `.gp/host-pools/pi/` + `.gp` |
| Goal gate | 精确 `$goal-plus...` 的 `UserPromptSubmit` 授权；`PreToolUse`、`Stop`、`SubagentStop` fail closed | 精确 `/goal-plus...` command 授权；extension 的 input/tool/turn event |
| 模型发现 | Codex app-server `model/list` | Pi RPC `get_available_models` |
| Observability | native session JSONL + 已绑定 metadata | `pi_metrics` + 已绑定 metadata |
| Workspace | `copy`、`git_worktree` | `copy`、`git_worktree` |

两种 host 都只支持固定的初始并行循环。当前可移植 planner 为 `agent_guided`（含
`agent`、`default`）和 `random`（含 `random_mode`）。两个 host 当前都不声明 `thinkthread`
capability；runtime 会在 freeze/create 边界拒绝该 backend。

资源分配由 runtime 在 freeze 时解析并固定，不由 Adapter 各自推断。Codex/Pi 的正式 verifier
都使用 CandidateTask 中持久化的进程环境和解析后的物理资源锁；Pi 还能在 worker 进程边界直接
强制该环境。Codex 原生 `spawn_agent` 当前不支持 `env`，所以它的本地 profile 命令必须按
candidate contract 显式应用环境变量，不能把 parent 的逻辑设备 `:0` 当作已隔离设备。

host 通过 `GOAL_PLUS_DEVICE_POOLS` 注册受信任的硬件无关映射：

```json
{
  "cuda":{"visibility_env":"CUDA_VISIBLE_DEVICES","devices":["0","1"]},
  "ascend-npu":{"visibility_env":"ASCEND_RT_VISIBLE_DEVICES","devices":["4","5"]}
}
```

SearchSpec 只能用 `{"device_pools":[{"pool_id":"cuda"}]}` 引用已注册 pool，对应 verifier
使用 `<pool_id>:0` 作为 `resource_lock`。它不能提供环境变量名或设备值。未声明 device pool
的普通 host resource lock 保持原有精确字符串语义。freeze preflight 使用第一条 candidate
lane 的单设备环境；后续宿主配置变化不会改写已冻结 run 的设备分配。每个引用 pool 必须
至少包含 `budget.max_parallel` 个设备，lane 按顺序一一分配，不会轮转复用；同一 spec 中
多个 pool 也不能使用相同的 `visibility_env`。

runtime 从 run 创建到 promoted、aborted 或 failed 期间独占每个引用 pool，避免不同 run
同时把 worker 放到同一组设备。pool lease 和 verifier resource lock 共用
`GOAL_PLUS_VERIFIER_RESOURCE_LOCK_DIR`；多个 runtime 进程或容器共享设备时，该变量必须指向
同一个 host 共享目录，否则只能保证单个文件系统范围内不超售。

## Candidate 循环

新的 Codex/Pi spec 使用 `orchestration_mode="parallel_loops"`。一次 Search
run 只做一次初始规划，创建固定数量的 candidate lane：

1. 启动全部初始 candidate；
2. 以 wait-any 方式处理最先完成的 worker；
3. 复用该 worker 已经落盘的准确 Evidence，缺失时才由 parent 补做 verifier；
4. 如果全局停止条件尚未满足，继续同一 candidate、workspace 和 native session；
5. selection 前停止或 drain 所有活跃 worker。

低分、同分或单次没有提升都不会触发 candidate 替换，Adapter 也不会自动 refill。较慢 lane 不会阻塞其他 lane 的 verifier 结算。

Codex 通过 `search_continue_agent_session` 和 `followup_task` 原地续跑。Pi 的 `candidate_ready`
表示 minimum lease 已释放且 durable Evidence 已存在；main 随后调用
`pi_search_pool_continue`，由新进程恢复同一 native Pi session。`timed_out`
表示 lease 耗尽但未满足，不能当作 ready candidate 继续。

## 模型与 Observability

`goal_plus_list_models` 提供只读模型发现。用户把选择写入
`strategy.models`，Adapter 在派发时将已解析模型投影到实际 host
schema；没有指定模型时，保留 host 原生继承行为。模型目录只说明当前可发现的模型，不保证旧版 host
tool 能转发所有可选字段。

所有 Adapter 都实现只读 `collect_observability`，并通过 `search_get_agent_observability`
暴露。该数据只用于 provenance、报告和诊断，不会把 host lifecycle 写入 Search record。

Codex rollout JSONL 提供 input、cached-input、cache-write、output 和 reasoning
token，但不提供实际账单。Goal Plus 使用 `src/goal_plus/codex_pricing.py`
中带版本、与 Pi 兼容的模型目录估算 `usage.cost_usd`；长上下文分层和 `flex`/`priority`
倍率遵循 Pi 语义。`usage.cost_estimate` 会注明目录版本和覆盖率，未知模型不套用猜测价格。

## Worker 预算

| Host   | 必需字段                            | 执行方式                                     |
| ------ | ----------------------------------- | -------------------------------------------- |
| Codex  | `worker_budget.max_runtime_seconds` | 首次等待、一次 closeout、最后等待、interrupt |
| Pi RPC | `worker_budget.max_runtime_seconds` | closeout steer + 进程硬 watchdog             |

`budget.max_parallel` 同时决定初始 candidate 数和活跃 worker 数。`max_turns`
对 Codex 和 Pi 都只是 prompt 提示。

可选的 `min_runtime_seconds` 和 `min_verifier_runs` 构成单 worker 的 AutoResearch minimum
lease。Codex 由 `SubagentStop` hook 在同一 child turn 内继续；Pi 由 worker `agent_end`
handler 和 pool supervisor 累计执行。lease 必须在外层软收尾前释放，`max_runtime_seconds`
仍是独立硬上限。基础设施故障、pool close 或外层 closeout 会立即终止 lease。

`strategy.worker_launch` 只保存 host 启动偏好。Codex 可映射 `model`、`reasoning_effort` 和
`service_tier`；Pi 通过可信进程配置映射 model 和 thinking level。这些字段不属于 Search state。

## 恢复与 Handoff

正常续跑优先复用 host 的 native session。工作区保持完整、仅需要 fresh context 时，使用
可移植的 state redispatch：

1. 对原 candidate 调用 `search_redispatch_candidate`；
2. 在同一 workspace 创建新的 `agent_session_id`；
3. worker 调用 `search_get_agent_context`；
4. 根据 candidate-local artifact、verifier iterations、`research_summary`、Global Evidence 和
   `.tmp/handoff.json` 恢复。

Pi 支持跨进程的 native session 续轮：PID 会变化，但 native session、runtime
`agent_session_id`、candidate 和 workspace 保持不变。只有 native
session 无法恢复但工作区完整时，才退回上述 state redispatch。

Worker 丢失且留下半次修改或结算时，使用 [Search 一致性恢复](search-recovery.md)：共享
runtime 固定 checkpoint 并 fence 旧 generation；host 提供停止事实；provider 恢复
`ArtifactRef`；随后启动 fresh worker。普通 redispatch 不回滚工作区，不能代替这个流程。

每份 handoff 应包含关键工作、verifier-backed feature entries、blockers、next
steps，以及最多五条带适用范围和条件的 pitfalls。`candidate_local`
pitfall 只约束当前 candidate；`feature_family`
pitfall 只有在机制和条件匹配时才可迁移。worker 提出的 verifier concern 在 main
agent 确认前始终只是 advisory。

## Verifier 失效与静默

main agent 确认 verifier 不再可信后，先 fence run，再由 host 停止全部 worker：

| 步骤     | Codex                                       | Pi RPC                                   |
| -------- | ------------------------------------------- | ---------------------------------------- |
| Fence    | `search_invalidate_run`                     | `search_invalidate_run`                  |
| 停止     | 对每个活跃 candidate 调用 `interrupt_agent` | `pi_search_pool_close(mode="interrupt")` |
| 确认静默 | `list_agents` / `wait_agent` 直到全部终止   | snapshot / wait 直到 `active_count=0`    |
| 重建     | 静默后修复并重新 freeze                     | 静默后修复并重新 freeze                  |
| 后继 run | `search_create(..., source_run_id=old)`     | 相同                                     |

失效 run 不再接受新 candidate、Pi pool 提交或 fence 后才完成的 verifier
result。旧 run 仍可提供有界研究上下文，但旧分数不能被 successor run 复用。

## Adapter 维护要求

Adapter 可以构建 launch/continuation payload、发现模型、校验 host
budget、声明 pool 能力，并保存原生 handle 和有界 host metadata。它不得创建 candidate
workspace、执行或排序 verifier、规划技术方向、生成报告或导出 promotion patch。

修改 Host Adapter 时，需要同步更新 host
asset、本文和对应测试。快速测试只验证 schema 和映射；真实 host 行为由以下 smoke path 覆盖：

| 路径                           | 验证内容                                                                 |
| ------------------------------ | ------------------------------------------------------------------------ |
| `codex_parallel_loop_cycle`    | 初始 candidate、wait-any、同 worker 续跑、best 更新、selection 和 report |
| `pi_rpc_managed_pool_wait_any` | 两个真实 Pi worker、pool 恢复、candidate-ready event 和 drain            |
| `pi_rpc_parallel_loop_cycle`   | 单次初始 plan、同 candidate 续跑、best 更新、selection 和 report         |
