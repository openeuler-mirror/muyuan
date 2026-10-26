# Evidence Annotator

Evidence Annotator 是 Search runtime 内部的异步标注子系统。它把一次已经结算的
worker process-verifier 尝试压缩成一句客观的简体中文描述，并把该描述作为
Global Evidence 中可选的 `view` 发布。

View 用于帮助 candidate 快速理解 peer 的实际代码变化。它不是评分、选择依据或新的
Evidence；标注任务由内部角色宿主执行。

## 角色边界

Evidence Annotator 与 Search candidate、main agent 和宿主 worker 的边界如下：

| 组件 | 所有内容 |
| --- | --- |
| Search runtime | 已结算 iteration、immutable artifact、durable queue/task、重试状态和不可变 View |
| Evidence Annotator | 通过授权 Artifact View 检查固化 Evidence，生成客观描述和可选补充评价 |
| candidate worker | 读取可见 Global Evidence，自主选择后续方向，不等待 View |
| main agent | 观察 Evidence、执行全局停止、选择和 promotion；不调度 annotation 内容 |
| Codex/Pi worker host | candidate worker 与内部 annotator/Verifier 的启动、续跑、deadline 和 interrupt |

Evidence Annotator 不创建 `AgentSessionRecord`，不占用 candidate lane。它有独立的
run-scoped drainer 进程，资源登记到所属 scope。普通 verifier、选择和
promotion 不等待这个进程；若冻结 run 要求 supplemental evaluation，selection 会在所有
AnnotationTask 成功发布补充评价前保持 `selection_blocked`。

worker 的 process-verifier 调用会创建 annotation task；candidate-facing schema 不要求模型
传 `agent_session_id`，当前 session 由可信宿主绑定。启用 Adaptive Search 时，parent process verification 也会创建 annotation task；Annotation 与
Verifier task 独立推进，不构成 Allocation 前置依赖。promotion verification 不会创建 View。

## 端到端流程

```text
candidate 调用 goal_plus_search_run_verifier
  -> runtime 固定并验证准确 attempt artifact（Git commit / TT snapshot）
  -> runtime 结算 keep / retain / discard / failure 和 candidate-local rollback
  -> runtime 幂等固化 EvidenceAnnotationTask 和 target/peer Artifact View 引用
  -> queue revision 单调递增
  -> Codex hook / Pi extension 的 verifier post-tool callback 唤醒 run-scoped drainer
  -> single-flight drainer 串行领取 task
  -> 打开 immutable full-workspace Artifact Views；构造 bounded diff 索引
  -> 按冻结宿主身份启动本地一次性 annotator 或唤醒 retained TT Annotation Child
  -> 校验单行 JSON 输出
  -> queue generation 仍匹配且 open 时写入不可变 EvidenceViewRecord
  -> goal_plus_search_get_global_evidence 投影 view 字段
```

一次 worker verifier settlement 会在返回前持久化 task 和 queue revision，随后由真实 Codex hook
或 Pi extension 的 post-tool 路径异步 kick drainer。共享 runtime/MCP 只注册和读取任务，不启动
Codex/Pi 模型进程。kick 只保证同一 run 同时至多有一个有效 drainer；它不等待推理完成。

Candidate 消息、commit 文本或“任务已完成”不会创建 task。task identity 由
`run_id/candidate_id/iteration/artifact_ref` 决定；重复 settlement 不能重复 admission。队列不等待
同批 barrier，每条新 Evidence 独立触发；comparison basis 固定 task 创建时已结算的 peers。

drainer 串行处理当前可运行的 task。清空当前 backlog 后会删除自己的 active worker
标记并退出；以后产生的新 task 会启动新的 drainer generation。worker 标记用于
single-flight 和崩溃恢复，不表示 Search candidate 的生命周期。

## Evidence 与 View

Global Evidence 的事实字段由 verifier settlement 同步产生，以下以 Git Evidence 为例：

```json
{
  "candidate_id": "c001",
  "iteration": 3,
  "commit": "<exact-attempt-commit>",
  "score": 13350,
  "disposition": "keep",
  "view": "将调度逻辑改为按依赖深度分组。"
}
```

其中 `candidate_id`、`iteration`、`commit`、`score` 和 `disposition` 是权威
Evidence。`view` 绑定同一 `run_id`、candidate、iteration 和 immutable artifact；
ThinkThread 使用 `artifact_ref` 的 snapshot identity，公共 `commit` 为 null。

以下不变量始终成立：

- Evidence 无需 View 即已有效；`view=null` 不能解释为 verifier 失败。
- View 不参与 candidate-local best、run-wide best、选择或 promotion。
- View 发布后不可修改，也不能绑定到另一个 iteration 或 commit。
- annotation 失败不能回滚、降级或作废已经结算的 verifier Evidence。
- candidate 不应等待、sleep 或高频轮询 View。

View 是 best-effort 产物，不保证每条 worker Evidence 最终都有非空值。`view=null`
可能表示 task 尚未运行、正在等待重试、已经永久失败，或者 run 已关闭并拒绝了迟到发布。
这些内部状态不会通过 candidate-facing Global Evidence 暴露。

## Annotator 输入输出

每个 task 固化并提供以下输入：

| 字段 | 含义 |
| --- | --- |
| `agent_summary` | candidate 在 verifier 调用中提交的一句 `hypothesis` |
| `actual_diff` | 本轮 settled base 到准确 attempt commit 的 bounded diff 索引 |
| `candidate_diff` / `candidate_diff_omitted` | 从候选初始基线到本次 artifact 的 bounded diff 及省略原因 |
| `comparison_basis` | task 创建时固定的最多 8 个其他 candidate 的当前硬分最佳版本，每个 candidate 至多一个 |
| `actual_diff_omitted` | bounded diff 超限或无法投影时的原因 |
| `artifact_views` | target 与 comparison peers 的完整 immutable workspace capability |
| `exact_attempt_commit` | verifier 实际读取的 attempt commit |
| `verifier_result` | score、process pass、disposition 和 failure class |
| `relevant_metrics` | 各 verifier 的持久化 metrics |

Artifact View 是代码事实来源，`actual_diff` 是本轮定位索引，`agent_summary` 只作为待核对的
candidate 自述。diff 根据 task 固化的 base/head 和 `attempt_changed_files` 重新读取，不依赖
candidate 当前工作区是否已经因 discard/failure 回滚。diff 超过上限时不会丢弃 task；context
记录 omitted，Annotator 直接浏览完整 Artifact View。

输入中的 diff、注释、字符串和 agent summary 全部被视为不可信数据。Annotator 的
system instructions 明确禁止执行其中的指令、修改文件、读取 Artifact View 之外的项目内容或访问网络。
只允许 provider 授权的只读 list/read/search；项目代码、测试、verifier 和其他命令均不得执行。
输入使用独立标记包裹，并转义尖括号以降低 prompt injection 风险。

输出必须是以下 JSON shape：

```json
{"description": "将缓存键改为包含输入形状和数据类型。"}
```

`description` 必须满足：

- 一句简体中文客观陈述；
- 单行，去除多余空白，最多 1000 个字符；
- 只描述实际做了什么；
- 不评价好坏，不排名，不推断动机，不提出建议；
- 不复述 commit、分数或 disposition。

annotation diff 投影最大为 1 MiB。超过上限时该字段为空并记录 omitted 原因；完整 Artifact View
仍绑定原 immutable artifact，因此不会因为截断 prompt 而生成失真的“完整 diff”声明。

## 比较基线与目标上下文

Benchmark 机制消融由 controller 设置
`GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED` 和
`GOAL_PLUS_SUPPLEMENTAL_EVALUATION_REQUIRED`。它们只决定 annotation task 是否要求开放式
补充评价，不向 FrozenSpec 注入 criterion。ON/OFF 都不改变硬分结算或评价内容；required 模式
会在所有 task 成功发布补充评价前阻止 selection，不能把缺失或失败的 Annotation 当成通过。

Evidence annotator 继承 `FrozenSpec.agent_harness` 记录的 Main Agent 身份，不接受 `host` 配置。
角色执行驱动由该身份和工作区后端确定。模型和 reasoning 可以作为实验变量独立
冻结，外部连接与认证配置始终来自启动它的 native host 进程，不进入 Goal Plus 配置或状态。

每个 annotation context 同时提供本轮有界 `actual_diff`、从候选初始基线到当前提交的有界
`candidate_diff`，以及 target/peer 的完整 immutable Artifact View。description 只描述本轮增量；
开放式补充评价直接核对完整候选 workspace，diff 只用于定位，超限时记录 omitted 原因。
annotation task 创建时还会快照最多 8 个其他 candidate 的当前硬分最佳结算版本，每个
candidate 最多一个，固定其 candidate、iteration 和 immutable artifact identity，后续推理基于这份可审计
比较基线。

为判断代码变化与原始请求是否相关，annotation task 记录与当前 Search run 绑定的准确
Goal revision 引用和 SHA-256，而不复制 `raw_goal`。annotator 启动时临时解析该 revision
并校验 hash；没有绑定 Goal 时才退回 FrozenSpec objective。原始上下文只进入隔离的
annotation prompt，不进入 Global Evidence、candidate task 或报告正文；报告只可保留来源、
引用和 hash。benchmark adapter 只能提供公开任务字段，不能把 hidden judge、答案 patch
或私有评测数据写入 Goal。

## 开放式补充评价与读取审计

View 只用一句中文客观描述实际做了什么，不评价好坏、不推断动机、不排名，也不推荐下一
步。事实来源是 immutable target Artifact View 与 actual diff，而不是 candidate 的自述。Changed files、verifier command
和 metrics 只提供验证上下文；命令名称本身或失败的测试不能证明目标行为已经实现。

`supplemental_evaluation` 不读取 FrozenSpec 软标准。annotator 只依据当前候选和 peer 的完整
Artifact View、公开 verifier Evidence 和 annotation task 创建时固定的 peer identity，自行提出 1–8 个与当前
任务实际相关的观察维度。它逐项给出 finding、证据与置信度，并对比较基线中的每个 peer
返回非定向的 `similar`、`different`、`tradeoff`、`complementary` 或 `unknown`，不能借此
选择赢家。没有 peer 时 comparisons 为空。limitations 明确列出公开 Evidence 无法判断的
事项。

这种评价发生在提交结算之后，因此不会在搜索开始前固定注意力方向。worker 可以把第三方
观察作为下一轮假设来源，但必须独立核对；评价不产生总分或最终推荐，不能改变硬 score、
PASS/FAIL、candidate-local 基线、run-wide 排名或 promotion gate。

annotator 收到的累计 diff 使用 Git 函数级上下文和至少 10 行普通上下文，并继续受字节
上限约束。上下文中未出现某个定义，不代表该定义不存在；这类判断必须降低置信度并写入
`limitations`。每次 worker 调用 `goal_plus_search_get_global_evidence`，runtime 都会在对应
`agent_sessions/*.json` 的 `global_evidence_reads` 中记录读取时间、当时 Evidence 数量、
已完成 View 的 candidate/iteration/artifact 引用以及其中是否含 supplemental evaluation。
该读取记录只用于审计 View 是否在后续 verifier 之前可见，不参与候选结算或最终验收。

完整输出 schema 还随启用的 supplemental evaluation 与 shared-dir 要求增加相应字段；
上面的 description-only 示例只适用于这些可选能力关闭时。

## Host 路由与隔离

本地 Codex/Pi 一次性 annotator 按当前任务裁剪输入：只有开启
`supplemental_evaluation_enabled` 才注入补充评价说明、peer Evidence 与比较基线；只有
`published_tools` 或 `tool_adoptions` 非空时，才分别注入工具发布或采用说明和数据。
输出 schema 同步裁剪未启用能力的嵌套定义，关闭的补充评价固定为 `null`，无发布工具时
`tool_views` 固定为空数组；持久化 View 结构和已有结果验收规则保持不变。

Annotator 始终跟随 `FrozenSpec.agent_harness` 记录的 Main Agent 身份，不会为了 annotation
跨用另一个 host。

### Codex

Codex 路径为每个 task 创建临时目录，并运行一次：

```text
codex exec --json --ephemeral --sandbox read-only ...
```

临时目录包含 annotator instructions 和结构化输出 schema。Codex 使用只读 sandbox、
独立输出文件和 Pydantic schema 校验；prompt 提供每个授权 Artifact View 的绝对只读入口，只允许
Agent 自行 list/read/search，禁止执行项目代码、测试、verifier 或其他命令，也禁止 workspace 修改
和网络访问。
这不是 candidate subagent，也不会继承 candidate transcript。

### Pi

Pi 路径为每个 task 运行一次：

```text
pi --mode json --print --no-session --no-builtin-tools \
  --tools submit_evidence_annotation,artifact_view_list,artifact_view_read ...
```

Pi annotator 没有 session、内置工具、自动发现的 extension、skill、prompt template 或
context file。运行时只加载临时 extension：bounded `artifact_view_list/read` 仅能进入授权的
filesystem Artifact View，`submit_evidence_annotation` 是 terminating output tool，参数复用 Codex
annotator 的同一份 Pydantic JSON Schema，包括启用 shared-dir 后完整且严格的 `tool_views` 字段。

ThinkThread 使用独立的 retained Annotation Child。它通过 Agent POSIX RPC 暴露已授权
snapshot 的 `list/stat/pread/diff` Artifact View；candidate 与 annotator tool set 双向隔离，命令
执行和 verifier 工具不会激活。读取直接针对原 immutable snapshot，不创建、patch 或删除派生
snapshot；private paths 由读取授权拒绝。selection 先推进 queue generation，再 interrupt Child，
因此迟到提交不能发布。该路径不会读取 Root live tree，对已安装的 Pi 与 Codex ThinkThread
Profile 均开放。

Artifact View 的完整 workspace 内容只保证在 Annotation task 生命周期内可用。terminal report、
Annotation、publication/recovery、session 和 owner pin 全部收敛后，runtime 可以回收 owned snapshot；
已发布 `EvidenceViewRecord`、comparison basis 和 artifact identity 继续保存在 `.gp`。回收后重开会
明确返回 `artifact removed after terminal cleanup`，不会重建 snapshot 或重进 Annotation Child。

两个路径都会在推理期间重复检查 run 和外层 deadline。run 关闭或 deadline 到期时，
当前模型进程会被终止，迟到结果不能发布。

## SearchSpec 配置

配置入口是 `strategy.evidence_annotator`：

```json
{
  "strategy": {
    "evidence_annotator": {
      "model": "gpt-5.6-terra",
      "reasoning_effort": "low",
      "timeout_seconds": 300
    }
  }
}
```

支持字段：

| 字段 | 含义 |
| --- | --- |
| `model` | annotator 模型；Pi 可使用 host-native 限定模型名 |
| `reasoning_effort` | host-native reasoning/thinking 配置 |
| `timeout_seconds` | 单次调用上限，默认且最大为 1800 秒 |

SearchSpec、FrozenSpec 和 task profile 不接受 provider、endpoint、credential 变量名或 host
配置目录。`ResolvedEvidenceAnnotatorProfile` 只保存 host、model、reasoning、timeout 和
`configuration_source="host_process"`。这是在 task 构建时即可验证的合同，而不是启动失败后的
fallback。

### 模型与宿主配置继承

task 在 verifier settlement 时解析并固化 profile；后续修改 session launch 或环境不会
改变已经创建的 task。

模型优先级为：

1. 显式 `strategy.evidence_annotator.model`；
2. host 原生 default/继承模型。

runtime 不从 selected worker、`strategy.worker_launch.model` 或环境变量推导 annotator model。
未显式配置时 task profile 保持 `model=null`，因此不会触发额外 probe。Pi launcher 在真正启动
一次性进程时从 run 的 host-local execution scope 应用当前 Main model；该值不是 Search 配置。

reasoning effort 优先使用 annotator 显式配置，其次是 `strategy.worker_launch`，再其次是
`GOAL_PLUS_EVIDENCE_ANNOTATOR_REASONING_EFFORT`，最后由 Pi launcher 继承当前 Main thinking。

Codex hook 和 Pi extension 从当前 native host 进程启动 drainer。drainer 及其一次性模型进程完整
继承该 host 进程的外部配置；Goal Plus 不解析、复制、白名单转发或持久化其中的认证和服务配置。
Pi 的限定模型名只用于 host-native 模型选择，不建立另一套连接配置。

### 环境变量

| 变量 | 用途 |
| --- | --- |
| `GOAL_PLUS_EVIDENCE_ANNOTATOR_REASONING_EFFORT` | 默认 reasoning effort |
| `GOAL_PLUS_OUTER_DEADLINE_AT` | verifier 和 annotation 共享的外层 deadline |
| `GOAL_PLUS_EVIDENCE_ANNOTATOR_DISABLED` | 阻止 native host callback 启动新 drainer |
| `GOAL_PLUS_INTERNAL_AGENT_HARNESS` | launcher 写入的非敏感 host provenance；不接受 SearchSpec 配置 |

Annotation profile 不声明模型服务的认证或 endpoint 变量；Codex MCP 进程所需的环境变量名
由安装器从可信 native provider 配置收集，详见 [开发指南](development.md)。执行器要求 provenance 与冻结 host
匹配；这可以在模型调用前拒绝从错误 host 或共享 runtime 进程启动的内部 agent。

显式 annotator model 通过 host catalog 解析并冻结。Goal command 和
`goal_plus_search_freeze_spec` 不额外发送推理请求；实际服务可用性在 Annotation 执行时检验，
失败按 worker 的错误分类和重试合同处理。

## Task 状态与重试

每个 worker iteration 对应至多一个不可变 identity 的 `EvidenceAnnotationTask`：

| 状态 | 含义 |
| --- | --- |
| `pending` | task 已创建，尚未领取 |
| `retry_wait` | 已领取或瞬时失败，等待当前/下一次尝试完成或 backoff 到期 |
| `completed` | View 已通过 identity 和 run fence 检查并持久化 |
| `terminal_error` | profile、输入、deadline、输出或重试已永久失败 |
| `cancelled` | Search selection 已关闭 queue，task 不再允许启动或发布 |

每个 task 最多尝试三次。瞬时失败使用 30 秒、120 秒的持久化 backoff；第三次失败后
进入 `terminal_error`。HTTP 429/5xx、timeout、连接错误和临时不可用等被视为瞬时失败；
无效 profile、Artifact View 不可用、deadline 到期等为永久失败。结构化输出缺失或解析失败可以重试。

`GOAL_PLUS_OUTER_DEADLINE_AT` 在 task 创建时固化。它接受 Unix 秒、Unix 毫秒或带时区的
ISO 时间戳。单次 host timeout 会被缩短到剩余外层时间；deadline 到期后不再启动或发布
annotation。

`evidence-annotator/queue.json` 保存 `state`、`revision` 和 `generation`。每个 admitted task 固定自己的
revision/generation。`goal_plus_search_select` 先将 queue 设为 `closed`、递增 generation 并取消未完成 task，
再进入 `ready_to_promote`；finish path 重复检查三者，拒绝任何 late publication。

当 FrozenSpec 的 `supplemental_evaluation_required=true` 时，`goal_plus_search_select` 会先检查所有已注册
task 均为 `completed` 且带 supplemental evaluation。否则 run 进入 `selection_blocked`，queue 继续
保持 open，native host 可以完成 backlog 后重试 selection；required task 的 `terminal_error` 不会
被伪装成通过。

允许发布 View 的 run state 是 `running`、`waiting_for_workers`、`selecting` 和
`selection_blocked`，且 run 不能已 invalidated。进入 `ready_to_promote`、`promoted`、
`aborted` 或 `failed` 后，未领取 backlog 不再运行；正在推理的迟到结果会被 publication
fence 拒绝。因此结束 Search 前无需等待 annotation backlog。

## Global Evidence 模式

Freeze 输入 `strategy.global_evidence_mode`（冻结记录保存于 `strategy.config`）只改变 Evidence 的交付方式，不改变 task 创建、
annotation 内容或 verifier settlement：

| 模式 | Candidate 可见行为 |
| --- | --- |
| `manual` | 默认；candidate 显式调用 `goal_plus_search_get_global_evidence` 读取共享 run Evidence |
| `auto` | verifier 成功返回时额外注入 `global_evidence_snapshot`；首次为完整快照，后续用 `global_evidence_update` 传递新增、变更、删除或 unchanged 状态 |
| `independent` | 不自动注入，显式读取也只返回调用 candidate 自己的 Evidence |

Annotator 仍按 run 处理所有 worker Evidence；`independent` 只是 candidate-facing 投影过滤。
任何模式下 View 都不影响分数和选择。详细交付契约见 [API](api.md#worker-context)。
Worker 首次修改前读取 Evidence；此后每完成三次 verifier iteration 刷新一次，连续两轮无提升
或准备切换技术路线时提前刷新。`auto` 返回的注入快照算作刷新，无需再次调用读取工具。

## 持久化布局

```text
.gp/runs/<run_id>/
  evidence-annotator/
    queue.json              # durable admission revision/generation 和 close fence
    worker.json             # 当前 drainer generation/PID；正常退出时删除
    worker.lock
    drain.lock
    tasks.lock
    annotator.log           # launch、task 或 drainer 错误
    artifact-views/         # task-scoped immutable full-workspace projections
  candidates/<candidate_id>/
    evidence-annotations/
      iteration-<n>.json    # identity、profile、状态、尝试、usage、错误和可选 View
```

`candidate.json` 中的 iteration 仍是 verifier Evidence 的事实来源。annotation task 只保存
派生 View 和执行状态；Global Evidence 在读取时从 iteration 与 task 即时投影，不维护第二份
可写 Evidence ledger。

task 注册是可恢复的独立分支，不是 reward、allocation 或 candidate retirement 的提交前置条件。
若注册写入瞬时失败，后续 worker verifier 结算或 Global Evidence 读取会扫描已持久化 worker
iteration 并幂等补齐缺失 task；candidate 已被 retirement fence 不影响该恢复。反向同样成立：
reward evaluator 或 allocation policy 失败不影响 task 注册和 View 重试。

`iteration-<n>.json` 中的 `usage` 和 `attempt_history` 会累计成功或失败调用的 host-native
token/cost 观测。不得从 transcript 猜测缺失 usage，也不要手工修改 task、View 或重试状态。

## 排障与恢复

先使用只读 monitor 确认 Goal Plus/Search run、candidate iteration 和 run state：

```bash
goal-plus-pi-tool goal_plus_monitor_snapshot \
  --root .gp \
  --args-json '{"run_id":"run_..."}' \
  --pretty
```

monitor 未提供 annotation 内部状态时，再检查：

1. `.gp/runs/<run_id>/candidates/<candidate_id>/evidence-annotations/iteration-<n>.json`；
2. `.gp/runs/<run_id>/evidence-annotator/annotator.log`；
3. 固化 profile 中的 host、model、`configuration_source`、deadline 和 `last_error`；
4. native Codex/Pi host callback 是否执行，以及 drainer 的 provenance 是否与冻结 host 一致。

常见现象：

| 现象 | 检查方向 |
| --- | --- |
| 没有 task 文件 | 是否是 parent/promotion verifier，或 worker 是否实际调用了 process verifier |
| `pending` 长时间不变 | run 是否仍允许 annotation、自动 kick 是否被禁用、drainer PID 是否有效 |
| `retry_wait` | `next_attempt_at`、网络/限流错误和剩余 outer deadline |
| `terminal_error` | `last_error`、`error_fingerprint`、profile 冲突、Artifact View 或输出 schema |
| `cancelled` | `queue.json` 是否因 `goal_plus_search_select` 关闭；这是正常结束状态，不应重开 queue |
| Evidence 有值但 `view=null` | 这是合法状态；不要重跑 verifier 只为补 View |
| run 结束后仍无 View | publication fence 已关闭；不要重新打开 run 或手工注入 View |

不要从共享 MCP shell 手工运行 drainer；缺少匹配的 native-host provenance 时执行器会在模型调用
前拒绝。恢复 backlog 应重新触发对应 Codex hook 或 Pi extension 的 host control path，仍不能绕过
run/deadline/publication fence。

相关入口：

- [Shared Plane](shared-plane.md)：Global Evidence、Git revision 和 candidate rollback；
- [API](api.md)：candidate-facing Evidence 工具和配置字段；
- [Debugging Runtime State](debugging-runtime.md)：完整 `.gp/` 状态和 host 日志；
- [Codex](codex.md) 与 [Pi](pi.md)：host setup 和 worker 行为。

聚焦测试：

```bash
python -m pytest tests/test_evidence_annotator.py tests/test_global_evidence.py -q
```
