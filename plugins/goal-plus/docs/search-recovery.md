# Search 一致性恢复

## 功能概览

Main 按[运行逻辑](runtime-logic.md)恢复会话并获准继续原任务后，可以将中断的 Search
candidate 恢复到最后一次完整结算的位置，再启动新的 worker 执行代际（generation）。
恢复沿用原 `run_id`、`candidate_id`、FrozenSpec、已结算 Evidence 和预算，不创建另一轮搜索。

这是内部 Search 操作，不是用户命令；与主任务 `goal-plus resume` 不是同一层。
Search 恢复不要求 Main 或 worker 执行 `git reset --hard`。
恢复点采用 Goal Plus 公共 `ArtifactRef` 语义，具体工作区恢复由
workspace provider 实现。

## 何时恢复

| 当前情况 | 处理方式 |
| --- | --- |
| Main 恢复后，原 worker 仍健康运行 | 重连并观察，不回滚其工作区 |
| 原 worker 已完成，工作区和上下文完整 | 正常 native continuation；仅需 fresh context 时使用 redispatch |
| 本地执行超时或 stop，宿主仍保有调用记录且旧资源已确认清理 | 新 call 直接 run，内部恢复原生 session；不回滚工作区 |
| Worker 丢失、半次修改残留或结算中途退出 | 准备一致性恢复、确认旧执行停止、恢复完整结算点、启动新 generation |
| Goal 绑定失效、session/fork 不匹配、旧 revision 的 run | 不授予恢复执行权 |
| Run 已结束或 invalidated，candidate 已退休 | 拒绝恢复，不通过重启重新打开 |
| 缺少完整 checkpoint、provider 不支持、worker 存活情况未知 | 明确报错并保留现场，不猜测安全性 |

原生 session 恢复只是获得继续原任务的身份，不证明 Search worker 存活，也不自动触发所有
candidate 的工作区恢复。Main 根据最新用户消息、共享平面和宿主事实选择需要恢复的 lane。

[持续 Main 循环](runtime-logic.md#持续-main-与-search-reconciliation)会跨轮重读
这些状态：一次 resume 负责重新启动 Main，Main 再反复推进必要的内部恢复步骤。
恢复事务完成但新 worker 尚未启动，仍属于 Main 的待办；当前先做非 Search 工作时可以先做，
之后回到关联 Search 再继续同一事务。仅完成 restore 不代表 worker 已重启或原任务已完成。

## 恢复流程

1. Main 读取 `goal_plus_search_recovery_status(run_id)`，取得每条 lane 的 checkpoint、generation、
   恢复事务及旧 host session；核对宿主的实际 worker 状态。
2. 对需要恢复的 candidate 调用 `goal_plus_search_prepare_recovery(run_id, candidate_id)`。
   Runtime 固定 checkpoint、持久化 `recovery_id`、增加 generation，隔离旧 session；不修改工作区。
3. 通过共同 `goal_plus_session_close` 关闭旧执行；控制器已丢失时由 scope cleanup 清理。
   Native session 的恢复批准核验绑定 scope 中该 session/run 的准确登记已 finished/released。
   GP 不探测 PID；released 只表示释放登记，遗留进程由 agent/宿主处理。
4. 调用 `goal_plus_search_recover_candidate(run_id, candidate_id, recovery_id)`。Host adapter 核验
   旧 session 关闭或释放后，runtime 在 verifier/settlement 锁保护下调用 provider 恢复，再校验 artifact 和 ledger。
5. 恢复成功后，`goal_plus_session_run` 准备并执行同一 candidate 的新 session。
   Worker 首先读取 `goal_plus_search_get_agent_context`，再继续探索。

准备之后、恢复完成之前，不允许该 candidate 启动 session、验证或被选择。旧 session 的
迟到调用不能结算；Codex/Pi 的 worker 工具入口也会拒绝旧 generation。关闭旧 session 不会
自动续跑，也不撤销其他健康 lane 的执行许可。

## 什么是完整恢复点

恢复点由 artifact、iteration、settlement ID 和 ledger 行数共同标识：

- 从未完成 verifier 结算：使用 materialization 后的初始 artifact 与初始 ledger。
- 已有完整结算：使用最后一条持久化 iteration 的 `workspace_artifact_after_settlement`。
  它必须与 candidate 的 ledger artifact 和 score report 一致。
- Verifier 运行过、甚至生成 Git commit，但完整结算记录尚未落盘：仍视为未完成尝试。
  恢复不从 transcript、部分文件或退出码制造 Evidence。

最后一次完整结算不一定是最高分尝试；它是 verifier 正常 keep/rollback 之后的确定工作状态。
完整的 score report、iteration 和 ledger 先作为结算记录落盘，best 等派生投影随后更新。
恢复可以从已提交记录补齐这些投影，不重新运行 verifier、不重复计分。

若共享工具索引已写入，但所属 iteration 尚未完整结算，恢复会将这些孤立索引记录归档为
`unsettled-shared-tools.json`，释放未结算的 family head/容量；不可变工具快照仍保留在磁盘。
已结算工具、其他 candidate 的发布和 Evidence 不回退。

## 状态、重试与数据保留

| 状态 | 含义 | 重试行为 |
| --- | --- | --- |
| `prepared` | checkpoint 已固定，旧 generation 已隔离 | 返回同一个 recovery ID；等待宿主静默 |
| `restoring` | provider 恢复或派生投影重建尚未确认完成 | 按同一 checkpoint 重试 |
| `restored` | artifact、ledger、派生投影均已检查并落盘 | 相同 recovery ID 返回原结果，不再次修改工作区 |

新 generation 启动后如果又中断，可以准备新的 recovery ID。旧 session 保留为历史 provenance，
不能重新绑定为当前执行。恢复不重置 Goal revision、外层 deadline、累计预算或其他 lane 的结果。

Codex 收尾静默检查认可已完成 recovery 明确覆盖的旧 generation session，因为 restore
已要求其可信静默证明；不要求 Main 再给这些 fenced session 补写 terminal metadata。
仅 prepared/restoring、仅 generation 过期或不在该事务 fenced 列表中均不足以放行；新 session
仍需自己的宿主终态证据，当前 scope 的活跃 native agent 检查也保持。

当前 Git-backed provider 在修改工作区前，将中断现场保存至：

```text
.gp/runs/<run_id>/candidates/<candidate_id>/recoveries/<recovery_id>/interrupted-workspace.tar
```

备份包含受管理 checkout 的 tracked、untracked 和 ignored 内容，不包含 `.git`；符号链接不
展开。重试不会覆盖首次备份。恢复只清理该 candidate 的 runtime-owned checkout，并恢复
checkpoint 内容和已结算 ledger；仅冻结 binding 要求 `results.tsv` 时恢复并校验该文件，
common candidate 的 ledger 保留在领域状态中，不新增工作区投影。
源仓库和其他 candidate 不变，已有 Git 提交历史仍然保留。
备份可能较大，磁盘空间不足时操作报错；它不是自动清理或自动导回未完成修改的机制。

恢复后的 worker 使用 fresh context。共享平面的 Evidence、candidate-local 历史、ledger 和
结构化 `resume.recovery` 是依据；中断现场的 `.tmp/handoff.json` 不再作为当前交接输入。
本功能不宣称原生模型 transcript snapshot 与文件系统 artifact 是原子配对的，也不重放
尚未完成的工具调用或外部副作用。

## Provider 与宿主职责

| 层次 | 公共合同 | 当前实现 |
| --- | --- | --- |
| Search runtime | 固定恢复点、隔离代际、检查完整性、幂等结算 | Codex/Pi 共用 |
| Host adapter | 提供真实 worker 身份和停止事实 | 绑定 native session/run 的准确 scope 进程资源 |
| Workspace provider | `recover(workspace, ArtifactRef, ...) -> ArtifactRef` | Copy/Git worktree 恢复同一逻辑 workspace 的完整树 |
| ThinkThread provider | 原生 snapshot/branch 恢复，须遵守同一完整性合同 | 尚未支持；`recover` capability 为 false，显式拒绝 |

Runtime 不成为进程 supervisor。模型没有 `quiescent=true` 开关：恢复依赖资源 owner
确认准确进程资源已退出。缺少 transport/scope/run/session 绑定或仍有活跃资源时拒绝恢复。
旧 Codex registry、task name 和 terminal metadata 不再提供 candidate 静默证明。
这些检查覆盖宿主管理的 worker 生命周期，不是通用文件系统隔离；自行脱离宿主的后台程序、
外部服务或其他用户进程必须另外停止，不能把它们的外部副作用当作可回滚内容。

ThinkThread 合同测试仅验证 `fs_snapshot` 和 provider handle 可穿过公共恢复路径，不代表
已接入真实 ThinkThread。未来 workspace_provider 需提供同一逻辑 workspace 的恢复、artifact 比较和
静默执行边界，才能开启 capability；不会让 Main 写 Git 专属恢复脚本。

## 验证与排障

确定性测试见 [test_search_recovery.py](../tests/test_search_recovery.py)，覆盖初始/已结算
checkpoint、结算中途退出、恢复重试、旧代际、进程观测、源仓库/其他 lane 保护及 provider 合同。
共同 controller 重建场景见 [test_st_session_host.py](../tests/st/test_st_session_host.py) 的
`cold_recovery`：保留 Evidence，恢复同一 candidate 并启动新 generation。
Main 场景见 [test_st_search_recovery.py](../tests/st/test_st_search_recovery.py)：Main 原生
session 中断后恢复同一 run/candidate，从完整结算点启动新 worker 并产生下一条 Evidence。
测试命令和前置条件见 [Tests](../tests/README.md)。

| 错误 | 检查方向 |
| --- | --- |
| `RecoveryCheckpointUnavailable` / `RecoveryCheckpointInconsistent` | checkpoint 或结算记录缺失/不一致；不能用最新 Git HEAD 猜测 |
| `RecoveryHostObservationMissing` | 旧 worker 没有可信宿主记录；升级前的运行也可能出现，不可伪造退出证明 |
| `RecoveryWorkerStillRunning` | 停止对应宿主 worker；Codex 在 prepare 后重新观察 registry |
| `StaleAgentSession` | 旧 generation 已隔离，改用恢复后的新 launch |
| `CandidateRecoveryRequired` | 完成本 candidate 已准备的恢复事务，不再启动旧 worker |
| `RecoveryArtifactMismatch` | Provider 未恢复精确 checkpoint，保持恢复中状态并排查 |

EdgeBench 的 Main readiness 与原生恢复规则保持不变；控制器不能借内部 Search 恢复取消用户
暂停。Main 获准继续后，仍需按本页流程核实并恢复 Search。
