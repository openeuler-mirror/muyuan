# Adaptive Search 与 Search Scheduler

Adaptive Search 是 Goal Plus 主线接入 Search Scheduler 的 orchestration mode。它在 verifier
结算后先等待对应 `AnnotationTask` 进入终态，再持久化一个
`ScheduleTask`，并通过一次 scheduler 推理完成 reward、allocation flag 和可选 Decision 草案。
scheduler 只做结构化推理；
`ScheduleTask.output` 是推理结果的唯一权威记录，runtime 负责按需索引 reward 并执行
已持久化的 `AllocationDecision`。scheduler 不是 candidate worker 的 session，也不参与 hard score、
最终选择或 promotion。

实现收敛为四个业务模块：`adaptive_search.py` 负责流程 hook 和协调，`scheduler.py` 负责配置、ScheduleTask、
公共上下文、异步管理及调用后端，`allocation.py` 负责 Decision 契约和执行，`policies.py` 负责版本化策略及
各版本的 reward/allocation context builder。
runtime 通过标准化 trigger 和 reward 节点绑定把 scheduler 输出交给 Allocation；Allocation 不读取或
改写 ScheduleTask，scheduler 也不持久化 Decision。

## 运行流程

```text
settlement
  -> persist AnnotationTask(settlement_id)
  -> AnnotationTask completed | terminal_error
  -> persist ScheduleTask(compact Evidence View)
  -> one-shot scheduler call
       -> compute reward / rubric / UCT / exploration from reward policy
       -> compute allocation flag from pruning policy
       -> apply derivation policy and return Decision only when flag=true
          and this is an allocation-confirmation task
  -> consume task in candidate iteration order
       -> record SchedulerOutput on ScheduleTask
       -> persist Decision only after lazy reschedule confirmation
  -> fence retired candidates
  -> apply Decision
       -> create derived workspace and native agent session
  -> continue the current or derived candidate
```

hard settlement 总是先完成。普通 iteration 不阻塞等待后续 AnnotationTask/ScheduleTask；只有上一
completed ScheduleTask 的 flag 为 `true` 时，lazy barrier 才等待当前 annotation -> scheduler 链。
scheduler 或 annotation 失败不会撤销 verifier score、results ledger、Git 结算、Evidence、selection
或 promotion。AnnotationTask 是 ScheduleTask 的弱依赖：annotation 失败仍会生成 ScheduleTask。

## 配置

```yaml
budget:
  max_parallel: 4
  max_candidates: 8
strategy:
  orchestration_mode: adaptive_search
  search_scheduler:
    host: pi-rpc
    model: bench-openai/gpt-5.6-sol
    reasoning_effort: low
    timeout_seconds: 180
    reward: evidence_llm_value/v3 @prompt rubric要关注本轮新增价值
    allocation: value_guided_replace/v2
workspace:
  backend: git_worktree
```

`strategy.orchestration_mode="adaptive_search"` 是 Goal Plus 主线进入该扩展的开关，并要求
`strategy.search_scheduler` 非 `null`。`parallel_loops` 是隔离的固定候选流程，不允许配置
Search Scheduler。主 runtime 只按 orchestration mode 调用 `AdaptiveSearch` 门面；进入门面后，
Scheduler、Allocation 和 policy 内部只校验非空 `search_scheduler`，不传播模式判断。启用时要求
`workspace.backend="git_worktree"`。`host`、`model`、
`reasoning_effort` 和 `timeout_seconds` 均为必填项，直接且完整地描述一次性 scheduler 调用，
不会继承 candidate worker 的 host、model 或 launch 参数；并发和重试由 runtime 内部决定。

`budget.max_parallel` 是实时活跃 candidate 上限 `K`。`budget.max_candidates` 是累计物化过的
唯一 candidate 上限；设为 `null` 或省略时不限制累计数量，有界值必须满足
`max_candidates >= max_parallel`。单次 Decision 可使用的预留派生 id 数量仍受当时可用并发容量
约束；有界模式再同时受累计剩余额度约束。

`reward` 和 `allocation` 使用 `name/vN [@prompt 补充说明]` 格式引用版本化策略。runtime 在 search
开始前解析准确版本并把完整策略快照写入 `FrozenSpec`；异步 scheduler agent 只读取该快照和已构建
context。runtime 根据冻结的准确版本调用其注册 context builder。可选 `@prompt` 一同被冻结并加入
scheduler 输入上下文。
内置 reward 策略仅保留 `evidence_llm_value/v3`，内置 allocation 策略仅保留
`value_guided_replace/v2`。删除的旧版本号不会复用；冻结配置引用旧版本时会明确报 unknown policy。
增加策略时直接修改 `policies.py`，仿照内置策略定义 instructions 和 `build_context`，再调用
`_register_reward_policy` 或 `_register_allocation_policy`；异步 worker 导入同一个文件，因此不提供
运行时动态注册入口。配置不接受函数名或脚本路径，也不会执行外部策略代码。backup
属于 reward 推理，expansion 属于 allocation/Decision，不再单独配置。

`evidence_llm_value/v3` 将 reward 定义为当前 iteration 的边际价值：公开结果增量、相对
本候选历史新增的 Evidence 信息和新的实现/覆盖方向分别形成 `outcome_gain`、
`information_gain`、`direction_gain`。重复 pass、无 diff、重复验证或注释维护不再因为候选绝对状态
良好而获得正 reward；恢复到已经观察过的最好结果也只获得少量分值。确认 allocation 时，它根据
显式节点绑定构造不落盘的临时价值投影，用于比较历史来源节点且不改写 `ScheduleTask`。如果
annotation 终态失败，View 固定为 `null`，上下文仅提示 scheduler 参考历史观测自行决定补偿方案，
不预设固定补偿公式。

每个 allocation policy 分为 `pruning_instructions` 和 `derivation_instructions`。
`value_guided_replace/v2` 判断继续占用 lane 的机会成本：
当前公开回退、连续两轮无正边际价值，或相对同质 peer 没有独有信息/方向时进入替换确认；历史高分
不再抵消当前回退或长期停滞。确认阶段保留每组同质方向中证据最强的 lane，并允许一个高价值历史
节点派生多个互不重复的探索方向，不再要求固定的来源价值差。普通调用只注入 pruning；确认调用中
当前 pruning 结果为 `true` 时，scheduler 才在同一次调用内执行 derivation。`allocation_flag`、lazy
confirmation、Decision 输出条件和 reserved id 校验属于 scheduler runtime 公共协议，不属于
allocation policy。

## Scheduler 调用上下文

模型输入固定分为三个顶层对象。`scheduler.py` 只维护 `scheduler_context`，保存当前 settlement 的
身份、目标、准确 commit/diff、公开 metric、disposition 和精简 Evidence View；不包含策略或历史。
它把 reward 绑定节点、候选和容量作为中性事实传给 `policies.py` 的统一入口，再拼接该入口返回的
`reward_context` 与 `allocation_context`。

每个注册策略必须提供 `build_context`。当前 reward context 投影本候选近期历史、
previous/best/last-positive 里程碑和 UCT；allocation context 投影当前 lane、精简 peer frontier
和容量。`allocation_context` 使用
`phase=screen|confirm` 表明阶段，只有 confirm 才由统一入口加入 derivation policy、全 candidate 的
reward-bound 节点和预留派生 id。模型 profile、runtime root、deadline 和进程监控路径属于执行后端
私有上下文，不注入模型 prompt。local reward 只依赖当前 candidate；UCT 的 `total_visits` 仍来自全局
已完成 ScheduleTask，只用于探索项。

## ScheduleTask

文件路径为：

```text
runs/<run_id>/candidates/<candidate_id>/schedule-tasks/iteration-<n>.json
```

task 固定以下输入：`frozen_spec_id`、`settlement_id`、`node_id`、AnnotationTask id/终态、精简
Evidence View、reward/allocation policy 引用、settled commit、attempt diff 文件、目标上下文 hash 和
scheduler profile。完整 policy 只保存在 FrozenSpec，ScheduleTask 不复制动态上下文或 policy hash。
若上一 completed ScheduleTask 的 `allocation_flag=true`，runtime 才为当前确认 task 预留 candidate
id。状态为：

```text
pending -> running -> completed
                  -> retry_wait -> running
                  -> terminal_error
```

同一 candidate 必须按 iteration 顺序消费，不同 candidate 可在
`budget.max_parallel` 派生的内部上限内并发。worker record 用于检测失效进程；恢复时 running task
可以重新进入处理。每次 scheduler 调用都是一次性进程，不恢复或创建 native session。

成功输出同时包含：

- `reward`：有限数值 reward、rubric score、exploitation、exploration、UCT、分量和依据；
- `allocation_flag`：布尔值，`false` 表示 continue，`true` 表示 reschedule；
- `allocation_rationale` 和 `allocation_confidence`：flag 的依据与置信度；
- `decision`：仅当上一 completed flag 为 `true` 且当前 flag 也为 `true` 时出现，包含 retirees 和
  replacement 草案。

## Reward 索引

系统不保存 `value-tree.json`，也不把 reward 复制到 candidate 或 verifier report。完成态
`ScheduleTask.output.reward` 是唯一持久化 reward；runtime 通过
`list_scheduler_rewards(run_id, candidate_id=None)` 从 task 索引生成稳定排序的只读列表：

```json
[
  {
    "schedule_task_id": "schedule-...",
    "settlement_id": "settlement-...",
    "node_id": "candidate-001/iteration-0001",
    "candidate_id": "candidate-001",
    "iteration": 1,
    "commit": "<git sha>",
    "reward": {"reward": 15.0, "uct": 15.1},
    "allocation_flag": false
  }
]
```

普通 scheduler 调用的 reward 阶段只读取当前 candidate 的历史 reward 和轻量 UCT 统计；allocation
screen 阶段同时读取每个 candidate 的精简 frontier，以发现重复方向。上一 completed flag 为 `true`
的 allocation 确认调用才获取所有 candidate 的完整 reward-bound 节点。系统不维护 run root、
类化 graph、transition、action edge、backup event 或 hindsight。

## Lazy Allocation

allocation 使用两步确认，避免每轮阻塞 worker：

1. settlement 创建 `AnnotationTask`，其终态后才创建绑定相同 settlement id 的 `ScheduleTask`。
2. 上一 flag 为 `false` 或尚不可用时，verifier 不等待当前 annotation/scheduler 链。
3. 上一 flag 为 `true` 时，runtime 把当前 task 视为确认任务并等待 annotation 和 scheduler 完成。
4. 普通异步 task 使用 `allocation_context.phase=screen`，即使输出 `true` 也不生成 Decision 草案。
5. 确认 task 使用 `phase=confirm` 并额外获得全 candidate 节点；只有当前 flag 再次为 `true` 且
   `SchedulerOutput.decision` 通过锚点、容量和身份校验，才持久化 `AllocationDecision`。

因此一次孤立的 `true` 只设置下一节点的 barrier，不直接退休 candidate。异步 task 在后台
只记录 reward/flag；只有 foreground lazy barrier 可以把确认后的草案提升为 Decision。每次 barrier
都会先回扫已经消费但尚未绑定 Decision 的确认 task，因此较晚上报的 flag/草案不会因 worker 已继续
下一轮而丢失。

## AllocationDecision

Decision 记录 scheduler 的标准执行计划：

```json
{
  "schema_version": 3,
  "decision_id": "allocation-...",
  "schedule_task_id": "schedule-...",
  "status": "pending",
  "trigger_candidate_id": "candidate-001",
  "trigger_iteration": 4,
  "rationale": "两次连续 reschedule flag，且另一来源具有更高公开过程价值。",
  "replacements": [
    {
      "retire_candidate_id": "candidate-001",
      "new_candidate_id": "candidate-005",
      "source": {
        "candidate_id": "candidate-002",
        "iteration": 3,
        "node_id": "candidate-002/iteration-0003",
        "settled_commit": "<git sha>",
        "artifact_hash": "<sha256>",
        "allocation_depth": 1
      },
      "exploration_direction": "验证不同的边界处理策略",
      "rationale": "来源节点公开证据更强，且方向与已尝试方案不同。",
      "worker_budget": {"max_runtime_seconds": 300, "on_exceed": "interrupt"}
    }
  ]
}
```

runtime 只接受预留的 derived id，以及能精确映射到已结算 iteration、artifact 和 ledger commit 的
source。Decision 持久化时一次 fence 全部 retirees 的下一轮 iteration。scheduler 自主提出退休、
来源、派生数量、探索方向和每个派生项的 worker budget；candidate worker 和父 agent 不得补写、
拆分或改写计划。系统不暴露单份 Decision 的 replacement 或派生深度配置项，但 Decision
仍受 `budget.max_parallel` 的实时并发容量约束；`budget.max_candidates` 非 `null` 时再受累计
唯一 candidate 剩余额度约束。

Decision 文件是恢复锚点。若进程在文件落盘后、候选 fence 或 run 索引更新完成前退出，下一次 barrier
会按同一 `schedule_task_id` 找到已有 Decision 并幂等重放这些状态投影，不会创建第二份 Decision。

父 agent 通过 `search_list_allocation_decisions(status="pending")` 获取计划，再调用幂等的
`search_apply_allocation_decision`。apply 从固定 source commit 创建 Git worktree，继承 results
ledger、全局 Evidence 可见性和 selected model，并创建每个派生 candidate 的 native session。
返回多项时必须全部启动，且 live worker 总数不能超过 `budget.max_parallel`。

## Worker 协议

`search_run_verifier` 返回 `scheduler_status`、`schedule_task_id` 和可选
`allocation_decision`。`scheduler_status=pending` 且 Decision 为空时，worker 继续当前循环，不等待、
轮询或自行运行 scheduler。只有非空 Decision 才表示下一轮已经被 fence，worker 应写 handoff 并
返回父级。

`search_monitor` 和 report 从 ScheduleTask 索引显示 scheduler task 状态、最近 reward/UCT/flag、
reward 聚合和 Decision 状态。最终 `search_select`、report 排名和 promotion 仍只采用 hard verifier
score。
