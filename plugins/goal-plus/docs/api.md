# API

`goal-plus --root .gp` 提供 host-neutral MCP 接口。运行中 server 返回的
tool schema 和说明始终是权威定义；本文说明各组工具在一次 Goal Plus /
Search 流程中的位置、调用方和所有权。

## 一次完整流程

普通 Goal Plus 任务和 Search Mode 使用同一条上层生命周期：

1. Codex `$goal-plus` 或 Pi `/goal-plus` 的 host adapter 在模型轮次前把 typed command
   config 与自然语言目标分离，保存两者并记录 host-command invocation Evidence；
2. `goal_plus_record_triage` 确定直接处理，还是先做 verifier/spec discovery；
3. `goal_plus_upsert_work_items` 建立当前 revision 的
   work DAG，main 按 route 自行处理、派发 subagent 或进入 Search；
4. Search route 先 `search_freeze_spec`，再 `search_create` 并通过 `goal_plus_link_search_run`
   关联；
5. `search_plan_next` 和 `search_start_batch` 只创建一次初始 candidate，host 启动固定 worker pool；
6. worker 循环读取 context/Evidence、修改 workspace、调用 `search_run_verifier`；
7. 达到全局停止条件后，host 先停止所有 worker，再执行 `search_select` 和
   `search_promote`；任务授权写回 source 时再执行 `search_apply_promotion`；
8. main 接受各 work item，完成
   `goal_plus_record_search_result`、final check 和终态审计，最后为每个已记录 run 调用一次 `search_report`。

Search runtime 不启动或监督 worker。Codex/Pi 的启动、wait-any、continuation 和 interrupt 由
host 负责，详见
[Agent Host Adapters](agent-host-adapters.md)。

## Goal Plus 工具

| 工具                             | 用途                                                                         |
| -------------------------------- | ---------------------------------------------------------------------------- |
| `goal_plus_create`               | deny-only 兼容 tombstone；始终报错，不创建记录                               |
| `goal_plus_status`               | 读取 goal phase、revision、关联 task 和 Evidence                             |
| `goal_plus_update_goal`          | deny-only 兼容 tombstone；始终报错，不修改记录                               |
| `goal_plus_record_triage`        | 选择普通 goal 工作或 verifier/spec discovery                                 |
| `goal_plus_upsert_work_items`    | 创建或更新当前 revision 的 host-neutral work DAG                             |
| `goal_plus_record_work_event`    | 管理按 generation 隔离的 subagent attempt，并记录结果、决策或 Search routing |
| `goal_plus_save_spec_draft`      | 持久化带类型的 candidate Search spec                                         |
| `goal_plus_list_models`          | 列出 Codex 或 Pi host 当前可用模型                                           |
| `goal_plus_link_search_run`      | 向 goal 追加 frozen Search run                                               |
| `goal_plus_record_search_result` | 绑定 selection/promotion Evidence，并预留 final report 路径                  |
| `goal_plus_prepare_final_check`  | 创建必需的独立 review 请求                                                   |
| `goal_plus_submit_final_check`   | 为准确 revision 记录 reviewer verdict                                        |
| `goal_plus_set_status`           | 设置 Evidence-backed terminal 或 paused state                                |
| `goal_plus_gate`                 | 返回适合 hook 使用的 allow/block decision                                    |

创建、编辑和恢复不是 MCP tool。只有 Codex `UserPromptSubmit` 对精确 `$goal-plus...`
的解析，以及 Pi extension 对精确 `/goal-plus...` 的 command handler 可以调用 runtime 的
host-command control path。每次授权都会持久化 host、transport、command、action、session、
native entry、输入 hash、时间和 invocation id；重复 invocation id 幂等返回同一记录。
普通消息、skill 自动加载、MCP/CLI 直调、错误 host 前缀和 SessionStart 都不授权后续变更。

### Host Command Config

`/goal-plus` 和 `$goal-plus` 只把开头连续的 `key=value` token 解释为配置；第一个非配置
token 开始就是自然语言目标：

```text
/goal-plus max_parallel=2 workspace_backend=git_worktree promotion_mode=artifact_only strategy=agent_guided Optimize the read-only template.
```

支持的 typed key 是：

| Key | 约束目标 |
| --- | --- |
| `mode` | `autonomous` 或 `probe` 探索模式 |
| `max_parallel` | `SearchSpec.budget.max_parallel` |
| `workspace_backend` | `SearchSpec.workspace.backend` |
| `promotion_mode` | `SearchSpec.workspace.promotion_mode` |
| `strategy` | `SearchSpec.strategy.name` |
| `main` | 当前 main session model 的兼容 assertion |
| `annotator` | `SearchSpec.strategy.evidence_annotator.model` |
| `workers` / `models` | `SearchSpec.strategy.models`；两者是互斥别名 |

解析后的 `GoalPlusRecord.command_config` 是显式用户约束，配置 token 不再保留在
`raw_goal` 中。高置信度且无 open question 的 spec draft 必须匹配所有显式 Search
配置，否则 `goal_plus_save_spec_draft` 拒绝保存；只有 `command_config` 中缺省的字段才由
Agent 根据目标、host 能力和资源信号决定。未知 key、重复 key、非法 enum、非正整数和不一致
的 worker count 都在创建 Goal Plus 记录前失败。

Host-only edit 仍要求 `expected_revision`，防止 stale adapter 覆盖新目标。Search result 以
`run_id` 为键，因此一个 goal 可以保留多个 Search task；`search_tasks` 是仅追加历史，`linked_search`
只是当前 task 的兼容视图。

### Work DAG 与 Attempt

每条 Goal Plus record 默认遵循 [`goal-plus-ultra-v1`](ultra.md)。`route="main"`
由 main 直接完成，`route="subagent"` 走 host 原生派发，`route="search"` 进入后文的 verifier-guided
Search。work event 是持久协调事实，但启动、等待、通信、中断和原生日志仍由 host 所有。

对 subagent work item，`dispatch` 只生成 `attempt_id`、`generation` 和 launch
deadline。host 启动成功后记录 `bind`，并在 `message`、`result`、`failed` 中携带同一 attempt
identity。过期 generation 的迟到结果保留为 `stale_result` Evidence，不能修改当前 item。main 检查
`result_ready` 后记录 `accepted` 或 `rework`；required item 未全部接受时，completion gate 不会放行。

## 创建 Search Run

### 冻结合同与创建 Run

| 工具                         | 用途                                                        |
| ---------------------------- | ----------------------------------------------------------- |
| `search_freeze_spec`         | preflight `SearchSpec` 和 verifier artifact，并用 hash 固定 |
| `search_create`              | 创建 `run_id`；可用 `source_run_id` 继承有界研究信息        |
| `search_status`              | 读取 budget、candidate 和当前 best                          |
| `search_invalidate_run`      | main 确认 verifier 不充分后，原子 fence 当前 run            |
| `search_list_history`        | 按 candidate 排名，并汇总当前 run 的 feature/verifier 研究  |
| `search_list_iterations`     | 显式读取单个 candidate 可能很大的完整 verifier 历史         |
| `goal_plus_monitor_snapshot` | 只读汇总 goal/run/session/host Evidence                     |

`search_invalidate_run` 需要 typed verifier reason、非空 summary 和具体 Evidence。它把 run 置为
`aborted`，拒绝新的 planning、session、verifier record、selection 和 promotion。runtime 不拥有 host
worker，因此调用方还必须停止完整 host pool，并确认活跃 worker 归零。

MCP 的 `search_freeze_spec` 和 `search_create` 是 Goal Plus 内部 bootstrap surface：runtime
要求当前 `.gp` 中恰好存在一条 active、host-verified Goal Plus 记录。没有精确 host command
创建的记录，或存在歧义的多条 active 记录时，调用会以
`GoalPlusEntrypointRequired` 失败。candidate worker 的既有 run 工具不依赖这个 bootstrap
检查；它们继续由绑定的 candidate/session identity 限制。

只有原合同无法继续使用时才创建 successor：

```text
search_create(new_frozen_spec_id, source_run_id=invalidated_or_exhausted_run)
```

新 run 的 `inherited_research` 可以包含 predecessor frontier、feature ledger 和 scoped
pitfalls，但 predecessor score 会明确标记为不可复用。

### 初始 Candidate 分配

| 工具                 | 用途                                                                       |
| -------------------- | -------------------------------------------------------------------------- |
| `search_plan_next`   | 持久化唯一一次初始 candidate 分配                                          |
| `search_start_batch` | 物化隔离的 candidate workspace，并返回每个 subagent 的 `task_skill_notice` |

新的 Codex/Pi spec 使用
`strategy.orchestration_mode="parallel_loops"`。`search_plan_next(requested_k)`
只能调用一次，数量为：

```text
min(requested_k, remaining max_parallel)
```

标准流程传入 `requested_k=budget.max_parallel`。`budget.max_parallel`
同时决定初始 candidate 数和活跃 worker 数，后续工作只继续既有 candidate。

多模型 run 先调用 `goal_plus_list_models(host=...)`，再把用户选择写入 frozen `strategy.models`。没有
`count` 的 entry 按 round-robin 扩展：四条 lane 中的 `A,B` 变为
`A,B,A,B`。全部 entry 都有 count 时，总和必须等于 `max_parallel`，例如 `A*1,B*3` 变为
`A,B,B,B`；不能混用 counted 和 uncounted entry。

runtime 在 freeze 前通过 host catalog 解析模型名。最终有序 `selected_models` 属于 runtime
state，每个 candidate 和 agent
session 在 continuation 中保持选定模型。模型 identity 只用于 provenance，不影响 planning、verifier 或 selection。

## Worker Context

### Session 与 Context

| 工具                             | 调用方           | 用途                                                                         |
| -------------------------------- | ---------------- | ---------------------------------------------------------------------------- |
| `search_start_agent_session`     | main             | 创建 provenance handle 和 host 原生 launch payload                           |
| `search_redispatch_candidate`    | main             | 在同一 candidate workspace 创建 fresh session                                |
| `search_bind_agent_handle`       | main/host driver | 绑定 Codex 或 Pi 原生 handle                                                 |
| `search_continue_agent_session`  | main             | host 支持时返回原生同 worker continuation 字段                               |
| `search_get_agent_context`       | candidate worker | 加载权威 ID、workspace、有界 candidate history 和恢复数据                    |
| `search_get_agent_observability` | main/monitor     | 读取单个 session 的归一化 model、timing、usage、artifact 和 handoff Evidence |

`search_start_agent_session` 只创建 runtime handle，不启动 worker。调用方必须使用返回的 `launch`
对象。初始启动、continuation 或 redispatch 都可以提供 dispatch-scoped
`worker_budget`，不会修改 frozen spec；Pi pool 的 minimum lease 会在同一 job 的 native-session
resume 之间累计。

`search_get_agent_context` 默认返回有界结构：`latest_result`、`best_iteration` 和
`recent_iterations`，而不是完整 `results` / `iterations`。`result_count`、`iteration_count` 和
`results_tsv` 保留准确 provenance。只有确实需要旧 verifier 细节时才调用 `search_list_iterations`。

### Evidence 与按需详情

| 工具                         | 调用方           | 用途                                           |
| ---------------------------- | ---------------- | ---------------------------------------------- |
| `search_get_global_evidence` | candidate worker | 读取已结算的 Global Evidence                   |
| `search_get_evidence_detail` | candidate worker | 按需展开一条 available supplemental evaluation |
| `search_reference_open`      | candidate worker | 打开 Evidence 的精确 reference 目录            |

每个 verifier-backed iteration 都会把 commit、score、disposition 和可用时异步生成的 View
投影到 Global Evidence。`view=null` 表示 annotation 尚未发布；worker 应继续独立工作，不等待或轮询。

`artifact_ref={kind,id,provider}` 是新的跨 backend 身份；Git 下 `kind=git_commit`，旧 `commit`
字段继续保留。窄 diff 足够时可以直接比较 commit；改动较大时用 `search_reference_open`
把完整树 checkout 到该 worker 的稳定 `ref-workspace` 目录，然后直接使用普通文件和命令工具查看。每个
`agent_session_id` 最多有一个 reference 目录；再次调用会丢弃该目录的本地变化，
并切到新的精确 artifact。reference 不是 peer 的活动 workspace，所有候选实现修改仍发生在自己的 candidate
workspace。runtime 不要求 worker 关闭 reference；目录随 run state 保留，数量受实际 worker 数限制。

`strategy.config.global_evidence_mode` 决定 candidate 如何收到 Evidence：

- `manual`：默认模式，由 candidate 显式读取共享 run view；
- `auto`：除显式读取外，每次成功的 worker process verifier 后注入 `global_evidence_snapshot`；
- `independent`：不自动注入，显式读取也只返回当前 candidate 自己的 Evidence。

parent/promotion verification 不进入 peer Evidence。快照失败只增加
`global_evidence_warning`，不会改变 verifier result。`GOAL_PLUS_GLOBAL_EVIDENCE_MODE`
可以在 freeze 前提供默认值；如果与显式 spec 配置冲突，freeze 会拒绝。

每次 Evidence 读取都会在 agent session 中保存 `global_evidence_reads`
receipt，记录读取时间和当时可见的 completed candidate/iteration/commit
View。receipt 只用于报告“某个 View 是否在后续尝试前可见”，不参与 settlement、selection、promotion 或 PASS/FAIL。

`search_get_agent_context` 的 `supplemental_evaluation_enabled`
告诉 worker 是否存在按需详情。关闭时不等待也不请求；开启时，窄 Global Evidence 只增加
`supplemental_available=true`，完整 summary、dimensions、peer comparisons 和 limitations 通过
`search_get_evidence_detail` 获取。`independent` 模式只能展开当前 candidate。完整 annotation 语义见
[Evidence Annotator](evidence-annotator.md)。

### 修改、验证与共享工具

| 工具                       | 调用方                             | 用途                                                                             |
| -------------------------- | ---------------------------------- | -------------------------------------------------------------------------------- |
| `search_run_verifier`      | candidate worker / parent fallback | 提交准确 attempt，运行 process verifier 并结算 candidate-local best              |
| `search_stage_shared_tool` | candidate worker                   | 从 `.tmp/tool-drafts/` stage 新工具或 `tool_family_catalog` head 的实质 revision |
| `search_copy_shared_tool`  | candidate worker                   | 将 Tool View 绑定的 immutable snapshot 复制到本地 inbox，供阅读或自主复用        |

worker 调用 process verifier 时必须提供一行客观
`hypothesis`，说明实际实施的尝试。`search_run_verifier` 返回 candidate-local
`keep`、`retain`、`discard` 或 `failure`，并向继承的 `workspace/results.tsv` 追加准确记录。parent
fallback 省略 `agent_session_id`，也不要求 `hypothesis`。

启用 `shared_dir` 后，worker 可以同时提交 `toolization_decision`。`staged`
需要工具名和至少一个正向 signal，`not_applicable`
需要具体排除原因；缺失或矛盾只产生 advisory。staging
inventory 是权威事实，decision 不影响 score、disposition、selection 或 promotion。

六类 capability signal 为
`repeated_workflow`、`domain_construction_or_probe`、`behavior_or_invariant_checker`、`reproducer_fixture_or_case_generator`、`parser_trace_or_comparator`
和 `peer_setup_or_feedback_reduction`。正式回归测试、fixture、reproducer、comparator 和 invariant
checker 也可以共享；candidate 产品实现、冻结 verifier/grader、隐藏答案或评分逻辑、凭据、原始日志和构建产物属于
`restricted_artifact`。

只有通过 process verifier、生成 Tool View 并绑定 snapshot 的工具才进入 Global
Evidence。peer 使用准确 `tool_id` 和 `snapshot_hash` 调用
`search_copy_shared_tool`；下一次 verifier 原子消费 copy
receipt，并把 adoption 记录到该 iteration。复制只证明 snapshot 进入本轮 context，不证明原工具被执行或保留；只有直接执行、导入或作为 runtime
dependency 时，才需要先在当前 workspace 验证原工具。完整路径为：

```text
staging -> passing process verifier -> immutable snapshot -> Tool View -> Global Evidence -> copy receipt -> adopted tool
```

Tool View 和 adoption 只提供研究参考，不产生独立工具分数或 selection
rule。完整共享平面语义见[共享平面](shared-plane.md#global-evidence)。

`search_get_agent_context.tool_family_catalog` 只暴露 family/head
identity、版本容量、累计 capability/coverage key 和 contract
fingerprint，不包含路径或源码。首次发布使用 `publication_intent=new`；更新只在
`revision_allowed=true` 时引用唯一的 `revision_head.tool_id`：`capability_extension`
新增稳定能力或覆盖键，`adoption_fix` 必须基于同 family 的真实 copy/adoption 缺陷，`contract_change`
表示 entrypoint、输入、输出或依赖合同发生实质变化。改名和同义键不构成新版本。

默认每个 family 最多有一个 pending revision 和两个 immutable published
record，pending 也占用上限。等待 Tool View 时，Global Evidence 继续显示
`discoverable_head`；settlement 成功后原子切换，失败则保留旧 head。`revision_allowed=false`
时，worker 应使用 catalog 给出的准确 `revision_blocker`，而不是制造新版本。

### Handoff 与 Observability

worker handoff 是有界协议。`key_results` 描述 artifact、code
surface/change、portability/dependencies、measured effect、verifier result 和 incumbent
relation；`verifier_assessment` 描述 Evidence-backed contract quality。顶层 `feature_ledger` 和
`verifier_assessments` 聚合整个 run，而不只展示 ranking frontier。

Pitfall 的 `scope` 为 `candidate_local`、`feature_family` 或 `evaluation_contract`，并带
`condition`、evidence artifact 和 `confidence`。缺省 scope 为 candidate-local。worker 的 verifier
concern 在 main 调用 `search_invalidate_run` 前只是 advisory，不是 run-wide deny list。

`search_get_agent_observability` 使用统一、版本化的跨 host schema。version 2 增加
`execution.provider` 和 `usage.processed_tokens`：Pi 包含 input、cache read/write 和 output
token；Codex 使用原生 total-token count。version 3 增加 `runtime_start`：Pi
RPC 从子进程自身的 session entry 报告 project trust、active
tools、Pi 内置 sandbox/approval 语义、cwd，以及由启动用户或外部 host
policy 决定而 Pi 不会伪造的 filesystem/network 边界；其他 host 或旧记录保持
`source="unavailable"`。该接口不返回 prompt、reasoning、tool argument 或 tool
output，也不等待或控制 worker。

`goal_plus_monitor_snapshot.statistics` 汇总 baseline/target improvement、success rate、terminal
duration、time-to-first-verifier/success、worker outcome、model/provider、candidate
lineage、selection survival、worker-vs-parent verifier、promotion
Evidence、usage、efficiency 和数据缺口。`selected_run.workspace_operations` 以同一套
provider-neutral 口径展示 Git/ThinkThread 的冻结时容量预估、candidate workspace 创建次数、
总耗时、均值、最大值，以及 promotion apply 的结果和耗时；对应的扁平时间字段也保留在
`selected_run.timing`，供静态报告统一渲染。绑定 Codex orchestrator
transcript 后，`statistics.orchestrator` 记录 Goal Plus 创建以来不含内容的 usage
delta，`statistics.total_usage` 与 worker usage 合并；每个 Search task 仍保留自己的 statistics。

`goal_plus_monitor_snapshot.feature_plugins` 提供可选的只读派生视图。内置 `orchestration`
plugin 统一展示普通 main/subagent 的 task packet、attempt
generation、assignment/result/decision，同时保留 Codex/Pi 的原生 operation 名。传入
`feature_plugins=["orchestration"]` 可显式选择，传入空数组可关闭全部投影。详见
[Feature Plugin](feature-plugins.md)。

## Selection、Promotion 与 Report

- `search_select`：按 ranking 恢复 immutable artifact，并选择通过 final verifier 的状态。
- `search_promote`：运行 promotion gate，并准备 provider-native publication。
- `search_apply_promotion`：将 publication 显式写回原 Git repo 或 ThinkThread Root。
- `search_report`：生成最终 `report.md` 和 self-contained `report.html`。

`search_select` 基于 verifier-backed immutable revision。promotion 是独立验收，不复用 process
pass；失败时 run 保持可重试，且不输出 patch。

新 Git run 会完整 mirror 原 repository 和历史，并保留 candidate 的全部 running commits。
`search_promote` 另行合成一个以 source baseline 为 parent 的 squash publication commit；
`search_apply_promotion` 将它 cherry-pick 回原仓。旧 run 的 `legacy_patch` 仍返回
`apply_cwd` 和 `apply_directory`，并从 `apply_cwd` 执行
`git apply --directory=<apply_directory> <artifact_path>`。

runtime 在 provider 调用的公共边界记录时间：每个 candidate 的
`task.workspace_creation_timing` 覆盖 workspace materialization，每条 provider-native
promotion 的 `apply_timing` 覆盖实际写回调用。两者都持久化开始、完成、单调时钟耗时和
成功/失败结果；重复读取已 applied promotion 不会重复计时。

`search_freeze_spec.workspace_capacity_estimate` 在 candidate 创建前记录 source scope 的当前
大小、有效 materialized source、完整 Git checkout 的文件数/每 node 字节数、
`budget.max_parallel` 个 node 的合计、共享 object store、预计增量、保守上界、可用空间和
`fits_available_space`。Git source 的 node 估算按整个 repository checkout，而不是只按
`source_path` 子目录；本地 `--local` mirror 通常 hard-link object，但保守上界仍将 object
store 再计一次。该结构也进入 monitor statistics 和最终 HTML report。
`fits_available_space=false` 时 `search_create` 会在创建 run 或 candidate 前拒绝执行。

关联 Goal Plus record 时，正常顺序是 select -> promote -> apply -> record Search result ->
final audit -> terminal status -> report。`promotion_mode="apply"` 尚未 applied 时 runtime 会
拒绝记录 Search result。只有显式 `artifact_only` 的受保护 source、只读 template 或测试
fixture 停在 prepared publication；`search_report` 会拒绝 active 或 needs-user record，
确保静态报告不会捕获中间态。

`report.html` 汇总全部关联 Search task、candidate/session/verifier Evidence、main agent
usage、metric gap 和独立时间线。worker
bar 使用观测到的 host 起止时间，配置 budget 不会伪装成实际运行时长。安装 `report`
extra 后使用 Plotly 绘制每个 candidate 的 verifier trajectory 和 global
best-so-far；没有 Plotly 时使用确定性的内联 SVG fallback。

报告只绘制持久状态中真实存在的数据，不从 prose 猜测 search-space
coordinate、embedding 或缺失 metric。HTML 内联 CSS/JavaScript，可直接打开；`report.md`
是稳定文本产物。详细生成和恢复方法见[调试运行时状态](debugging-runtime.md)。

## Pi 本地 Pool 工具

Pi extension 通过 `goal-plus-pi-tool` 调用同一 Python runtime。以下工具归 host-local
pool 所有，不进入共享 MCP server：

| 工具                      | 用途                                                    |
| ------------------------- | ------------------------------------------------------- |
| `pi_search_pool_open`     | 创建或恢复 fixed pool，并启动初始 candidate             |
| `pi_search_pool_wait_any` | 返回新终态 event；区分 `candidate_ready` 和 `timed_out` |
| `pi_search_pool_snapshot` | 查看 pool，或按 `run_id` 重新发现                       |
| `pi_search_pool_continue` | 新进程恢复同一 candidate 和 native Pi session           |
| `pi_search_pool_close`    | drain 或 interrupt 活跃 pool job                        |

pool 不负责 candidate planning，也不提供 submit/refill。只读调用示例：

```bash
goal-plus-pi-tool goal_plus_monitor_snapshot \
  --root .gp \
  --args-json '{"run_id":"run_..."}' \
  --pretty
```

## 控制执行的 SearchSpec 字段

| 字段                                | 含义                                                     |
| ----------------------------------- | -------------------------------------------------------- |
| `objective`                         | 可度量的优化目标                                         |
| `metric_name`, `metric_direction`   | ranking value 和方向                                     |
| `source_path`                       | baseline source snapshot                                 |
| `editable_globs`, `forbidden_globs` | candidate edit surface                                   |
| `process_verifiers`                 | correctness gate                                         |
| `ranking_signals`                   | 产生 metric 的 command                                   |
| `promotion_verifiers`               | promotion 前必须通过的检查                               |
| `budget.max_parallel`               | 初始 candidate 和活跃 worker 数                          |
| `strategy.worker_host`              | 执行 host：`pi-rpc` 或 `codex`                           |
| `strategy.worker_budget`            | host deadline 和可选 minimum lease                       |
| `workspace.backend`                 | `git_worktree`（默认）、`copy`，或接口占位 `thinkthread` |
| `workspace.promotion_mode`          | `apply`（默认）或显式 `artifact_only`                    |

`thinkthread` 当前只注册统一 provider/capability API。Codex 和 Pi RPC 的 host capability 都只声明
`copy` 与 `git_worktree`；`search_freeze_spec` 会在 verifier preflight 前以
`UnsupportedHostCapability` 拒绝 ThinkThread，`search_create` 还会复检已有 frozen
spec。底层 provider 仍会以 `UnsupportedWorkspaceCapability` fail closed，
因此不会静默退回 Git，也不会声称已提供 protected execution、FsSnapshot
checkpoint 或 snapshot reference。TT Child 内执行 Codex 和 FsSnapshot 接入属于后续 TODO。

每条 ranking command 必须成功退出，并输出最后一个 JSON object，其中 `metric_name` 对应 finite
number。临时 verifier 输出必须位于 `GOAL_PLUS_VERIFIER_TMPDIR`；verifier artifact 和 evaluation
input 都会用 hash 固定。

## 错误语义

- Validation error：调用方必须修正 spec 或 tool argument。
- Candidate verifier failure：正常 Search Evidence，不是 runtime 故障。
- `VerifierWorkspaceSideEffect`：evaluator 破坏隔离；停止 candidate，修复后重新 freeze。
- Frozen artifact hash mismatch：本次 scoring 无效。
- 已确认 verifier defect：先 `search_invalidate_run`，再停止 host 并确认静默，最后创建 successor
  run。
- Host timeout：证明 deadline enforcement 生效；runner failure：需要 host recovery Evidence。

完整状态机和所有权边界见[共享平面](shared-plane.md)。
