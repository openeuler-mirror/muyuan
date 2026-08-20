# API

`goal-plus --root .gp` 暴露 host-neutral 的 MCP 接口。运行中服务返回的工具 schema 和描述是权威依据；本文只提供简明索引和所有权说明。

## Goal Plus 工具

| 工具 | 用途 |
|---|---|
| `goal_plus_create` | 在 triage 前创建持久 goal |
| `goal_plus_status` | 读取 goal phase、revision、关联任务和 Evidence |
| `goal_plus_update_goal` | 替换完整的有效目标并开始新 revision |
| `goal_plus_record_triage` | 选择普通 goal 工作或 verifier/spec discovery |
| `goal_plus_save_spec_draft` | 持久化带类型的 candidate Search spec |
| `goal_plus_list_models` | 列出所选 Codex 或 Pi host 当前可用的模型 |
| `goal_plus_link_search_run` | 向 goal 追加一个已冻结的 Search run |
| `goal_plus_record_search_result` | 绑定 selection/promotion Evidence，并预留规范的最终报告路径 |
| `goal_plus_prepare_final_check` | 创建必需的独立审查请求 |
| `goal_plus_submit_final_check` | 为准确 revision 记录 reviewer 结论 |
| `goal_plus_set_status` | 设置有 Evidence 支持的终态或暂停状态 |
| `goal_plus_gate` | 返回便于 hook 使用的 allow/block 决策 |

`goal_plus_update_goal` 要求提供 `expected_revision`，防止过期 agent 覆盖较新的目标。Search result 以 `run_id` 为键，因此一个 goal 可以保留多个 Search task。

## Search 工具

### Spec 与 Run

| 工具 | 用途 |
|---|---|
| `search_freeze_spec` | 预检 `SearchSpec` 及 verifier 产物，并用 hash 固定它们 |
| `search_create` | 创建 `run_id`；可选 `source_run_id` 会快照有界的前驱研究信息 |
| `search_status` | 读取预算使用情况、candidate 和当前 best |
| `search_invalidate_run` | main agent 确认 verifier 不充分后，原子地 fence 该 run |
| `search_list_history` | 对 candidate 排序，并返回当前 run 的 feature/verifier 研究汇总 |
| `search_list_iterations` | 显式查看单个 candidate 可能很大的完整 verifier 历史 |
| `goal_plus_monitor_snapshot` | 在不控制 worker 的情况下读取 goal/run/session/host 的组合证据 |

### 初始 candidate 分配

| 工具 | 用途 |
|---|---|
| `search_plan_next` | 持久化唯一一次初始 candidate 分配 |
| `search_start_batch` | 物化该 plan 的隔离 candidate workspace |

新的 Pi/Codex spec 使用 `strategy.orchestration_mode="parallel_loops"`。`search_plan_next(requested_k)` 只能调用一次，后续工作恢复已有 candidate。规划数量为：

```text
min(requested_k, remaining max_parallel)
```

标准流程在这次唯一的 planning 调用中传入 `requested_k=budget.max_parallel`。`budget.max_parallel` 是初始 candidate 数和活跃 worker 数的唯一来源。

`search_invalidate_run` 要求带类型的 verifier 原因、非空摘要和具体证据。它把 run 改为 `aborted`，并阻止新的 planning、session、verifier record、selection 和 promotion。它不拥有 host worker：调用方随后必须中断完整 host pool，并等待活跃 worker 数归零，才能修复 verifier 文件。

确实需要创建后继 run 时，使用：

```text
search_create(new_frozen_spec_id, source_run_id=invalidated_or_exhausted_run)
```

对于多模型 run，先调用 `goal_plus_list_models(host=...)`，再把用户选择一次性写入冻结的 `strategy.models`。没有 `count` 的条目会以 round-robin 方式扩展到 `budget.max_parallel`：四条 lane 下的 `A,B` 变为 `A,B,A,B`。所有条目都显式提供 count 时，其总和必须等于 `max_parallel`：用户层的 `A1B3` 分配（或简写 `A*1,B*3`）会变为 `A,B,B,B`。不能混合带 count 和不带 count 的条目。运行时会在冻结前用 host catalog 解析每个请求名称，并拒绝缺失或有歧义的名称。

最终有序的 `selected_models` 属于运行时状态，不是第二份用户输入。每个 candidate 及其 agent session 在续轮期间都保留选定模型。所有模型读取同一个 run-scoped `search_get_global_evidence` 接口。每个 verifier-backed iteration 都会连同 commit、score、disposition 和可用时异步生成的客观 View 投影到该接口。模型身份只用于 provenance，不改变 plan admission 或 iteration selection。

新 run 会暴露 `inherited_research`，其中包含前驱 frontier、feature ledger 和带适用范围的 pitfalls。前驱分数会被标记为不可复用。

<a id="worker-context"></a>

### Worker 上下文

| 工具 | 调用方 | 用途 |
|---|---|---|
| `search_start_agent_session` | main | 创建 provenance handle 和 host 原生启动 payload |
| `search_redispatch_candidate` | main | 在同一 candidate workspace 创建 fresh session |
| `search_bind_agent_handle` | main/host driver | 绑定 Codex 或 Pi 原生 handle |
| `search_continue_agent_session` | main | host 支持时返回原生同 worker 续轮字段 |
| `search_get_agent_context` | candidate worker | 加载权威 ID、workspace、有界的 recent/best candidate 历史、results ledger 路径和恢复数据 |
| `search_get_global_evidence` | candidate worker | 将当前 run 已结算的 worker 尝试投影为 score、disposition、准确 attempt commit 和可能延迟的客观 View |
| `search_stage_shared_tool` | candidate worker | 把调用方 `.tmp/tool-drafts/` 中显式选择的源码复制到有界 `.tmp/share-out` staging；该操作不会发布工具 |
| `search_copy_shared_tool` | candidate worker | 把与 Tool View 绑定的 shared-dir 快照复制到调用方本地 inbox，以便可逆验证 |
| `search_get_evidence_detail` | candidate worker | 展开调用方当前 run 中一项可用的 supplemental evaluation；independent 模式仅限当前 candidate |
| `search_get_agent_observability` | main/monitor | 读取单个 session 的归一化 model、timing、terminal、usage、context、artifact 和 handoff Evidence |

`search_start_agent_session` 不会启动或监督 worker。调用方必须使用返回的 `launch` 对象。初始启动、续轮或重新派发都可传入 `worker_budget`，而不会修改 frozen spec。Pi pool 的最小值字段会在一个 pool job 内部的 native session 恢复之间累计；普通 override 仍只对单次派发生效。

`search_get_agent_context` 会暴露 `supplemental_evaluation_enabled`。为 false 时，worker 不等待也不请求 supplemental evaluation。启用时，`search_get_global_evidence` 只增加 `supplemental_available=true`；完整 summary、dimensions、peer comparisons 和 limitations 通过 `search_get_evidence_detail` 为选定的不可变行按需获取。

默认上下文是有界的：`latest_result`、`best_iteration` 和 `recent_iterations` 取代原来的完整 `results` 和 `iterations` 数组；`result_count`、`iteration_count` 和 `results_tsv` 保留准确的历史 provenance。只有少数需要核查旧 verifier 细节的场景才显式调用可能很大的 `search_list_iterations`。

worker 调用 process verifier 时必须提供一句 `hypothesis`，说明实际实施的尝试。启用 `shared_dir` 后还可以提供 `toolization_decision`。`staged` 要求一个或多个正向 signal 和工具名；`not_applicable` 要求具体排除原因。缺失或矛盾的决策只生成 iteration advisory。staging inventory 始终是权威依据，decision/advisory 不影响 score、disposition、selection 或 promotion。

Global Evidence 中的 `view=null` 表示 annotation 尚未发布；worker 应独立继续，不能等待或轮询。`strategy.config.global_evidence_mode` 控制 Evidence 投递，但不改变 candidate 可见的 prompt 或工具表面：

- `manual` 是默认模式，candidate 显式读取 run 的共享视图；
- `auto` 还会在每次成功的 worker process verifier 后，以 `global_evidence_snapshot` 注入共享视图；
- `independent` 不自动注入，并把显式读取限制为调用 candidate 自己的 Evidence。

parent 和 promotion verification 不受影响。快照失败时会增加 `global_evidence_warning`，但不会改变成功的 verifier 结果。冻结前可通过 `GOAL_PLUS_GLOBAL_EVIDENCE_MODE` 提供模式；有效值会持久化到 frozen spec，且与显式 `strategy.config.global_evidence_mode` 冲突时会被拒绝。

每次调用都会在调用方 agent session 中持久化一条 `global_evidence_reads`。该 record 包含读取时间，以及当时可见的准确 completed candidate/iteration/commit View 引用，因此报告可以区分“在最后一次 verifier 后才发布的 View”和“后续尝试前已经可用的 View”。这些 receipt 只用于观察，不影响 settlement、selection、promotion 或硬 PASS/FAIL。

当 `shared_dir.enabled=true` 时，Global Evidence 还会投影已经生成 Tool View 并由运行时绑定的工具。worker 可以用准确的 `tool_id` 和 `snapshot_hash` 调用 `search_copy_shared_tool`；下一次 process verifier 会原子消费本地 copy receipt。该过程会记录 candidate-local adoption，但不会产生独立工具分数、推荐或选择规则。

`ToolizationDecision` 是 iteration-local review fact，永远不会进入 Global Evidence。发布路径保持为 staging -> attributed passing process verifier -> immutable shared snapshot -> annotator-bound Tool View -> Global Evidence -> exact copy receipt -> adopted tool record。

每次 worker settlement 都会把准确的 attempt base/head、worker host 和已解析的 annotator model/provider 快照到内部 task。Codex 通过临时 `codex exec` 运行 annotation；Pi 通过无工具的临时 `pi --mode json` 运行。显式 `strategy.evidence_annotator.provider` 适用于 Codex，且只保存 API key 环境变量名，从不保存 key 值。Pi 默认继承 `PI_MODEL` 和 `PI_PROVIDER`；限定为 `provider/model` 的 annotator model 或显式 `strategy.evidence_annotator.pi_provider` 可以选择独立 Pi provider。Pi 从 `PI_CODING_AGENT_DIR` 解析该 provider 的模型和凭据。

`search_get_agent_observability` 使用统一的跨 host 版本化 schema。Schema version 2 增加 `execution.provider` 和 `usage.processed_tokens`：Pi 的 processed tokens 包含 input、cache read/write 和 output tokens；Codex 使用原生 total-token 计数，因为其中已经包含 cached input。Codex 读取原生 subagent session JSONL（由 `SubagentStop` 绑定，或通过唯一 task name 发现）；Pi 归一化 `metadata.pi_metrics`。旧 record 仍可能包含已绑定元数据。该调用从不返回 prompt、reasoning、工具参数或工具输出内容，也不会等待或控制 worker。`goal_plus_monitor_snapshot` 在每个 `subagents[].observability` 下嵌入同一对象，同时为向后兼容保留旧 Pi 字段。

`goal_plus_monitor_snapshot.statistics` 是统一统计视图。所选 run 的 payload 会报告 baseline/target improvement、success rates、稳定终态时长、首次 verifier/成功耗时、worker outcome 和 model/provider 分布、candidate lineage、selection survival、worker 与 parent verifier 数量、promotion report Evidence、归一化 usage、efficiency 和数据完整性缺口。

绑定 Codex Goal Plus transcript 后，`statistics.orchestrator` 会报告从 Goal Plus 创建起、不含内容的 usage 增量，`statistics.total_usage` 会把它与 worker usage 合并。每个任务的统计仍保留在 `search_tasks[].statistics`，并聚合到 `search_task_aggregate.statistics`。快照根节点还会暴露 monitor 契约的 `schema_version` 和生成该快照的运行时 `goal_plus_version`。生成的报告数据会把产品版本与报告 schema 版本分别保存。

worker handoff 继续使用一个有界协议。`key_results` 提供 feature ledger entries，包括 artifact、code surface/change、portability/dependencies、measured effect、verifier result 和 incumbent relation；`verifier_assessment` 报告有证据支持的契约质量。candidate 历史会保留这些字段，顶层 `feature_ledger` 和 `verifier_assessments` 则聚合当前 run 中所有 candidate，包括可见 ranking frontier 内外的 candidate。

Pitfalls 不是 run 级 deny list。其 `scope` 为 `candidate_local`、`feature_family` 或 `evaluation_contract`，并带有 `condition`、evidence artifact 和 `confidence`。缺少 scope 时默认为 candidate-local。worker 的 `verifier_assessment` 在 main agent 确认并调用 `search_invalidate_run` 前始终只是 advisory。

### 验证与收尾

| 工具 | 用途 |
|---|---|
| `search_run_verifier` | 提交并验证准确 attempt，返回 candidate-local `keep`/`retain`/`discard`/`failure`；保留最新同分尝试，在退化或 verifier 失败后恢复最佳代码，再向继承的 `workspace/results.tsv` 准确追加一行。worker 调用需传入准确的 `run_id`、`candidate_id`、`agent_session_id` 和客观 `hypothesis`；parent fallback verification 省略 session ID，且不要求 hypothesis |
| `search_select` | 恢复排序后的 commit，并选择第一个通过 final verifier 的状态 |
| `search_report` | 生成最终 `report.md` 和自包含 `report.html`；关联的 Goal Plus record 必须已经终止 |
| `search_promote` | 把选中 commit 导出为 patch；正常 Goal Plus 流程此时还没有需要刷新的报告 |

`report.html` 是传给 `search_report` 的 run 的完整 Goal Plus 审计视图。如果该 run 属于某个 Goal Plus record，页面会分别保留每个关联的 Search task，并提供跨任务聚合。规划轮数仍在归一化数据中，但没有独立报告面板。

报告包含统一统计、candidate/session/verifier Evidence、归一化 main agent usage、明确的 metric gaps，以及每个 Search task 的独立执行时间线。Goal Plus 状态只在顶部汇总，不在单独 lifecycle 面板重复。每条 Search 时间线由 run 创建、worker-session observability、verifier iterations 和 promotion Evidence 组成。worker 条形使用观测到的 host 起止时间；配置的最大或最小预算不会被渲染为实际时长。

文件仅含内联 CSS/JavaScript，无需 Web server 即可阅读。安装可选 `report` extra 后，生成器会内嵌 Plotly.js，并把简略最佳分数条替换为完整的各 candidate verifier 调用轨迹和全局 best-so-far 曲线。生成前会计算 score scale 和调用密度：较大的正数分值范围使用对数轴；完整 run 保持在一条调用轴上，并自适应 tick 和 marker 密度；失败的 verifier 尝试单独标记，不进入 score scale 或 best-so-far 曲线。没有 Plotly 时，现有内联 SVG 分数条仍是确定性后备方案。

在持久 Search 状态包含相应坐标或 embedding 前，报告有意不绘制 search-space contour 或 surface。`report.md` 仍是稳定的文本产物。记录 Goal Plus Search result 时，会在文件生成前预留两个规范路径。正常顺序是 select、promote、record result、final audit、terminal status，再为每个已记录 run 生成一次报告。中间态 Goal Plus 报告会被拒绝。

## Pi 本地工具

Pi extension 使用 `goal-plus-pi-tool`，它是同一 Python runtime 的 JSON CLI facade。以下 pool 工具归 host 本地所有，不会加入共享 MCP server：

| 工具 | 用途 |
|---|---|
| `pi_search_pool_open` | 创建或恢复固定 pool，并启动初始 candidate |
| `pi_search_pool_wait_any` | 返回新的终态 pool event；`candidate_ready` 表示最小 lease 和 durable Evidence 已满足，`timed_out` 表示 lease 未满足 |
| `pi_search_pool_snapshot` | 查看一个 pool，或按 `run_id` 重新发现 pool |
| `pi_search_pool_continue` | 在新进程中恢复同一 candidate 和原生 Pi session |
| `pi_search_pool_close` | drain 或终止活跃 pool job |

正常 Pi Search 只使用这些固定 lane pool 工具。pool 不提供 submit/refill API，也不负责规划 candidate。

只读调用示例：

```bash
goal-plus-pi-tool goal_plus_monitor_snapshot \
  --root .gp \
  --args-json '{"run_id":"run_..."}' \
  --pretty
```

## 控制执行的 SearchSpec 字段

| 字段 | 含义 |
|---|---|
| `objective` | 可度量的优化目标 |
| `metric_name`, `metric_direction` | 排序值及方向 |
| `source_path` | baseline 源码快照 |
| `editable_globs`, `forbidden_globs` | candidate 编辑范围 |
| `process_verifiers` | 正确性 gate |
| `ranking_signals` | 产生 metric 的命令 |
| `promotion_verifiers` | promotion 前必须通过的检查 |
| `budget.max_parallel` | 初始 candidate 数和活跃 worker 数 |
| `strategy.worker_host` | 维护中的执行 host：`pi-rpc` 或 `codex` |
| `strategy.worker_budget` | host 执行的上限和可选最小 lease |
| `workspace.backend` | `git_worktree`（默认）或 `copy` |

每条 ranking command 都必须成功退出，并输出最后一个 JSON 对象，其中 `metric_name` 对应有限数值。临时 verifier 输出必须放在 `GOAL_PLUS_VERIFIER_TMPDIR` 下；verifier artifact 和 evaluation input 都会用 hash 固定。

## 错误语义

- Validation error 表示调用方必须修正 spec 或工具参数。
- Candidate verifier failure 是正常的 Search Evidence。
- 带 infrastructure-failure metric 的 `VerifierWorkspaceSideEffect` 表示 evaluator 破坏了隔离；应停止 candidate，修复后重新 freeze。
- Frozen artifact hash 不匹配会使评分无效。
- main agent 确认 verifier 缺陷后，必须调用 `search_invalidate_run`，再中断 host 并等待静默，随后冻结修复后的 spec 并创建 successor run。
- Host timeout 和 runner failure 含义不同：timeout 证明 deadline enforcement 生效；runner failure 则需要 host 恢复 Evidence。

调用顺序、状态与所有权见[共享平面](shared-plane.md)。
