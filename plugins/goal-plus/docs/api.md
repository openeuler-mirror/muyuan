# API

`goal-plus --root .gp` 提供 host-neutral MCP 接口。运行中 server 返回的
tool schema 和说明始终是权威定义；本文说明各组工具在一次 Goal Plus /
Search 流程中的位置、调用方和所有权。

## 一次完整流程

普通 Goal Plus 任务和 Search Mode 使用同一条上层生命周期：

1. Codex `$goal-plus` 或 Pi `/goal-plus` 的 host adapter 在模型轮次前把 typed command
   config 与自然语言目标分离，保存两者并记录 host-command invocation Evidence；
2. `goal_plus_record_triage` 确定直接处理，还是先做 verifier/spec discovery；
3. main 直接执行原始目标；实际派发普通 subagent 时，由 `goal_plus_record_work_event`
   按 dispatch/bind/result 记录轻量 attempt；
4. 进入 Search 时先 `search_freeze_spec`，再 `search_create` 并通过 `goal_plus_link_search_run`
   关联；
5. `search_plan_next` 和 `search_start_batch` 只创建一次初始 candidate，host 启动固定 worker pool；
6. worker 循环读取 context/Evidence、修改 workspace、调用 `search_run_verifier`；
7. 达到全局停止条件后，host 先停止所有 worker，再执行 `search_select` 和
   `search_promote`；任务授权写回 source 时再执行 `search_apply_promotion`；
8. main 检查普通 subagent 和 Search 结果，完成
   `goal_plus_record_search_result`、final check 和终态审计，最后为每个已记录 run 调用一次 `search_report`。

Search runtime 不启动或监督 worker。Codex/Pi 的启动、wait-any、continuation 和 interrupt 由
host 负责，详见
[Agent Host Adapters](agent-host-adapters.md)。

## Goal Plus 工具

| 工具                             | 用途                                                                         |
| -------------------------------- | ---------------------------------------------------------------------------- |
| `goal_plus_create`               | deny-only 兼容 tombstone；始终报错，不创建记录                               |
| `goal_plus_status`               | 默认读取当前状态和待办；`detail=true` 按需读取目标、配置与草案               |
| `goal_plus_history`              | 分页读取 revision、dispatch、Search 结果、final check 或审计事件             |
| `goal_plus_update_goal`          | deny-only 兼容 tombstone；始终报错，不修改记录                               |
| `goal_plus_record_triage`        | 选择普通 goal 工作或 verifier/spec discovery                                 |
| `goal_plus_record_work_event`    | 按实际派发懒创建 subagent 记录，并管理 generation、结果和 Main 决策          |
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
普通消息、skill 自动加载、MCP/CLI 直调、错误 host 前缀和 SessionStart 都不能创建或重新绑定
session。普通消息不会撤销精确 host command 已建立、仍处于 active 的同一原生 session 绑定；
SessionStart 是否仍属同一原生 session 由 host adapter 根据可信 lifecycle identity 判定，不能仅凭
持久化 active ID 自动授权新 session。

### 返回视图

`goal_plus_status` 默认返回 `goal_plus_id`、`goal_revision`、`status`、`control`、`phase`、
`next_action`、deadline、`current_search_run_id`、待处理 `work_items`、当前 revision 的
final-check 状态和恢复待办。默认 `detail=false`；返回的 `details` 指向只读详情工具。
初始化、恢复会话或 revision 变化时调用 `goal_plus_status(detail=true)`，读取完整
`raw_goal`、`policy`、`triage`、`spec_draft`、`command_config`、source、session、当前
Search 结果/报告路径、final check 和恢复诊断。
`command_config` 只表示用户显式约束，不把草案或宿主默认值伪装成已生效配置。
Search 运行状态和分数使用 `search_status`；冻结后的 worker budget、host/model 配置、并发数、
workspace backend 和 promotion mode 可在 `goal_plus_monitor_snapshot(detail=true)` 的 `strategy` 中读取。

两个视图都不回放 revision/事件历史。`detail` 替代旧的 `view` 参数；不同时维护两个开关。
恢复待办保留 run/candidate/generation/recovery identity、`next_step` 和诊断错误；checkpoint
和历史 session 通过 `search_recovery_status` 按需读取。未完成或待审查工作不因数量限制被隐藏。

`goal_plus_history` 的 `collection` 支持 `goal_revisions`、`work_items`、`search_tasks`、
`final_checks`、`events`，按持久顺序返回完整条目。每页默认 20 条、最多 100 条，返回
`total` 与 `next_offset`；`next_offset=null` 表示当前读完。它不是跨状态变更的快照游标。
条目正文不截断，读取成本仍取决于现有持久文件大小。

Goal 变更工具返回核心状态回执，不再序列化整个 Goal：work event 附带单个 `work_item`
（含 attempt/generation、host handle identity 与结果摘要），link/result 仅附带本次
`search_task` 的 run/candidate identity 和结果记录时间，不回显提交的总结与路径；
save draft 返回 `spec_draft_saved=true`，submit final check 附带本次 `final_check` 摘要。
完整证据仍保存在持久状态，通过 history 读取。恢复消费者应使用当前状态/context，不能把变更
回执当作完整 Goal record。

`goal_plus_monitor_snapshot` 默认返回进度计数、最佳/选中结果、审查队列、恢复动作、scope 和错误。
成功 candidate/session 的逐项结果、配置、artifact 路径和历史诊断使用 `detail=true`；直接
Python Monitor/dashboard 接口仍保留完整 snapshot。异常、违规、清理错误与未知活跃数不被隐藏。
Pi pool open/snapshot 默认返回计数、提交回执、活跃/未交付 job 和各 candidate 最新 job 的错误；
wait 保留本次交付事件及精确 Evidence/lease 结果。close 仅返回 pool identity、关闭状态、
活跃/未交付计数和未解决事项，不回放已交付的成功结果。完整诊断使用
`pi_search_pool_snapshot(detail=true)`。详情读取不重做 close/result 等写操作，不改变交付语义。

### Host Command Config

`/goal-plus` 和 `$goal-plus` 只把开头连续的 `key=value` token 解释为配置；第一个非配置
token 开始就是自然语言目标：

```text
/goal-plus max_parallel=2 max_duration_seconds=300 workspace_backend=git_worktree promotion_mode=artifact_only strategy=agent_guided Optimize the read-only template.
```

支持的 typed key 是（此表由 runtime command option registry 自动生成）：

<!-- BEGIN GENERATED GOAL PLUS COMMAND CONFIG -->
| Key | Value | Default（省略时） | 约束目标 |
| --- | --- | --- | --- |
| `mode` | `autonomous \| probe` | `autonomous` | 探索模式；省略时默认为 `autonomous`。 |
| `max_parallel` | `正整数` | 未设置；由 Agent 根据任务和资源决定。 | 约束 `SearchSpec.budget.max_parallel`。 |
| `max_duration_seconds` | `正整数（秒）` | 未设置；不创建命令级外层 deadline。 | 从 Goal 创建时间起固定外层 deadline，覆盖 worker、verifier 和收尾边界。 |
| `workspace_backend` | `copy \| git_worktree \| thinkthread` | 未设置；由 Agent 根据任务和 host 能力决定。 | 约束 `SearchSpec.workspace.backend`。 |
| `promotion_mode` | `apply \| artifact_only` | 未设置；由 Agent 根据任务授权和 workspace 决定。 | 约束 `SearchSpec.workspace.promotion_mode`。 |
| `strategy` | `NAME` | 未设置；由 Agent 选择 maintained strategy。 | 约束 `SearchSpec.strategy.name`。 |
| `inner_agent` | `ADAPTER_ID` | 未设置；沿用当前 host/backend 的兼容默认值。 | 约束 `SearchSpec.strategy.inner_agent`。 |
| `annotator` | `MODEL` | 未设置；不单独指定 annotator model。 | 约束 `SearchSpec.strategy.evidence_annotator.model`。 |
| `workers` / `models` | `MODEL[,MODEL...] 或 MODEL*COUNT[,MODEL*COUNT...]` | 未设置；worker 使用 host 的 parent/default model。 | 约束 `SearchSpec.strategy.models`；带 count 时每个模型都必须带 count。 |
<!-- END GENERATED GOAL PLUS COMMAND CONFIG -->

`main=` 和单数 `model=` 都不是受支持的配置。main model 必须在启动 Codex/Pi 时或
提交 Goal Plus 命令前通过 `/model` 选择；Goal Plus 不切换它。数据模型暂时保留 legacy
`command_config.main`，仅用于读取旧 `.gp` 记录，新建和编辑命令都会拒绝该 key。

Host command 入口只对这里显式出现的 `annotator`、`workers`/`models` 执行真实推理
preflight；未指定时不检查 Main default。后续 `search_freeze_spec` 同样只检查 spec 中显式写出的
worker、annotator 和 scheduler model。每个边界都按模型去重并并发请求，任一失败则在 agent
launch 前拒绝状态转换。检查使用 Codex/Pi 原生 model API，Goal Plus API 不接受 provider
配置、API key 或 endpoint。

解析后的 `GoalPlusRecord.command_config` 是显式用户约束，配置 token 不再保留在
`raw_goal` 中。高置信度且无 open question 的 spec draft 必须匹配所有显式 Search
配置，否则 `goal_plus_save_spec_draft` 拒绝保存；只有 `command_config` 中缺省的字段才由
Agent 根据目标、host 能力和资源信号决定。未知 key、重复 key、非法 enum、非正整数和不一致
的 worker count 都在创建 Goal Plus 记录前失败。

`max_duration_seconds` 不映射到 FrozenSpec。runtime 在创建 Goal Plus 记录时固定绝对
`outer_deadline_at`，并让关联 Search 的 host pool、worker launch 和 verifier 读取同一个
持久 deadline。达到时间条件后停止新增探索，但仍须选择、回写、复验并审计已经完成的有效结果。

Host-only edit 仍要求 `expected_revision`，防止 stale adapter 覆盖新目标。Search result 以
`run_id` 为键，因此一个 goal 可以保留多个 Search task；`search_tasks` 是仅追加历史，`linked_search`
只是当前 task 的兼容视图。

### Subagent Attempt

每条 Goal Plus record 默认遵循 [`goal-plus-ultra-v1`](ultra.md)。main 自行拆解任务；只有实际
派发普通 subagent 时才懒创建记录。Search 使用后文独立的 verifier-guided 状态。work event 是
持久协调事实，但启动、等待、通信、中断和原生日志仍由 host 所有。

`dispatch` 只生成 `attempt_id`、`generation` 和 launch
deadline。host 启动成功后记录 `bind`，并在 `message`、`result`、`failed` 中携带同一 attempt
identity。过期 generation 的迟到结果保留为 `stale_result` Evidence，不能修改当前 item。main 检查
`result_ready` 后记录 `accepted`、`rework` 或 `cancelled`。只有仍处于 `launching` 或 `active`
的派发会阻止 final check 与 completion。

## 创建 Search Run

### 冻结合同与创建 Run

| 工具                         | 用途                                                        |
| ---------------------------- | ----------------------------------------------------------- |
| `search_freeze_spec`         | 验证 origin 合同，hash-pin verifier，并固定 source artifact |
| `search_create`              | 校验 frozen source identity 后创建 `run_id`                 |
| `search_status`              | 读取 budget、candidate 和当前 best                          |
| `search_invalidate_run`      | main 确认 verifier 不充分后，原子 fence 当前 run            |
| `search_list_history`        | 按 candidate 排名，并汇总当前 run 的 feature/verifier 研究  |
| `search_list_iterations`     | 显式读取单个 candidate 可能很大的完整 verifier 历史         |
| `goal_plus_monitor_snapshot` | 只读汇总 goal/run/session/host Evidence                     |

`search_invalidate_run` 需要 typed verifier reason、非空 summary 和具体 Evidence。它把 run 置为
`aborted`，拒绝新的 planning、session、verifier record、selection 和 promotion。runtime 不拥有 host
worker，因此调用方还必须停止完整 host pool，并确认活跃 worker 归零。

MCP 的 `search_freeze_spec` 和 `search_create` 是 Goal Plus 内部 bootstrap surface，两者都要求
显式传入当前 `goal_plus_id`。host hook/extension 先核对该 ID 属于当前 native session，shared
facade 再要求目标是 active、host-verified Goal，并校验 successor `source_run_id` 的归属。
因此同一 `.gp` 可以包含多个 session 各自拥有的 active Goal，而不会靠“恰好一条 active 记录”
猜测目标。candidate worker 的既有 run 工具不依赖 bootstrap 参数；它们继续由绑定的
candidate/session identity 和 run owner 限制。

Git backend 的 freeze 会直接在 `SearchSpec.source_path` 执行一次完整 process-verifier suite，
并在成功后记录 `FrozenSpec.source_artifact_ref` 与 `origin_verification`。MainAgent 负责给出正确
的 `source_path`、verifier `cwd` 和 artifact 路径；runtime 不扫描目录来猜另一个 task root。
`search_create` 要求 origin 仍是同一个 clean commit。

本地 Git/Copy 的 process suite 在冻结前必须实际输出有限数值分数，即使没有显式
`ranking_signal`。只有正常退出或文本分数不足以冻结；缺失数值时返回
`MissingNumericMetric`，不创建 FrozenSpec。指标从 stdout 的单行 JSON 对象读取，
不读取任意结果文件，也不能用 promotion verifier 补足缺失的 process metric。

`search_start_batch` 对 Git run 先只物化 `c001`，在真实 worktree 中重跑同一 suite，并把结果
记录为 `RunRecord.workspace_bootstrap_verification`。通过后才物化其余 candidate。失败会自动以
`verifier_target_mismatch` abort run，保留 `c001`，不创建后续 candidate 或 agent session。
这两类 bootstrap Evidence 不属于 candidate iteration，不参与 score、ledger 或选择。

A new Codex/Pi spec uses `strategy.orchestration_mode="parallel_loops"` for fixed
candidates or `"adaptive_search"` for Scheduler-driven replacement.
`search_plan_next(requested_k)` can be called only once for either run.

SearchSpec cannot select an Agent family or execution driver. Freeze derives
`FrozenSpec.native_host` (`codex` or `pi`) from the authorized Main session and
binds it into the spec hash. `strategy.worker_host`, `worker_launch.agent_profile`,
`evidence_annotator.host`, and `search_scheduler.host` are rejected as extra fields.
Internal role drivers follow the frozen identity and workspace backend.
Their persisted profiles contain only resolved host/model/reasoning/timeout plus
`configuration_source="host_process"`; service authentication, endpoints, and host configuration
paths are not SearchSpec fields. Codex hooks and the Pi extension launch internal agents from the
active native host process, while the shared runtime only registers and validates their tasks.

Frozen records now require `native_host`. Records from the previous format, with
host selection inside `spec.strategy`, are not silently converted or repaired.
Finish those runs with their original plugin version, or start a new Goal and
freeze a new contract through the native host. Do not edit `.gp` to supply identity.

An `adaptive_search` run requires a non-null `search_scheduler` and still has one initial plan, but
may replace a retired lane through persisted runtime decisions. It requires a
Git worktree. `budget.max_parallel` is the live candidate limit; optional
`budget.max_candidates` limits cumulative unique candidates, while `null` is
unbounded and any finite value must be at least `max_parallel`. After hard settlement, the runtime
persists an `AnnotationTask`; only after it reaches `completed` or
`terminal_error` does the runtime persist a `ScheduleTask` with its compact
Evidence View projection. A recoverable, concurrent, one-shot scheduler call
applies reward, pruning, and optional derivation in order. Only a lazy-confirmation
task receives the all-candidate allocation context plus derivation instructions and may return a separate
Decision draft. `allocation_flag` is boolean (`false=continue`,
`true=reschedule`). `ScheduleTask.output` is authoritative; completed rewards
are exposed as a read-time list indexed from ScheduleTasks and are not copied
into a value-tree file or candidate/report record. Rewards retain explicit
settlement-id, ScheduleTask-id, and node-id bindings. A failed annotation supplies
`view=null` plus guidance to choose any compensation from historical observations;
the runtime defines no fixed compensation. There is no run-root tree, graph, value-backup,
action-edge, or hindsight projection. A verifier report may include
`scheduler_status`, `schedule_task_id`, and `allocation_decision`. The main agent
applies the exact Decision and launches every returned session payload; it does
not author a replacement. Reward never replaces the hard metric used by
`search_select`.

Lazy allocation gates only when the previous completed flag was `true`. An
ordinary `true` flag contains no plan. A current confirmation `true` must
include a separate draft; the Decision service
validates and persists it with retirees, rationale, reserved derived ids, exact
settled source nodes, exploration directions, and optional worker budgets. The
scheduler chooses how many available reserved ids to use; there is no separate
per-Decision replacement or expansion-depth setting. Tasks complete asynchronously; the runtime
does not wait or launch a model in the verifier call. While a confirmation task is unresolved,
continuation, redispatch, and another verifier call for that lane are rejected without polling.
Before a decision becomes visible, its triggering iteration has completed Git
and results-ledger settlement. AnnotationTask is a weak prerequisite for
ScheduleTask creation: it must be terminal, but `terminal_error` continues with
`view=null` and historical-compensation guidance. Missing annotation tasks are
idempotently recovered from durable iterations on later verifier settlements or
Global Evidence reads. Retirement
fences only the candidate's next iteration: it neither cancels View generation
nor removes the settled result from Global Evidence. While annotation is still
pending, `schedule_task_id` may be absent.
Hidden-answer QA benchmarks must not use this feedback path: gold-derived
correctness, reward, or allocation is an answer oracle. Keep only public format
validation worker-visible and score all final answers externally.

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
| `search_start_batch` | 执行 backend bootstrap gate，物化 candidate，并返回 `task_skill_notice`    |

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
catalog discovery 不是 inference preflight；现有 host gate 会对本次 spec 中显式 agent model
去重并并发发送最小请求。默认 model 不进入该集合。

## Worker Context

### Session 与 Context

| 工具                             | 调用方           | 用途                                                                         |
| -------------------------------- | ---------------- | ---------------------------------------------------------------------------- |
| `search_start_agent_session`     | main             | 创建 provenance handle 和 host 原生 launch payload                           |
| `search_redispatch_candidate`    | main             | 在同一 candidate workspace 创建 fresh session                                |
| `search_bind_agent_handle`       | main/host driver | 绑定 Codex 或 Pi 原生 handle                                                 |
| `search_continue_agent_session`  | main             | host 支持时返回原生同 worker continuation 字段                               |
| `search_get_agent_context`       | candidate worker | 加载精简的权威 candidate contract 与恢复数据                                 |
| `search_get_agent_observability` | main/monitor     | 读取单个 session 的归一化 model、timing、usage、artifact 和 handoff Evidence |
| `search_recovery_status`         | main             | 只读列出 run 各 lane 的完整 checkpoint、generation、恢复事务与旧 session     |
| `search_prepare_recovery`        | main             | 固定 checkpoint、隔离旧 generation，返回幂等 recovery ID；不改工作区         |
| `search_recover_candidate`       | main             | 宿主静默核验后，通过 provider 恢复 checkpoint；不 spawn 或重置预算           |

准备和执行恢复须匹配当前 Goal revision 的关联 run；只读查询不授予执行权。
`search_recovery_status` 接收 `run_id`；
`search_prepare_recovery` 增加 `candidate_id`；`search_recover_candidate` 再增加原样传回的
`recovery_id`。详细流程、数据保留与 provider 边界见 [Search 一致性恢复](search-recovery.md)。

`search_start_agent_session` 只创建 runtime handle，不启动 worker。调用方必须使用返回的 `launch`
对象。初始启动、continuation 或 redispatch 都可以提供 dispatch-scoped
`worker_budget`，不会修改 frozen spec；Pi pool 的 minimum lease 会在同一 job 的 native-session
resume 之间累计。

`search_get_agent_context` 默认返回有界结构：`latest_result`、`best_iteration` 和
`recent_iterations`，而不是完整 `results` / `iterations`。`result_count`、`iteration_count` 和
`result_ledger_source="gp"` 保留准确 provenance。`workspace_ledger_projection` 返回 binding 的
`none` 或 `results_tsv`；只有后者的 `results_tsv` 才是路径，否则为 `null`。`inner_agent` 返回
frozen logical adapter ID；它不会暴露 host-native role。持久化的完整 `CandidateTask` 只投影为
工作所需字段；model、directive、scheduler mode 和 run budget 不重复下发。ThinkThread
candidate 只额外返回受限的 logical workspace access，不暴露 Root 本地路径。只有确实需要旧
verifier 细节时才调用 `search_list_iterations`。

Git 与 ThinkThread outer provider 都支持 `inner_agent="autoresearch"` 和
`inner_agent="common"`。兼容 ID `autoresearch` 解析为 AutoResearch-Git：前者在一个 dispatch
或一组 retained TT wakes 中自主执行多轮，并由 trusted adapter 维护 private attempt commits 与
`results.tsv`；后者完成一个有界 attempt 后返回，不启用 inner Git ledger mechanics。两者都由
runtime 在 verifier 边界创建 authoritative outer checkpoint，worker 不需要也不得主动 commit。

开启 supplemental evaluation 时，context 才返回
`supplemental_evaluation_enabled=true`，`search_get_global_evidence` 只增加
`supplemental_available=true`；完整 summary、dimensions、peer comparisons 和 limitations
通过 `search_get_evidence_detail` 按 immutable row 获取。关闭时不暴露该能力。

Codex MCP 对 context、Global Evidence、detail 和完整 iteration history 使用单个紧凑
`TextContent`，避免同时把同一 JSON 放入 `content` 和 `structuredContent`。Pi-local facade
保留结构化 `details`，给模型的文本同样使用紧凑 JSON。

### Evidence 与按需详情

| 工具                         | 调用方           | 用途                                           |
| ---------------------------- | ---------------- | ---------------------------------------------- |
| `search_get_global_evidence` | candidate worker | 读取已结算的 Global Evidence                   |
| `search_get_evidence_detail` | candidate worker | 按需展开一条 available supplemental evaluation |
| `search_reference_open`      | candidate worker | 打开 Evidence 的精确 reference 目录            |

每个 verifier-backed iteration 都会把 commit、score、disposition 和可用时异步生成的 View
投影到 Global Evidence。`view=null` 表示 annotation 尚未发布；worker 应继续独立工作，不等待或轮询。

`artifact_ref={kind,id,provider}` 是新的跨 backend 身份；Git 下 `kind=git_commit`，旧 `commit`
字段继续保留。`artifact_availability` 为 `available`、`removal_pending`、`removed` 或 `unknown`；
只有 `available` 时 `reference_available=true`。窄 diff 足够时可以直接比较 commit；改动较大时用
`search_reference_open`
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
`hypothesis`，说明实际实施的尝试。Git/Pi RPC 的 `search_run_verifier` 同步返回包含
candidate-local `keep`、`retain`、`discard` 或 `failure` 的 `ScoreReport`。ThinkThread Message
proxy 因 quiescent snapshot 约束先返回 `CandidateAttemptAccepted`，完整
`CandidateAttemptFeedback` 随 `candidate_ready` 产生，并强制注入同一 retained Child 的下一次
wake。两条路径都向 canonical `.gp` ledger 追加准确记录；只有 inner binding 声明
`workspace_ledger_projection="results_tsv"` 时才另写并提交 `workspace/results.tsv` 兼容投影，
`common` 不生成该文件。ThinkThread AutoResearch-Git 的 context 返回逻辑路径 `results.tsv`，
但文件实际位于 Child-private workspace，Root 不会物化本地 candidate 路径。它的 public
`git_head` 仍为 `null`，因为外层 Evidence identity 是 attempt `fs_snapshot`。parent fallback
省略 `agent_session_id`，也不要求 `hypothesis`。

确定性的 verifier 失败也是 attempt 结果，不是缺失结果。Git backend 的 non-zero exit、timeout 和
process start failure 都返回失败 `ScoreReport` 并写入带 `attempt_id` 的 iteration；TT 已确认的
verifier/provider execution error 同样写失败 iteration。只有 provider completion outcome unknown
才进入 `needs_recovery`，此时不能伪造成功或失败记录。

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

启用 `shared_dir` 时，`search_get_agent_context.tool_family_catalog` 只暴露 family/head
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

ThinkThread run 的 `run.host_cleanup` 和 `search_tasks[].host_cleanup` 是不暴露物理路径的结构化
cleanup 投影。顶层 `state` 为 `pending`、`complete`、`retained`、`needs_recovery`、`failed` 或
`not_applicable`；`candidate_pools`、`annotation`、`snapshots` 和 `final_artifacts` 分别提供状态计数
与 `complete`，`blockers` 只包含语义原因。`retained` 表示 snapshot 仍被 publication 或 recovery
等 durable pin 引用，不等于删除失败。普通完成流程应读取该字段，不应猜测 session-private
`host-pools` 文件名。

`goal_plus_monitor_snapshot.feature_plugins` 提供可选的只读派生视图。内置 `orchestration`
plugin 统一展示普通 main/subagent 的 assignment summary、attempt
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

`git_worktree` 对 Git source 只接受 clean `HEAD`。
`search_freeze_spec` 在 verifier preflight 前检查整个 repository，`search_create` 在写入 run
前复检；staged、unstaged 或 non-ignored untracked 会返回 `DirtySourceWorkspace`。Runtime
不会自动 stash、clean 或 commit source；有意使用当前 filesystem 状态时改用
`copy + artifact_only`，或从受保护 ThinkThread Root 显式选择 `thinkthread`。普通非 Git 目录
仍沿用现有 synthetic Git baseline。`.gp`、`.search`、`.tmp` 等 runtime noise 不进入 product
delta。
MCP/Pi facade 的 `verifier_artifact_paths` 只接受相对 `source_path` 且不含 `..` 的路径；runtime
解析后再冻结，避免 host cwd 或绝对路径改变 artifact 归属。

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

Pi extension 还内部调用 `host_execution_scope_open`、`host_execution_scope_close_run`、
`host_execution_scope_close` 和 `host_execution_scope_snapshot`，把 pool、worker process 与
Annotation 绑定到 main owner。它们是 host adapter 控制面，不注册为模型可见工具；
`execution_scope_id` 也不会出现在模型可见的 pool schema 中。

同一 host control path 在 worker verifier 完成后调用一次 capability-protected internal-agent kick，
同时唤醒 Annotation 与 Adaptive Scheduler queue。该命令不是模型可见工具，不接收外部连接配置。

安装的 `goal-plus-pi` ThinkThread Profile 还暴露以下 host-local 工具。它们通过
Agent POSIX Control 通道运行，不进入共享 MCP server：

| 工具                                  | 用途                                                          |
| ------------------------------------- | ------------------------------------------------------------- |
| `thinkthread_search_pool_open`        | 从同一个 `S0` 启动 initial private Pi Children                |
| `thinkthread_search_pool_wait_any`    | 返回 ready Child 的 attempt feedback 与 snapshot Evidence     |
| `thinkthread_search_pool_snapshot`    | 读取或按 `run_id` 恢复 durable pool                           |
| `thinkthread_search_pool_continue`    | 注入上次 feedback 并唤醒同一 Child/Session/private branch     |
| `thinkthread_search_pool_close`       | drain 或 interrupt，并清理该 pool 拥有的 Child/branch         |
| `thinkthread_annotation_next`         | Annotation Child 领取 fenced immutable Evidence task          |
| `thinkthread_artifact_view_list`      | 列出授权 target/peer snapshot 目录                            |
| `thinkthread_artifact_view_stat`      | 读取授权 snapshot 路径元数据                                  |
| `thinkthread_artifact_view_pread`     | 分块读取授权 snapshot 文件                                    |
| `thinkthread_artifact_view_diff`      | 比较两个授权 snapshot                                         |
| `thinkthread_annotation_submit`       | 按 queue generation/revision fence 发布 annotation            |

candidate 与 Annotation Child 的 active tool 集合互斥。`search_select` 关闭 annotation
queue 并推进 generation 后，旧 task 的 submit 返回未发布。ThinkThread pool 状态位于
`.gp/host-pools/thinkthread/`，Annotation Child 状态位于
`.gp/host-pools/thinkthread-annotations/`。
Annotation Child 不激活命令执行或 verifier 工具，也不为读取 Artifact View 创建或 patch snapshot。

## 控制执行的 SearchSpec 字段

| 字段 | 含义 |
| --- | --- |
| `objective` | 可度量的优化目标 |
| `metric_name`, `metric_direction` | ranking value 和方向 |
| `source_path` | baseline source snapshot |
| `editable_globs`, `forbidden_globs` | candidate edit surface |
| `process_verifiers` | correctness gate |
| `ranking_signals` | 产生 metric 的 command |
| `promotion_verifiers` | promotion 前必须通过的检查 |
| `budget.max_parallel` | 初始 candidate 和活跃 worker 数 |
| `candidate_acceptance` | 受界且显式下发给 worker 的验收条件 |
| `constraints` | runtime 私有的结构化控制数据，不下发给 worker |
| `strategy.worker_budget` | host deadline 和可选 minimum lease |
| `workspace.backend` | `git_worktree`（默认）、`copy` 或 `thinkthread` |
| `workspace.promotion_mode` | `apply`（默认）或显式 `artifact_only` |
| `device_pools` | 可选的受信任 host pool ID；由 host 环境注册 |

`thinkthread` 只在 Pi `goal-plus-pi` Profile 中启用；Agent 身份由 Main 宿主确定。
freeze/create 会校验宿主身份与 workspace backend 的配对；Root
preflight 会精确校验 release、Control protocol、contract fingerprint、direct filesystem 和
delegated capability，任何不匹配都 fail closed。它不会静默退回 Git。每个 candidate 使用
独立 Child/Session/private branch；worker context 不暴露本地 workspace 路径。verifier、
Evidence、selection 和 promotion 都绑定 exact `fs_snapshot`。

ThinkThread `workspace.dependencies[].path` 相对 `source_path`，promotion 会将其规范化为 Root
snapshot path。`search_apply_promotion` 的终态是 `applied`、
`awaiting_main_integration` 或 `recovering`：前两者分别排队成功或 pending-integration 通知；
`recovering` 不发送这两类通知，也不生成 pending artifact。所有 provider mutation 使用 durable
RequestId；response loss 后只查询或重放同一 identity。业务 mutation outcome 与 request close
状态分别持久化；close 未确认时只重试 `fs.request.close`，不会重做已经成功的 mutation。report durable 后，cleanup 删除未被 active
Evidence consumer、pending publication、recovery、其他 active run、外部 owner 或 active host pool
引用的 owned snapshot。已删除 snapshot 的 identity 和派生 Evidence/Annotation 继续保存在 `.gp`，
monitor 的 `artifact_availability` 显示 `removed`，但不支持重开完整 Artifact View。删除 Child、
private branch 或 snapshot 不会删除 score、diff、annotation、provider journal 或最终 report。

每条 ranking command 必须成功退出，并输出最后一个 JSON object，其中 `metric_name` 对应 finite
number。临时 verifier 输出必须位于 `GOAL_PLUS_VERIFIER_TMPDIR`；verifier artifact 和 evaluation
input 都会用 hash 固定。

每个引用 device pool 必须至少提供 `budget.max_parallel` 个设备，且同一 spec 的多个 pool
不能复用同一个可见变量。lane 与设备一一对应。active run 通过
`GOAL_PLUS_VERIFIER_RESOURCE_LOCK_DIR` 下的 host pool lease 防止跨 run 超售；共享设备的多个
进程或容器必须使用同一个共享锁目录。

## 错误语义

- Validation error：调用方必须修正 spec 或 tool argument。
- Candidate verifier failure：正常 Search Evidence，不是 runtime 故障。
- `VerifierWorkspaceSideEffect`：evaluator 破坏隔离；停止 candidate，修复后重新 freeze。
- Frozen artifact hash mismatch：本次 scoring 无效。
- 已确认 verifier defect：先 `search_invalidate_run`，再停止 host 并确认静默，最后创建 successor
  run。
- Host timeout：证明 deadline enforcement 生效；runner failure：需要 host recovery Evidence。

完整状态机和所有权边界见[共享平面](shared-plane.md)。
