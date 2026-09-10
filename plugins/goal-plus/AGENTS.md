# Goal Plus 开发约定

本文件是 `plugins/goal-plus/` 下所有开发工作的入口，适用于整个目录，除非更深层的
`AGENTS.md` 明确覆盖。它说明应先读什么、代码和状态归谁负责、修改必须遵守哪些约束，
以及交付前需要什么证据；不重复完整产品规格或功能使用手册。

## 文档入口

先读 [SPEC.md](SPEC.md) 了解稳定系统合同，再按改动范围选择文档：

| 改动范围 | 文档 |
| --- | --- |
| 架构分层、状态职责与角色能力 | [架构图集](docs/architecture.md) |
| 安装、快速开始和文档索引 | [README.md](README.md) |
| 命令、状态、续跑、暂停及恢复设计 | [运行逻辑](docs/runtime-logic.md)，注意区分当前实现与目标设计 |
| Search 架构、Evidence、selection 和 promotion | [共享平面](docs/shared-plane.md) |
| Adaptive Search 与 Search Scheduler | [Adaptive Search](docs/adaptive-search.md) |
| Main delegation 与轻量 dispatch 记录 | [Ultra 编排](docs/ultra.md) |
| MCP 与 Pi-local tool 合同 | [API](docs/api.md) |
| Codex/Pi 共同边界 | [Agent Host Adapters](docs/agent-host-adapters.md) |
| Codex 或 Pi 的安装与宿主行为 | [Codex](docs/codex.md)、[Pi](docs/pi.md) |
| Monitor feature plugin | [Feature Plugins](docs/feature-plugins.md) |
| 状态、日志和 report 排障 | [Runtime 调试](docs/debugging-runtime.md) |
| Checkout 开发与安装刷新 | [开发指南](docs/development.md) |
| SearchSpec 示例 | [Examples](examples/README.md) |
| 测试层次、marker 和真实 host ST | [Tests](tests/README.md) |

受版本控制的 model、行为测试和文档共同定义当前协议。旧日志、`.gp/` 产物和 transcript
只用于诊断，不作为协议来源。

## 架构边界

Goal Plus 包含 Goal Mode、内部 Search Mode 和 host 集成。Runtime 负责持久化领域状态；
Main Agent 负责理解目标、拆解工作、决定 delegation/Search 和审查结果；candidate 负责在
自己的合同和 workspace 内探索；host 负责真实进程或 native agent 的生命周期。

Search runtime 不是 worker supervisor。不要在 runtime 或 SearchTools 中增加 host wait loop、
heartbeat、进程 liveness、通用 abort API 或观察总线。`AgentSessionRecord` 是 context 与
provenance 绑定，不代表进程存活；launch payload 是 host 启动 worker 的权威输入。

Ultra 只记录实际发生的 dispatch 和 attempt identity，不要求预建 work DAG。Search 使用自己的
FrozenSpec、candidate、verifier 和 promotion 状态，不能映射成普通 subagent dispatch。

### 状态与代码 owner

规则应落在掌握可信输入或持久状态的最窄 owner 中：

| Owner | 职责 |
| --- | --- |
| `domain/` | 无 I/O 的严格 schema、字段边界、跨字段结构不变量和纯策略；`models.py` 仅保留公共 re-export |
| `ports/` | 无 I/O 的组合存储 DTO 与执行输入输出；不拥有领域状态转换 |
| `application/annotation.py`、`application/annotation_admission.py` | Annotation admission/reconciliation、claim、completion、retry、queue close、comparison/Artifact View references 和 Tool head reconciliation |
| `application/annotation_context.py`、`ports/artifacts.py` | 从已结算输入构造 Annotation context，校验 exact artifact/历史 context/cleanup；窄读取接口不承担 worker 生命周期 |
| `infrastructure/artifacts/` | Git/Copy/TT immutable diff 与 Artifact View I/O；共用 bounded capture/进程组清理在 `infrastructure/process_output.py`，不启动模型 |
| `workspaces.py`、`infrastructure/artifacts/reader.py` | 前者分离本地创建、LocalGitWorkspace、GitReferences 和 GitPublication，能力不互相继承；后者接线本地 View 与 TT immutable handle；不恢复全能 WorkspaceProvider/factory 或 TT/Copy 假方法 |
| `application/search_context.py` | 只读解析 run 链接的 Goal revision 文本和有效 deadline；不替代 Goal/session 授权 gate |
| `application/scheduler.py`、`application/scheduler_context.py`、`application/scheduler_lanes.py` | Scheduler admission/reservation、claim、completion/retry、confirmation output gate、只读 context、顺序消费和 lane barrier；不执行模型或持久化 AllocationDecision |
| `application/allocation.py`、`ports/allocation.py` | Decision 提案校验、记录、退休/trigger 投影与计数恢复；不读取 Scheduler task、不物化 workspace 或启动 agent |
| `application/allocation_preparation.py` | 同一 run 锁内整批预检 Goal/run、settled source/ledger、容量、child/session；返回值仅供锁内消费，不写入或物化 |
| `application/allocation_apply.py`、`ports/candidate_creation.py` | Allocation retirement、applied 提交、物化计数和 session 重放；调用窄 candidate creation 用例，不持有 Runtime 或监督 worker |
| `domain/results_ledger.py`、`infrastructure/state/results_ledger.py` | 纯 ledger revision 前缀选择与既有 canonical/legacy TSV 解码；读取不落盘修复 |
| `application/candidate_execution.py` | Run invalidation/state、candidate recovery 与 session generation/归属的共享纯 gate；不读取 host liveness 或解除 recovery fence |
| `application/internal_agents.py`、`internal_agents.py` | 前者只读冻结 host/run admission policy；后者由可信 host callback 驱动队列、终态 cleanup 和重扫，不构造 Search Runtime |
| `annotation_host.py` | Annotation 用例装配和完成后的 host callback；TT 直接使用 workflow/admission/context，不通过 candidate host service 访问状态 |
| `application/candidate_host.py`、`application/candidate_queries.py` | Candidate launch 准备、归属/预算 gate、精确 launch task identity 和只读 host 查询；session 准备经窄 API，查询不授予执行权，host 不私读 Search store |
| `domain/worker_policy.py`、`application/candidate_ledger.py` | 共用预算、环境与默认 inner-agent 纯规则；Application 拥有 ledger 继承、初始化和 projection，workspace I/O 经窄 port，不启动模型 |
| `application/search_bootstrap.py`、`application/search_run.py` | Freeze、初始 run/plan/batch、invalidation 与显式 device lease 提交/释放；配置、workspace、verifier 和持久化由 bootstrap 接线，不回调 Runtime |
| `infrastructure/search_configuration.py`、`artifacts/source_repository.py`、`artifacts/search_source.py`、`artifacts/thinkthread_baseline.py` | 分别负责确定性 host/model/policy 配置、只读 source 检查、FreezeInputs 和 TT baseline；用例注入 SourceInspection、SearchRunBindings、BaselinePreparation，不能恢复全能 SearchSource |
| `application/candidate_creation.py`、`application/candidate_sessions.py` | 候选任务/物化与 session identity、预算、代际、绑定和续跑；返回 host launch 输入，不监督 worker |
| `application/adaptive_admission.py`、`application/search_research.py` | 前者共用已完成 decision 投影与 candidate admission；后者为 successor/history 提供有界持久研究查询，不读取 transcript |
| `application/candidate_verification.py`、`application/thinkthread_verification.py` | 本地/TT attempt admission、exact artifact 和 verifier 调用；promotion Evidence 独立，不启动模型；Runtime 入口保留 verifier 锁与 host Scheduler barrier |
| `application/candidate_settlement.py`、`application/settlement_policy.py` | 两种后端共用唯一硬结算；完整 candidate 提交后才更新 Annotation/Scheduler、best 和 session 投影，异步失败不撤销 Evidence |
| `application/candidate_recovery.py`、`application/candidate_checkpoints.py` | generation fence、host approval、checkpoint 恢复和派生状态修复；provider I/O 经窄 port，不控制 worker、不重置预算 |
| `application/candidate_artifacts.py`、`application/shared_tools.py` | edit-surface/artifact 与保留 ledger 的恢复；Shared Tool stage/copy/catalog、结算、advisory 和精确 Tool View 验收 |
| `application/selection.py`、`application/promotion.py`、`application/thinkthread_publication.py` | 精确 selected/publication、独立 promotion Evidence 与提交；本地验证请求由 host driver 执行原 verifier/Scheduler barrier，不在用例内等待模型 |
| `application/publication_apply.py`、`application/publication_notifications.py` | 授权 apply、同请求恢复、pending integration 和持久通知；provider I/O 经窄 publication port |
| `ports/publication.py`、`infrastructure/artifacts/publication.py`、`thinkthread_publication.py` | 按 local prepare/apply、TT snapshot intent/物化、独立 verifier 和 apply 分离能力；adapter 编码 wire/解码结果，用例核验 request/artifact 并提交状态；Publication State 按消费者裁剪，不提供任意 mutation 入口 |
| `application/references.py`、`application/artifact_lifecycle.py` | Reference 授权与生命周期；snapshot ownership、跨 run/provider/publication pins 和可重试清理，host 状态仅作清理事实 |
| `ports/references.py`、`infrastructure/state/references.py`、`state/artifact_lifecycle.py` | Reference 和 cleanup State 按实际读写声明，不继承 candidate settlement 写权限；具体 adapter 共用只读查询与原文件锁 |
| `infrastructure/verifiers/`、`infrastructure/artifacts/thinkthread_mutations.py` | 本地/TT verifier 执行、exact hash/输出与 provider request journal 同请求恢复；不决定硬结算或 candidate disposition |
| `ports/thinkthread_verification.py`、`infrastructure/verifiers/thinkthread_attempt.py` | Child/snapshot/inner Git 回包由 adapter 解码为具名事实；client 在 bootstrap/Runtime 绑定，用例只接收 ThinkThreadAttemptIO 并重核归属与 ledger，唯一结算 owner 不消费 SDK 字典 |
| `domain/edit_surface.py`、`codex_file_tools.py` | 前者共用既有纯路径匹配；后者解析原生 patch 声明并按 typed candidate boundary 检查命名/真实目标，不解析模型 JS，也不声称 shell 沙箱 |
| `application/goal_authorization.py`、`ports/goal_authorization.py` | Start replay/session admission、显式工具目标、scope attachment 和历史 Search 归属/执行许可；只读 typed source，不替代 host 可信 session 事实 |
| `infrastructure/state/goal.py` | Goal/event 历史解码、session/pool 归属查询；不写入或修复 Goal 状态 |
| `infrastructure/state/files.py`、`infrastructure/clock.py` | 共用 JSON 原子写、文件锁、文件哈希与系统时钟；不决定业务状态 |
| `execution/process_monitor.py`、`ports/process_monitor.py`、`infrastructure/state/process_monitor.py` | 前者共用 process 诊断限频与事件摘要，Ports 定义纯 operational DTO，文件 adapter 原子写监控文档；不决定 task 重试、硬分或 host 存活 |
| `execution/managed_process.py`、`execution/process_output.py`、`ports/process.py` | 共用本地进程等待、TERM/KILL、异常退出清理、有界完整 pipe capture 与 operational outcome；回收组长前保留成员身份，超限不解析截断结果；host 经窄 ProcessRegistration 登记/完成资源，角色保留 active fence 检查时点与错误分类 |
| `execution/launch_gate.py` | 共用继承管道的登记后 exec 放行；Pi RPC、drainer、scope 内推理和 guardian 未登记时不会运行目标命令，启动者丢失则 EOF 退出；不读取领域状态或决定 admission |
| `execution/structured_inference.py`、`execution/inference_output.py`、`ports/inference.py` | 共用原生 Codex/Pi 一次性 CLI、schema、授权 Pi 工具包装配、输出读取与 invocation 观测；角色保留结果验收和 usage 口径；不构造业务用例、处理 TT Child 或接管 native candidate |
| `infrastructure/inference_bindings.py`、`ports/inference.py` | Annotation/Scheduler 声明有限角色能力；可信 adapter 统一生成 Pi schema、扩展及白名单，无授权 View 则不装配读取工具；PiInferenceTools 仅是 adapter 到 Execution 的传输 DTO |
| `execution/drainer.py`、`ports/drainer.py`、`infrastructure/state/drainer.py` | 共用 detached worker 的 single flight、启动登记、generation 归属、退出重扫与有界关闭；放行前经限 admission 期间使用的 callback 独立登记 scope 资源，队列退场不完成资源；任务领取/重试/并发和可信 scope admission 仍由各自 owner 决定 |
| `execution/process_identity.py`、`ports/process_identity.py` | 只读 procfs/group snapshot、drainer/Pi pool 共用精确 argv 检查与既有 recovery ps identity；核验结果直接携带启动身份；未知观测不授权停止或替换，两个 token 格式不互相猜测转换 |
| `execution/process_resources.py`、`ports/process_identity.py` | scope/drainer/Pi pool wrapper/Pi RPC/已登记推理共用单进程和专用组的身份复核、成员观测及有界 TERM/KILL；各资源 owner 通过窄 callback 保存/恢复 boot/member witness，已观测逃逸成员仍按身份停止；不构造 store，清理失败不完成 scope token；未观测逃逸后代不在现有追踪保证内 |
| `ports/scope.py`、`infrastructure/state/scope.py` | 前者定义 Goal/Search typed attachment、既有 host-local scope/agent/process DTO 与窄 State；后者保存原 JSON、scope/owner/cleanup 锁和枚举；attachment 的类型不授予领域执行权 |
| `execution/codex_scope.py`、`execution/scope.py` | 原生 agent 的登记、spawn/wake invocation、代际观察与精确归属；共用 scope admission 和创建顺序选择，不构造 store 或调用 native agent |
| `execution/scope_processes.py`、`execution/scope_cleanup.py` | Scope 进程登记、witness 合并/恢复、观测和停止，以及先 fence 后互斥的完整/candidate-only 关闭；经窄 host services 关闭 pool/drainer 和读取静默事实，不构造 store 或改写领域状态 |
| `execution/scope_activation.py`、`execution/scope_guardian.py`、`host_scope.py` | Execution 拥有 owner 锁内的激活/替换、guardian 登记后启动与恢复及窄观察持续重试；激活/登记/admission/cleanup 使用 typed attachment，host 保留 service 接线、CLI 和公开薄入口；不重新激活 Goal/Search |
| `application/goal_revisions.py`、`goal_control.py`、`goal_dispatch.py` | Goal revision、可信 session/control 和实际 dispatch attempt 转换；复用 GoalAuthorization，保持入口锁、代际和幂等前态 |
| `application/goal_search.py`、`goal_completion.py`、`goal_gate.py`、`goal_reconciliation.py` | Search link、终态审查、事件 gate 和当前 revision 的恢复 agenda；不启动 host 或构造 Search Runtime |
| `domain/goal_commands.py`、`goal_policy.py`、`goal_closeout.py` | 命令结构与纯解析、时间/continuation 和 closeout 投影；时钟由调用方提供 |
| `domain/worker_policy.py`、`domain/goal_authorization.py`、`application/goal_dispatch.py` | 分别拥有共用 worker model/count 解析、显式命令与 SearchSpec 匹配和 dispatch 结果摘要；不跨模块借用私有 helper |
| `goal_plus.py`、`application/goal_observation.py` | 前者为 Goal 公共薄入口；后者保存宿主已归一化的用量观测，写入经 typed State |
| `codex_hook_input.py`、`codex_models.py` | Codex 原生输入/turn 解码、模型事实与显式 override preflight；不拥有 Goal 状态转换 |
| `assets/pi/extensions/lib/goal-plus-schemas.ts`、`goal-plus-transport.ts` | Pi tool schema 与 CLI/capability transport；主 extension 保留唯一注册入口和可信 host 事实 |
| `runtime.py`、`bootstrap.py` | 前者保留 Search 公共入口与本地 verifier/Scheduler barrier；后者装配窄用例，`SearchServices` 仅供 facade 接线，不传入用例或充当全能 port |
| `application/candidate_context.py`、`search_queries.py`、`evidence_reads.py` | 授权 session 的 context/ledger 初始化、只读 history/查询，以及保留 reconciliation/读取计数的 Evidence 用例；不启动模型 |
| `ports/search_inspection.py`、`ports/monitor.py`、`projections/monitor_workers.py` | 分离只读查询与 context 初始化写入，定义候选 context/Monitor 已采集输入；候选/session 投影不读取文件或执行状态恢复 |
| `application/report.py`、`ports/report.py` | 终态 gate、typed 报告输入与输出/cleanup/刷新次序；未完整解码的历史 Goal 链接仍参与终态 gate |
| `infrastructure/monitor_inputs.py`、`report_inputs.py`、`report_output.py` | canonical 状态与已有 host observation 取数、文件输出和资产装配；不通过 Runtime 或展示恢复领域状态 |
| `infrastructure/state/scheduler_views.py` | Monitor/report 共用 ScheduleTask/Decision 只读装配，保留严格读取与跳过损坏记录两种既有语义；Adaptive facade 不私读这些文件 |
| `projections/` | 纯统计、publication/context 展示、timeline 与 MD/HTML；显式时钟和已采集输入，缺失值保持 unavailable，不执行 I/O |
| `adaptive_search/adaptive_search.py` | Adaptive settlement/barrier 的统一 facade 与模块协调 |
| `adaptive_search/scheduler.py` | Scheduler 公共 facade、barrier 错误日志、按 candidate 并发消费与原生推理的角色适配；领域状态经 Application/typed queries 访问，不构造 Search Runtime；CLI/监控/等待/drainer 与身份检查已共用，scope 推理登记由 bootstrap 装配，不因 candidate-only fence 误停内部推理 |
| `adaptive_search/allocation.py` | Allocation 公共 facade 与结果序列化；Decision、预检和 apply 状态归 Application |
| `domain/adaptive/policies.py` | 版本化 reward/allocation policy 与纯 context builder；旧入口仅 re-export |
| `tools.py` | Codex/Pi 共用 facade 和 caller 前置条件 |
| `server.py`、`pi_tool.py` | MCP/Pi 参数转换、调用和序列化 |
| `agent_hosts.py`、Pi pool/worker | 声明性 HostBinding、模型目录、candidate launch/continue 和只读 observation 分别暴露；pool/worker 保留 budget、interrupt 和 host-local 状态，不恢复 AgentHostAdapter 全能对象 |
| `infrastructure/worker_handoff.py`、`state/resource_locks.py`、`verifiers/output.py` | 分别共用 Pi workspace handoff、verifier/device 资源锁命名空间和本地/TT 有界日志；不从角色私有实现借用机制 |
| `pi_pool.py` | Pool/job 索引、lease 和结果交付；启动身份登记后放行，同锁核验已提交 PID 与 request 归属后才执行；持久 witness 不进入公开 snapshot，按真实 wrapper 资源关闭，不用 task 终态代替退出；不接管 RPC 子进程的独立 session |
| `execution/thinkthread_child.py`、`execution/thinkthread_rpc.py`、`ports/thinkthread.py` | Candidate/Annotation 共用 TT Child 归属、retained turn 静默、spawn/wake/interrupt/destroy 与 RPC hash/chunk/发送/游标消费；角色持有授权、request intent、过期/重传政策和 provider journal，SDK bridge 实现窄 transport port |
| `execution/thinkthread_requests.py`、`ports/thinkthread_requests.py` | Pool baseline/provider mutations 共用 exact request 观察、关闭及 unknown 后有界确认；不写 journal、不重做 mutation，RequestNotFound 不证明操作成功，各 owner 保留 intent/重试/提交责任 |
| `assets/pi/extensions/lib/goal-plus-thinkthread-rpc.ts` | Pi Worker 的独立 RPC 协议端点，处理请求重传、分块复核/重组与 ack；主 extension 保留可信 parent/session、角色工具集和 inner Git 归属，仅主 extension 注册工具 |
| Codex hook、Pi extension | 可信 command transport、session identity 和 host lifecycle 事实 |
| `monitor.py`、`reporting.py` | Monitor 公开入口与报告适配入口；renderer 的纯实现位于 `projections/` |
| Runtime 生成的 `candidate_task` | Candidate edit surface、环境和已启用能力的可见合同 |
| Skill/prompt | 用户意图、评价设计、技术方向等必须由模型判断的工作 |

`runtime.py` 仅根据 orchestration mode 调用 `AdaptiveSearch` facade，不重复实现其调度与分配
逻辑。Adaptive Search 的具体行为合同见 [Adaptive Search](docs/adaptive-search.md)。

## 实现原则

### 确定性规则由代码执行

先判断规则属于哪一类：

1. **可确定规则**：输入与持久状态足以判定。例如 caller role、Goal/run 归属、phase、edit
   surface、artifact、容量和 promotion 前态。必须由 model、facade 或状态 owner 执行。
2. **Host 事实**：只有宿主知道的 command transport、session identity、worker handle、deadline
   和进程状态。由 hook、extension、adapter 或 supervisor 采集和验证。
3. **模型判断**：需要理解用户目标、设计 evaluator、评价边际价值或选择探索方向。保留在
   skill/prompt，代码校验其结构和后续状态转换。

所有模型生成的结构化结果都是非可信输入。Pydantic model 负责结构边界，状态 owner 在持久化
或执行前校验 identity、归属、artifact、容量、当前状态和幂等前态。AGENTS、SPEC、README、
skill 和普通 prompt 可以解释规则，但不能成为确定性规则的唯一防线。

例如，skill 只需告诉 candidate 在实质修改后调用 `search_run_verifier`。Host adapter 负责提供
可信的 session identity；shared facade/runtime 校验该 session 的 run/candidate 归属、workspace
和 verifier 前态后才执行并记录 Evidence。不要把这些校验复述进 Codex、Pi 的 prompt，也不要
让 host adapter 各自实现一套。

### 保持状态边界

- Search bootstrap、Main 后续操作、candidate 操作和终态 report 分别使用其可验证的归属依据；
  run 完成链接后，Main 对该 run 的操作必须匹配当前 Goal 的持久化链接。
- 非法调用不得改变 Goal、run、candidate、Evidence、selection 或 promotion 等领域状态。
  审计事件、counter、lock 和临时目录属于操作性状态，是否允许变化由入口合同明确说明。
- 异步任务失败、重试或恢复只能更新它拥有的 task/worker/审计状态，不得撤销已经结算的
  verifier Evidence 或改写 hard score。
- 持久化状态是恢复依据，prompt 记忆不是。恢复逻辑必须幂等，不通过手工改写 `.gp/` 跨 gate。

### 保持隔离与最小可见性

- Candidate 只获得 runtime 投影的工作合同。不得泄露 Main context、peer workspace、credential、
  runtime-private constraints、hidden evaluation 或任意 host environment variable。
- Candidate 可以读取自己的 workspace 并本地诊断，但交付修改受 `allowed_files` 限制；冻结
  verifier 和 `denied_files` 保持只读。
- 可选能力关闭时，不生成对应工具说明或操作要求；启用时由 runtime 根据冻结配置动态投影。
- Codex 与 Pi 共用的业务规则在 shared facade 或领域 owner 中实现一次；host 层只处理自己的
  可信事实，不复制业务状态机。

### 保持实现精简

- `domain/` 仅依赖 Domain、标准库和 Pydantic，不读取文件、环境或时间；`ports/` 仅依赖
  Domain、Ports 和纯类型依赖。函数内 import、alias 和 `TYPE_CHECKING` 同样受边界约束。
  `application/` 只依赖 Domain、Ports 和已迁入的纯用例，不构造具体 store 或启动模型。
  AST/import tests 约束 Domain、Ports、Application、Execution 和 Projections；旧 owner 删除与
  真实接线还须结合调用链和行为验证，不以依赖测试代替 lifecycle 证据。
  已迁入的 `execution/` 仅依赖 Domain/Ports、本层与纯 `application.contracts`、标准库和
  纯类型依赖；具体 store/用例在 bootstrap 装配，不能在执行模块中反向构造。

- 优先修改现有状态 owner。新生产抽象必须统一至少两处真实重复、封闭已有职责边界，或关闭
  测试复现的绕过；不要预建 policy DSL、通用快照框架、错误体系或测试 runner。
- 使用现有 Pydantic model 和结构化 API，不用 ad hoc JSON 或字符串解析代替已有合同。
- Skill/prompt 只保留模型判断与正常调用路径，不复制 schema、错误分支或已由代码拒绝的规则。
  删除仍承担真实功能入口的 task-specific workflow 前，必须先迁移并证明等价。
- 将已有 prompt 防御迁移到代码时，先用行为测试复现绕过并建立等价 gate，再删除对应文字
  约束；不保留旧路径兼容层。
- 不增加未要求的旧格式兼容、双写或 fallback。确需迁移时，明确旧数据边界、切换方式和删除
  条件。
- 不使用无来源的魔数。协议边界需要命名、依据和边界测试。
- Verifier 执行和 artifact 绑定必须确定、可复现，并由 runtime 掌握。

## 修改流程

1. 确认改动所属 owner，阅读对应规格、文档和现有测试。
2. 用最小行为测试明确预期，测试规模与改动风险匹配。
3. 在 owner 边界内完成最小实现，并同步受影响的 facade、host asset 或 projection。
4. 运行 focused test 和 default，再按影响范围选择 integration；真实 host ST/E2E 仅在
   开发者显式要求时运行，未运行的 lifecycle 验证记录为缺口。
5. 系统不变量变化时更新 `SPEC.md`；用户可见合同变化时更新对应专项文档。
6. 检查最终 diff，排除无关修改、生成状态和 credential。

## Host 与资产同步

Host asset 是可执行产品界面，不是装饰性文档。

- 产品宿主资产只在 `assets/codex/`、`assets/pi/` 维护；安装器在 release 中生成
  Codex 发现入口及发布副本。源码不得恢复可自动发现的同名 skill、extension 或 marketplace。
  `AGENTS.md` 保留开发约定；`install.sh`、`uninstall.sh` 保留显式执行入口。
- Codex 合同变化时检查 `assets/codex/skills/`、`assets/codex/agents/`、hook/config 模板及
  `tests/test_codex_assets.py`。
- Pi 合同变化时检查 `assets/pi/prompts/`、`assets/pi/skills/`、`assets/pi/extensions/`、worker/pool 及
  `tests/test_pi_assets.py`。
- 不要让任一 host asset 重新引用已删除的 runtime API。共同业务合同不得只在某一端修复。
- `install.sh` 是唯一受支持的源码安装入口。安装行为变化时同步 `scripts/install.py`、安装测试
  和 [开发指南](docs/development.md)；不要静默替换另一 checkout 或删除用户管理的数据。
- `./install.sh --pi --thinkthread` 验证 ThinkThread Pi 所需配置和接口，并为受保护的
  Pi Search Root 写入 Goal Plus 管理的 Profile，不安装、升级或删除 ThinkThread。
  release、protocol、Profile schema 和 fingerprint 差异只警告，不阻止兼容底座独立升级；
  实际接口和 delegated capability 仍须通过检查。Codex ThinkThread 在具备等价真实 host
  覆盖前保持不支持。卸载器只能在
  复核 ownership marker 后移除该 Profile，不得删除 ThinkThread Runtime、Root、状态目录或包。

Pi checkout-local 开发和 Codex 正常 plugin 刷新的具体步骤见
[开发指南](docs/development.md)。不要把账号、认证或全局 host 配置写进仓库。

## 测试与验收

按用户要求，不维护或重新加入 Torch、AscendC/CANNBench 的测试、资产断言或真实硬件
测试入口。通用资源与宿主合同使用中性 fixture 验证。

测试应验证行为、返回结构和状态变化，不断言整段 prompt、中文措辞或内部调用顺序。可以断言
稳定 schema、窄的 reason token 和真正属于接口的文字。

- 共同业务规则在 shared facade/runtime 层测试一次。
- MCP、Pi 和 Codex 各保留薄的入口、序列化及 host-specific 测试。
- 默认 hook 测试用 `tests/_runtime_helpers.py` 的 `run_host_hook_inprocess` 进程内驱动
  `goal_plus_stop_hook.main()`；入口、进程隔离/退出或环境继承等依赖真实进程语义的测试
  保留子进程，不用进程内 helper 代替。源码子进程测试必须设 `PYTHONPATH` 指向仓库
  `src/`，避免解析到已安装的旧拷贝；安装产物测试仍验证自己的隔离安装环境。
- 默认测试不得触发真实模型调用；会启动 `codex exec` 的路径（如 model preflight）必须 mock。
- 状态 gate 同时覆盖合法前态、非法前态、领域状态不变和允许的操作性副作用。
- Worker context 或操作性 prompt 变化时验证结构化 projection，而不是复述文案。
- 模块通过 typed API 和受控中间结果独立验证，优先在对应 owner 复现故障，不以整个 E2E
  作为每次修改的默认检查。参见[模块验证](tests/README.md#module-boundaries)。
- 修改 hook、Pi extension、worker context、skill/prompt 的真实操作合同时，需要对应真实 host
  证据才能声称 lifecycle 已验证；ST/E2E 只在开发者显式要求时触发，否则记录未验证项。
  pytest ST 必须显式加 `--run-system-tests`，不能写入默认参数或由安装自动触发。

### 故障分支测试增补

- 每个需要模型的模块以一个简单 LLM ST 跑通正常路径为基本验收，已有适用证据直接复用。
  确定性模块不附加 LLM；完整 E2E、模型矩阵或全部故障组合不作为模块交付前置条件。
- 遇到真实故障，或本次修改涉及有具体依据的失败风险时，再增补相关分支；不为尚未观察到
  的组合穷举测试。已知缺陷单独记录触发条件和影响，未验证项不能写成通过。
- 先从 Monitor 和准确 task/attempt 记录确定 owner、合法前态、故障点及预期结果；只有
  宿主行为不明时才读 transcript。提取最小复现，不把整个现场或原始日志复制成 fixture。
- 优先在最窄 owner 的现有测试中，用 typed port、受控返回、显式时钟或必要的本地进程
  注入故障；通过正常 API 建立前态，不手改既有 `.gp`，不新增通用故障框架或测试专用旁路。
- 断言可观察行为：错误或明确拒绝、允许的操作性变化、领域状态和已结算 Evidence 的保护。
  对支持恢复的路径验证同请求重试的幂等及最终状态；对不支持的路径验证明确拒绝。
  仅在故障涉及身份、代际、预算或资源清理时补对应断言，不机械复制全部边界矩阵。
- 能由无 LLM 测试复现的错误不依赖真实模型重现。确需真实宿主证据时，仍须开发者显式
  授权，只补目标模块的最小 ST，设置有限超时并在失败后清理资源；不反复看护 Main 全流程。
- 修复后运行相关分支和必要回归，记录故障、修复、证据及剩余限制；合并或删除重复、失效
  及仅约束实现细节的测试，不以放宽有效断言掩盖失败。详见
  [模块验证](tests/README.md#module-boundaries)。

### 检查命令

修改过程中运行相关 test node/file；每个可提交改动运行：

```bash
python -m pytest -q
git diff --check
```

阶段结束按影响范围运行：

```bash
python -m pytest -m integration -q
python -m pytest -m example -q
python -m pytest -m codex -q
python -m pytest -m pi -q
```

真实 host ST 的命令与前置条件见 [Tests](tests/README.md)。环境不具备时明确记录未验证行为，
不得把跳过描述成通过。默认测试使用 `pytest.ini` 的 32 workers 配置；小型环境覆盖并行度时
不超过可用 CPU 的一半和 32 workers。真实 host ST 使用 `-n 0` 或 `-o addopts=''` 串行运行，
避免 host lifecycle 相互干扰；Git lock contention 必须作为失败诊断，不通过放宽断言规避。

Markdown 改动必须运行 `markdownlint-cli2` 并检查受影响的相对链接。

## 调试与本地状态

排查运行问题时：

1. 先用 `goal_plus_monitor_snapshot` 查看 Goal/Search 持久状态。
2. Monitor 未投影所需字段时，再读取 `.gp/runs/<run_id>/` 中的准确记录。
3. 只有分析 worker/host 行为时才查看 native transcript 或 raw log。
4. 使用 `goal_plus_id`、`run_id`、`candidate_id`、`agent_session_id` 和 host handle 关联记录。

Report 是终态持久数据的静态投影，不是 live UI。生成顺序和恢复方法见
[Runtime 调试](docs/debugging-runtime.md)。缺失的观测数据保持 unavailable，不从 transcript 猜测。

- 不提交 `.gp/`、`.tmp/`、`.goal-plus-dev/`、transcript、raw log、credential 或 cache。
- 不手工修改 FrozenSpec、candidate workspace 或既有运行状态来制造通过结果。
- 保留用户已有修改；并行实验使用不同目标目录或 Git worktree。
- Commit 保持单一目的，避免无关格式化和生成文件变动。
