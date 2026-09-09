---
name: goal-plus
description: Internal Goal Plus plugin skill for an already-bound Goal Plus Main Agent to run, resume, or edit Codex Goal Plus. Not for Search candidate workers; do not invoke manually.
---

# Codex 的 Goal Plus

此 skill 只描述 Main Agent 必须作出的判断和正常调用路径。角色、phase、Goal/run 归属、
candidate 边界、workspace、verifier、promotion 和 feature gate 由 runtime 强制执行；以工具
返回的 schema、`next_action` 和错误为准。

从 Codex host 已绑定的 session 读取 active `goal_plus_id`。通过当前 registry 中的原生
`goal_plus_*` 和 `search_*` MCP tools 操作状态；不要通过 shell 或手写 client 调用 server，
也不要用 MCP resource discovery 判断 tool 是否存在。新建或修订目标使用 `$goal-plus` 或
`$goal-plus edit`；中断后通过 `codex resume <session-id>` 恢复原生 session，再用精确
`$goal-plus resume` 恢复执行。查询、暂停和关闭分别使用 host 的 `summary`、`pause`、`clear`。

## 主流程

1. 每轮调用默认 `goal_plus_status` 读取当前 revision、待处理 dispatch、
   final-check policy、`control`、`reconciliation` 和 `next_action`。初始化、会话恢复或 revision
   变化时用 `detail=true` 读取完整 `raw_goal`、显式配置、policy 和 spec draft；需要历史证据时通过
   `goal_plus_history` 分页读取。显式配置优先，
   只有缺省项才由 Main 决定。先处理最新用户消息，再继续 active 任务；范围、交付物或成功标准
   变化时请其改用 `$goal-plus edit`。每轮重读状态，非 Search 工作后仍推进 reconciliation。
2. 尚未分类时调用 `goal_plus_record_triage`。普通交付进入 Goal Mode；需要补齐可执行评价合同的优化进入
   Spec Discovery；合同已明确时进入 Search Mode。高置信度且无 open question 的 Search 不等待
   额外批准。
3. Goal Mode 中直接完成原始目标。Main 自行拆解、并行和集成；不要预建 work DAG。实际派发普通
   subagent 前，以 `goal_plus_record_work_event(event="dispatch")` 获取 attempt identity，再用
   `spawn_agent(fork_turns="none")` 启动并记录 `bind`。检查返回和测试后记录 `accepted`；需要原
   worker 修正时记录 `rework` 并使用新的 attempt identity；其他终态按实际记录。
4. 只有工作同时具备数值 metric、确定性 verifier、隔离 edit surface、多个有价值假设和足够预算
   时才进入 Search。准备完整 SearchSpec 后调用 `goal_plus_save_spec_draft`；ready 后按内部
   `search` skill 创建和驱动 run。
5. Search 完成 selection、promotion 和授权的 apply 后，调用
   `goal_plus_record_search_result`。仍有不同的可度量子问题时可创建新 run；同一评价合同下的新
   技术方向留在现有 run。
6. 对照完整 `raw_goal` 审计实现、验证和所有实际 dispatch。目标满足后调用
   `goal_plus_set_status`；`blocked` 只用于确实依赖外部输入、权限、资源或状态变化的情况。
7. final check 为 required 时，调用 `goal_plus_prepare_final_check(checker_host="codex")`，将返回的
   launch payload 映射到前台 `spawn_agent`，保持 `fork_turns="none"`。等待 checker 自行调用
   `goal_plus_submit_final_check`；Main 不代交结论，失败后修复全部发现并重新检查。
8. Goal 达到终态后，才为每个成功记录的 run 调用一次 `search_report`。停止前遵循
   `goal_plus_gate(event="stop")` 的返回。

## FrozenSpec

Main 负责把原始目标和公开任务合同中的正确性、性能、适用范围与测量方法一次性落实为统一
验收合同；candidate iteration、selection、promotion 和最终验收使用同一标准。worker 需要知道
的公开条件写入 `candidate_acceptance`，runtime 私有控制不通过 `constraints` 下发。

按工具 schema 定义 source、数值 metric、correctness gate、edit surface、预算和 promotion。
自定义 verifier 放在 source 拥有且 Git 跟踪的路径，例如 `.goal-plus-verifiers/`；并发临时输出
使用 `GOAL_PLUS_VERIFIER_TMPDIR` 或等价的唯一目录。通常使用
`workspace.backend="git_worktree"`。标准共享 Evidence 使用 manual 模式；auto、shared_dir、
supplemental evaluation、device pool 和角色模型路由只在目标或显式配置需要时启用，其 worker
合同由 runtime 动态投影。显式 `command_config.inner_agent` 必须写入
`strategy.inner_agent`，不能由 Main 改成其他 adapter。

若评价合同本身有缺陷，调用 `search_invalidate_run`，再修复、重新冻结并创建后继 run；不要直接
比较不同合同的分数。

对于 AscendC Direct Invoke 目标，记录 `scenario="ascendc_direct_invoke"`，并完整读取：

- `examples/ascendc-direct-search/SPEC_DISCOVERY.md`
- `examples/ascendc-direct-search/request.schema.json`

按该场景生成并校验 request、Golden、cases、verifier、baseline 和 SearchSpec；使用固定 Git
revision 的知识材料，不调用外部 AscendC Agent、plugin 或编排工作流。

## 模式与预算

- Goal Mode：普通实现、文档、审查或调查，不创建 SearchSpec。
- Spec Discovery Mode：补齐可冻结的验收合同。
- Search Mode：运行 verifier 引导的并行候选优化。

`mode=autonomous` 持续推进当前目标；`mode=probe` 以可行性、潜力和阻塞证据为交付。
success threshold 是验收条件，不自动中断仍有价值且符合用户时间要求的探索。存在时间预算时，
停止新增工作后仍完成 drain、选择、promotion、审计和报告。
