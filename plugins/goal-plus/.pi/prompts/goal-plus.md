首先调用 `goal_plus_create(raw_goal="$ARGUMENTS")`，必须在 triage、规划、编辑或 Search 之前调用。
除了加载 goal-plus skill 之外，在调用 `goal_plus_record_triage` 之前不要读取或审计目标文件。
创建成功后，由你语义检查原始目标或当前对话上下文是否明确给出了整个 Goal Plus 任务的最大运行时间；
如果没有，先用一句简短 commentary 告知用户：“当前未设置最大运行时间，Goal Plus 将持续运行
直至目标完成或用户手动停止。”这只是告知，不要询问或等待确认；告知后立即继续执行。

入口已把逻辑 Main `thinking=max` 映射到 Pi 原生最高档。triage 后调用
`goal_plus_upsert_work_items` 建立当前修订版的
编排计划。普通独立工作项通过 `pi_goal_plus_run_work_item` 执行；用
`goal_plus_record_work_event` 记录验收或返工。只有可量化、具有确定性 verifier、隔离编辑面
和多个有价值假设的工作项才路由到 Search。
普通工作项的 Pi wrapper 会把原生下发与返回操作写入 `orchestration_monitor` metadata；
`goal_plus_monitor_snapshot(feature_plugins=["orchestration"])` 提供与 Codex 可比较的语义链。

原生入口若注入了已解析的显式角色模型，严格使用该路由：`main=` 只表示入口已校验当前
Pi session model，入口没有切换模型；`annotator=` 写入
`strategy.evidence_annotator.model`；`workers=` 或兼容别名 `models=` 按现有分配规则写入
`strategy.models`。未显式指定的角色保持现有默认或继承语义。

# Goal Plus

对以下原始用户目标使用 `/skill:goal-plus`：

$ARGUMENTS

当此 Pi prompt 开启 Search Mode 时，SearchSpec strategy 必须设置
`worker_host: "pi-rpc"` 和 `orchestration_mode: "parallel_loops"`，SearchSpec 必须显式
设置 `workspace.backend="git_worktree"`，使 worker
通过持久化 Pi pool 以一组固定的初始自主候选循环运行。
只有用户明确要求兼容隔离时才能设置 `workspace.backend="copy"`。

创建 draft 和冻结 verifier 前，Main 必须把当前 Search 工作项从原始目标与公开任务契约
继承的正确性、性能、适用范围和测量方法落实为统一验收契约。candidate iteration、
selection、promotion 和最终验收必须遵循同一标准；允许使用预先声明的不同成本或覆盖规模，
但不得临时改变标准。worker 只优化冻结契约。若契约有缺陷，调用
`search_invalidate_run` 后修复并重新冻结，不能直接比较新旧契约结果。

冻结前，要求每个 `ranking_signal` 输出一个最终 JSON 对象，其中包含有限数值类型的
`spec.metric_name`。命令可以内联，也可以调用现有工具。只在必要时创建自定义 verifier 文件，
并在冻结前的 Spec Discovery 阶段将其写入源码拥有的路径，例如 `.goal-plus-verifiers/`，
绝不能放在 `.gp/` 或 `.search/` 中。Spec Discovery 可以为此使用 `bash`、`write` 和
`edit`。freeze 工具会暴露完整的嵌套 `SearchSpec` schema；不要根据校验错误猜测字段。
`expected_outputs` 只列出产物路径或 glob。
verifier 必须保持候选工作区只读，并使用唯一的 `GOAL_PLUS_VERIFIER_TMPDIR`/`TMPDIR`
或 Python `tempfile` 存放编译器和临时输出。绝不能使用固定的 `/tmp` 路径，因为 Pi pool
可能并发验证多个候选。冻结操作会在消耗候选预算前拒绝任何工作区副作用。

首次获得有意义的优化结果后，执行 skill 中已有的原始目标审计，但不要把该结果视为运行完成。
相对 baseline 的巨大改进并不能证明结果已接近有意义的成功，尤其是在没有绝对目标或验收条件时。
默认继续使用同一个冻结 spec 和 run。只有存在具体的评估/编辑契约缺陷时才使用
`upgrade_spec`；只有实际目标发生变化时才使用 `revise_goal`。新搜索方向或特性迁移应留在
当前 run 内。这是现有流程中的推理，不是新的运行时阶段或审批步骤。

如果 worker 报告 verifier 问题，在核查其证据期间暂停继续执行。`parallel_loops` 模式下
不存在常规 slot 补充。不要因为诊断稀疏、分数低或进展缓慢而重建。
如果主 agent 确认确有契约、覆盖范围、确定性、目标或基础设施缺陷，先调用
`search_invalidate_run`，中断整个 Pi pool 并等待 `active_count=0`，然后修复并重新冻结，
再使用 `source_run_id` 创建后继 run。绝不能选择或提升已经失效的 run。

在 Search 执行、提升、结果记录或最终原始目标审计期间，绝不能调用 `search_report`。
Goal Plus 记录达到终态后，对每个成功记录的 `run_id` 调用且只调用一次 `search_report`，
交互式 Pi 不在自然语言回复中重复报告路径；由 extension 的可展开完成卡展示。非交互输出遵循
goal-plus skill 的紧凑终态格式。

目标给出的预设时长只决定何时停止新增工作和转入收尾。若结果、验证、提升和审计均已完成，
即使实际收尾时间超过预设也使用 `complete`；不要输出 `late`，也不要仅因耗时超出预设而使用
`blocked`。完成卡会分别展示预设时间和实际完成收尾时间。
