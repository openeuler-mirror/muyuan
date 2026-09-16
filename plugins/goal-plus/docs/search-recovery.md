# Search 一致性恢复

## 功能概览

Main 按[运行逻辑](runtime-logic.md)获准继续后，默认恢复原 candidate 的 native session 和
workspace，保留未验证修改。只有明确放弃现场时才恢复 checkpoint、开启新 generation。
两者均沿用原 run、candidate、FrozenSpec、Evidence 和累计预算，不要求手动 `git reset --hard`。
本页是 Search 内部恢复，不代替用户的 `goal-plus resume` 授权。

## 何时恢复

| 当前情况 | 处理方式 |
| --- | --- |
| 原 worker 仍运行 | 原控制器继续观察；新控制器不得重复启动 |
| Worker 已返回，或超时/stop/MCP 重建后旧资源已清理 | 新 call 继续原 session 和 workspace |
| 原 native session 无法加载，或明确需要 fresh context | 显式 `new_session` 接手原 workspace |
| verifier / settlement 未完成 | 等待或 retry 原 attempt，再继续 worker |
| 明确放弃当前现场 | 显式 checkpoint 恢复 |

绑定失效、旧 revision、已结束/invalidated run 或已退休 candidate 不得借恢复重新执行。
Main 按[恢复待办](runtime-logic.md#持续-main-与-search-reconciliation)选择 lane 并跨轮推进；
恢复 session 不证明 worker 存活，完成 checkpoint restore 也不代表 worker 已重启。

## 原地续接

Main 使用 `goal_plus_session_run` 和新 `call_id` 继续。Host 复用控制器，或从持久 native handle、
scope 绑定和用量基线重建；两者均检查身份、generation、预算、pending verification 和旧资源清理。
原生进程持有独占执行 lease 到退出，只有取得 lease 才能收束遗留资源并恢复同一 session。
`released` 不证明进程退出，GP 不通过历史 PID 接管未知进程。

重建不重放旧调用。旧结果不可用时报告 `SessionCallHistoryUnavailable`，Main 读取 Evidence 后
以新 call 继续。显式 `new_session=true` 在资源清理后隔离旧 generation、建立新对话，保留
workspace，不要求原对话能加载，也不回滚 checkpoint；宿主错误不会自动触发新建对话。

## 验证事务续接

查询原 attempt：执行锁仍被持有时返回 `pending`；锁已释放但未结算时返回 `needs_recovery`。
持久化的 `running` 不证明进程存活，控制器退出也不立即释放存活 verifier 子进程持有的锁。

本地提交回执前保存不可变 artifact、归属、hypothesis、工具 staging 和 copy receipts；
完整 ScoreReport 在结算前保存。使用 `goal_plus_search_run_verifier(attempt_id=..., retry=true)`
续接：有完整结果则只补结算，否则在原快照上重跑冻结 verifier，仍受 timeout 和剩余预算约束。
完整的 Timeout/失败结果不重评；输入缺失、不一致或 workspace 历史脱离原 artifact 时拒绝并保留现场。

Retry 保留原 attempt、session、generation 和预算，只产生一条 Evidence 与 ledger。当前代码、
暂存修改和 scratch 不回滚，Git 历史连接已结算 artifact；仅 `results_tsv` binding 同步受管 ledger。
后续代码修改和工具草稿不属于原 attempt，须另行验证。

## 显式 Checkpoint 恢复

1. `goal_plus_search_recovery_status(run_id)`：读取 checkpoint、generation 和旧 session 概要，
   核对宿主状态；`detail=true` 附带完整 session_handle、host metadata 和 recovery 记录。
2. `goal_plus_search_prepare_recovery(run_id, candidate_id)`：固定 checkpoint 和 `recovery_id`，
   增加 generation、隔离旧 session，暂不修改工作区。
3. `goal_plus_session_close`：关闭旧执行；控制器丢失时由 scope cleanup 清理，取得准确归属的静默证明。
4. `goal_plus_search_recover_candidate(run_id, candidate_id, recovery_id)`：校验宿主证明，
   在 verifier/settlement 锁内恢复并核验 artifact 和 ledger。
5. `goal_plus_session_run`：启动同一 candidate 的新 session；worker 读取 `goal_plus_search_get_agent_context`。

恢复期间禁止该 candidate 启动、验证或被选择；旧 generation 的迟到结果不能结算，其他 lane 不受影响。

## 什么是完整恢复点

无完整结算时使用物化后的初始 artifact/ledger；否则使用最后一条 iteration 的
`workspace_artifact_after_settlement`，并核对 settlement ID、ledger 行数和 score report。
这是最近一次 keep/rollback 后的状态，不一定是最高分尝试；部分文件、Git commit 或 transcript
不能代替完整结算。

score report、iteration 和 canonical ledger 原子落盘，包含工具回执、消费 receipt IDs 和结算顺序。
正常执行与 retry 按稳定 ID 幂等补齐索引、best、计数器、队列、workspace/ledger 投影及清理，
不重复计分或改变同分 best 顺序。必需投影/清理失败保留本地 verification gate；异步队列失败
由队列恢复补齐，不撤销 Evidence。

工具快照可提前准备并校验复用，提交前不进索引、不占 family 容量。清理只消费原 staging；
拒绝的输入恢复到空 share-out，已有新草稿时另存为私有 rejected 输入。Checkpoint 放弃的工具输入
归档至 recovery 的 `shared-tool-input/`，旧请求清除。

## 状态、重试与数据保留

| 状态 | 含义 | 重试行为 |
| --- | --- | --- |
| `prepared` | 已固定 checkpoint、隔离旧 generation | 返回同一 recovery ID，等待宿主静默 |
| `restoring` | 恢复或投影未完成 | 按同一 checkpoint 重试 |
| `restored` | artifact、ledger、投影已核验落盘 | 返回原结果，不再次修改工作区 |

新 generation 再中断时可准备新 recovery ID；旧 session 仅保留历史归属。恢复不重置 Goal revision、
外层 deadline、预算或已结算 Evidence。Codex 收尾仅豁免已完成 recovery 明确覆盖的旧 session；
新 session 仍需终态证据，当前 scope 的活跃 agent 检查不变。

Git-backed provider 修改工作区前备份现场，重试不覆盖首次备份：

```text
.gp/runs/<run_id>/candidates/<candidate_id>/recoveries/<recovery_id>/interrupted-workspace.tar
```

备份包含 tracked、untracked、ignored 内容，不含 `.git`、不展开符号链接；空间不足时报错。
恢复仅清理该 candidate 的受管 checkout，保留 Git 历史，不改源仓库或其他 candidate。
仅 `results_tsv` binding 恢复并校验 ledger 文件；备份不会自动导回未完成修改。

新 worker 从 Evidence、历史、ledger 和 `resume.recovery` 恢复上下文，不使用旧 `.tmp/handoff.json`。
原生 transcript 与 artifact 不保证原子配对，未完成工具调用及外部副作用不重放。

## Provider 与宿主职责

Codex/Pi 共用恢复规则；Host 提供准确 worker 身份及停止事实，provider 按 `ArtifactRef` 恢复。
当前 Copy/Git worktree 支持 checkpoint 恢复；ThinkThread 的 `recover` capability 为 false，
合同测试不代表真实支持。它的原生 Child 续接与 checkpoint 恢复是两回事。

缺少完整 checkpoint、provider 能力或可信静默证明时拒绝并保留现场。证明须绑定准确
transport/scope/run/session，不能由模型传入 `quiescent=true`，也不接受旧 registry/task name。
Runtime 不监督未知进程；脱离宿主的后台程序和外部服务须另行停止，其副作用不在回滚范围内。

## 验证与排障

确定性覆盖见 [test_search_recovery.py](../tests/test_search_recovery.py)。真实宿主场景见
[session ST](../tests/st/test_st_session_host.py) 的 `host_restart` / `cold_recovery` 和
[Main recovery ST](../tests/st/test_st_search_recovery.py)；运行条件见 [Tests](../tests/README.md)。

| 错误 | 检查方向 |
| --- | --- |
| `RecoveryCheckpointUnavailable` / `RecoveryCheckpointInconsistent` | checkpoint 或结算记录缺失/不一致，不能用当前 Git HEAD 猜测 |
| `RecoveryHostObservationMissing` | 缺少可信宿主记录，不可伪造退出证明 |
| `RecoveryWorkerStillRunning` | 停止对应 worker，并通过宿主重新确认静默 |
| `StaleAgentSession` | 旧 generation 已隔离，改用恢复后的新 launch |
| `CandidateRecoveryRequired` | 完成本 candidate 已准备的恢复事务，不再启动旧 worker |
| `RecoveryArtifactMismatch` | Provider 未恢复精确 checkpoint，保留恢复状态并排查 |
