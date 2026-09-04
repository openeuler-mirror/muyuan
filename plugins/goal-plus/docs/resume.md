# Goal Plus 恢复（Resume）功能说明

## 功能概览

Goal Plus 支持通过宿主的原生 session 恢复入口，继续同一会话中尚未结束的任务。宿主负责恢复
对话，Goal Plus 根据持久化任务状态和会话身份，决定是否允许继续以及何时启动下一轮。

Goal Plus 不提供 `$goal-plus resume`、`/goal-plus resume` 或 `goal-plus resume` 命令。
恢复不需要重新创建目标，也不应通过 `edit` 来模拟恢复。

本功能适用于 Codex 和 Pi 的 Main Agent。两者共用任务延续规则，但交互界面与启动下一轮的
方式由宿主能力决定；它不依赖 Codex 内建 `/goal`，也不要求 Pi 提供内建 Goal 功能。
Search candidate 的一致性恢复属于下一层机制，见 [Search 一致性恢复](search-recovery.md)。
Main 恢复后先核对共享平面与宿主 worker 状态；仍健康的 worker 不回滚，丢失的 worker 可以从
最后一次完整结算的 artifact 恢复工作区，再以新 generation 继续同一 candidate。

本文按两层恢复组织：先说明[设计方案](#设计方案与总体框架)，再介绍[原生恢复入口](#使用方式)、
[状态规则](#状态与恢复规则)、[自动化](#自动化与-edgebench)和[验证范围](#验证范围)。

## 设计方案与总体框架

### 两层恢复，分别判断

第一层回答“当前 Main 是否可以继续这个 Goal”；第二层回答“这条 Search lane 应从什么完整
状态继续”。原生 resume 不等于对所有 candidate 执行回滚，Search 的 `active` 状态也不证明
worker 仍然存活。Main 获准继续并处理最新用户意图后，才判断是否需要 Search 一致性恢复。

```text
  codex resume / codex exec resume       pi --session / --continue / --resume
                   |                                      |
                   v                                      v
       +----------------------+               +----------------------+
       | Codex hooks          |               | Pi extension         |
       | identity + lifecycle |               | identity + lifecycle |
       +-----------+----------+               +-----------+----------+
                   |                                      |
                   +------------------+-------------------+
                                      v
                  +----------------------------------------+
                  | Shared Goal continuation state machine |
                  | host/session + Goal + active_session   |
                  +-------------------+--------------------+
                                      | eligible + turn input/confirmation
                                      v
                  +----------------------------------------+
                  | Main: latest user intent + next_action |
                  | inspect shared state AND host workers  |
                  +-------------------+--------------------+
                                      |
              +-----------------------+-----------------------+
              |                       |                       |
              v                       v                       v
     Healthy live worker     Complete workspace;       Lost worker / partial
     reconnect / observe     context-only restart      attempt to recover
              |                       |                       |
              +--- no rollback -------+                       v
                                            +------------------------------+
                                            | Shared Search recovery       |
                                            | checkpoint + generation      |
                                            | host quiescence gate         |
                                            | provider restore + reconcile |
                                            +--------------+---------------+
                                                           |
                                                           v
                                             Fresh worker, same candidate
```

身份不匹配、fork、新会话、失效绑定和终态任务在第一层即不获得继续资格；暂停或缺少输入时，
按[启动规则](#同一会话的启动行为)等待。图中的最后一条路径是内部 Search 工具流程，不增加
任何用户 resume 命令；未进入 Search 的 Goal 只需要第一层。

### Search 恢复事务与执行顺序

恢复依据是 `.gp/` 中完整结算的 artifact、ledger 和 Evidence，不是退出前最后一句模型回复，
也不是任意最新 Git HEAD。无完整 iteration 时使用初始 checkpoint；否则使用最后一次完整
settlement 的工作状态，不一定是最高分尝试。

```text
 Main / shared facade       Search runtime         Host adapter       Workspace provider
          |                       |                      |                    |
  inspect recovery status ------->|                      |                    |
  inspect native workers ------------------------------->|                    |
          |                       |                      |                    |
  prepare recovery -------------->|                      |                    |
          |                 fix checkpoint               |                    |
          |                 fence generation N           |                    |
          |                 persist [prepared]           |                    |
          |                       |                      |                    |
  stop/observe old execution --------------------------->|                    |
          |                       |            trusted quiescence proof       |
          |                       |                      |                    |
  recover candidate ------------------------------------>|                    |
          |<---------- approve / reject -----------------+                    |
          |  only if approved     |                      |                    |
          +---------------------->|                      |                    |
          |                 lock + [restoring]           |                    |
          |                       +------ recover(ArtifactRef) -------------->|
          |                       |<------------------ restored ArtifactRef --+
          |                 validate artifact + ledger   |                    |
          |                 rebuild settled projections  |                    |
          |                 persist [restored]           |                    |
          |                       |                      |                    |
  start agent session ----------->|                      |                    |
          |<--- fresh launch -----+                      |                    |
  launch generation N+1 -------------------------------->|                    |
          |                       |           fresh context -> next Evidence  |
```

`prepare` 先阻止旧 generation 继续结算，但不停止进程或改动工作区。宿主确认旧执行静默后，
provider 才恢复文件；宿主状态未知或旧 worker 仍在运行时拒绝恢复。模型不能自行提交
`quiescent=true`，runtime 也不承担进程轮询或 supervisor 职责。

同一 `recovery_id` 可重试：`prepared` 等待静默，`restoring` 继续同一恢复点，`restored` 返回
原结果而不重复修改工作区。新 worker 读取共享 Evidence、ledger 和结构化 `resume.recovery`；
不将旧 transcript snapshot 当成与 artifact 原子配对的可执行现场。

### Provider 无关的恢复合同

公共语义是 `WorkspaceProvider.recover(workspace, ArtifactRef, ...) -> ArtifactRef`。
Main 和共享 Search 流程只表达“恢复到这个完整 artifact”，不表达 `git reset --hard`。

| 层次/实现 | 必须承担的责任 | 当前边界 |
| --- | --- | --- |
| 共享 Goal/Search | 身份授权、checkpoint、generation、恢复事务、结算一致性 | Codex/Pi 共用，不复制业务状态机 |
| Codex host | 真实 worker 身份、原生 interrupt、prepare 后的 terminal 观测或原 owner 退出证明 | 不以 session record 猜测存活 |
| Pi host | RPC worker 进程身份、pool 停止与退出确认、新 worker 启动 | 不以恢复单条 lane 为由提前关闭其他健康 lane |
| Copy / Git worktree provider | 归档中断现场，恢复完整工作树及 ledger，返回可校验 artifact | 只操作 runtime-owned candidate；保留旧 Git 历史，不修改源仓库 |
| ThinkThread provider | 未来以 snapshot/branch 和 provider handle 实现同一合同 | 尚未接入真实恢复；`recover` capability 关闭，显式拒绝 |

恢复保留 Goal ID/revision、run/candidate ID、FrozenSpec、已结算 Evidence、预算和其他 lane；
变化的是当前 candidate 的工作状态、执行 generation、新 worker session 及其恢复审计记录。
宿主退出证明不是通用文件系统 sandbox，也不能回滚外部副作用。

完整工具参数见 [API](api.md)；checkpoint 校验、现场归档、派生投影重建、错误及 provider
边界见 [Search 一致性恢复](search-recovery.md)。ThinkThread 合同测试只证明公共接口能够
传递 `fs_snapshot` 和 provider handle，不代表真实 backend 已通过验收。

## 使用方式

### 前置条件

- 原生 session 历史、目标 workspace 和对应的 Goal Plus 持久化状态仍然可用。
- 恢复到任务当前持久化绑定的同一宿主、同一 session，而不是新会话或 fork。
- Goal Plus runtime 与宿主集成已正确安装；Codex 的 lifecycle hooks（包括 `Interrupt`）已加载并受信任。

安装和更新统一使用 `install.sh`，不要仅替换源码或单独更新某个缓存。具体步骤见
[Codex 安装](codex.md)、[Pi 安装](pi.md)和[开发安装指南](development.md)。Pi 开发态使用
`pi-goal-plus` 启动器，下文所有 `pi` 参数同样适用。

### 恢复入口

| 场景 | 原生命令 | 下一轮如何开始 |
| --- | --- | --- |
| Codex 交互式，指定会话 | `codex resume <session-id>` | 恢复后发送普通用户消息 |
| Codex 交互式，最近会话 | `codex resume --last` | 同上，需确认选中的是原任务会话 |
| Codex exec | `codex exec resume <session-id> "继续当前任务"` | 命令中的普通消息触发下一轮 |
| Pi，指定会话 | `pi --session <session-id>` | 按任务与绑定状态自动继续、询问或等待输入 |
| Pi，最近会话 | `pi --continue` | 同上，最近会话不一定是目标会话 |
| Pi，选择会话 | `pi --resume` | 在原生选择器中选中任务会话后按状态处理 |
| Pi print/JSON | `pi -p --mode json --session <session-id> "继续当前任务"` | 命令中的普通消息触发下一轮 |

自动化应优先使用明确的 session ID，避免“最近一次”选择到其他任务。原生命令选中了会话，
不等于获得该 workspace 中任意 Goal Plus 任务的权限。

### 典型流程

1. 在某个原生 session 中，通过显式 Goal Plus 命令创建任务。
2. 运行中如果需要暂停，使用宿主的 interrupt/abort 操作；Goal Plus 收到事件后记录暂停。
3. 重新进入同一原生 session。Codex 等待普通消息；Pi 按下述规则处理自动继续或确认。
4. 发送“继续当前任务”等普通消息，或在 Pi 的继续确认中选择继续。
5. Main Agent 根据已保存的 Goal、当前阶段和下一步工作继续，不创建第二个目标。

恢复操作本身不改变 `goal_plus_id`、`raw_goal`、`goal_revision`，也不新增 create/edit 命令记录。
已有 Search 关联、冻结规格和已落盘结果不会因恢复而重置。恢复可以更新绑定状态、时间戳和
审计事件；恢复后的实际工作仍按正常规则写入新进展。

## 状态与恢复规则

任务状态与会话绑定状态是两层信息，不能混为一谈：

| 层次 | 状态 | 含义 |
| --- | --- | --- |
| Goal | `active` | 任务尚未结束，可继续推进 |
| Goal | `needs_user` | 任务需要用户信息或决定，不能仅靠无输入重启继续 |
| Goal | `complete` / `blocked` / `abandoned` | 任务已结束，原生 resume 不重新打开任务 |
| `active_session` | `attached` | 当前宿主/session 具有任务执行绑定 |
| `active_session` | `paused` | 绑定保留，但已收到用户中断，不能自行续跑 |
| `active_session` | `detached` / `stale` | 绑定已脱离或失效，普通消息和原生 resume 不重新激活 |

`attached` 表示身份绑定，不表示进程仍然存活。`paused` 不是 Goal 终态；它保留原任务和进度。
`stale` 也不是“运行时间较长”的同义词，不应仅凭耗时推断绑定失效。

### 同一会话的启动行为

以下均以 host 和 session ID 匹配、持久化状态可读取为前提。终态或失效绑定优先拒绝；
其余行只适用于未结束任务和 `attached` / `paused` 绑定。

| 恢复时的状态/输入 | Codex 交互式 | Pi 交互式 | Codex exec / Pi 非交互模式 |
| --- | --- | --- | --- |
| `active` + `attached`，无新输入 | 恢复 context，等待用户消息 | 可自动启动继续轮次 | 不由 Goal Plus 主动创建无输入轮次 |
| `paused`，无新输入 | 保持暂停，等待用户消息 | 询问是否继续 | 不自动继续 |
| `needs_user`，无新输入 | 等待用户消息 | 询问是否继续 | 不自动继续 |
| 未结束任务，有普通用户消息 | 优先处理该消息，再决定后续工作 | 同左 | 同左；消息由调用方提供 |
| 任务终态，或绑定为 `detached` / `stale` | 不恢复 Goal Plus 执行 | 同左 | 同左 |

Codex 的 `SessionStart` hook 只恢复 context，不直接创建模型 turn；Goal Plus 没有另外实现一套
Codex TUI 的“是否继续”弹窗。下一条普通消息通过 `UserPromptSubmit` 进入共享恢复规则。

Pi 的交互模式以原生 TUI 为准，RPC 即使暴露 UI 方法也不视为交互终端。需要确认时，Pi 显示
“Continue Goal Plus?”：选择继续才启动轮次；拒绝不启动，也不会把任务自动标记为放弃或完成。
如果 Goal 是 `needs_user`，确认只允许开始处理下一步，不代表缺少的信息已经补齐。

Pi 启动时带有消息、附件或 stdin，或已有待处理用户消息时，先交给宿主处理输入，不抢先插入
自动继续轮次。extension reload 也不会自行启动轮次。Pi print/JSON 的无 prompt 启动不能代替
有输入的恢复调用。

### 用户消息与修改目标

普通消息是本轮的权威输入，状态机只核对继续资格，不按“继续”等关键词判断用户意图：

- 同一有效会话中的普通消息可以把 `paused` 重新激活为 `attached`，但不修改目标文本或 revision。
- 询问进度、补充信息、讨论其他话题时，Main Agent 先处理最新消息；无关讨论不得当作目标修改，
  也不会仅因话题变化自动解绑任务。
- 消息是否要求继续原工作、是否存在歧义，由 Main Agent 判断。意图不清楚时应先澄清；
  不能把“存在 active Goal”当作忽略最新消息的理由。
- 真正改变工作范围、交付物或成功标准时，必须使用显式 `edit`。

```text
Codex 创建：$goal-plus <目标>
Codex 修改：$goal-plus edit <完整的新目标>
Pi 创建：  /goal-plus <目标>
Pi 修改：  /goal-plus edit <完整的新目标>
```

普通聊天不等于暂停命令，也不保证禁止后续 Stop gate 继续原任务。需要明确打断当前运行时，
使用宿主 interrupt/abort；恢复入口负责延续身份，不能代替对用户意图的处理。

### 中断、异常退出与上下文压缩

宿主报告用户 interrupt/abort 后，Goal Plus 将有效绑定标记为 `paused`，记录中断原因，并停止
自动续跑。仅重新打开会话不会取消这个暂停；普通用户消息或 Pi 的肯定确认可以重新激活绑定。

异常退出与用户中断不同：断电、强制杀进程等可能来不及产生 lifecycle event，落盘绑定仍可能是
`attached`。下次启动按实际持久状态处理，不能把“进程消失”推断为“用户明确要求暂停”，也不能
保证强制退出一定落盘为 `paused`。

同一 session 的上下文压缩、普通 follow-up 或暂时的 provider error 不构成换会话或目标修改。
Goal Plus 不应因此主动脱离绑定；provider error 本身也不授权跨 session 恢复。宿主内部重试、
Stop gate 续轮和退出后恢复是不同事件，不需要任何 Goal Plus 专用 resume 命令。

## 会话隔离与功能边界

### Fork 不继承执行权

`codex fork <session-id>` 从原会话历史创建新会话；新会话有不同的 session ID，并保留
`forked_from_id` 来源信息。它不是恢复原会话。

Goal Plus 按实际 host/session ID 校验绑定，不以复制的历史、Goal ID 或来源信息授予执行权。
因此 fork 不能自动继续源任务，即使在 fork 中发送普通“继续”消息，也不能重新激活源绑定。
Pi 的新 session/fork 同样不因复制了历史或 extension 状态就获得原任务权限。

Fork 本身不改变源任务的状态、revision 或绑定，也不负责暂停、结束源任务。要继续源任务，应
返回源 session。这里隔离的是 Goal Plus 的执行权限，并非通用文件系统写权限：如果两个会话
使用同一工作目录，普通宿主工具仍可能操作同一批文件。

### 不属于恢复的操作

- 不跨 host/session 自动接管任务，不因 workspace 相同而继承。
- 不通过普通消息恢复 `detached`、`stale` 或终态任务。
- 不新建任务、修改目标或重置时间预算；显式 `edit` 所涉及的修改及重绑定是另一项操作合同。
- 不保证恢复尚未落盘的内容，也不保证恢复退出前的进程、worker handle 或工具调用现场。
- 不因恢复就跳过 verifier、promotion 或必需的 final check。

终态记录仍可用于查询和报告；“不恢复执行”不等于禁止所有只读查询或合法的终态报告操作。

## 自动化与 EdgeBench

非交互调用必须提供普通用户消息，不依赖 UI 确认。例如：

```bash
codex exec resume <session-id> "Continue the active Goal Plus task."

pi -p --mode json \
  --session-dir <session-dir> \
  --session <session-id> \
  "Continue the active Goal Plus task."
```

Pi 自动化应在首次启动时分配并保存稳定 session ID，保留 session 目录、任务 workspace 和对应
runtime 状态，后续使用该 ID 恢复。无需 `/goal-plus resume` 或 Goal Plus 专用 headless flag。

必须区分两种授权：

| 发起者 | 恢复规则 |
| --- | --- |
| 用户显式发起 headless 调用，并提供普通消息 | 同一有效 session 可重新激活 `paused`；`needs_user` 的输入仍需由 Main Agent 处理 |
| EdgeBench 自动重启控制器 | 只在状态探测明确返回 `native_continuation_ready=true` 时发送继续消息 |

EdgeBench 的自动恢复只针对仍可继续的 `attached` 任务。`paused`、`needs_user`、身份不匹配、
`detached`、`stale`、终态或状态探测失败时，不应自动重启。否则控制器生成的普通 prompt 会被
当作新的用户输入，可能无意间取消用户暂停。

`native_continuation_ready` 是 EdgeBench harness 的状态探测结果，不是新的 Goal Plus 用户命令。
它不能替代 runtime 的身份校验，也不证明所有 Search worker 都已恢复。Main 恢复后仍应根据
持久记录和真实 host 状态决定下一项工作。

## 运行机制与职责

共享 runtime 的 `handle_host_session_event` 统一处理 `session_start`、`interrupt`、`user_turn`
和 `continue_confirmed`。先检查任务是否终态，再检查身份和绑定有效性，最后处理暂停、确认或
用户输入；宿主不能仅凭本地缓存绕过这些规则。

| 层次 | Codex | Pi |
| --- | --- | --- |
| 原生身份与历史 | Codex session ID 与原生历史 | Pi session manager 与原生 session entries |
| 启动/用户输入/中断 | `SessionStart`、`UserPromptSubmit`、`Interrupt` hooks | `session_start`、`input`、实际 abort signal 与 settled event |
| 恢复时的交互与发轮 | hook 注入 context，等待用户 turn | extension 按模式处理确认、待处理输入和继续轮次 |
| 正常任务续轮 | `Stop` hook 执行 gate | `agent_end` 在宿主等待中的生命周期内排入 follow-up |
| 任务身份、暂停和恢复决策 | 共享 Goal Plus runtime | 同一共享 Goal Plus runtime |

Pi 的 `agent_settled` 负责中断落盘和终态展示，不在 print 生命周期结束后另起不受等待的异步
turn。Main Agent 始终负责理解用户意图、判断下一步和审查结果，runtime 不承担语义分类。

进入共享状态机的决策记录为 `session_continuation_decided`；暂停与重新激活另有
`session_paused`、`session_reactivated` 事件。没有匹配任务的宿主入口可能提前返回，不保证
每次 session 启动都会产生这些事件。状态与日志排查见 [Runtime 调试](debugging-runtime.md)。

## 验证范围

以下测试共同定义恢复合同；单元测试与真实宿主测试证明的范围不同，不互相替代。

| 验证层次 | 覆盖内容 | 测试入口 |
| --- | --- | --- |
| 共享状态机 | Codex/Pi 同输入同决策；暂停、确认、终态、mismatch、detached/stale；恢复不修改目标身份与 revision | [test_goal_plus.py](../tests/test_goal_plus.py) |
| Codex 入口 | 中断与普通消息恢复、compact、不同 session、工具权限与绑定保护 | [test_codex_goal_plus_hooks.py](../tests/test_codex_goal_plus_hooks.py) |
| Pi 集成资产 | lifecycle 接线、输入优先与交互/非交互路径约束 | [test_pi_assets.py](../tests/test_pi_assets.py) |
| 真实 Codex | interrupt 后 exec resume；交互式 resume；原生 fork 生成新身份且源任务不变 | [test_st_codex_native_resume.py](../tests/st/test_st_codex_native_resume.py) |
| 真实 Pi | `--session`、字面 `--continue`、`--resume` 选择器；进程丢失/abort；JSON 普通消息、TUI 无输入与启动消息 | [test_goal_plus_pi.py](../tests/st_pi/test_goal_plus_pi.py) |
| Search 一致性恢复 | 原生 Main 恢复后保留原 run/candidate 与旧 Evidence，重建 worker 并继续验证 | [test_st_search_recovery.py](../tests/st/test_st_search_recovery.py) |

真实 Codex fork 测试验证新身份及源记录不变；拒绝未授权 Goal Plus 操作由状态机和入口测试
覆盖，不能把前者理解成真实 fork 已尝试所有越权工具。Pi 暂停后的确认决策和确认接线有测试，
不据此声称真实 TUI 的每个肯定/拒绝分支都已端到端验证。

测试命令、环境要求和分层说明见 [Tests](../tests/README.md)。真实宿主测试需显式启用；默认
测试通过不代表在当前机器或任意宿主版本上重新验证过全部交互路径。

## 常见问题

| 现象 | 说明与检查方向 |
| --- | --- |
| 提示发送 `goal-plus resume` | 这不是当前功能入口；检查旧版本、旧 prompt 或实际加载的 runtime/plugin 来源，使用原生恢复 |
| Codex resume 后没有自动开工 | `SessionStart` 不发起 turn；在原 session 发送普通消息 |
| 用户中断后仍显示 `attached` | 检查宿主是否实际交付 interrupt、hook 是否受信任、是否加载了旧 runtime；强制退出也可能未落盘暂停 |
| 相同目录的新会话不能继续任务 | 目录相同不代表 session 身份相同；恢复原 session，不要手工改绑定 |
| Pi print/JSON 恢复后直接结束 | 确认携带普通 prompt，而非依赖无输入启动自动发轮 |
| 恢复后提示需要用户信息 | `needs_user` 不会被重启自动解决；提供所缺信息或决定 |
| 自动化没有重启 | 查看 readiness 和阻止原因；暂停、终态、身份不符或探测失败时停止是预期保护 |

Codex 可在目标 workspace 执行 `goal-plus-codex-doctor --project-root .` 检查 runtime 来源和
hook 配置。需要更新时使用 `install.sh` 刷新对应集成；不要通过手工改写 `.gp/`、Goal ID 或
session ID 绕过恢复规则。
