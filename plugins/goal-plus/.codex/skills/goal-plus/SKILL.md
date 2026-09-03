---
name: goal-plus
description: 仅供 Goal Plus Main Agent 运行、恢复或编辑 Codex Goal Plus（含 $goal-plus-with-final-check），并通过 goal-plus MCP 编排 Agentic Search；不适用于 Search candidate worker。
---

# Codex 的 Goal Plus

此 skill 仅供 Goal Plus Main Agent 使用，不适用于 Search candidate worker。

对 `$goal-plus` 使用此 skill，这是 Ultra 编排工作流。主 Agent 请求 max 推理强度，
主动委派独立工作；当成功标准可度量且已冻结时，再升级为多候选 Agentic Search。

使用 `goal-plus` MCP server 暴露的逻辑 `goal_plus_*` 和 `search_*` 工具。
Codex 可能显示带客户端特定前缀的 MCP 工具；按最后的逻辑工具名匹配。
Codex 把这些能力暴露为原生 MCP tools；直接调用当前 tool registry 中与逻辑名匹配的
工具，例如 `goal_plus_status`。成功调用会留下 `mcp_tool_call` 事件。
`list_mcp_resources` 和 `list_mcp_resource_templates` 只枚举 MCP resource，不枚举 tool，
空结果也不表示 Goal Plus tool 不可用；不得用它们做 tool discovery。不得通过
`exec_command`、shell、Python 或手写 stdio client 调用 Goal Plus server，也不得用
这类旁路代替上述原生 MCP tool call。

## 工作流

1. 首先读取隐藏的 Codex hook 上下文。只有其中明确包含本轮由精确
   `$goal-plus`、`$goal-plus edit`、`$goal-plus resume` 或
   `$goal-plus-with-final-check` host command 授权的 active `goal_plus_id` 时，
   才能执行此工作流。skill 本身不能创建、编辑、恢复或授权 Goal Plus；不得调用
   `goal_plus_create` 或 `goal_plus_update_goal`，也不得通过 MCP、CLI、shell、Python
   或手写 stdio client 绕过 host 入口。如果缺少该上下文，立即停止所有 Goal Plus / Search
   变更，并要求用户使用精确的 `$goal-plus <完整目标>`、`$goal-plus edit <完整目标>`
   或 `$goal-plus resume`。
   仅在新记录的首轮，由 Main 语义检查原始目标或当前对话上下文是否明确给出了整个 Goal
   Plus 任务的最大运行时间；如果没有，先简短告知用户：“当前未设置最大运行时间，Goal
   Plus 将持续运行直至目标完成或用户手动停止。”这只是告知，不询问、不等待确认，随后
   立即继续执行。
   `$goal-plus-with-final-check` 会预先创建，并设置
   `policy.final_check.mode="required"`。`$goal-plus edit <完整的修订目标>` 会由 host
   在模型轮次前更新同一记录；使用新的 `goal_revision`，不要继续旧修订版。
   host 中断后，`$goal-plus resume` 会恢复同一个 active 修订版。
   `$goal-plus mode=autonomous <目标>`（默认）要求为初始候选分配充足 lease，
   并在没有全局停止条件时继续同一候选；`$goal-plus mode=probe <目标>`
   要求进行短期可行性、潜力和阻塞因素探查。未指定 mode 的编辑保留当前选择。
   显式 mode 保存在 `GoalPlusRecord.command_config`，对应执行指导仍写入 `raw_goal`
   的规范末行；它不是 phase 或 Search strategy。
   host adapter 会把开头连续的 typed `key=value` 配置与自然语言目标分开；第一个非配置
   token 开始就是目标。`command_config` 中的显式字段必须原样落实。只有缺省字段才由 Main
   判断。不要从 `raw_goal` 重新解析配置，也不要用自然语言覆盖显式配置。
   恢复 active 记录前，把最新用户消息视为本轮的权威依据：
   - 如果它继续或引导现有目标，但没有改变范围、交付物或成功标准，保留当前修订版。
   - 如果它改变了实际范围、交付物或成功标准，停止变更并要求用户使用精确的
     `$goal-plus edit <完整修订目标>`；不得由模型调用 `goal_plus_update_goal`。
   - 如果消息与目标无关，直接回复而不改变目标。如果它与目标的关系不明确，
     在修订或恢复前先澄清。不要仅因 Goal Plus 记录处于 active 就恢复工作。
2. 检查足够的上下文以分类任务。
3. 调用 `goal_plus_record_triage`。
4. triage 后直接执行原始目标。主 Agent 自己决定拆分、并行、等待、追加消息和重试；
   只有实际派发普通 subagent 时才记录该派发。Goal Mode 下不要创建 SearchSpec。
5. 如果 triage 选择 Spec Discovery Mode，确定 baseline、metric、正确性门禁、编辑范围、
   verifier 产物、预算和提升规则。ranking verifier 必须输出一个最终 JSON 对象，
   其中包含有限数值类型的 `spec.metric_name`；其文件应放在源码拥有的路径，
   例如 `.goal-plus-verifiers/`，绝不能放在 `.gp/` 或 `.search/`。
   `expected_outputs` 只列出产物路径或 glob，不解析 stdout。要求 verifier 保持候选
   工作区只读，并使用唯一的 `GOAL_PLUS_VERIFIER_TMPDIR`/`TMPDIR`（或 Python
   `tempfile`）存放编译器和临时输出；并行 Search 下固定 `/tmp` 路径不安全。
   Git freeze 会先在 MainAgent 给出的 origin `source_path` 运行一次 process-verifier
   suite，验证后复检 clean HEAD 与 verifier hash，并把该 commit 固定到 FrozenSpec；
   `search_create` 会拒绝已切换到另一个 clean HEAD 的 source。第一次
   `search_start_batch` 随后先在 `c001` worktree 重复该 suite，通过后才创建其余 lane。
   relocation 失败时 run 会自动 abort 并保留 `c001`，不得启动 worker；修复
   `source_path`、verifier `cwd` 或 artifact 合同后重新 freeze/create。两次 bootstrap
   验证都不属于 candidate iteration。先把
   `GoalPlusRecord.command_config` 中的 `max_parallel`、`workspace_backend`、
   `promotion_mode`、`strategy`、`annotator` 和 `workers` 映射到 SearchSpec；ready draft
   不匹配时 runtime 会拒绝保存。未配置字段才根据目标、host 能力和资源信号决定。
   并行 candidate 使用单卡 accelerator 时，只在 SearchSpec `device_pools` 中引用 host
   预先注册的 `pool_id`，并让相关 verifier 使用 `<pool_id>:0` 作为 `resource_lock`。
   host 通过 `GOAL_PLUS_DEVICE_POOLS` 注册非敏感设备清单；SearchSpec 不能指定或读取任意
   环境变量。`budget.max_parallel` 不得超过任一引用 pool 的设备数；runtime 在 freeze 时固定
   pool，并在 active run 期间独占它。把需要 worker 遵守的公开验收条件显式写入
   `candidate_acceptance`；`constraints` 是 runtime 私有配置，不会下发给 worker。
   冻结操作会在消耗候选预算前拒绝工作区副作用。新 SearchSpec 必须显式设置
   `workspace.backend="git_worktree"`，以便候选共享 Git object database 并解析彼此的
   Evidence commit；`workspace.promotion_mode` 默认是 `apply`。只有受保护 source、只读
   template 或测试 fixture 才显式设置 `promotion_mode="artifact_only"`；`copy` backend
   自动使用 artifact-only。只有用户明确要求兼容隔离时才能设置 `copy`。任务本地 Skill 无需写入
   SearchSpec；把它提交到 source Git 的 `.agents/skills/<name>/SKILL.md`，同一 Skill 目录内
   的所有资源也必须由 Git 跟踪。freeze 返回的
   `task_skill_notice` 会列出自动发现结果；`workspace_capacity_notice` 会同时展示当前 source
   大小、按 `budget.max_parallel` 计算的 node checkout、共享 Git object store、保守上界和
   可用空间。必须在 `search_create` 前展示该预检；`fits_available_space=false` 时不得创建
   run。每个 candidate 的 `task_skill_paths` 会指向自己
   worktree 中的对应文件；`search_start_batch` 还会为每个 subagent 返回一条启动清单。
   将这些清单明确展示出来，不扫描 parent 或 global/plugin Skill。使用
   `git_worktree` 以及未来的 ThinkThread backend 时，source Git 必须对应 clean `HEAD`。
   `DirtySourceWorkspace` 发生后原样展示路径和修复提示并停止启动；不得自动 stash、clean
   或 commit source。确实需要当前 filesystem 状态时显式改用 `copy + artifact_only`。
   `goal_plus_save_spec_draft` 保存完整契约。
   创建 draft 和冻结 verifier 前，Main 必须把当前 Search 工作项从原始目标与公开任务契约
   继承的正确性、性能、适用范围和测量方法落实为统一验收契约。candidate iteration、
   selection、promotion 和最终验收必须遵循同一标准；允许使用预先声明的不同成本或覆盖规模，
   但不得临时改变标准。worker 只优化冻结契约。若契约有缺陷，调用
   `search_invalidate_run` 后修复并重新冻结，不能直接比较新旧契约结果。
   不要在 SearchSpec 中生成软 rubric 或预设评价维度。Spec Discovery 只能冻结硬 metric、
   verifier、编辑范围、预算和 promotion 合同。开放式补充评价发生在每次 Evidence 结算之后：
   独立 annotator 根据当前候选累计 diff 和当时其他已结算候选的快照，自行提出与任务实际
   相关的观察维度并动态比较。它不读取 hidden 数据，不产生总分或最终推荐，也不改变硬
   PASS/FAIL、数值排名、candidate-local 结算、selection 或 promotion。MainAgent
   不负责定义这些维度，也不要根据 benchmark 类型向 annotator 预埋固定清单。
   Codex 的 main agent 就是已经运行的当前 session；必须在启动 Codex 时或提交
   `$goal-plus` 前通过 `/model` 选择，不能由 Goal Plus prompt 切换。`main=` 不是受支持的
   Goal Plus 命令配置；真正的角色模型配置使用
   `annotator=B` 和 `workers=C,D`，并必须调用
   `goal_plus_list_models(host="codex")`，将用户填写的名称解析为唯一可用模型；
   annotator 冻结到 `strategy.evidence_annotator.model`，workers 冻结到
   `strategy.models`。不存在或不唯一时，在创建 run 前直接返回错误。旧的
   `models=` 是 `workers=` 的兼容别名，两者不能同时填写；`main=` 和单数 `model=` 无效。
   `workers=A,B max_parallel=4` 表示按 A、B、A、B 轮转；
   `workers=A*1,B*3 max_parallel=4` 表示显式 A1B3，所有显式数量之和必须等于
   `max_parallel`。省略的角色保持 Codex host 默认或继承语义，不从另一个
   显式角色猜默认值；没有填写 `annotator=`、`workers=` 或 `models=` 时不要探测目录。
   对于用语义、大致 shape/dtype 和参考提示描述的 AscendC Direct Invoke 算子目标，
   记录 `scenario="ascendc_direct_invoke"`，并完整读取
   `examples/ascendc-direct-search/SPEC_DISCOVERY.md`。遵循其中的 request schema
   和源码模板。针对准确固定的 Git commit，使用 `knowledge.sources.json` 运行
   `materialize_knowledge.py` 生成任务局部 `_skills/`；绝不能复制 live Skill 目录。
   将精编的 AKG AscendC tree 作为主要知识，只对未覆盖的算子类别使用声明的 CANNBot
   补充。主 agent 必须在 Spec Discovery 期间生成 Golden、cases、verifier、baseline
   和 SearchSpec。调用 `search_freeze_spec` 前，使用 JSON Schema validator 按照
   `examples/ascendc-direct-search/request.schema.json` 校验生成的
   `_task/operator_request.json`；仅做 JSON 解析或手动字段清单检查不够，校验失败会
   阻止冻结。
   绝不能要求用户运行任务准备器、提供任务目录或编写 verifier。仅支持 Direct Invoke；
   生成的知识是只读的，不能启动源码 Agent 或 Plugin 工作流。该场景自包含；
   不要调用外部 AscendC Agent、plugin 或编排工作流。
6. 只有已保存 draft 的 `confidence="high"` 且没有 open question 时，才进入 Search Mode。
7. Search 是自主升级。draft 达到高置信度且无 open question 后，直接进入 Search Mode
   gate，不要要求用户批准 verifier、metric、编辑范围、提升规则或 mode 变化。
   用户提示是有用证据，但不是必要条件。
8. 将 `origin="initial"` 或 `origin="in_progress"` 仅作为 provenance。
   它不能改变已准备好的 search draft 是否可以继续。
9. 调用 `search_freeze_spec` 等 Search Mode 工具前，调用
   `goal_plus_gate(event="pre_tool_use", context={"tool_name": "search_freeze_spec"})`。
10. 在 Search Mode 中使用内部 `search` skill：默认以
    `strategy.orchestration_mode="parallel_loops"` 冻结固定初始候选的 Codex spec。只有目标或 benchmark
    明确要求 runtime reward/allocation 时才使用 `adaptive_search`；恢复已使用该模式的 spec 时也先
    读取本 skill 目录下的 `references/adaptive-search.md`。所有 scheduler 专属行为都由该文件定义。
    Search 只做一次初始规划。依次使用
    `search_freeze_spec`、`search_create`、`search_plan_next`、
    `search_start_batch`、`search_start_agent_session`、最终
    `search_run_verifier`、`search_select` 和 `search_promote`。任务授权写回 source 时，
    对新的 provider-native promotion 调用 `search_apply_promotion`；artifact-only 或受保护
    source 任务保留 prepared publication，不执行 apply。旧 `legacy_patch` 仅在任务授权时
    按 `search_promote` 返回的坐标写回。
11. `search_create` 后调用 `goal_plus_link_search_run`。
12. 选择并提升后，在任务授权时完成 apply，再调用 `goal_plus_record_search_result`。此时不要调用
    `search_report`。结果记录只预留规范 Markdown 和 HTML 路径，不创建报告。
13. 执行原始目标审计。如果还需要其他有 verifier 支持的 Search，使用相同
    `goal_plus_id` 冻结/创建新 run，并重复步骤 9-12。每个不同的 `run_id`
    都作为另一项 Search 任务追加；不要为新的冻结 spec 复用旧 `run_id`。
14. 主 Agent 必须检查普通 subagent 返回的代码、证据和测试；采用时记录 `accepted`，
    需要原 worker 补充时记录 `rework`，不采用时记录 `cancelled`。这些决定用于监控，
    不替代整体测试。最后执行一次原始目标审计。对于普通 Goal Plus 记录，只有当前目标已满足时
    才调用
    `goal_plus_set_status(status="complete", evidence=[...])`。当
    目标给出预设时长或时间预算时，把它作为停止新工作和衡量效率的独立观测条件：若必需工作、
    验证、提升和审计已经完成，即使实际收尾晚于预设，也仍使用 `complete`。在 evidence 和最终
    展示中分别记录预设时间与实际完成收尾时间，不要输出 `late`，也不要仅因超出预设而使用
    `blocked`；`blocked` 只表示仍缺少外部输入、权限、资源或状态变化，导致任务确实无法继续。
    当
    `policy.final_check.mode="required"` 时：
    - 调用 `goal_plus_prepare_final_check(checker_host="codex")`
    - 把 `launch.task_name`、`launch.message` 和 `launch.fork_turns` 映射到当前
      `spawn_agent` schema，并在前台启动；只有 schema 明确列出
      `goal_plus_final_checker` 时使用 `launch.agent_type`，否则使用
      `launch.fallback_agent_type="default"`，不得自行选择其他角色
    - 使用 `fork_turns="none"`；审查员必须从工作区和运行时证据重建结果，
      不能继承父级 transcript
    - 等待审查员返回；它必须自行调用 `goal_plus_submit_final_check`
    - 失败时处理其所有发现并准备新检查；绝不能代表审查员提交结论
    通过的必需检查会原子地把 Goal Plus 记录标记为 complete。
15. 只有 Goal Plus 记录达到终态（`complete`、`blocked` 或 `abandoned`）后，
    才对每个成功记录的 `run_id` 调用且只调用一次 `search_report`。
    绝不能生成中间 Goal Plus 报告。向用户返回最终 Markdown 和 HTML 路径。
    通过的必需最终检查属于终态，因为它会原子地把记录标记为 complete。
16. 停止前调用 `goal_plus_gate(event="stop", context={})`；如果它返回继续 prompt，
    则继续工作。

## Ultra 编排

Codex hook 把本次执行绑定到 host-neutral `goal-plus-ultra-v1`，但已开始的首轮 reasoning
effort 不能由 hook 修改；因此需要由 Codex host 在首轮开始前提供真实 max effort，
不能把 prompt 请求当作已生效证据。

对普通 subagent，在实际派发前记录 `dispatch`；首次使用该 `work_item_id` 时 runtime 会按
summary 自动创建轻量记录并返回 `attempt_id`、`generation`，随后调用
Codex `spawn_agent`；dispatch 使用
`metadata={"orchestration_monitor":{"native_operation":"spawn_agent"}}`。把清楚、完整的
任务直接放入 summary 和 worker message，并使用 `fork_turns="none"`。spawn 成功后立即用原 attempt、generation、实际 handle 和相同的
`orchestration_monitor` operation 记录 `bind`；
只有 bound attempt 才能记录 `message`、`result` 或 `failed`。恢复时不要重复启动仍在 TTL 内的
`launching` attempt；TTL 过期后再次 `dispatch` 会创建下一 generation。所有 worker 事件都传回
attempt、generation，迟到的旧结果只会记录为 `stale_result`，不会覆盖当前 worker。实际
`spawn_agent`、`wait_agent`、`followup_task` 和 `interrupt_agent` 仍归 Codex host 管理；Goal Plus
runtime 只维护工作项状态。subagent 返回后记录 `result`，主 Agent 验证其 diff 与测试后记录
`accepted`；需要修正时记录 `rework`，保存返回的新 attempt/generation，再向原 worker 发送
具体证据。不要记录私有推理或完整
transcript。`result` 使用
`metadata={"orchestration_monitor":{"native_operation":"wait_agent"}}`；`rework` 使用相同
namespace 记录 `followup_task`。

只有任务同时具备可量化 metric、确定性 verifier、隔离编辑面、多个有价值假设和足够预算时，
才进入现有 Search Mode。Search 使用自己的 run/candidate 状态，不创建普通 subagent 记录；
普通检索、代码定位或单一路径修复使用普通工具或普通 subagent。运行时只在仍有
`launching`/`active` 派发时拒绝最终检查与 `complete`。

顶层 Stop gate 会阻止每条仍处于 active 的记录，并返回完整的当前原始目标、创建/检查
时间戳、已用时间、phase、next action 和最终检查 policy。根据该 prompt 审计全部目标要求
以及任何时间条件。时间条件决定何时停止新增工作并转入收尾，但不覆盖已经完成的结果终态；
完成后使用 `complete`，并把预设时间和实际完成收尾时间作为两个独立事实报告。未完成时继续；
只有确有外部条件导致无法继续时才使用 `blocked`，否则先用真实的终态调用
`goal_plus_set_status`。
候选 lease 结束绝不会完成父级 Goal Plus 任务。只有 host command 显式给出
`max_duration_seconds` 时，Goal Plus 才从创建时间持久化 `outer_deadline_at`；缺省时不编造
单独的任务 deadline。

一条 Goal Plus 记录就是完整任务。`search_tasks` 是其仅追加的 Search Mode 任务历史，
每个 `run_id` 对应一个冻结 spec；`linked_search` 只是当前任务的兼容视图。在一项 Search
任务中，规划轮次和已启动搜索轮次由 `goal_plus_monitor_snapshot` 分别报告。

目标编辑同样仅追加：`goal_revisions` 保留每个实际目标。更新目标会为新修订版重置
intake/triage，并在不删除旧 Search 任务和最终检查的情况下将其变为历史。如果轮次中断，
调用 `goal_plus_status` 并恢复持久化修订版。如果审查员中断，其尝试会记录为
`interrupted`；调用 `goal_plus_prepare_final_check` 创建并启动新尝试。

## Triage Schema

`goal_plus_record_triage` 要求以下运行时 schema：

```json
{
  "is_optimization": false,
  "confidence": "high",
  "recommended_phase": "goal",
  "identified_at": "initial",
  "scenario": null,
  "reasons": ["此分类正确的原因"],
  "missing": []
}
```

`recommended_phase` 只能使用 `"goal"`、`"spec_discovery"` 或 `"search"`。
不要发送名为 `mode` 或 `reason` 的字段，也不要使用 `"goal_mode"` 等值。

建议映射：

- Goal Mode：`is_optimization=false`、`recommended_phase="goal"`、
  `confidence="high"`。
- Spec Discovery Mode：`is_optimization=true`、
  `recommended_phase="spec_discovery"`，并列出缺失的 baseline、metric、正确性门禁、
  编辑范围、verifier、预算或提升细节。
- Search Mode：`is_optimization=true`、`recommended_phase="search"`、
  `confidence="high"`。

## 模式

Goal Mode 用于普通编码、文档、审查和调查任务。它使用编排工作项和普通 Codex 验证证据，
不使用 SearchSpec。

Spec Discovery Mode 用于具有优化形态、但 metric、baseline、正确性门禁或编辑范围仍不明确
的目标。

Search Mode 用于已冻结、可度量的优化。它把候选工作区创建、verifier 执行、选择、报告和
提升委托给现有 Search MCP 流程。

## Hook 兼容性

installed plugin 通过 `hooks/hooks.json` 提供 Goal Plus host hook；
`.codex/hooks.example.json` 只是必须与它同步的开发源码模板，不是另一条安装路径。
该 hook 会为 `UserPromptSubmit`、`SessionStart`、`PreToolUse`、`PostToolUse`、
`Stop` 和 `SubagentStop` 运行 `goal-plus --goal-plus-host-hook`。
`UserPromptSubmit` 会在模型轮次前预先创建并绑定 `$goal-plus`，
也识别 `$goal-plus-with-final-check` 和显式 `$goal-plus edit` 更新。
新记录首轮会显示与当前 `turn_id` 精确匹配的 Codex 运行时权限；Search candidate
第一次读取自己的 agent context 前也会显示其自身权限。该信息来自 Codex transcript 的
`turn_context`，缺失时保持 unavailable，不能从 TOML、环境变量或 parent 权限猜测。
Main 在首条用户可见进度中、Search candidate 在最终交接中完整照抄 hook 给出的权限摘要。
`SessionStart` 的 startup、resume、clear 或未知 source 只发现可恢复状态，不授权或注入
active id；用户必须显式提交 `$goal-plus resume`。同一 turn 的追加 `UserPromptSubmit` 和
`source=compact` 不创建授权，也不撤销已有绑定。`PreToolUse` 对未由本轮精确 host command
授权的 Goal Plus / Search 变更 fail closed。不存在 `PostToolUse(goal_plus_create)` 兼容后备。
Search 候选 subagent 的
PostTool 事件还会执行一次只读 verifier 时间提示检查；它们绝不会绑定 Goal Plus 所有权，
主 agent、最终检查员和普通 subagent 的事件会被忽略。顶层 `Stop` 会强制执行完整原始目标
和耗时审计，直到主 agent 记录终态。
`SubagentStop` 感知所有权：Search 候选只会被阻止到其自己的
`search_run_verifier(..., agent_session_id=...)` 调用被持久记录为止。它返回后，
父 agent 执行完成验证，观察是否出现新的 verifier 支持的全局最佳，并按内部 `search` skill 的
模式路由继续。普通 subagent 不继承父级 action；最终检查审查员保留其独立审查 gate。

即使 hook 是强制后备，也应保留上述显式工作流调用作为可审计状态转换。subagent 工具事件
不会绑定 Goal Plus 所有权。`goal_plus_gate` 不监管 worker 生命周期；Codex worker 预算和
前台 subagent 行为仍由内部 `search` skill 负责。
