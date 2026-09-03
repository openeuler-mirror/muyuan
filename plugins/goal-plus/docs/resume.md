# 原生 Session Resume

Goal Plus 不提供 `$goal-plus resume`、`/goal-plus resume` 或 `goal-plus resume`。Goal Plus
任务做到一半时，恢复入口始终是宿主自己的 session resume；Goal Plus 只负责核对原生
session identity、持久化 Goal 状态和 lifecycle event，再决定是否继续。

创建和修改仍是两个显式 Goal Plus 操作：

```text
Codex: $goal-plus <目标>       / $goal-plus edit <完整的新目标>
Pi:    /goal-plus <目标>       / /goal-plus edit <完整的新目标>
```

原生 resume 不会创建 Goal、改变 `raw_goal` 或递增 `goal_revision`。

## 统一状态规则

两个宿主共用 runtime 中的 continuation 决策。Host adapter 只把真实 lifecycle 映射为事件：

| 场景 | 持久化状态/事件 | 结果 |
| --- | --- | --- |
| 同一 session 的普通用户消息 | `user_turn` | 用户消息优先，继续 unfinished Goal |
| 用户 abort/interrupt | `interrupt` | `active_session.state=paused`，不自动续跑 |
| 原生恢复到同一 attached session | `session_start` | 交互式可继续；Codex 等用户消息，Pi 可发起 promptless turn |
| 原生恢复到同一 paused/needs-user session | `session_start` | 交互式询问或等待用户消息；headless promptless fail closed |
| 不同 session、fork、detached/stale binding | 任意 | 不继承 Goal Plus 权限，不续跑 |
| Goal 已是 `complete`/`blocked`/`abandoned` | 任意 | 不续跑 |

所有 continuation 决策都记录为 host event。普通消息只回答/推进当前 unfinished Goal，不能
替代 `edit` 修改目标。Search candidate 的 worker-local continuation 是另一层机制，见
[Agent Host Adapters](agent-host-adapters.md)。

## Codex

交互式恢复使用 Codex 原生命令：

```bash
codex resume <session-id>
# 或恢复当前目录最近一次 session
codex resume --last
```

Codex `SessionStart` hook 只能恢复 Goal Plus context，不能凭空创建一个模型 turn。因此恢复后，
CLI 中的下一条普通用户消息既是“是否继续”的用户决定，也是本轮的权威输入；如果用户聊别的，
先处理该消息，并在同一 unfinished Goal 上继续，不注入伪造的 `$goal-plus resume`。

exec 模式没有交互式确认，所以 resume 命令必须带一个普通 prompt：

```bash
codex exec resume <session-id> "Continue the active Goal Plus task."
# 或
codex exec resume --last "Continue the active Goal Plus task."
```

该 prompt 触发 `UserPromptSubmit`，共享状态机确认 session identity 后继续。用户 interrupt 会先把
binding 标记为 paused；无 prompt 的 headless 恢复没有继续授权。

## Pi

Pi 使用原生 session 选项：

```bash
pi --session <session-id>
pi --continue
pi --resume                 # 交互式选择
```

交互式 Pi 恢复精确同一 attached session 时，可以在没有新 prompt 的情况下继续 unfinished Goal；
如果之前由用户 abort，或 Goal 正在等待用户信息，则先询问是否继续。任何普通 input 都优先作为
本轮用户消息，再继续同一 Goal。

Pi 的交互模式由原生 `ctx.mode=tui` 标识；RPC 即使暴露 UI 方法也不视为交互终端。
启动命令自带消息、附件或 stdin 时，extension 等待 Pi 提交该输入，不抢先创建自动 turn。

正常 Stop gate 在 `agent_end` 排入 follow-up，由 Pi 在 retry/compaction 后继续消化；
`agent_settled` 只负责中断落盘及终态展示，不再启动异步 turn，避免 print 进程提前退出。

自动化/EdgeBench 为首次启动分配稳定 session ID，后续恢复同一 session，并发送普通继续消息：

```bash
pi -p --mode json \
  --session-dir <dir> \
  --session <session-id> \
  "Continue the active Goal Plus task."
```

Pi print/JSON 无 prompt 时会直接结束；`session_start` 中异步触发 turn 不能替代宿主等待 turn
完成的生命周期。因此这里与 Codex exec 一样使用普通 prompt，不增加 Goal Plus 专有 resume
命令或 headless flag。EdgeBench 只对仍 attached 的 unfinished Goal 自动发送该消息；paused、
needs-user、session mismatch、状态探测失败或终态都 fail closed，harness 不应盲目重启。

## 回归场景

实现至少覆盖以下契约：

1. create/edit 是唯一 Goal Plus rebind/revision 入口，伪 resume command 被明确拒绝；
2. Codex interrupt 后原生 SessionStart 仍保持 paused，下一条普通消息才 reattach；
3. Codex exec resume 通过普通 prompt 继续，不依赖交互；
4. Pi 通过稳定 session ID 与普通 prompt headless 续跑；paused headless 不自动继续；
5. Pi/Codex 的 mismatch、fork、detached、terminal 都不继承；
6. EdgeBench 只在 status probe 明确返回 `native_continuation_ready` 时恢复稳定 Pi session。
