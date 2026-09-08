---
name: goal-plus
description: 仅供 Goal Plus Main Agent 运行、编辑或恢复 Pi Goal Plus（含 /goal-plus-with-final-check）；不适用于 Search candidate worker。
---

# Pi 的 Goal Plus

此 skill 只描述 Main Agent 的判断和正常调用路径。角色、phase、Goal/run 归属、candidate 边界、
workspace、verifier、promotion 和 feature gate 由 runtime 强制执行；以工具返回的 schema、
`next_action` 和错误为准。

当前 Pi session 必须已由精确 slash command 绑定 active `goal_plus_id`；skill 和兼容 prompt 不
创建、编辑或恢复记录。入口保留当前 Pi model 与 thinking 档位，并持久化 typed
`command_config`。显式配置必须落实，只有缺省项由 Main 决定。
中断后通过 Pi 原生 `--session`、`--continue` 或 `--resume` 恢复 session，再用精确
`/goal-plus resume` 恢复执行。查询、暂停和关闭分别使用 host 的 `summary`、`pause`、`clear`。

## 主流程

1. 每轮调用默认 `goal_plus_status` 读取当前 revision、显式配置、待处理 dispatch、
   final-check policy、`control`、`reconciliation` 和 `next_action`。初始化、会话恢复或 revision
   变化时用 `view="context"` 读取完整 `raw_goal`、policy 和 spec draft；需要历史证据时通过
   `goal_plus_history` 分页读取。先处理最新用户
   消息，再继续 active 任务；范围、交付物或成功标准变化时请其改用 `/goal-plus edit`。
   每轮重读状态，非 Search 工作后仍推进 reconciliation。
2. 尚未分类时调用 `goal_plus_record_triage`。普通交付进入 Goal Mode；需要补齐可执行评价合同的优化进入
   Spec Discovery；合同已明确时进入 Search Mode。高置信度且无 open question 的 Search 不等待
   额外批准。
3. Goal Mode 中直接完成原始目标。Main 自行拆解、并行和集成；不要预建 work DAG。独立工作用
   `pi_goal_plus_run_work_item` 运行，该入口负责 dispatch、bind 和 worker result。Main 检查实际
   产物和测试后，用 `goal_plus_record_work_event` 记录 `accepted`、`rework` 或其他真实决定。
4. 只有工作同时具备数值 metric、确定性 verifier、隔离 edit surface、多个有价值假设和足够预算
   时才进入 Search。准备完整 SearchSpec 后调用 `goal_plus_save_spec_draft`；ready 后执行下方
   Search 流程。
5. Search 完成 selection、promotion 和授权的 apply 后，调用
   `goal_plus_record_search_result`。仍有不同的可度量子问题时可创建新 run；同一评价合同下的新
   技术方向留在现有 run。
6. 对照完整 `raw_goal` 审计实现、验证和所有实际 dispatch。目标满足后调用
   `goal_plus_set_status`；`blocked` 只用于确实依赖外部输入、权限、资源或状态变化的情况。
7. final check 为 required 时，调用 `goal_plus_prepare_final_check(checker_host="pi")`，将返回的
   launch 交给 `pi_goal_plus_run_final_check`。checker 自行提交结论；失败后修复全部发现并重试。
8. Goal 达到终态后，才为每个成功记录的 run 调用一次 `search_report`。Pi extension 自动执行
   pre-tool 与 stop gate，并展示持久化完成结果。

## FrozenSpec

Main 负责把原始目标和公开任务合同中的正确性、性能、适用范围与测量方法一次性落实为统一
验收合同；candidate iteration、selection、promotion 和最终验收使用同一标准。worker 需要知道
的公开条件写入 `candidate_acceptance`，runtime 私有控制不通过 `constraints` 下发。

按工具 schema 定义 source、数值 metric、correctness gate、edit surface、预算和 promotion。
自定义 verifier 放在 source 拥有且 Git 跟踪的路径，例如 `.goal-plus-verifiers/`；并发临时输出
使用 `GOAL_PLUS_VERIFIER_TMPDIR` 或等价的唯一目录。Pi Search 通常使用
`workspace.backend="git_worktree"`；安装的 `goal-plus-pi` ThinkThread Root 可使用
`workspace.backend="thinkthread"`。标准共享 Evidence 使用 manual 模式；auto、shared_dir、
supplemental evaluation、device pool 和角色模型路由只在目标或显式配置需要时启用，其 worker
合同由 runtime 动态投影。显式 `command_config.inner_agent` 必须写入
`strategy.inner_agent`。

若评价合同本身有缺陷，调用 `search_invalidate_run`，停止 pool，再修复、重新冻结并创建后继
run；不要直接比较不同合同的分数。

对于 AscendC Direct Invoke 目标，记录 `scenario="ascendc_direct_invoke"`，并完整读取：

- `examples/ascendc-direct-search/SPEC_DISCOVERY.md`
- `examples/ascendc-direct-search/request.schema.json`

按该场景生成并校验 request、Golden、cases、verifier、baseline 和 SearchSpec；使用固定 Git
revision 的知识材料，不调用外部 AscendC Agent、plugin 或编排工作流。

## Pi Search

已有 run 按 `goal_plus_status.reconciliation` 继续。Git/Pi RPC lane 用
`search_recovery_status` 和 `pi_search_pool_snapshot` 核对 checkpoint 与真实 worker；停止且有未
结算修改时，先调用 `search_prepare_recovery`，关闭旧 pool 并确认退出，再用返回的
`recovery_id` 调用 `search_recover_candidate`。ThinkThread lane 使用
`thinkthread_search_pool_snapshot` 和 provider journal 恢复同一 Child/session identity。任何 host
preflight 或恢复失败都 fail closed，不切换 backend。launch/bind 间中断时先核对原生
task/session，再补齐下一步。

1. 使用当前 `goal_plus_id` 和相对 `source_path` 的 verifier artifact 路径调用
   `search_freeze_spec`，再使用同一 `goal_plus_id` 调用 `search_create`，然后调用
   `goal_plus_link_search_run`。
2. 调用一次 `search_plan_next(requested_k=budget.max_parallel)` 和一次 `search_start_batch`，创建
   初始 candidate。只有 plan 返回 `requires_agent_proposals=true` 时才向
   `search_start_batch` 传 `proposals`。普通 `parallel_loops` 不补 lane 或重新规划。
3. 普通 Pi 用全部 candidate ids 调用
   `pi_search_pool_open(..., max_parallel=budget.max_parallel, final_verify=true)`；ThinkThread Root
   调用 `thinkthread_search_pool_open(..., max_parallel=budget.max_parallel)`。pool 负责 session
   启动、绑定、watchdog 和 durable verifier Evidence。
4. 用所选 host 的 `*_wait_any` 等待事件，以对应 `*_snapshot` 核对状态。全局停止条件未满足时，
   用对应 `*_continue` 恢复同一 candidate/native session；continuation lease 使用冻结上限与
   “外层剩余时间减收尾预留”的较小值。
5. 全局停止后用对应 `*_close` drain 或 interrupt，并确认 `active_count=0`。随后调用
   `search_select`、`search_promote`，以及任务授权时需要的 `search_apply_promotion`。报告持久化后
   runtime 自动回收不再被引用的 ThinkThread snapshots。

Pi worker 首次启动或 fresh session 恢复时调用 `search_get_agent_context`，并以 runtime 返回的
`candidate_task` 为完整合同。Main 不复制 workspace、Evidence、shared_dir、supplemental、
Adaptive、回滚或 verifier 规则。普通低分、一次退化或 peer 领先都不是替换 worker 的理由。

只有目标明确要求 runtime reward/allocation，或恢复的 FrozenSpec 已使用 `adaptive_search` 时，
才完整读取 `references/adaptive-search.md` 并执行额外 pool 流程。

## 模式与预算

- Goal Mode：普通实现、文档、审查或调查，不创建 SearchSpec。
- Spec Discovery Mode：补齐可冻结的验收合同。
- Search Mode：运行 verifier 引导的并行候选优化。

`mode=autonomous` 持续推进当前目标；`mode=probe` 以可行性、潜力和阻塞证据为交付。
success threshold 是验收条件，不自动中断仍有价值且符合用户时间要求的探索。存在时间预算时，
停止新增工作后仍完成 pool close、选择、promotion、审计和报告。优先用
`goal_plus_monitor_snapshot` 查看持久状态，单个 worker 使用 `search_get_agent_observability`。
