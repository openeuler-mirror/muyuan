# API

`goal-plus --root .gp` 提供 host-neutral MCP 接口。运行中 server 返回的
tool schema 和说明始终是权威定义；本文说明各组工具在一次 Goal Plus /
Search 流程中的位置、调用方和所有权。

Pi extension 的共同业务工具直接加载 MCP tools/list 的 description 与 inputSchema，调用
通过标准 MCP stdio Client 进入同一服务，同一 runtime root 的调用复用连接，宿主 shutdown
时关闭；Pi/TT 专属控制仍由宿主适配。宿主绑定的 candidate MCP
只公开候选工具，并要求调用携带自身 agent_session_id；role 与绑定身份不是模型参数。
Session 准备回执返回身份、工作区和选定模型，handle 不展开 metadata；完整 launch 输入留在持久 session 中，
由可信宿主启动器读取。执行边界见 [Agent Host Adapters](agent-host-adapters.md)。

所有模型可见工具使用 `goal_plus_` 前缀；只注册当前名称，不保留旧名别名。
Main 使用稳定目录，candidate/checker 按角色与冻结能力过滤；宿主 gate 不进入模型目录。
Codex 只识别已注册的 Goal Plus MCP namespace，其他插件的同名动作不会获得授权。

宿主将嵌套参数显示为 `unknown` 时，Main 可用 `goal_plus_get_tool_schema(tool_name)`
读取当前注册工具的完整 `inputSchema` 与说明。名称不带 MCP namespace；查询不改变状态，
不暴露当前角色未注册的工具，也不维护第二份 schema。

工具 schema 用本地 `$defs/$ref` 复用重复结构，并合并等价的基础类型联合；不裁剪字段、
约束或可选功能。`shared_dir` 由 Main 选择 `enabled`，按需覆盖工具数、发布版本数、文件数、
路径条目数、深度和字节限额；省略时使用默认值。模型 provenance/context policy 和固定动作由 runtime 补齐。
`strategy.models[]` 分配各模型席位；`worker_launch` 设置全局 model、reasoning_effort、
service_tier 默认值，各席位的非空设置优先。
`strategy.global_evidence_mode` 直接选择证据共享方式，不再接受任意 `strategy.config`；
`allocation.policy` 由 runtime 固定，Main 只配置 features 和 switch_margin。

`goal_plus_session_run(run_id, candidate_id, call_id, prompt, timeout_seconds, new_session=false)`
内部准备领域 session、打开 native session、绑定准确身份并执行一轮任务，立即返回
session/call identity、实际状态及错误。后续 run 默认复用当前 session；worker 在一轮内自主工作。
本地调用超时、stop 或 MCP 重启后，宿主核验旧执行资源清理完成，下一次新 call 的 run
内部恢复同一个 Codex/Pi 原生会话；保留上下文、candidate generation、Evidence 和预算。
这不需要 `new_session=true`，也不重放旧调用；新一轮使用新的 call_id。
同一 candidate 的相同 call_id 与相同参数返回原调用状态，参数冲突或旧 generation 请求被拒绝。
控制器重建后旧 call 的完整结果不可用时返回 `SessionCallHistoryUnavailable`；
Main 读取 candidate/Evidence 状态后使用新 call_id 继续，不重发原操作。
已有活动调用时拒绝不同 call_id，steering 使用 `goal_plus_session_message`，不排队或自动中断。

`new_session=true` 先关闭空闲旧资源并确认清理，再隔离旧 generation、创建新 session。
已有代码、Evidence 和累计预算保留。原 native session 无法加载时也可使用该入口，
直接接手包含未验证修改的 workspace。冻结 worker budget 和 Goal deadline 限制整轮执行额度；
wait 的 timeout 只控制读取等待，不延长额度、不取消执行。
`goal_plus_session_wait` 默认返回准确状态与错误，`detail=true` 才读取正文和诊断；
completed 表示调用返回，不能代替 verifier 通过。Search Evidence 用
`goal_plus_search_list_history` 查询；已注入的 Evidence 无需机械重复读取。
wait 的 `timeout_seconds` 上限为 300 秒：用户输入只能在 turn 边界交付，单次长阻塞
会锁死用户唯一的插话出口。结果未就绪时使用短等待或结束当前 turn，由宿主 Stop
continuation 重新点火；需要向活动调用插话时改用 `goal_plus_session_message`。

`goal_plus_session_interrupt` 中断指定轮次，随后仍需确认结果；stop 停止执行，
close 关闭 controller 和资源并保留历史及 artifact。Main 暂停后仍可读取与清理。
TT stop 后，下一次 run 可在重新授权、确认原调用退出后重开 retained Child。
本地 controller 重建读取持久 native handle 和用量基线；未完成验证须先结算或恢复，
普通未提交修改保留，显式 checkpoint recovery 才回滚 workspace。
TT 的同请求 run 可通过准确调用日志接管原 Message，只观察与完成原结算，不重发 spawn/wake，
不改变 deadline 或 generation。Codex TT 仍不支持，见 [Codex 支持范围](codex.md#支持范围与验证)。
TT interrupt/message 通过准确控制通道取得原生确认；确认超时不能声明执行已停止。
恢复流程见 [Search 一致性恢复](search-recovery.md)，Main 控制合同见 [运行逻辑](runtime-logic.md)。

## 错误返回

gate 类拒绝消息携带权威持久状态（control state/reason、status）与恢复指引：
暂停返回 `GoalPlusResumeRequired`（含 reason 与 resume 命令提示），终态返回
`GoalPlusTerminal`，等待用户输入返回 `GoalPlusUserInputRequired`。模型应把消息中的
恢复步骤直接转述给用户，而不是猜测内部失败原因；只读历史工具在终态下仍可用。

Main 概要来自同一份 [instructions](../assets/goal-plus-instructions.txt)：
Codex 经 MCP 和可信 Goal hook context 接收，
Pi 从 MCP manifest 读取并在每轮 system prompt 中装配，candidate/内部角色不接收 Main 概要。
详细参数、调用语义和下一步由工具 schema/description 与状态回执提供。

## 一次完整流程

普通 Goal Plus 任务和 Search Mode 使用同一条上层生命周期：

1. Codex `$goal-plus` 或 Pi `/goal-plus` 的 host adapter 在模型轮次前把 typed command
   config 与自然语言目标分离，保存两者并记录 host-command invocation Evidence；
2. main 默认直接执行原始目标，无需调用 `goal_plus_record_triage`；实际派发普通 subagent 时，
   由 `goal_plus_record_work_event` 按 dispatch/bind/result 记录轻量 attempt；
3. 需要 Search 时准备评价合同，可用 `goal_plus_record_triage` 记录判断与缺项；
4. 完整 SearchSpec 只交给 `goal_plus_search_freeze_spec` 一次，再通过带 request_id 的
   `goal_plus_search_create` 创建并自动关联当前 Goal；
5. `goal_plus_search_plan_next` 和 `goal_plus_search_start_batch` 创建初始 candidate，通过共同 session 工具启动本地候选；
6. worker 循环读取 context/Evidence、修改 workspace、调用 `goal_plus_search_run_verifier`；
7. 达到全局停止条件后，host 先停止所有 worker，再执行 `goal_plus_search_select` 和
   `goal_plus_search_promote`；任务授权写回 source 时再执行 `goal_plus_search_apply_promotion`；
8. main 检查普通 subagent 和 Search 结果，完成
   `goal_plus_record_search_result`、final check 和终态审计，最后为每个已记录 run 调用一次 `goal_plus_search_report`。

Search runtime 不启动或监督 worker。Codex/Pi 的启动、wait-any、continuation 和 interrupt 由
host 负责，详见
[Agent Host Adapters](agent-host-adapters.md)。

## Goal Plus 工具

| 工具                             | 用途                                                                         |
| -------------------------------- | ---------------------------------------------------------------------------- |
| `goal_plus_status`               | 默认读取当前状态和待办；`detail=true` 按需读取目标、配置与草案               |
| `goal_plus_history`              | 分页读取 revision、dispatch、Search 结果、final check 或审计事件             |
| `goal_plus_record_triage`        | 按需记录 Search 判断与缺项；普通 coding 无需调用                             |
| `goal_plus_record_work_event`    | 按实际派发懒创建 subagent 记录，并管理 generation、结果和 Main 决策          |
| `goal_plus_list_models`          | 列出 Codex 或 Pi host 当前可用模型                                           |
| `goal_plus_get_tool_schema`      | Main 按需读取当前注册工具的完整参数合同                                      |
| `goal_plus_record_search_result` | 绑定 selection/promotion Evidence，并预留 final report 路径                  |
| `goal_plus_run_final_check`      | 启动只读 reviewer，等待结构化结论并关闭原生资源                              |
| `goal_plus_run_work_item`        | 运行普通子任务，支持同 session 返工；Main 审查结果                           |
| `goal_plus_submit_final_check`   | 为准确 revision 记录 reviewer verdict                                        |
| `goal_plus_set_status`           | 设置 Evidence-backed terminal 或 paused state                                |

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
Search 运行状态和分数使用 `goal_plus_search_status`；冻结后的 worker budget、host/model 配置、并发数、
workspace provider 和 promotion mode 可在 `goal_plus_monitor_snapshot(detail=true)` 的 `strategy` 中读取。

两个视图都不回放 revision/事件历史。`detail` 替代旧的 `view` 参数；不同时维护两个开关。
恢复待办保留 run/candidate/generation/recovery identity、`next_step` 和诊断错误；checkpoint
和历史 session 通过 `goal_plus_search_recovery_status` 按需读取。未完成或待审查工作不因数量限制被隐藏。

`goal_plus_history` 的 `collection` 支持 `goal_revisions`、`work_items`、`search_tasks`、
`final_checks`、`events`，按持久顺序返回完整条目。每页默认 20 条、最多 100 条，返回
`total` 与 `next_offset`；`next_offset=null` 表示当前读完。它不是跨状态变更的快照游标。
条目正文不截断，读取成本仍取决于现有持久文件大小。

成功变更返回新增身份、实际状态、必要输出位置及解析差异，不回显输入或完整配置。
work event 保留 attempt/generation 与结果摘要；Search result 保留准确 artifact 与必要结算事实。
完整证据保存在历史中。iterations/allocation 每页默认 20 条、最多 100 条，使用 offset/limit，
返回 items、total 和 next_offset；allocation 默认只查 pending，status=null 查询全部。

`goal_plus_run_final_check(goal_plus_id)` 内部准备、启动并等待独立只读 checker，随后清理资源。
它核对当前 Main、revision/control version；重复请求不启动另一个活动 checker，已通过结论可重读。
失败后修复再调用同一工具。回包保留结构化 verdict、findings、evidence 和 native 调用状态，
只有准确绑定的 checker 可调用 submit。普通正文不构成通过证据，GoalCompletion 决定终态。
普通 work item 保留完整交付正文一次，不重复回显 result_summary。

`goal_plus_monitor_snapshot` 默认返回进度计数、最佳/选中结果、审查队列、恢复动作、scope 和错误。
成功 candidate/session 的逐项结果、配置、artifact 路径和历史诊断使用 `detail=true`；直接
Python Monitor/dashboard 接口仍保留完整 snapshot。异常、违规、清理错误与未知活跃数不被隐藏。
candidate 条目附带 `last_verifier_at` 与 `avg_round_seconds`（相邻两次结算 verifier 的平均
间隔，少于两次结算时为 null），供运行中进度展示推算当前轮耗时与预计结算剩余。

### Host Command Config

`/goal-plus` 和 `$goal-plus` 只把开头连续的 `key=value` token 解释为配置；第一个非配置
token 开始就是自然语言目标：

```text
/goal-plus max_parallel=2 max_duration_seconds=300 workspace_provider=git_worktree promotion_mode=artifact_only strategy=agent_guided Optimize the read-only template.
```

支持的 typed key 是（此表由 runtime command option registry 自动生成）：

<!-- BEGIN GENERATED GOAL PLUS COMMAND CONFIG -->
| Key | Value | Default（省略时） | 约束目标 |
| --- | --- | --- | --- |
| `mode` | `autonomous \| probe` | `autonomous` | 探索模式；省略时默认为 `autonomous`。 |
| `max_parallel` | `正整数` | 未设置；由 Agent 根据任务和资源决定。 | 约束 `SearchSpec.budget.max_parallel`。 |
| `max_duration_seconds` | `正整数（秒）` | 未设置；不创建命令级外层 deadline。 | 从 Goal 创建时间起固定外层 deadline，覆盖 worker、verifier 和收尾边界。 |
| `workspace_provider` | `copy \| git_worktree \| thinkthread` | 未设置；由 Agent 根据任务和 host 能力决定。 | 约束 `SearchSpec.workspace.provider`。 |
| `promotion_mode` | `apply \| artifact_only` | 未设置；由 Agent 根据任务授权和 workspace 决定。 | 约束 `SearchSpec.workspace.promotion_mode`。 |
| `strategy` | `NAME` | 未设置；由 Agent 选择 maintained strategy。 | 约束 `SearchSpec.strategy.name`。 |
| `inner_agent` | `ADAPTER_ID` | 未设置；direct 默认 autoresearch，ThinkThread 默认 common。 | 约束 `SearchSpec.strategy.inner_agent`。 |
| `annotator` | `MODEL` | 未设置；不单独指定 annotator model。 | 约束 `SearchSpec.strategy.evidence_annotator.model`。 |
| `workers` / `models` | `MODEL[,MODEL...] 或 MODEL*COUNT[,MODEL*COUNT...]` | 未设置；worker 使用 host 的 parent/default model。 | 约束 `SearchSpec.strategy.models`；带 count 时每个模型都必须带 count。 |
<!-- END GENERATED GOAL PLUS COMMAND CONFIG -->

`main=` 和单数 `model=` 都不是受支持的配置。main model 必须在启动 Codex/Pi 时或
提交 Goal Plus 命令前通过 `/model` 选择；Goal Plus 不切换它。数据模型暂时保留 legacy
`command_config.main`，仅用于读取旧 `.gp` 记录，新建和编辑命令都会拒绝该 key。

Host command 入口只对这里显式出现的 `annotator`、`workers`/`models` 执行真实推理
preflight；未指定时不检查 Main default。后续 `goal_plus_search_freeze_spec` 同样只检查 spec 中显式写出的
worker 和 annotator model。每个边界都按模型去重并并发请求，任一失败则在 agent
launch 前拒绝状态转换。检查使用 Codex/Pi 原生 model API，Goal Plus API 不接受 provider
配置、API key 或 endpoint。

解析后的 `GoalPlusRecord.command_config` 是显式用户约束，配置 token 不再保留在
`raw_goal` 中。freeze 准入校验完整 SearchSpec 与所有显式 command config，
不再要求 ready draft。未知字段、非法枚举、非正整数和不一致的 worker count 仍由严格 schema 拒绝。
`workers=A` 只约束模型，不要求 spec 省略 count；`workers=A*3` 才固定该模型的 count 为 3。
spec 不填 count 时按 round-robin 分配；填写时必须全部填写，且总和等于 `max_parallel`。
历史草稿通过 status detail 读取；未解决的评价问题使用 triage 记录。

`max_duration_seconds` 不映射到 FrozenSpec。runtime 在创建 Goal Plus 记录时固定绝对
`outer_deadline_at`，并让关联 Search 的 session 调用、worker launch 和 verifier 读取同一个
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

完整 SearchSpec 仅在 `goal_plus_search_freeze_spec` 提交，FrozenSpec 绑定 Goal revision。
`goal_plus_search_create(goal_plus_id, frozen_spec_id, request_id, source_run_id?)` 在副作用前
保存请求指纹、revision/control version 与预分配 run ID。相同请求恢复原创建进度，
不同参数冲突；创建、bootstrap 和关联分阶段提交。未知 provider 结果使用原 journal 确认，
不盲目重发。关联前重核 Goal；暂停或编辑不能把旧请求关联到新 revision。
未关联 run 不允许启动候选，失败请求可在 Goal status 查询；关联回包丢失时重试返回原 run。
本地候选和 provider artifact 的清理由各自 owner 保持精确归属。

### 返回内容边界

共同工具使用以下返回合同；只裁剪模型回执，不裁剪领域状态或 host 执行输入。

| 工具类别 | 默认返回 |
| --- | --- |
| Session 控制 | 操作回执、准确 identity/state/error；wait 正文显式 `detail=true` |
| Goal 变更、最终检查 submit | 当前 Goal/检查 identity、状态和下一步；不回显草案、提交证据或总结 |
| Goal status/history、Monitor | 默认状态摘要；显式详情或分页历史保留完整信息 |
| Search freeze/create/plan/batch | 新建 ID、实际分配数量/模型；固定 work orders 保留待派发任务 |
| Search invalidate、promotion/apply | 当前状态、精确身份、输出位置、变更文件及错误；不回显内部 provider 记录 |
| Session run | session/call identity 与实际状态；完整 launch 由可信宿主消费 |
| Recovery、observability、模型目录 | 恢复身份及所需状态、显式请求的诊断或模型发现结果 |
| Candidate context/Evidence/verifier/reference/shared tool | 权威工作合同、新验证结果、可见证据及生成的路径/工具身份；这些内容供 worker 决策 |

`goal_plus_search_get_evidence_detail`、`goal_plus_search_list_iterations` 和 AllocationDecision 查询属于显式详情，
保留证据、历史及可执行计划。它们不应作为 Main 的日常状态轮询。

### 冻结合同与创建 Run

| 工具 | 用途 |
| --- | --- |
| `goal_plus_search_freeze_spec` | 验证 origin 合同，hash-pin verifier，并固定 source artifact |
| `goal_plus_search_create` | 校验 frozen source identity 后创建 `run_id` |
| `goal_plus_search_status` | 读取 budget、candidate 和当前 best |
| `goal_plus_search_invalidate_run` | main 确认 verifier 不充分后，原子 fence 当前 run |
| `goal_plus_search_list_history` | 按 candidate 排名，并汇总当前 run 的 feature/verifier 研究 |
| `goal_plus_search_list_iterations` | 显式读取单个 candidate 可能很大的完整 verifier 历史 |
| `goal_plus_monitor_snapshot` | 只读汇总 goal/run/session/host Evidence |

`goal_plus_search_list_history` 默认返回候选排名、分数及其 Evidence 来源、变更文件、异常和一份
当前 run 的研究汇总。best 与 latest 验证结果不一致、latest 被回滚或失败时单独返回
`latest`；未验证的分数与验证结果保持 `null`。研究汇总仍包含排名之外候选的发现，
`top_n` 只限制排名候选数。空研究不展开，存在 predecessor 研究时用
`inherited_research_available` 提示。`detail=true` 显式读取完整 session/dispatch、日志路径、
配置、研究和失效证据；完整 iteration 仍通过 `goal_plus_search_list_iterations` 读取。

`goal_plus_search_invalidate_run` 需要 typed verifier reason、非空 summary 和具体 Evidence。它把 run 置为
`aborted`，拒绝新的 planning、session、verifier record、selection 和 promotion。runtime 不拥有 host
worker，因此调用方还必须通过共同 session stop/close 停止所属候选，并确认资源退出。

MCP 的 `goal_plus_search_freeze_spec` 和 `goal_plus_search_create` 是 Goal Plus 内部 bootstrap surface，两者都要求
显式传入当前 `goal_plus_id`。host hook/extension 先核对该 ID 属于当前 native session，shared
facade 再要求目标是 active、host-verified Goal，并校验 successor `source_run_id` 的归属。
因此同一 `.gp` 可以包含多个 session 各自拥有的 active Goal，而不会靠“恰好一条 active 记录”
猜测目标。candidate worker 的既有 run 工具不依赖 bootstrap 参数；它们继续由绑定的
candidate/session identity 和 run owner 限制。

freeze 成功返回 `frozen_spec_id`、`state="frozen"`、`metric_name`、`baseline={evaluation_completed,passed,score}`
和 `promotion_verifiers` 名称；ThinkThread 的 baseline 尚未执行，返回 `null`。容量不足附带 warning。
完整 FrozenSpec、verifier hash、技能清单和容量估算仍持久化。冻结尚未创建 run，
后续 `goal_plus_search_create` 使用返回 ID；正常回执不回显提交的 SearchSpec。
`strategy.name` 为 `agent_guided`（别名 agent/default）或 `random`（别名 random_mode），
不是自定义标签；前者需要 candidate proposals。名称按原规则忽略大小写并将连字符转为下划线。
`worker_budget` 不限时省略，否则至少填写一个正数上限。策略与宿主能力在 baseline 前校验。

Git freeze 在 `SearchSpec.source_path` 原位、Copy 在一次性源码副本执行完整 process suite，
结果保存为 `origin_verification`；Git 同时固定 `source_artifact_ref`。Main 负责给出正确
的 `source_path`、verifier `cwd` 和 artifact 路径；runtime 不扫描目录来猜另一个 task root。
`goal_plus_search_create` 要求 origin 仍是同一个 clean commit。

本地 Git/Copy 的 process suite 在冻结前必须实际输出有限数值分数，即使没有显式
`ranking_signal`。只有正常退出或文本分数不足以冻结；缺失数值时返回
`MissingNumericMetric`，不创建 FrozenSpec。指标从 stdout 最后一个可解析的单行 JSON 对象读取，
允许混合日志，精确匹配 `metric_name`，不猜别名、不读取结果文件或运行时元数据。
`returncode`、`elapsed_seconds` 为保留字段。每个 suite 至多一个 `ranking_signal`，由它供分；
未声明时只允许一个非 diagnostic/anti-cheat verifier 产出分数。promotion 不能补足 process metric。
stdout JSON 可用 `outcome=valid/invalid/error` 区分有效候选、确定无效候选、评估故障。

| outcome | 退出码与诊断 | 含义 |
| --- | --- | --- |
| `valid` 或省略 | 退出 0，`error` 缺失/null | 候选通过 |
| `invalid` | 退出 0/1，保留 `error` | `CandidateRejected`；有分数可作基线，但不能选中或提升 |
| `error` | 保留退出码与 `error` | 评估故障，阻止冻结 |

`valid` 布尔字段只作一致性检查：`valid=false` 必须显式分类，不能与 `outcome=valid` 并存；
`valid=true` 不能与 `outcome=invalid` 并存。未知 outcome 拒绝。
未知故障不得转为 invalid，超时、中断、anti-cheat 失败仍阻止启动；诊断角色失败不阻断 suite。
`candidate_acceptance` 只是 worker 文字要求。`promotion_verifiers` 仅提升时执行，freeze 不证明其可用。
`goal_plus_search_promote` 自动执行已配置的 promotion suite，无需先手动验证。
Bootstrap 失败直接返回 verifier 名称、失败类别、已有的退出码/错误摘要及日志路径，无需重跑获取原因。

`goal_plus_search_start_batch` 对 Git run 先只物化 `c001`，在真实 worktree 中重跑同一 suite，并把结果
记录为 `RunRecord.workspace_bootstrap_verification`。通过后才物化其余 candidate。失败会自动以
`verifier_target_mismatch` abort run，保留 `c001`，不创建后续 candidate 或 agent session。
这两类 bootstrap Evidence 不属于 candidate iteration，不参与 score、ledger 或选择。

SearchSpec 使用 `workspace.provider` 选择工作区，不接受 agent 身份或执行托管方式。
Freeze 从已授权 Main session 固定 `agent_harness=codex|pi`，由装配层解析
`runtime_provider=direct|thinkthread`；FrozenSpec 必须保存两者并纳入 spec hash。
Session 的 `agent_harness`、`runtime_provider` 必须与 `session_handle` 及冻结绑定一致。
角色 profile 同样保存这两个字段、model/reasoning/timeout 和
`configuration_source="host_process"`；服务认证、endpoint 和宿主配置路径不进入 SearchSpec。
Candidate/Annotation 使用冻结绑定；LLM Verifier、delegation 和 final checker 使用直接执行路径。
最终检查使用 `checker_harness`。模型侧使用 run/wait/message/interrupt/stop/close 六个 session 工具。

旧 `native_host`、`worker_host`、`host_handle`、`workspace.backend` 等字段不提供 alias。
旧 build 负责关闭活动资源并收尾旧记录，新 build 创建新记录，不能手工补写 `.gp` 身份。
新字段由可信接线补齐；自然语言启动不增加必填参数。

An `adaptive_search` run requires `strategy.allocation` and a Git worktree, and
automatically runs the LLM Verifier. Outside Adaptive Search, including `quality_score`
in `strategy.selection.ranking_keys` enables the same verifier. The allocation policy
version owns the additional LLM scoring rules. It
still has one initial plan, but may replace a retired lane through persisted
runtime decisions. `budget.max_parallel` is the live candidate limit; optional
`budget.max_candidates` limits cumulative unique candidates, while `null` is
unbounded and any finite value must be at least `max_parallel`.

After hard settlement, the runtime idempotently persists one `VerifierTask`.
The reliable asynchronous drainer uses one structured LLM call to produce one
immutable `VerifierRecord` bound to the exact candidate, iteration, settlement,
commit and artifact hash. Adaptive mode includes every metric defined by the frozen policy
under `allocation_scores`; minimal mode requires that field to be `null`. Missing signals remain explicitly
`pending`, `terminal_error`, or `unavailable`, never numeric zero.

Adaptive reconciliation builds an immutable `SearchSnapshot`, runs the frozen
pure FeaturePipeline and `priority_replace/v1` policy, and persists the resulting
canonical `AllocationDecision`. The LLM does not return a winner, allocation flag,
or executable plan. Candidate ids are assigned only when the Executor applies a
pending Decision. Apply revalidates the Snapshot, exact settled source, retire
eligibility, capacity and budget under the run lock, and is recoverable after a
partial retirement, materialization or session write.

Verifier tasks accumulate in the run-scoped reliable queue and are consumed one
at a time without blocking candidate start or continuation. Final `goal_plus_search_select`
resolves every configured ranking source for the exact subject and uses inclusive progressive tolerances.
Pending required scores wait; unavailable scores exclude an option; an empty pool
becomes `selection_blocked` without runtime fallback. Hard integrity and promotion
gates remain mandatory even when `hard_score` is not a ranking key.

The main agent lists and applies the exact pending Decision and launches every
returned session payload; it does not author a replacement. Retirement neither
cancels Evidence generation nor removes settled results from Global Evidence.
Hidden-answer QA benchmarks must not use this feedback path: gold-derived
correctness, reward, or allocation is an answer oracle. Keep only public format
validation worker-visible and score all final answers externally.

只有原合同无法继续使用时才创建 successor：

```text
goal_plus_search_create(goal_plus_id, new_frozen_spec_id, request_id, source_run_id=invalidated_or_exhausted_run)
```

新 run 的 `inherited_research` 可以包含 predecessor frontier、feature ledger 和 scoped
pitfalls，但 predecessor score 会明确标记为不可复用。

### 初始 Candidate 分配

| 工具 | 用途 |
| --- | --- |
| `goal_plus_search_plan_next` | 持久化唯一一次初始 candidate 分配 |
| `goal_plus_search_start_batch` | 执行 workspace_provider bootstrap gate，物化 candidate，并返回 candidate ID、provider 和选定模型 |

两种 orchestration mode 都只有一次初始分配。`parallel_loops` 中
`goal_plus_search_plan_next(requested_k)` 的数量为：

```text
min(requested_k, remaining max_parallel)
```

标准流程传入 `requested_k=budget.max_parallel`。`budget.max_parallel`
同时决定初始 candidate 数和活跃 worker 数。`parallel_loops` 后续只继续既有 candidate；
`adaptive_search` 的替换另经持久 AllocationDecision。

`goal_plus_search_start_batch` 首次成功调用会固定该 plan 的批次。需要两个候选时，在同一次调用的
`proposals` 列表中提供两个 proposal；后续同 plan 调用只返回已有 candidate，不追加或替换，
即使传入不同的 proposals 也是如此。create 成功回执已完成 Goal 关联；
物化后逐个调用 `goal_plus_session_run`。
batch 回执不包含完整 CandidateTask、instructions 或 workspace 内部 handle；宿主从持久
task/session 交付工作合同，worker 通过 `goal_plus_search_get_agent_context` 读取自己的工作区与技能清单。

多模型 run 先调用 `goal_plus_list_models(agent_harness=..., runtime_provider=...)`，再把用户选择写入 frozen `strategy.models`。没有
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

| 工具 | 调用方 | 用途 |
| --- | --- | --- |
| `goal_plus_search_get_agent_context` | candidate worker | 加载精简的权威 candidate contract 与恢复数据 |
| `goal_plus_search_get_agent_observability` | main/monitor | 读取单个 session 的归一化 model、timing、usage、artifact 和 handoff Evidence |
| `goal_plus_search_recovery_status` | main | 只读列出 run 各 lane 的 checkpoint、generation、恢复事务与 session 概要；`detail=true` 附完整 session_handle 与恢复记录 |
| `goal_plus_search_prepare_recovery` | main | 固定 checkpoint、隔离旧 generation，返回幂等 recovery ID；不改工作区 |
| `goal_plus_search_recover_candidate` | main | 宿主静默核验后，通过 provider 恢复 checkpoint；不 spawn 或重置预算 |

准备和执行恢复须匹配当前 Goal revision 的关联 run；只读查询不授予执行权。
`goal_plus_search_recovery_status` 接收 `run_id`；
`goal_plus_search_prepare_recovery` 增加 `candidate_id`；`goal_plus_search_recover_candidate` 再增加原样传回的
`recovery_id`。详细流程、数据保留与 provider 边界见 [Search 一致性恢复](search-recovery.md)。

Main 通过 `goal_plus_session_run` 准备并执行 candidate。领域 session identity 是
context/provenance 绑定，不代表进程存活；完整 launch 与准确 handle 由 host 内部接线。
恢复回执保留必须清理的旧 session 身份，run 不能越过 recovery fence。

`goal_plus_search_get_agent_context` 默认返回有界结构：`latest_result`、`best_iteration` 和
`recent_iterations`，而不是完整 `results` / `iterations`。`result_count`、`iteration_count` 和
`result_ledger_source="gp"` 保留准确 provenance。`workspace_ledger_projection` 返回 binding 的
`none` 或 `results_tsv`；只有后者的 `results_tsv` 才是路径，否则为 `null`。`inner_agent` 返回
frozen logical adapter ID；它不会暴露 host-native role。持久化的完整 `CandidateTask` 只投影为
工作所需字段；model、directive、orchestration mode 和 run budget 不重复下发。ThinkThread
candidate 只额外返回受限的 logical workspace access，不暴露 Root 本地路径。只有确实需要旧
verifier 细节时才调用 `goal_plus_search_list_iterations`。

Git 与 ThinkThread outer provider 都支持 `inner_agent="autoresearch"` 和
`inner_agent="common"`。兼容 ID `autoresearch` 解析为 AutoResearch-Git：前者在一个 dispatch
或一组 retained TT wakes 中自主执行多轮，并由 trusted adapter 维护 private attempt commits 与
`results.tsv`；后者完成一个有界 attempt 后返回，不启用 inner Git ledger mechanics。两者都由
runtime 在 verifier 边界创建 authoritative outer checkpoint，worker 不需要也不得主动 commit。

开启 supplemental evaluation 时，context 才返回
`supplemental_evaluation_enabled=true`，`goal_plus_search_get_global_evidence` 只增加
`supplemental_available=true`；完整 summary、dimensions、peer comparisons 和 limitations
通过 `goal_plus_search_get_evidence_detail` 按 immutable row 获取。关闭时不暴露该能力。

Codex MCP 对 context、Global Evidence、detail 和完整 iteration history 使用单个紧凑
`TextContent`，避免同时把同一 JSON 放入 `content` 和 `structuredContent`。Pi-local facade
保留结构化 `details`，给模型的文本同样使用紧凑 JSON。

### Evidence 与按需详情

| 工具 | 调用方 | 用途 |
| ---------------------------- | ---------------- | ---------------------------------------------- |
| `goal_plus_search_get_global_evidence` | candidate worker | 读取已结算的 Global Evidence |
| `goal_plus_search_get_evidence_detail` | candidate worker | 按需展开一条 available supplemental evaluation |
| `goal_plus_search_reference_open` | candidate worker | 打开 Evidence 的精确 reference 目录 |

每个 verifier-backed iteration 都会把 commit、score、disposition 和可用时异步生成的 View
投影到 Global Evidence。`view=null` 表示 annotation 尚未发布；worker 应继续独立工作，不等待或轮询。

`artifact_ref={kind,id,provider}` 是新的跨 workspace_provider 身份；Git 下 `kind=git_commit`，旧 `commit`
字段继续保留。`artifact_availability` 为 `available`、`removal_pending`、`removed` 或 `unknown`；
只有 `available` 时 `reference_available=true`。窄 diff 足够时可以直接比较 commit；改动较大时用
`goal_plus_search_reference_open`
把完整树 checkout 到该 worker 的稳定 `ref-workspace` 目录，然后直接使用普通文件和命令工具查看。每个
`agent_session_id` 最多有一个 reference 目录；再次调用会丢弃该目录的本地变化，
并切到新的精确 artifact。reference 不是 peer 的活动 workspace，所有候选实现修改仍发生在自己的 candidate
workspace。runtime 不要求 worker 关闭 reference；目录随 run state 保留，数量受实际 worker 数限制。

`strategy.global_evidence_mode` 决定 candidate 如何收到 Evidence：

- `manual`：默认模式，由 candidate 显式读取共享 run view；
- `auto`：除显式读取外，每次成功的 worker process verifier 后注入 `global_evidence_snapshot`；
- `independent`：不自动注入，显式读取也只返回当前 candidate 自己的 Evidence。

parent/promotion verification 不进入 peer Evidence。快照失败只增加
`global_evidence_warning`，不会改变 verifier result。`GOAL_PLUS_GLOBAL_EVIDENCE_MODE`
可以在 freeze 前提供默认值；如果与显式 spec 配置冲突，freeze 会拒绝。

每次 Evidence 读取都会在 agent session 中保存 `global_evidence_reads`
receipt，记录读取时间和当时可见的 completed candidate/iteration/commit
View。receipt 只用于报告“某个 View 是否在后续尝试前可见”，不参与 settlement、selection、promotion 或 PASS/FAIL。

`goal_plus_search_get_agent_context` 的 `supplemental_evaluation_enabled`
告诉 worker 是否存在按需详情。关闭时不等待也不请求；开启时，窄 Global Evidence 只增加
`supplemental_available=true`，完整 summary、dimensions、peer comparisons 和 limitations 通过
`goal_plus_search_get_evidence_detail` 获取。`independent` 模式只能展开当前 candidate。完整 annotation 语义见
[Evidence Annotator](evidence-annotator.md)。

### 修改、验证与共享工具

| 工具 | 调用方 | 用途 |
| --- | --- | --- |
| `goal_plus_search_run_verifier` | candidate worker / parent fallback | 提交准确 attempt；带 attempt_id 时查询或等待原结果 |
| `goal_plus_search_stage_shared_tool` | candidate worker | 从 `.tmp/tool-drafts/` stage 新工具或 `tool_family_catalog` head 的实质 revision |
| `goal_plus_search_copy_shared_tool` | candidate worker | 将 Tool View 绑定的 immutable snapshot 复制到本地 inbox，供阅读或自主复用 |

worker 调用 process verifier 时必须提供一行客观
`hypothesis`，说明实际实施的尝试。本地 Codex/Pi 共用 MCP：提交不可变快照后短等，完成时返回包含
candidate-local `keep`、`retain`、`discard` 或 `failure` 的 `ScoreReport`，未完成则返回 pending 和
准确 attempt_id。通过同一工具带 attempt_id 继续等待，不再传 hypothesis、toolization_decision
或 promotion scope；timeout_seconds 仅限制查询等待，范围为 0–60，默认 30，0 表示只查询。
等待结束不会取消验证或启动新 attempt，worker 保持工作区不变直到结算。
资源等待不消耗 verifier 执行 timeout；执行仍遵守冻结的时限。ThinkThread Message
proxy 因 quiescent snapshot 约束先返回 `CandidateAttemptAccepted`，完整
`CandidateAttemptFeedback` 在准确 invocation 完成结算时产生，并注入同一 retained Child 的下一次
wake。回执的 candidate_action 为 wait_for_verifier 时查询原 attempt，为 end_turn 时结束
当前 turn 等待宿主反馈。TT 查询 accepted attempt 不重新封存或提交；结算后返回准确 ScoreReport。
两条路径都向 canonical `.gp` ledger 追加准确记录；只有 inner binding 声明
`workspace_ledger_projection="results_tsv"` 时才另写并提交 `workspace/results.tsv` 兼容投影，
`common` 不生成该文件。ThinkThread AutoResearch-Git 的 context 返回逻辑路径 `results.tsv`，
但文件实际位于 Child-private workspace，Root 不会物化本地 candidate 路径。它的 public
`git_head` 仍为 `null`，因为外层 Evidence identity 是 attempt `fs_snapshot`。parent fallback
省略 `agent_session_id`，也不要求 `hypothesis`。

确定性的 verifier 失败也是 attempt 结果，不是缺失结果。Git workspace_provider 的 non-zero exit、timeout 和
process start failure 都返回失败 `ScoreReport` 并写入带 `attempt_id` 的 iteration；TT 已确认的
verifier/provider execution error 同样写失败 iteration。执行结果未知或本地工作区无法同步时进入
`needs_recovery`；前者不伪造成功或失败记录，后者保留已结算 Evidence。
本地 attempt 可用同一工具传 `attempt_id` 和 `retry=true` 恢复：复用已完成结果或重验提交快照，
幂等结算并保留当前 workspace。仍在执行时拒绝重复启动；记录不足时明确报错，由 Main 决定
是否恢复 checkpoint。ThinkThread 仍由 retained attempt owner 恢复，不使用本地 retry 参数。

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
`goal_plus_search_copy_shared_tool`；下一次 verifier 原子消费 copy
receipt，并把 adoption 记录到该 iteration。复制只证明 snapshot 进入本轮 context，不证明原工具被执行或保留；只有直接执行、导入或作为 runtime
dependency 时，才需要先在当前 workspace 验证原工具。完整路径为：

```text
staging -> passing process verifier -> immutable snapshot -> Tool View -> Global Evidence -> copy receipt -> adopted tool
```

Tool View 和 adoption 只提供研究参考，不产生独立工具分数或 selection
rule。完整共享平面语义见[共享平面](shared-plane.md#global-evidence)。

启用 `shared_dir` 时，`goal_plus_search_get_agent_context.tool_family_catalog` 只暴露 family/head
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
concern 在 main 调用 `goal_plus_search_invalidate_run` 前只是 advisory，不是 run-wide deny list。

`goal_plus_search_get_agent_observability` 使用统一、版本化的跨 host schema。version 2 增加
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
`not_applicable`；`candidate_sessions`、`annotation`、`snapshots` 和 `final_artifacts` 分别提供状态计数
与 `complete`，`blockers` 只包含语义原因。`retained` 表示 snapshot 仍被 publication 或 recovery
等 durable pin 引用，不等于删除失败。普通完成流程应读取该字段，不应猜测 session-private
`host-pools` 文件名。

`goal_plus_monitor_snapshot.feature_plugins` 提供可选的只读派生视图。内置 `orchestration`
plugin 统一展示普通 main/subagent 的 assignment summary、attempt
generation、assignment/result/decision，同时保留 Codex/Pi 的原生 operation 名。传入
`feature_plugins=["orchestration"]` 可显式选择，传入空数组可关闭全部投影。详见
[Monitor Feature Plugin](adaptive-search.md#monitor-feature-plugin)。

## Selection、Promotion 与 Report

- `goal_plus_search_select`：按 ranking 恢复 immutable artifact，并选择通过 final verifier 的状态。
- `goal_plus_search_promote`：运行 promotion gate，并准备 provider-native publication。
- `goal_plus_search_apply_promotion`：将 publication 显式写回原 Git repo 或 ThinkThread Root。
- `goal_plus_search_report`：生成最终 `report.md` 和 self-contained `report.html`。

`goal_plus_search_select` 基于 verifier-backed immutable revision。promotion 是独立验收，不复用 process
pass；失败时 run 保持可重试，且不输出 patch。

新 Git run 会完整 mirror 原 repository 和历史，并保留 candidate 的全部 running commits。
`goal_plus_search_promote` 另行合成一个以 source baseline 为 parent 的 squash publication commit；
`goal_plus_search_apply_promotion` 将它 cherry-pick 回原仓。旧 run 的 `legacy_patch` 仍返回
`apply_cwd` 和 `apply_directory`，并从 `apply_cwd` 执行
`git apply --directory=<apply_directory> <artifact_path>`。
`artifact_only` 回执保留手动应用所需的这两个坐标；由 `goal_plus_search_apply_promotion` 执行的
publication 不重复返回它们。apply 回执保留状态、变更文件、目标路径（若已知）与错误，
完整 provider handle、timing 和 request journal 仍在持久状态中。

`git_worktree` 对 Git source 只接受 clean `HEAD`。
`goal_plus_search_freeze_spec` 在 verifier preflight 前检查整个 repository，`goal_plus_search_create` 在写入 run
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

持久化的 `FrozenSpec.workspace_capacity_estimate` 在 candidate 创建前记录 source scope 的当前
大小、有效 materialized source、完整 Git checkout 的文件数/每 node 字节数、
`budget.max_parallel` 个 node 的合计、共享 object store、预计增量、保守上界、可用空间和
`fits_available_space`。Git source 的 node 估算按整个 repository checkout，而不是只按
`source_path` 子目录；本地 `--local` mirror 通常 hard-link object，但保守上界仍将 object
store 再计一次。该结构也进入 monitor statistics 和最终 HTML report。
`fits_available_space=false` 时 `goal_plus_search_create` 会在创建 run 或 candidate 前拒绝执行。

关联 Goal Plus record 时，正常顺序是 select -> promote -> apply -> record Search result ->
final audit -> terminal status -> report。`promotion_mode="apply"` 尚未 applied 时 runtime 会
拒绝记录 Search result。只有显式 `artifact_only` 的受保护 source、只读 template 或测试
fixture 停在 prepared publication；`goal_plus_search_report` 会拒绝 active 或 needs-user record，
确保静态报告不会捕获中间态。

`goal_plus_record_search_result` 始终预留该 run 的 `report.md` 与 `report.html`，不要求文件
已经生成；模型传入的 `report_path` 不会覆盖 runtime 路径，外部评测回执也不是 Search 报告。

`report.html` 汇总全部关联 Search task、candidate/session/verifier Evidence、main agent
usage、metric gap 和独立时间线。worker
bar 使用观测到的 host 起止时间，配置 budget 不会伪装成实际运行时长。安装 `report`
extra 后使用 Plotly 绘制每个 candidate 的 verifier trajectory 和 global
best-so-far；没有 Plotly 时使用确定性的内联 SVG fallback。

报告只绘制持久状态中真实存在的数据，不从 prose 猜测 search-space
coordinate、embedding 或缺失 metric。HTML 内联 CSS/JavaScript，可直接打开；`report.md`
是稳定文本产物。详细生成和恢复方法见[调试运行时状态](debugging-runtime.md)。

## Pi 宿主接线

本地 Pi 和 ThinkThread 候选使用共同 MCP session 工具；`pi_search_pool_*` 和
`thinkthread_search_pool_*` 已从 extension 注册和 CLI
转发中删除。宿主诊断仍可经 `goal-plus-pi-tool` 读取同一 runtime，例如：

```bash
goal-plus-pi-tool goal_plus_monitor_snapshot \
  --root .gp \
  --args-json '{"run_id":"run_..."}' \
  --pretty
```

Pi extension 还内部调用 `host_execution_scope_open`、`host_execution_scope_close_run`、
`host_execution_scope_close` 和 `host_execution_scope_snapshot`，把 session、worker process 与
Annotation 绑定到 main owner。它们是 host adapter 控制面，不注册为模型可见工具；
`execution_scope_id` 也不会出现在模型可见的工具 schema 中。

同一 host control path 在 worker verifier 完成后调用一次 capability-protected internal-agent kick，
同时唤醒 Annotation 与 LLM Verifier queue。该命令不是模型可见工具，不接收外部连接配置。

ThinkThread 的 session run/wait/message/interrupt/stop/close 均经共同 MCP。
同请求 run 返回原控制器的 `call_id`，控制器丢失后可用它 wait 原调用；接管保留
原 Child、Message 和执行截止时间，不重新发送 spawn/wake。message 在当前 turn 经认证
Parent Message 及 Pi 原生 steer 注入，必须收到准确的接收确认；普通邮箱写入不代表成功。

安装的 `goal-plus-pi` ThinkThread Profile 还为 Annotation 暴露以下 host-local 工具。它们通过
Agent POSIX Control 通道运行，不进入共享 MCP server：

| 工具 | 用途 |
| --- | --- |
| `goal_plus_thinkthread_annotation_next` | Annotation Child 领取 fenced immutable Evidence task |
| `goal_plus_thinkthread_artifact_view_list` | 列出授权 target/peer snapshot 目录 |
| `goal_plus_thinkthread_artifact_view_stat` | 读取授权 snapshot 路径元数据 |
| `goal_plus_thinkthread_artifact_view_pread` | 分块读取授权 snapshot 文件 |
| `goal_plus_thinkthread_artifact_view_diff` | 比较两个授权 snapshot |
| `goal_plus_thinkthread_annotation_submit` | 按 queue generation/revision fence 发布 annotation |

candidate 与 Annotation Child 的 active tool 集合互斥。`goal_plus_search_select` 关闭 annotation
queue 并推进 generation 后，旧 task 的 submit 返回未发布。ThinkThread candidate 资源和
当前调用日志位于 `<runtime>/host-scopes/<scope>/thinkthread-sessions/`；Annotation Child
状态位于 `<runtime>/host-pools/thinkthread-annotations/`。
Annotation Child 不激活命令执行或 verifier 工具，也不为读取 Artifact View 创建或 patch snapshot。

## 控制执行的 SearchSpec 字段

| 字段 | 含义 |
| --- | --- |
| `objective` | 可度量的优化目标 |
| `metric_name`, `metric_direction` | ranking value 和方向 |
| `source_path` | baseline source snapshot |
| `edit_surface.allow`, `edit_surface.deny` | candidate edit surface |
| `process_verifiers` | 评分、硬 gate 与可选诊断命令；`role=ranking_signal` 指定评分来源 |
| `promotion_verifiers` | promotion 前必须通过的检查 |
| `budget.max_parallel` | 初始 candidate 和活跃 worker 数 |
| `candidate_acceptance` | 下发给 worker 的文字要求，不自动执行验收 |
| `constraints` | runtime 私有的结构化控制数据，不下发给 worker |
| `strategy.worker_budget` | 可省略的单次执行上限；不强制最低时长或验证次数 |
| `workspace.provider` | `git_worktree`（默认）、`copy` 或 `thinkthread` |
| `workspace.promotion_mode` | `apply`（默认）或显式 `artifact_only` |
| `device_pools` | 可选的受信任 host pool ID；由 host 环境注册 |

`thinkthread` 只在 Pi `goal-plus-pi` Profile 中启用；Agent 身份由 Main 宿主确定。
freeze/create 会校验宿主身份与 workspace provider 的配对；Root
preflight 校验实际接口、direct filesystem 和 delegated capability；release、Control protocol
和 contract fingerprint 偏离已测基线时告警。它不会静默退回 Git。每个 candidate 使用
独立 Child/Session/private branch；worker context 不暴露本地 workspace 路径。verifier、
Evidence、selection 和 promotion 都绑定 exact `fs_snapshot`。

ThinkThread `workspace.dependencies[].path` 相对 `source_path`，promotion 会将其规范化为 Root
snapshot path。`goal_plus_search_apply_promotion` 的终态是 `applied`、
`awaiting_main_integration` 或 `recovering`：前两者分别排队成功或 pending-integration 通知；
`recovering` 不发送这两类通知，也不生成 pending artifact。所有 provider mutation 使用 durable
RequestId；response loss 后只查询或重放同一 identity。业务 mutation outcome 与 request close
状态分别持久化；close 未确认时只重试 `fs.request.close`，不会重做已经成功的 mutation。report durable 后，cleanup 删除未被 active
Evidence consumer、pending publication、recovery、其他 active run、外部 owner 或活跃 session 资源
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
- 已确认 verifier defect：先 `goal_plus_search_invalidate_run`，再停止 host 并确认静默，最后创建 successor
  run。
- Host timeout：证明 deadline enforcement 生效；runner failure：需要 host recovery Evidence。

完整状态机和所有权边界见[共享平面](shared-plane.md)。
