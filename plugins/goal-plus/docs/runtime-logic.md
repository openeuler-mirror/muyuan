# Goal Plus 运行逻辑

## 范围与验收状态

本文定义 Codex/Pi 共用的 Goal Plus 运行控制合同，以及其在本分支的实现边界。
只修改 Goal Plus 和必要的 benchmark 适配，不修改宿主本体、不创建原生 Goal。
功能实现、自动化验证和真实宿主验证分别记录；不能用 active/restored 状态代替完成证据。

实现依据：[SPEC](../SPEC.md)、[共享控制](../src/goal_plus/goal_plus.py)、
[宿主执行观察](../src/goal_plus/host_recovery.py)。具体恢复事务见
[Search 一致性恢复](search-recovery.md)。真实测试入口见 [Tests](../tests/README.md)。

## 整体模型

| 层次 | 恢复什么 | 不做什么 |
| --- | --- | --- |
| 原生 session resume | 原对话、历史及同一 session 绑定 | 不解除任务停止，不主动启动 Main |
| Goal Plus resume | 同一任务的持续 Main | 不新建 Goal/revision/Search，不重置预算 |
| Search recovery | 受影响 candidate 的完整结算点及 fresh worker | 不无条件回滚健康 worker，不替换原 trajectory |

运行资格、会话归属和真实执行分离。attached 不证明进程还活着；active 不等于正在运行。

```text
 Codex hooks / native input                  Pi extension / native input
             |                                           |
             +---------------------+---------------------+
                                   v
                    Shared command + control owner
                   Goal / revision / session / version
                                   |
            +----------------------+----------------------+
            |                      |                      |
      native restore         explicit resume        ordinary input
      binding only           enable same Main       handle input first
            |                      |                 if active: continue
            |                      v                 if stopped: chat only
            |             one native Main turn             |
            |                      +-----------------------+
            |                      v
            |          phase / next_action / work_items
            |                  Search reconciliation
            |                      |
            |             bounded work + persist
            |                      |
            |           normal end -> shared Stop gate
            |                    /       \
            +-- wait     active /         \ paused / closed / needs_user
                               v           v
                          next Main       stop
```

续跑是事件驱动的，不增加 Search runtime 常驻监督器。进程观察、停止及有界等待由宿主负责。

## 命令合同

表中 GP 表示 Codex 的 `$goal-plus` 或 Pi 的 `/goal-plus`，不是 shell 可执行程序。

| 命令 | 行为 |
| --- | --- |
| GP / GP summary | 查询当前 session 的任务摘要，不创建或更改目标 |
| GP <目标> | 为当前 native session 创建并启动；本 session 已有未结束任务时拒绝静默替换 |
| GP edit <完整目标> | 有效变更产生新 revision，默认回到普通 Goal 执行，隔离旧 work/Search/final check |
| GP pause | 停止后续自主续跑及新派发，不主动强杀当前 turn |
| GP resume [补充信息] | 显式重启原 Main，并在同一次恢复中交付可选用户输入；busy/重复交付不得创建第二个 Main |
| GP clear | 永久撤销执行资格；保留历史、Evidence、产物和报告，不可 resume |

独立最终检查仍由 `$goal-plus-with-final-check` / `/goal-plus-with-final-check` 创建。
summary/pause/resume/clear 是保留词，不能解析成目标。控制操作只接受可信宿主入口，
模型工具、自动加载 skill、历史消息中的命令和普通 child 都不能伪造权限。

`resume` 后的正文支持多行，作为用户补充信息保留在原生会话上下文中；不解析其中的
`key=value`、不替换原目标，也不产生新 revision。显式 host resume 会将 `blocked` 或
`needs_user` 恢复为 `active`，解除运行时执行门禁；Main 仍须先处理补充信息，并按
`next_action` 和原始目标判断业务问题是否已经解决。省略正文时也执行同样的状态恢复；
summary/pause/clear 仍不接受正文。例如：

```text
/goal-plus resume 依赖已经安装好，请使用 /opt/tool/bin/tool 继续。
```

Codex 使用 `$goal-plus resume ...`，原始用户输入随恢复 turn 一起交付。Pi 的交互命令及
print/JSON 输入均先完成恢复授权，再将补充信息交给 Main；正在运行时通过 steering 交付，
已有 Main 等待启动时附到该次启动，不额外启动一轮模型。

### 默认执行与 Search 进入

创建和有效编辑目标后，共享 runtime 设置 `phase=goal`、`next_action=execute_goal`。
Main 直接进行普通编码，不需要调用 `goal_plus_record_triage` 或记录每次编辑。用户明确要求
Search、并行/多 agent 协作、优化、多方案探索或比较时，Main 应进入 Search 准备；同一任务
具备可评分目标、可执行 verifier、隔离 edit surface，且多 agent 可探索互补方案并有预期优化
价值时，也应主动进入。触发后评价合同尚不完整时，先记录缺项并准备，不能因默认 Goal 路径
跳过；不能建立合同则向用户说明原因并继续可执行的普通工作。随后调用
`goal_plus_search_freeze_spec` 提交完整评价合同；`goal_plus_record_triage` 可按需记录 Search 判断与
缺项，不是普通工具的前置条件。
Search 工具仍受冻结、归属和 publication 检查；Goal 的暂停、deadline、终态审计
及可选独立最终检查继续生效。Codex/Pi 共同使用这一状态转换。

控制命令按宿主实际交付时机生效。Codex UserPromptSubmit 通过正常输入启动 Main，
不会为了拦截忙时 pause 使用会结束当前 turn 的 hook 返回值。不承诺原生 /goal 的无模型 UI。
Pi 交互 command 与 print/JSON input 共用同一 host-only 控制入口。

## 状态模型与命令可用性

| 维度 | 持久字段 | 含义 |
| --- | --- | --- |
| 业务状态 | status | active / needs_user / blocked / complete / abandoned |
| 任务控制 | control.state / reason / version | active / paused / closed，以及停止原因和变更版本 |
| 会话绑定 | active_session | host、原生 session_id、attached/detached/stale |
| 宿主执行事实 | host-recovery/mains、宿主 worker observation | PID 与进程开始身份、真实 native handle、静默证明 |

明确中断或执行丢失将 control 写为 paused，绑定仍可 attached。中断来源不明时保守停止。
正常 steering、普通输入或 turn 切换不等于中断。不能用后来出现的普通消息推断中断已被撤销。

| 状态/事件 | 普通消息 | 显式 resume | edit |
| --- | --- | --- | --- |
| 正常 active | 先处理输入，再继续原任务 | 不重复创建运行中的 Main | 有效修改后按新 revision 继续 |
| paused | 可以聊天，不恢复 | 同一 session 显式恢复 | 仍保持暂停 |
| blocked | 不自动重试 | 开启一次明确重试，保留 next_action | 仍保持停止 |
| needs_user | 不用无关输入伪造问题已解决 | 同一 session 显式恢复为 active；仍由 Main 处理补充信息和 `next_action` | 按新目标重新判断所需输入 |
| complete | 普通聊天 | 拒绝重开原 revision | 有效编辑可开启新 revision |
| abandoned / clear | 普通聊天 | 拒绝 | 拒绝；须创建新任务 |
| fork / 新 session / detached / stale | 不继承执行权 | 拒绝接管 | 不借 edit 接管其他任务 |

fork 或新 session 可以用带完整新目标的精确 GP start 创建独立 Goal。其他 session 的未终态 Goal
不会被读取为本 session 的 resume/edit 目标，也不会阻止该 start；同一 session 的新 start 仍拒绝。
并行 Goal 的 Main/Search 工具分别通过显式 `goal_plus_id` 或已链接 `run_id` 校验归属。

原 session 已完成任务的历史绑定仅可摘要查询及有效 edit；这不等于 detached 未完成任务可恢复。
blocked 不是新版本的结算终态，不能沿用旧的自动解绑/最终报告行为。

旧记录加载时显式迁移：没有 control 的 complete/blocked/abandoned 保持历史 closed；
来源不明的旧 paused 绑定变为 attached + paused/legacy_pause，不自动复活。迁移不重建预算或身份。

## 事件驱动与自动续跑

原生 session_start 只恢复状态。reload/compact 或 PID 变化不触发 GP 进程身份检查，
也不自动记录 execution_lost；执行丢失由宿主事件报告。
Scope 激活取自可信 native session 和已授权 Goal/control version；
开放 scope 保留当前工作，关闭后的同一激活不可重开，新授权可以创建下一代 scope。
查询使用不可变创建顺序，不把旧日志的更新时间当作新归属。
GP 没有 guardian、进程存活监测或遗留进程接管。

每次 continuation 必须针对当前 Goal、revision、session 和 control version。
pause/clear/edit/complete 使旧排队请求失效；重复同一 invocation 不重新应用旧恢复授权。
用户输入优先。Pi 在宿主等待的 agent_end 中排入 follow-up，不能在 print 已结束后的
agent_settled 中启动无人等待的后台 turn。实际异常/abort 在 settled 阶段记录。

Plan 模式不应被自主 continuation 绕过。模式、busy、输入排队是宿主事实；不能从提示词推断。
各宿主的排队/模式实际覆盖范围必须用真实测试证明，未覆盖的路径仍记为未验证。

### 持续 Main 与 Search Reconciliation

Main 在启动、恢复，以及 worker 返回、报错、求助、需要作出搜索决策或接近预算时读取
`goal_plus_status` 的最新输入、phase、next_action、work_items 和 reconciliation；需要整体
概览时优先调用 `goal_plus_monitor_snapshot(detail=false)`。稳定探索期间把全部活跃调用放入
同一次 `goal_plus_session_wait_any`，默认等待 900 秒，任一调用返回就处理；超时且全部仍为
running 时继续 wait-any，不例行查 status/history、审查 diff、重算耗时或更新进度记录。启动和
恢复阶段可以使用短等待。Main 可先做非 Search 工作，但不能遗忘必要恢复。

| 持久状态 | 下一步 |
| --- | --- |
| 未发生显式 recovery | `continue_candidate`：用新的 call_id 调用 session run，由 host 启动或续接同一 candidate；旧执行仍持有 lease 时等待 |
| verifier / settlement 未完成 | 先检查原 attempt；确认仍在执行时等待，否则恢复，再允许 worker 执行 |
| prepared / restoring | 继续同一 recovery_id，停止并观察旧 worker，再恢复固定 checkpoint |
| restored、没有当前 generation session | 创建幂等 fresh session |
| 已分配 session、未确认绑定 | 核对实际 native task/进程，确认 launch 后绑定，不能盲目重复启动 |
| fresh worker 已绑定、没有新 Evidence | 等待/推进 fresh verifier |
| 已取得新 Evidence | 回到 Search 的 selection、promotion、结果登记与审计 |

这里的 fresh worker 和新 Evidence 均以当前 candidate execution generation 为准。
已 restored 的 recovery 保留最初恢复代际，后续 new_session 可以推进当前代际；旧 Evidence
仍可共享，但不能代替新代际 worker 的恢复验收，详见 [Search 一致性恢复](search-recovery.md#状态重试与数据保留)。

以上步骤持续投影在 Main context/Stop continuation 和 monitor 中，不只注入 resume 首轮。
完成/最终检查会拒绝仍处于恢复中、仅 restored 或尚无新 Evidence 的必要恢复事务。

```text
 explicit GP resume
        |
        v
 reload Main agenda ----- ordinary work first
        |                         |
        +-------------------------+
        v
 continue same native session and workspace
        |
        v explicit decision to discard current workspace
 prepare fixed checkpoint; fence generation N
        |
 host stop + observe quiescence
        |
 provider recover(ArtifactRef), same recovery_id
        |
 restored -> reconcile launch -> bind generation N+1
        |
 fresh Evidence -> select/promote -> final audit/report
        |
 normal end -> reload Main agenda
```

launch 与 bind 不是跨系统原子操作。未知 launch 结果必须先由宿主核对稳定身份，不得用另一个
session “试一次”。永久 checkpoint/provider/权限错误要记录真实阻塞，不能无限 recover 空转。

## Search 一致性恢复

checkpoint 是最后完整 settlement 的 artifact 和 ledger；没有 settlement 时用初始 checkpoint，
不是最高分版本或任意最新 HEAD。先 fence 旧 generation，旧 worker 不可继续结算。
宿主提供实际静默证明后，provider 才能恢复；同一 recovery_id 可跨轮幂等推进。

Git/copy 使用公共 provider 恢复语义，在受管理 candidate 内归档未完成现场，再恢复完整树和
ledger；不直接 reset，不改源工作区或其他 candidate，不删除既有 Evidence。
不假定 native transcript snapshot 与 filesystem artifact 原子配对，不自动重放外部副作用。

ThinkThread 不支持这里的 candidate checkpoint recovery。已接入的 TT controller 接管只恢复
同一 scope 的原 call/Message 观察，不恢复候选工作树或新 generation；两者不可混称。
具体工具、归档和失败恢复见 [Search 一致性恢复](search-recovery.md)。

## 预算与收尾

resume 保留 Goal ID、revision、Search/candidate 身份、累计用量、创建时刻和原 deadline。
暂停不冻结 wall clock。预算耗尽时不能靠 resume 或 fresh worker 刷新探索额度；
仅允许已有合同规定的 finalization-only 收尾。显式 edit 的预算变更仍按原创建时刻计算，
不会把编辑时刻作为新计时起点。完成证据仍需覆盖原目标、验证、必要 promotion
与独立最终检查；超时本身不伪装为 blocked，也不能把单次 worker 返回当成 Main 完成。

## 原生会话与非交互运行

仅打开会话不点火：

```text
codex resume <session-id>
pi --session <session-id>
pi --continue
pi --resume
```

恢复会话并显式恢复原 Main：

```text
codex resume <session-id> '$goal-plus resume'
codex exec resume <session-id> '$goal-plus resume'
pi -p --mode json --session <session-id> '/goal-plus resume'
```

Pi 的 --resume 是原生交互式 picker；--session / --continue / --resume 均不额外赋予 Goal 权限。
开发态使用 install.sh --codex --pi-dev 安装的 pi-goal-plus，参数相同。
自动化优先精确 session ID，不用“最近一次”猜测目标。Codex shell 参数要单引号保护美元符号。

## 共享逻辑与宿主实现

| 逻辑 | 共享实现 | Codex | Pi |
| --- | --- | --- | --- |
| 控制命令、迁移、revision、停止版本 | domain/goal_commands.py、application/goal_revisions.py、goal_control.py | UserPromptSubmit 精确解析 | command/input 统一 host-only capability |
| 原生身份和 Main 进程归属 | host_recovery.py | session ID + native PID | session manager + Pi PID |
| 中断事实 | 共享 control 转换 | Interrupt | abort signal / settled error |
| 持续 Main | 共享 gate 与 reconciliation | 正常输入点火，Stop 接续 | 显式点火，awaited agent_end follow-up |
| Search checkpoint/generation | application/candidate_recovery.py | 共同 session 与原生观察 | 共同 session 与 RPC 观察 |
| 工作区恢复 | 窄 workspace/artifact port | 与宿主无关 | 与宿主无关 |

## 验证边界

验收须区分实现与证据，不沿用旧“普通消息自动恢复”测试的结果。
共享回归覆盖控制转换、旧记录、同 session/fork、重复命令、预算保持及 Main 跨轮恢复清单；
provider 测试覆盖 prepare/restore 再次中断、旧 generation fencing 和新 Evidence。

Steering、中断后的普通聊天隔离、显式 resume、pause/clear 和宿主原生 session 恢复，
按改动涉及的实际风险选择模块测试；已有适用证据直接复用。真实宿主 ST 仍需显式授权，
不要求穷举 TUI/exec/fork 或所有排队命令组合。具体规范见
[测试指南](../tests/README.md#module-boundaries)。

Search 的选优/提升还会检查 fresh generation 的已绑定 worker Evidence，不能用 Main
自行补一次 verifier 来绕过恢复验收。原 deadline 已到时，收尾只能使用先前完整 worker
Evidence；不得为了补齐恢复步骤重启优化。
