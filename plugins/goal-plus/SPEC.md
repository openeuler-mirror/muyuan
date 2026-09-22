# Goal Plus 系统规格

状态：当前实现合同。本文描述 Goal Plus 必须保持的稳定行为；精确字段以 Pydantic model 和
tool schema 为准。文中的“必须”和“不得”表示不变量，“当前”表示可以通过显式设计继续演进
的实现。改变合同需要同时修改实现、行为测试和相关用户文档。

## 目标

Goal Plus 为长时间 agent 工作提供可恢复、可审计的执行记录。普通任务由 Main Agent 在
Goal Mode 完成；具备明确 metric 和 verifier 的优化任务进入 Search Mode，在隔离 candidate
中探索，并只提升经过冻结 verifier 验证的结果。

系统必须做到：

- 用户目标、执行状态和证据在会话退出后仍可恢复；
- Codex 与 Pi 共用同一套领域规则；
- candidate 可自主探索，但不能改变评价合同或访问 peer 私有工作区；
- 最佳结果绑定精确 artifact，而不是模型自述或当前目录的偶然状态；
- 未启用的能力不占用 worker context，也不暗示不存在的工具或数据。

系统不负责理解 benchmark 的隐藏语义，也不保证公开 verifier 等价于外部官方评测。它也
不是通用 worker supervisor；进程启动、等待、续跑和中断由 host 管理。

GP 不读取操作系统进程表，不核验 PID/启动时间/boot identity/进程组，不提供 Linux/macOS
进程身份适配，不启动 guardian，也不因 Main PID 消失而接管或清理遗留进程。
进程生命周期由 agent/宿主负责；GP 仅控制自己当前持有的直接子进程和原生 session/Child 句柄。
Scope 以可信 native session 和已授权 activation 为依据。启动日志的 PID 仅供诊断；
registered/finished/released 分别表示登记、调用方报告完成和释放登记，不证明进程树已退出。
后台消费者通过锁和 generation 领取、重扫和撤销，异常遗留 generation 须显式释放后重启。
本地输出收集保持有界，不依赖停止后代来获得 EOF。Goal control、deadline、session generation、
Evidence 和 publication 的业务 gate 保留；宿主未报告的执行丢失不由进程探测补造。

## 宿主安装身份

正常 Codex/Pi 安装绑定经过验证的不可变代码 build、锁定依赖和受管 Python 解释器。
Hook、MCP、Pi tool 和内部 Python worker 不得因 shell 环境或目标 workspace 改变而加载另一份
Goal Plus。安装副本与 runtime 必须核验同一 build；开发源码加载仅由显式 Pi 开发入口启用。
更新一个宿主不得隐式替换其他宿主或仍被保留的旧版本。新 runtime 准备失败不改变已有宿主
注册；卸载单个宿主保留 runtime，运行时清理只删除通过归属检查且没有宿主引用的版本。
源码 checkout 的产品宿主资产只存放在非自动发现的 `assets/codex/`、`assets/pi/` 下；
Codex 正式发现入口只在受管 release 中生成，Pi 通过 package 或开发 launcher 显式引用资产。
已存在所选宿主注册时，安装器先列出来源并要求 y/n 确认（默认取消，`--yes` 显式确认），
准备并核验新 release 后再卸载重装对应注册；取消不改变注册或版本引用。
Codex 安装成功前必须核验实际缓存的 build、安装身份与 MCP/hook 启动配置，不能只以注册来源
或 `plugin add` 成功作为验收。缓存校验失败不得提交新版本引用或报告安装成功。
Pi 安装成功前必须核验实际 package 注册、宿主解析出的启用 extension/skill 和 Python build。
校验失败恢复原设置并撤销 pending 引用；显式资源配置和项目自动发现也属于冲突检查范围。
本地和 ThinkThread verifier 的裸 `python` / `python3` 命令在执行时绑定当前 Goal Plus
解释器，保留 venv 路径，不经宿主 PATH 解析；冻结命令和显式解释器路径保持原值。
Verifier 的 Python 模块搜索仅加入其工作目录，不继承外部 `PYTHONHOME`、`PYTHONPATH`
或用户 site-packages。ThinkThread Profile 必须只读开放安装 venv 及解释器软链接链的目标。
ThinkThread Profile 写入后的宿主或配置校验失败时，恢复原文件内容和权限；首次安装则删除
新增文件，不提交新版本引用。回滚失败必须明确报告，不能报告安装成功。
ThinkThread 独立升级不要求重装 Goal Plus。Runtime、Profile schema、SDK 协议版本和 contract
fingerprint 偏离已测基线时仅警告；Profile 的可选协议声明不决定实际 Extension 握手。
安装和运行仍校验所需配置结构、接口调用结果、Root/Child filesystem 与授权 capability。
ThinkThread 的 Goal Plus runtime 目录必须位于 Root 附着文件树之外，使用受管 Profile 的
原生 session 目录；baseline 捕获前拒绝位于 Root 内的自定义路径或指向它的软链接，
不得把 Goal/session 私有状态作为 candidate snapshot 交付。
Pi 开发 launcher 的预检查和工具调用使用开发 venv 的隔离 Python。
两端不发布 Goal Plus 导航 skill。命令仍由宿主授权，公共导航来自 MCP instructions；
删除 skill 不扩大 Main 或 candidate 的执行权限。

### 上下文说明的事后检查

检查以 session 所用 build 的概要原文为准，不读取模型私有思考或把助手复述当作加载证据。
Codex 仅检查 rollout 中的工作上下文，排除 `guardian_history`；Main 的 compact hook
重新注入公共概要，即使 Goal 已结束或不存在也不创建、恢复或授权 Goal。
此诊断不参与 Goal 完成判定。Pi 默认在每次 inference
turn 从宿主最终 system prompt 记录概要哈希和匹配布尔值到原生 custom entry，不保存完整 prompt；
`GOAL_PLUS_SESSION_DIAGNOSTICS=0` 显式关闭新增 session 观测和归档，不关闭宿主原生历史或
业务记录。检查器由用户手动调用，沿选定 session branch 消费观测，
旧历史或关闭采集的历史缺少观测时报告 unavailable。
缺少、损坏、版本不符的证据不得报告通过；记录中可见不等于模型实际遵循流程。

默认情况下，Pi 在 provider request hook 记录本扩展注册的工具描述/schema 与该
请求中的对应合同，并按 session 分支复用相同合同快照。观测不修改请求，不保存完整
prompt、认证 header 或模型思考；它只证明该 hook 时点的可见内容，后续 hook 仍可能修改
请求。离线 session 分析关联工具调用、原生工具错误和参数修正，不据此自动判断模型意图。
缺少请求观测的旧历史只能与显式提供的 tools/list 参考合同比较，不能确认当时模型可见性。
`GOAL_PLUS_DEBUG=1` 仅额外启用请求大小和 API 返回大小统计，不覆盖 session 采集的显式关闭。
TT Child 在每次执行结束后通过原有 Message 通道发送压缩的增量 session 记录；Root 按已绑定的
Child/session 身份收取并在删除资源前补收，存入 runtime 的 `host-logs/session-diagnostics/`。
已发送游标保存在原生 custom entry，跨同一 session 的 Pi 进程重建延续增量，不作为业务恢复依据。
归档保留完整的 Pi native session entry，同时供运行诊断和离线行为复盘使用；批次按大小切分，
并保留压缩校验、entry identity、sequence/cursor 和 partial 状态。Main 索引引用原生历史，
不复制完整历史。
诊断归档不授予执行权，不进入模型上下文，不改变结算或清理 gate；缺失批次、损坏记录和
采集失败不得作为完整证据。突然退出前未送出的尾部不保证保留，外部删除 Root 后也不保证
保留其 runtime；默认不外传或上传归档。

## 核心对象

| 对象 | 含义 |
| --- | --- |
| Goal Plus record | 一个完整用户目标及其 revision、实际 dispatch、Search links 和终态 |
| Dispatch attempt | Main 实际发起的一次原生 subagent 派发及其 attempt/generation |
| SearchSpec | Main Agent 准备的可执行评价合同 |
| FrozenSpec | 已校验、固定 source 和 verifier artifact 的不可变合同 |
| Search run | 在一份 FrozenSpec 上执行的一次搜索 |
| SearchPlan | 为 run 一次性分配初始 candidate lanes 的计划 |
| Candidate | 一条长期探索 lane 及其隔离 workspace、iteration 和 best artifact |
| AgentSessionRecord | candidate 与 host-native session 的上下文/来源绑定，不表示进程存活 |
| Evidence | 一次 verifier settlement 及其精确 artifact、分数和状态 |
| VerifierScoreContract / VerifierTask / VerifierRecord | 冻结评分语义、对精确已结算 subject 的可靠异步评估及不可变输出 |
| SearchSnapshot / UctTreeView | 权威 allocation 输入与仅在 Policy 内重建的临时 UCT 视图 |
| AllocationDecision | 确定性 policy 产生、已校验并可幂等执行的 candidate 替换计划 |
| Promotion | 对已选择 artifact 的独立最终验证与 publication/apply 结果 |

## 架构与所有权

```text
host command
    |
    v
Goal runtime ---- dispatch records / Search links / final audit
    |
    v
Search runtime -- FrozenSpec -- candidate workspaces -- Evidence -- promotion
    ^                                      |
    |                                      v
Codex/Pi host <---- launch / wait / continue / stop ---- candidate worker
```

| 所有者 | 权限与职责 |
| --- | --- |
| Goal runtime | host 授权、Goal revision、dispatch identity、Search 归属、final check 和终态 |
| Search runtime | freeze、workspace、verifier、Evidence、回滚、selection、promotion 和报告 |
| LLM Verifier | settlement 后的局部质量与可选 adaptive metric；不停止、选择或分配 candidate |
| Adaptive Search | Snapshot、确定性 Allocation/Steering 互斥计算和对应 Executor 交接 |
| AllocationExecutor | 持久化 Decision 的淘汰、按序派生、精确身份恢复与 run.lock |
| Main Agent | 理解目标、设计验收合同、决定 delegation/Search、审查结果和全局收尾 |
| Candidate worker | 在自己的合同和 workspace 内选择方向、修改、验证和提交 handoff |
| Host adapter/supervisor | worker launch、native session、wait、budget、continuation、interrupt 和日志 |
| Evidence annotator | 为已结算 Evidence 生成客观 View；不改变硬分和 selection |

防御性规则按其可信信息来源落到不同的可执行边界：

| 边界 | 执行内容 |
| --- | --- |
| Model/schema | 单字段与跨字段结构不变量 |
| Goal/Search state owner | phase、归属、artifact、workspace、Evidence 与终态转换 |
| Shared tool facade | Codex/Pi 共用的 caller role 和调用前置条件 |
| Host hook/extension/adapter | command transport、session identity 与真实 lifecycle 事实 |
| Candidate contract projection | edit surface、process environment 和已启用能力的动态可见性 |
| Skill/prompt | 只能由模型完成的意图理解、合同设计和探索判断 |
| AGENTS/SPEC/docs | 开发约束与设计解释，不承担运行时拒绝 |

“Runtime enforcement”指以上可执行边界共同形成的防御，不要求所有逻辑进入同一个
`runtime.py`。同一领域规则只在共享 owner 中实现；Host adapter 只处理 host 独有事实，skill
只描述模型判断和正常调用顺序。

Pi 共享 Goal/Search 工具从 MCP tools/list 加载 description/schema，extension 在同一 runtime
root 内复用标准 MCP stdio 连接，shutdown 时关闭；调用进入共同校验与实现，Pi/TT 专属
宿主控制保留本地 schema。连接丢失不隐式重启其已托管执行。宿主可在 MCP 构造时绑定 caller
role 与 candidate session identity，按角色限制工具可见性并校验每次调用的 session 参数。
Main 的公共概要只维护在 `assets/goal-plus-instructions.txt`：Codex 的 MCP instructions
和可信 Goal hook context 同源加载，Pi 从同一 MCP manifest 读取并在每轮 system prompt
中加载；其他角色不注入 Main 概要。
概要首行用不超过 250 字符的完整短句覆盖核心流程，后续补充解释；全文不超过 500 字符及
1,000 UTF-8 字节。宿主截取首行时仍保留核心流程，安装和使用不要求选择版本。
该绑定不出现在模型工具参数中，也不替代领域 owner 的归属、代际及当前执行状态 gate。
Pi bridge 已使用该绑定；默认 Codex MCP 的可信角色事实仍由原生 hook 校验。
受信任的 Codex app-server candidate 声明只收窄角色；原生 hook 必须同时核对当前代际的
candidate session、app-server handle 和准确 native thread 才授予候选工具与文件修改权限。
缺失或不匹配的绑定不能退回 Main 权限，也不能按模型参数猜测另一 candidate identity。

统一 native session 控制使用宿主 MCP metadata 中的当前 Main thread identity；模型参数仅
标识目标 candidate/session。run 和 message 必须匹配已授权 Goal 的当前 revision、attached Main、
candidate generation 与有效 scope activation，并受已准备的 worker budget 和当前 deadline 约束。
Worker budget 可省略；不强制最低探索时长、最低验证次数或提前收尾窗口。候选返回后由
Main 判断是否继续，宿主不因返回过早而自动续跑。显式执行上限和 Goal deadline 仍生效，
继续不会重置 Goal deadline。返回不生成 Evidence；未验证的修改仍不得选择或发布，
verifier、RPC 与资源清理的操作超时独立保留。
`goal_plus_session_run` 组合准备、打开、准确绑定及执行；后续调用默认复用当前 session。
相同 call_id 与相同参数不重复执行或结算，冲突参数和旧 generation 请求被拒绝。
活动调用不接受新的 call_id；steering 经 message。
new_session 先确认旧资源清理，再隔离旧 generation；不回滚代码、Evidence 或累计预算。
wait 超时不中断执行。
pending Allocation Decision 原子封住退休目标的启动、续跑、redispatch、recover 和新 verifier
admission；准确归属的 wait、interrupt、stop、close 仍可用，已进入硬结算的事务照常完成。
公共 apply 经可信 session host 保存绑定 Decision/candidate/session/generation/call 的取消
intent 与 receipt，再由 Allocation owner 重核并提交。接受回执可幂等复用，拒绝保持 pending，
结果未知或已启动 session 的 controller 缺失要求恢复，不冒充取消成功。
未知取消可在同一 controller 确认原 call 为 completed/interrupted 后通过同一 apply 请求恢复，
只重放准确观察，不重发不确定的中断；failed 或缺失的调用事实不足以解除恢复要求。
接受取消不证明退出，旧资源清理独立执行；退休后的返回不触发补验。
派生 candidate 从记录的 exact attempt artifact
物化，准备当前 session provenance，再经共同 session 启动。
本地执行超时、stop 或控制器重建不销毁持久原生 session。下一次新 call 的 run
在重新授权并取得该 session 的独占执行 lease 后恢复同一 Codex/Pi session；不更换上下文、
candidate generation 或 workspace，不重置预算、重复统计已观测用量或重放旧 call。
控制器重建从持久 session 读取 native handle 和用量基线。旧 call 的完整结果不可用时
明确报告，不重放原调用；Main 检查领域状态后以新 call_id 继续。
执行 lease 由 native 进程持有到退出；旧进程仍存活时拒绝第二个控制器。新控制器取得 lease
后可确认遗留 scope resource 已结束；released 本身仍不作为停止证明。
恢复待办不将 verification 的持久 running 当作执行存活；结果查询依据结算锁区分
仍被执行方持有的 attempt 与锁已释放的未结算 attempt，后者要求恢复。
缺少原生会话或恢复失败须明确报错，不自动退回新建 session；显式 new_session 可在
旧资源清理后直接接手原 workspace，无需先加载旧对话。已结算 Evidence 保留。
stop 封住当前 controller 的后续调用并停止执行；保留的 TT Child 只能经重新授权的显式
run 恢复控制能力，确认旧调用回调退出且原 execution 已消失后才能继续。close 撤销重开能力
并释放资源；失败的关闭可重试，不能绕过清理。TT interrupt 经准确 session/dispatch/Message
控制请求转发原生 turn abort，宿主在活动 turn 内校验并确认；确认丢失不能宣称中断完成，
不以平台信号请求成功代替原生中断事实。
模型侧 session 准备回执只投影身份、工作区和模型来源；完整 launch 输入由宿主从持久
session 读取。本地 Codex/Pi worker policy 使用共同 session 控制工具。
Main 暂停后仍可读取并关闭自己持有的资源，不能借这些操作恢复执行；MCP 断连的取消不能
跳过资源清理。本地控制器丢失后的普通续接保留 workspace，包括未验证的修改；
pending verifier/settlement 仍阻止执行，须先结算或恢复对应 attempt。
只有 Main 明确放弃当前现场时，显式 Search checkpoint recovery 才隔离旧 generation、归档现场并恢复 checkpoint；
普通续接不重连未知活跃执行，也不重放未完成调用。
ThinkThread 的原生 Child 可在控制器进程丢失后继续存在。当前 Main 可通过同请求 session run
接管同一 scope 中有准确调用日志且原控制器锁已释放的 Child：只观察原 Message、重试
幂等结算及观测提交，保留原 call_id、dispatch、执行截止时间与候选代际，不重发 spawn/wake。
无法确定实际执行身份时保持恢复要求，不猜测另一 Child 或把旧调用作为新调用执行。
宿主返回的调用观测按准确 native handle 和 invocation 写入已有 session provenance，
重复回包幂等，矛盾回包拒绝；不保存模型正文或改写 Evidence。用量采集缺失保持 unavailable，
增量口径以最后成功观测为基线，不将最后一条助手消息冒充整次调用或累计 session 用量。
调用返回状态与 scope 中的进程退出事实分别保留。

最终检查通过共同 `goal_plus_run_final_check(goal_plus_id)` 准备、启动、等待并清理；宿主从
当前 Main 的可信绑定及 scope 模型快照选择执行器。独立 checker 的 MCP 仅能读取所属 Goal
和提交准确 check/revision 的结论，native session 由启动器绑定，提交时在 Goal 锁内重核
control version。Codex 使用只读 sandbox，Pi 仅开放读取工具；模型返回的普通正文不构成
通过证据，结构化 verdict 仍由 GoalCompletion 验收并决定终态。资源返回和退出分别处理。

## 授权边界

只有以下精确 host command 可以创建或编辑 Goal Plus：

- Codex：`$goal-plus ...`、`$goal-plus edit ...`
- Pi：`/goal-plus ...`、`/goal-plus edit ...`
- 独立最终检查：`$goal-plus-with-final-check ...` 或 `/goal-plus-with-final-check ...`

原生 session resume 只恢复会话与绑定，不启动任务。精确 host command 还支持
`summary`、`pause`、`resume`、`clear`；只有显式 resume 能解除中断后的任务停止。
新的原生 session 使用精确 start 命令会创建独立 Goal；其他 session 的 nonterminal、paused 或
blocked Goal 不得阻止创建，也不得被隐式接管。同一 host session 已有未终态 Goal 时，新的 start
必须拒绝，并要求使用 edit 或 resume。
共享 continuation 状态机核对 `control`、revision 和 `active_session`。自然语言提及、skill 自动发现、普通 child、
candidate worker、直接 MCP 调用和 generic CLI 都不能创建或编辑 Goal Plus。Search bootstrap、
Main 后续操作、candidate 操作和终态 report
分别使用其可验证的归属依据；run 完成链接后，Main 对该 run 的操作必须与当前 Goal 的持久化
链接一致。同一 Codex session 的 `SessionStart` 和普通 `UserPromptSubmit` 保留已有绑定；
是否继续执行由持久化 control 决定。拒绝时不得改变领域状态。

同一原生 session 正常运行时，普通用户消息优先处理，然后继续原任务。明确中断或执行丢失将
control 标为 paused；普通聊天、原生 session_start 均不解锁。正常 steering 不当作中断。
pause 禁止后续续跑和新派发，不强杀当前 turn；paused 下已关联 run 的结算类工具
（verifier 提交、恢复/迭代读取、work event 与 final check 提交）仍允许完成已运行工作，
clear 永久撤权且保留历史。
blocked 是显式可恢复停止，旧终态 blocked 迁移为 closed，不复活历史。paused/blocked 下 edit
不激活；有效编辑 complete 可产生新 revision。不同 session、fork、detached/stale 和已结束任务
不继承执行权。resume 保留原 deadline、累计用量及 Search 身份。
显式 resume 只为命令发生前已经关闭的 Execution Scope 授权下一代 scope；新 generation
持久化该 resume identity，后续操作持续复用。scope 尚未关闭时观察到的 resume 不得在其
之后关闭时再次授权。Codex `SubagentStop` 使用 child transcript 的 canonical task identity
与 native identity 共同结算；二者指向不同 child 时不更新终态，保留 active fence 并记录
结构化 reconciliation 结果。
完整控制合同、持续 Main/reconciliation 和验证边界见[运行逻辑](docs/runtime-logic.md)。

## Goal 生命周期

一条 Goal 的正常流程是：

```text
goal -------------------------------------> [final_check] -> terminal
  \-> spec_discovery -> search -> final_audit -> [final_check] -> terminal
```

创建目标和有效编辑后的新 revision 默认进入 `goal`，下一步为 `execute_goal`。
普通 coding 无需调用 `goal_plus_record_triage`；缺少 triage 不得阻止正常工具使用、
普通任务完成或按 policy 申请独立终审。用户明确要求 Search、并行/多 agent 协作、优化、
多方案探索或比较时，Main 应进入 Search 准备；同一任务具备可评分目标、可执行 verifier、
隔离 edit surface，且多 agent 探索互补方案有预期优化价值时，也应主动进入。评价合同不完整时
先记录缺项并准备，而不是因默认 Goal 路径跳过；无法建立合同则向用户说明原因并继续可执行的
普通工作。完整 SearchSpec 直接提交 freeze，或先按需记录分流判断和缺失条件。
进入 Search 后仍须完成冻结、候选验证、selection、promotion 和所需 apply。

`active` 表示仍可推进；`needs_user` 只用于确实缺少用户决定；`blocked` 是可恢复停止，
`complete` 和 `abandoned` 是终态。Runtime 的 `next_action` 是当前 revision 的权威下一步，skill 不应自行
维护第二套状态机。

Main Agent 自主拆解普通任务并决定是否使用 subagent。Runtime 不要求预建执行计划或
work DAG，只记录实际发生的 dispatch。共同 `goal_plus_run_work_item` 创建 dispatch attempt，
启动成功后绑定 handle；后续 message、result、rework 和终态必须携带当前 attempt identity。
同一 work_item_id 的 result_ready 通过该工具在同一 native session 返工，结果由 Main 审查后
记录 accepted。宿主在状态 owner 的锁内重新核验启动/返工的 Main identity 和 control version；
已返回的 session 可保持空闲，其存在不代表任务仍在执行。MCP/scope 负责资源关闭；
result_ready 在控制器丢失后可由再次运行该工具恢复同一原生会话，前提是准确登记的旧资源
已完成清理。缺失记录或仍活跃的资源不授权恢复。Pi 专属 work-item 编排已移除。
迟到的旧 generation 结果保留为 stale Evidence，但不能覆盖当前 attempt。具有确定 metric、
可执行 verifier、隔离 edit surface，且并行探索有价值的任务适合 Search；合同不完整时先进入
Spec Discovery。

模型可见的状态和变更回执必须使用显式投影，不能默认回放整个持久记录与审计历史。
当前 control、revision、下一步动作、待审查结果和恢复身份保持可见；完整目标与草案按需
读取，历史详情分页读取。变更回执保留本次操作的准确 attempt、Search 或 final-check identity。
状态与 Monitor 默认 `detail=false`；配置、成功结果明细和报告路径经
`detail=true` 只读查询。写操作回执指向只读详情入口，不要求重复执行写操作。
Search freeze/plan/batch 和 publication 的默认回执保留后续操作需要的 ID、实际分配、
输出位置、状态与异常，不回显完整 spec、CandidateTask 或 provider 记录。candidate 工作合同
仍由 host/context 完整交付。Session 控制回执绑定准确 call/native invocation；wait 默认
返回状态、错误及结果可用标记，`detail=true` 只读取得同一调用的完整正文和诊断。
`completed` 不代表验证通过，Search Evidence 仍由原查询与 selection gate 判定。
普通工作交付正文与独立 checker 的结构化 findings 保留，资源关闭不回放已交付结果。
错误、未交付结果、未完成清理与未知资源数始终保持可见。
Codex/Pi 共用这些返回合同；持久化、授权、Evidence 和恢复不因展示裁剪而改变。

## SearchSpec 与冻结

Main 决定目标、metric、source/edit surface、verifier、并发预算、策略及可选能力；
模型 provenance、context policy 和固定策略由 runtime 补齐；可调资源限额仅在省略时使用默认值。
输入字段、verifier 输出协议与冻结回执见 [API](docs/api.md#冻结合同与创建-run)。

`constraints` 是 runtime 私有控制数据，不会整体投影给 candidate。Main Agent 必须让最初
目标、candidate 验收、ranking 和最终验收使用同一标准；不能先按一套标准搜索，再用未经
冻结的另一套标准选择结果。

Freeze 与 run bootstrap 必须：

1. baseline 前校验完整 SearchSpec、策略、宿主能力与显式 host command config，不要求 ready draft；
2. freeze 时固定 clean source identity 和 verifier artifact hash；
3. Git 原位、Copy 在一次性副本执行完整 process baseline；ThinkThread 在 create 时执行；
4. 创建 run 时确认 source identity 未变化；
5. 首次物化 Git worktree 时执行 workspace relocation gate；
6. 全程校验 metric、退出码、timeout、edit surface 和可选 device pool。

本地 Git/Copy 冻结必须确认 process verifier baseline 实际产出有限数值分数；没有声明
`ranking_signal` 也不能跳过。分数必须来自精确 metric，且来源唯一，不能用运行时元数据或
promotion 分数替代。评估完成与候选通过分别记录：明确无效但有分数的基线允许启动搜索，
无效候选不能进入 best、selection 或 promotion。故障、超时、中断与 anti-cheat 失败不能
当作无效基线；diagnostic 失败不阻断。分类由 evaluator 或了解任务的适配器提供，不得
一律吞错转零分。promotion suite 仅提升时执行，freeze 不证明其可用。

FrozenSpec 创建后不可原地改写。评价合同有误时，使旧 run invalid，冻结新合同并创建
successor run；不同合同的分数不得直接比较。

FrozenSpec 绑定 Goal revision。模型侧 create 在副作用前保存 request_id 指纹、revision/control
version 与预分配 run ID，分阶段恢复创建、bootstrap 和 Goal 关联。同请求返回原 run，参数冲突
拒绝；未知 provider 结果通过原 journal 确认，不盲目重发。关联时重新核验 Goal 状态与 revision，
未关联 run 不允许候选执行。失败请求保留可查询状态，旧 revision 请求不能关联到新 revision。

模型可见工具统一使用 goal_plus_ 前缀，仅注册新名称。Main 目录稳定，worker/checker/annotator
按角色及冻结能力可见；host gate 和创建/编辑拒绝入口不进入模型目录。宿主 namespace 必须精确
匹配，其他插件的同名动作不获得授权。历史草稿仍可读，总 Goal 编辑仍由精确 host edit 命令授权。

## Search 生命周期

单次尝试的 `failure` 是结算结果，不是 Goal 或 Search 的终态；它和其他 disposition 一样
保留 Evidence。缺少有效分数的尝试不得计入成功统计，缺分数时记录 `MissingNumericMetric`。
Candidate verifier 或结算派生投影的操作异常向调用方报错，不得仅凭异常把整个 Search
置为 `failed`、关闭其他 candidate 的执行或撤销已结算 Evidence。恢复仍遵守既有 artifact、
session 和 generation gate，selection 从已结算 Evidence 重建 best。

Process verifier 的提交和等待绑定准确 attempt 与不可变 artifact。同一 candidate 仅允许一个
未结算验证；Codex/Pi、本地/ThinkThread 共用 `goal_plus_search_run_verifier`，省略 attempt_id
提交，提供 attempt_id 则查询或有界等待原结果。等待超时不取消执行，不创建新 attempt。
回执通过 candidate_action 指示等待或结束 turn；ThinkThread 在静默快照验证后通过下一次
wake 反馈。完成结果必须已硬结算；LLM Verifier 独立异步处理已结算结果。
本地与 ThinkThread 执行按冻结 resource_lock 在共享控制器锁目录内互斥，不同资源可并行。
TT 执行结果未知时持久保留原 request 的资源占用，只允许准确请求恢复后释放；不伪造失败分数。
该互斥不覆盖未接入同一锁目录的外部执行者，也不提供分布式资源调度。

本地未结算 attempt 的提交输入与完整 verifier 结果持久化。中断后按原 attempt ID 显式 retry，
在原不可变快照上继续验证或幂等补齐结算，保留当前 workspace、Evidence 归属和累计预算。
存活的直接 verifier 子进程持有候选与资源锁，控制器丢失不授权重复执行。只有无法核验提交
记录或恢复 artifact 时才需要 Main 决定 checkpoint；完整失败结果不作为中断自动重试。
本地 verifier 被信号终止不构成评价结果：保留诊断日志与未结算 intent，retry 在原快照上
重跑，不生成失败 Evidence。正常退出的失败和配置 timeout 仍按原合同结算。

本地 intent 固定 artifact、工具输入与消费回执。完整 iteration、分数及 ledger 原子提交后，
才发布工具索引或清理输入。正常执行与 resume 共用幂等 reconciliation，不重复计分或改变
同分 best 的结算顺序。恢复与门禁见 [Search 一致性恢复](docs/search-recovery.md)。

一次标准 run 按以下顺序执行：

1. 使用当前 `goal_plus_id` 调用 `goal_plus_search_freeze_spec`
2. 使用同一 `goal_plus_id` 调用 `goal_plus_search_create` 并链接当前 Goal revision
3. 创建一个 SearchPlan，一次性物化初始 candidate lanes
4. Host 为每个 candidate 启动并绑定 native session
5. Candidate 循环修改并调用权威 verifier
6. Host 在预算允许时 continuation 同一 candidate、session 和 workspace
7. 所有 live worker 静止后执行 selection
8. 对选中 artifact 执行 promotion verifier
9. 在用户授权时 apply，否则输出 artifact-only publication
10. Goal 记录 Search result，完成 final audit/可选 final check，进入终态
11. 终态后生成一次 report

`parallel_loops` 使用固定 lanes：只做初始分配，不因低分补建 candidate；后续优先继续同一
native session，必要时才在相同 candidate/workspace 上 redispatch。

`parallel_loops` 和 `adaptive_search` 共享固定的 LLM-Verifier 质量规则与逐级容差 Selection。
显式配置 `strategy.verifier` 或 Selection 使用 `quality_score` 时，settlement 提交单次事实、身份和 diff，
并幂等推入 Verifier 队列。评估器内部处理模型失败、缺分与重试，最终生成绑定精确
candidate、iteration、settlement 和 artifact 的完整评分或失败 VerifierRecord。通用 `scores`
包含 `strategy.verifier.scores` 选择的已注册指标，无需启用 allocation；基础质量评估始终使用
`quality_score`。FrozenSpec 分别固化指标 key 选择和 policy 版本/参数。Verifier 冻结自己的规则到
task，Record 的评分 key 就是唯一得分身份；score contract hash 绑定规则，不充当指标版本。
公共策略配置在 Freeze 前补齐已启用 Allocation/Steering 声明的输入指标，保留用户额外选择的
key；未知或重复 key 仍拒绝。提供 allocation 或 steering 时自动选择 adaptive_search；显式冲突模式仍拒绝。
parameters 省略的字段使用 policy 版本已有默认值，完整配置参与冻结与 spec hash。
没有 policy 时，Selection 使用 quality_score 也补齐基础 Verifier 配置。仅 hard_score Selection
不需要模型；没有 policy、没有质量评分需求且未显式配置 verifier 时才关闭 LLM-Verifier。
`swe_search_uct/v1` 消费 `progress_reward`
与 `allocation_utility`，范围分别为 `[-1,1]` 与 `[0,1]`。Adaptive Search 核对所需评分 key，
再构建不可变 SearchSnapshot，经无 I/O AllocationPolicy 及其固定的纯算法生成唯一
canonical Decision，再由 AllocationExecutor 在 run.lock 内幂等淘汰、物化和派发。
`budget.max_parallel` 是实时活跃 candidate 上限；可选
`max_candidates` 是累计物化上限。Candidate id 只在 Executor 应用 Decision 时分配。

SearchSnapshot 的父边必须跟随 SettlementReceipt 的 attempt base、ledger endpoint 和 rollback
target，不能按 VerifierRecord 顺序跨过缺失评分或 rollback 边界。每个 Candidate 显式投影当前
已评分 `tip_node_id`；当前 checkpoint 无法绑定评分 node 时，AllocationPolicy 必须 no-op。

Agent 身份使用 `AgentHarnessKind=codex|pi`，执行托管方式使用
`AgentRuntimeProvider=direct|thinkthread`，工作区使用
`WorkspaceProvider=copy|git_worktree|thinkthread`。SearchSpec 使用 `workspace.provider`，
其他工作区 DTO 使用 `workspace_provider`；`ArtifactRef.provider` 保留精确 artifact identity。
Freeze 从 Goal 当前已授权的 Main 会话取得 harness，由装配层解析 runtime provider，
保存必填 `FrozenSpec.agent_harness`、`runtime_provider` 并一起纳入 spec hash。
工作区不能改写 agent 身份；模型不能指定 caller 身份。支持 Codex/Pi + direct + Copy/Git，
以及通过 Root/Profile/capability preflight 的 Pi + ThinkThread 和 Codex + ThinkThread
（TT workspace）；两者都支持 `parallel_loops` 和 `adaptive_search`。Codex + ThinkThread 不支持显式 `workers=` 模型 override 与
`reasoning_effort`/`service_tier`，TT Root 内的 Ultra delegation、final check 与
Stop-hook 续跑保持不支持。

Session、session_handle、角色 profile、worker policy 和调用观测分别表达 harness/provider；
状态 owner 在执行或写入前核对冻结绑定、精确 handle、scope 和 generation。
Candidate 与 Annotation 使用冻结绑定；LLM Verifier 复用 direct structured inference。
Delegation 与最终 checker 保持直接启动。`strategy.inner_agent` 名称和行为不变，
省略时 direct 默认 autoresearch、ThinkThread 默认 common，显式选择优先。

用户仍只需 `$goal-plus <自然语言诉求>` 或 `/goal-plus <自然语言诉求>`。
普通任务进入 Goal Mode；Main 在需要 Search 时准备评价合同，信息不足时继续原有澄清流程。
新身份字段不增加用户必填配置。模型发现工具显式接收 `agent_harness` 和 `runtime_provider`。
不保留旧字段 alias、双写或迁移。切换前使用旧 build 关闭活动资源并收尾旧记录，
新 build 创建新记录；旧或未完整解码的记录不得被忽略而绕过授权和终态 gate。
`.gp`、scope/resource journal 与安装目录布局保持不变，不自动刷新用户安装。

Annotation 与 LLM Verifier 继承冻结的 Main Agent 类型，角色驱动按支持矩阵装配。
Codex hook 或 Pi extension
从当前 native host 进程启动这些内部 agent，子进程完整继承宿主外部配置并携带非敏感 host
provenance；Goal Plus schema、FrozenSpec 和 task profile 不接受或持久化服务认证、endpoint 或
host 配置目录。Pi extension 还把当前 Main 的 exact model 和 thinking 固定为 execution scope 的
非敏感 native launch snapshot；未显式覆盖的 Pi child 必须使用该 snapshot，不能重新读取机器级
default。shared runtime 不启动模型，也不等待模型进程。Verifier task 只进入 run-scoped 可靠队列，
不阻塞 candidate 的启动、续轮、hard settlement、恢复或收尾。required supplemental Annotation 或
required selection score 尚不可用时，selection
保持 wait/blocked，不能运行时改变 `ranking_keys` 降级。

模型目录只用于解析显式 model 引用，不构成推理连通性证明。现有 Codex hook/Pi extension
入口必须在 Goal 创建和 SearchSpec freeze 前，收集本次新增的显式 worker 与 Annotation
model override，按模型去重并并发执行最小推理请求；任一失败时不得创建 Goal 或冻结
SearchSpec。没有显式 override 时不发送 probe：正在运行的 Main 已经证明当前模型可用，内部
agent 保持 host 原生继承语义。该检查只使用 native host model 接口，不读取或传递 API
key、endpoint 或独立 provider 配置。

LLM Verifier 输出是不可信的局部评估。Verifier owner 校验动态 schema、精确 subject、task identity、
task 自身的 verifier score contract、lease 与幂等前态；它不能返回 winner 或可执行 Decision。Selection 和
AllocationPolicy 使用冻结参数执行
纯计算。AdaptiveSearch 单次计算 Plan 并持久化 Decision；Executor 仅执行既定排序和条件，
检查 source 身份、candidate 当前状态、容量及幂等前态，不重算策略。Verifier、annotation 或 allocation 失败不得撤销 hard settlement
或绕过 promotion。评估、队列和确定性 Selection 合同见
[LLM Verifier](docs/llm-verifier.md)，Allocation、Steering 与 Host 接线合同见
[Adaptive Search](docs/adaptive-search.md)。

Policy Algorithm 只处理普通数值、序列和拓扑，不理解 Candidate 或生成 Plan。具体
AllocationPolicy 独立拥有算法依赖、组合公式、缺失值规则及参数语义。
Policy 固化按优先级排列的淘汰和派生备选及兼容名单；已知可评分 tip 数量不足冻结并行宽度时
不决策。Snapshot 不推断候选是否仍活跃，真实资格和容量只由 Executor 检查；`max_parallel=1` 也执行相同规则，
可从更强的历史 attempt 派生 replacement。当前唯一版本 `swe_search_uct/v1` 从 Snapshot lineage 临时重建 visits
和 utility 回传均值，完整组合 exploitation、exploration、early bonus 与 late penalty，并用连续
低 progress streak 做硬止损；所有派生 UCT 状态都不持久化。

Steering 缺省关闭，首个版本为 search_diagnostic_guidance/v1；显式 strategy.steering 冻结
策略参数及 shadow/apply delivery，可独立于 Allocation 启用。省略参数时展开 v1 默认值，delivery
默认 apply。五项诊断指标由 Verifier 独立注册并由配置层自动补齐，仍在一次评估中通过
通用 scores 输出，结构化 findings 与其引用检查属于同一 task 的原子完成合同。
AdaptiveSearch 在未配置 Allocation 或 AllocationPolicy 返回 None 时计算 Steering；已有同 Snapshot 的
AllocationDecision、计算失败或 Plan 执行失败均不回退。SteeringExecutor 独立复核来源、
代际、退休与活动调用，在发送前登记一次性机会，再于 run.lock 外尝试 Host 消息。
缺少回执、拒绝或崩溃不重发；允许登记后未送达，不保证候选消费。消息失败不改变评估交付，
不撤销结算或修改资源分配。Host 不拥有策略协调；无消息恢复队列或扫描补发入口。
诊断视图只读取自身有界结算历史，不读取私有 rationale 或 peer 状态；固定输入重放不增补事实。
冷却、候选总次数、问题次数和同问题同证据抑制由发送前的锁内准入再次核验，登记后无论回执
均计次，换动作或会话不解除占用。shadow 不发送、不取得发送许可，不能升级旧 trace 为待办。
引导只促进纠错、可证伪检验、精确去重和边界暴露，不修改验收或保障存活；充分边界证据为
no-op，Allocation 仍独立执行原止损和退休规则。

Executor 在 run.lock 内按既定排名检查执行资格，持久化唯一 selected_retirement 后 fence 该候选，
再由 host 按精确身份中止 live worker；只有中止确认为成功后才能物化和派发。
执行恢复只消费持久 Decision，不重新交付 VerifierRecord 或重算 Policy；未知原生结果保留身份并
阻止较晚 Decision 执行，不阻塞 Verifier 评分。
这一流程不等待 worker 自然结束，也不得 fence 整个 run、关闭 sibling 或清理其持久 Evidence。
Codex/Pi 由可信 native host 调用精确 session/call 的中止接口；Allocation runtime
只接收可信 host 回执，不直接监督 worker。

## Candidate 合同

Candidate 首次启动或 fresh session 恢复时，通过 `goal_plus_search_get_agent_context` 得到顶层目标、
metric 和 `candidate_task`；这些内容共同构成 worker 的完整运行合同，包括：

- 当前 `run_id`、`candidate_id`、objective 和 metric；
- candidate workspace、允许/禁止交付的文件；
- 对 worker 可见的 `candidate_acceptance`；
- candidate 专属 process environment；
- 仅针对当前已启用能力动态生成的 instructions；
- task-local、Git-tracked skill 路径。

Candidate 可以读取 workspace 内所有任务文件并进行本地诊断，但交付修改只能落在
`allowed_files`。冻结 verifier 和 `denied_files` 保持只读。`.tmp/` 是 candidate-local scratch，
不属于交付 artifact。

Candidate 不应获得：

- Main Agent 的私有编排上下文或完整 raw goal；
- `constraints` 整包内容；
- peer 的真实 workspace、transcript 或私有推理；
- hidden evaluation、credential 或任意 host environment variable。

本地命令只提供分析信息。`goal_plus_search_run_verifier(agent_session_id=...)` 是唯一形成正式 Evidence
并参与共享搜索的验证路径。Verifier settlement 后，runtime 以已结算 workspace 状态作为下一
轮起点。

## Evidence、选择与提升

每条 Evidence 必须绑定 candidate、iteration、精确 commit/artifact hash、verifier result 和
metric。Global Evidence 可以共享这些已结算事实及其客观 View，但不共享 peer 私有推理。

Evidence 模式：

- `manual`：worker 主动读取 Global Evidence；
- `auto`：verifier 返回时注入最新共享快照；
- `independent`：只向 worker 暴露自身 Evidence，不暴露 peer Evidence。

Supplemental evaluation 和 shared_dir 都是可选能力。启用时由 runtime 把必要入口动态写入
candidate contract；关闭时不出现相关说明。它们不改变 verifier 硬分、selection 或 promotion
gate。

Evidence annotator 只能读取授权的 immutable Artifact View 和已结算 Evidence，不得运行项目
代码、测试、verifier 或执行只读查看之外的命令。Host 必须只向 annotator 激活领取任务、只读
Artifact View 和提交结果所需的精确工具集合；snapshot 保持原样，不为 annotation 创建、patch
或删除派生副本。

Runtime 根据 `metric_direction` 从有效 Evidence 中维护 candidate-local best 和 global best。
Selection 必须固定某条已验证 Evidence 的精确 artifact。Promotion 恢复该 artifact，并独立
执行已配置的 promotion verifier；不得用缓存分、当前 workspace 或模型声明代替所需的
promotion evidence。失败的 promotion 不生成可应用结果。

## Host 与资源合同

Codex 和 Pi 共用 Goal/Search 领域规则与 MCP session 控制；Codex adapter 使用 app-server，
Pi adapter 使用原生 RPC，ThinkThread adapter 使用 retained Child 与 Message 协议。
旧 Pi/TT pool 编排和模型工具已删除。Worker policy 的 `session_control` 声明共同七个控制
工具；结果就绪和资源退出分别观察，scope journal 保存 TT Child 的准确资源身份。
Monitor 的 `candidate_sessions` 只展示这些已采集资源状态，缺失记录不能证明关闭。

Host 持有的 launch payload 是权威映射输入。Runtime 不轮询 host 进程，不把 host liveness
写进 Search 领域状态。`AgentSessionRecord` 只保存绑定、上下文和 provenance。
候选启动输入不包含模型侧的 native spawn/followup 工具脚本；行为由 `inner_agent` 选择，
不接受旧 `worker_agent_type`。候选 handle 使用 `external_id` 绑定原生 session，不接受
task name 作为替代身份。Codex hook 只从可信启动声明与持久化的准确 app-server handle
识别 candidate，不从 prompt、旧角色名或任务名授予 candidate 身份。恢复和 selection 静默
依赖 scope 中准确 session/run 的进程资源退出，旧 terminal metadata 不作为退出证明。
既有普通 native subagent 的 scope 跟踪与清理独立保留，不参与 candidate session 授权。

设备资源由 host 通过受信任的 `GOAL_PLUS_DEVICE_POOLS` 注册。SearchSpec 只能引用 pool ID；
runtime 为 lane 固定非敏感可见变量和设备，并通过共享 lease/resource lock 防止超售。不能让
SearchSpec 读取任意环境变量。

## 持久化与恢复

目标 workspace 下的 `.gp/` 保存 Goal、FrozenSpec、run、candidate、Evidence、promotion、
host metadata 和最终报告。启用 LLM Verifier 时另外持久化 VerifierTask 和 VerifierRecord；
Adaptive Search 还持久化追加式 Snapshot 与 AllocationDecision，不存在 reconciliation journal。
Settlement 同时保存单次事实、身份与 diff 的不可变 submission，入队中断可重放。
VerifierTask 的重试和缺分不是终态；完整得分或最终失败才生成不可逆 VerifierRecord。
串行队列把 record 同步交付 AdaptiveSearch：追加 Snapshot、确定性计算 Plan、持久化 Decision，
由 Executor 在 run.lock 内完成淘汰与按排序尝试派生、派发，然后领取下一项。
未知派发保留原 child/session 身份，消费保持待恢复；不得假定失败后重复启动新候选。
评分、Snapshot 与计算不取得 run.lock；只读查询不驱动流程，执行器不重算策略。
状态文件是恢复依据，prompt 记忆不是。

恢复时优先使用 `goal_plus_monitor_snapshot`，再根据持久状态执行 `next_action`。底层文件可
补充 monitor 未投影的字段，transcript 用于排查 host 行为；二者都不能取代持久领域状态。
不得手工修改 `.gp/` 来跨过 gate。

显式 checkpoint 恢复以最后一次完整结算的 `ArtifactRef` 与 ledger 为 checkpoint；无完整结算时
使用初始 checkpoint。准备恢复先隔离旧 execution generation，宿主确认旧执行静默后，才由
workspace provider 恢复受管理的 candidate 工作区。恢复事务必须幂等，不重置 run/candidate
身份、Goal revision、预算或已结算 Evidence；旧代际迟到结果不得结算。恢复后使用新 worker
session；不假定原生 transcript 与 artifact 原子配对，不以 Git 专属 reset 作为公共合同。
不支持恢复的 provider、未知宿主状态和不一致 checkpoint 必须显式拒绝。
详细合同见 [Search 一致性恢复](docs/search-recovery.md)。

## 变更验收

涉及不变量或状态转换的变更，应在实现或评审材料中回答：

1. 哪个持久状态所有者负责这条不变量？
2. 正常前态和非法前态分别是什么？
3. 哪些领域状态必须不变，哪些操作性副作用允许存在？
4. 这是保持现有行为、修复 bug，还是明确删除行为？
5. 哪个行为测试能在不比较 prompt 措辞的情况下证明它？
6. 是否改变 Codex/Pi 共同合同，还是仅改变某个 host lifecycle？

共同规则在共享 facade/runtime 层实现和验证。Host 的真实启动、hook、context、操作性 prompt
或 continuation 合同变化时，同时修改对应 adapter 测试并运行真实 host ST；环境不具备时明确
记录尚未验证的行为。
