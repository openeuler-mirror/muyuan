# ThinkThread 与 Git Workspace 架构

Goal Plus 的 Workspace/Sandbox 框架是 Search Mode 的执行底座。它从确定的
source baseline 创建彼此独立的 candidate 执行空间，把每次参与决策的尝试固化成
immutable artifact，并让 verifier、Annotation 和最终发布引用同一份精确状态。

正式架构有两个正交维度：outer provider 是 `git_worktree` 或 `thinkthread`，inner agent 是
AutoResearch-Git 或 `common`。兼容配置 `inner_agent=autoresearch` 解析为前者。Git outer 以
candidate commit 表示 Evidence；ThinkThread outer 以 provider snapshot 表示 Evidence，并在
选择 AutoResearch-Git 时于 private Child 内建立独立 Git repository。`common` 不维护 inner-agent
Git history 或 `results.tsv`。`copy` 仍是显式的 artifact-only 兼容路径。

```text
MainAgent
  |  SearchSpec(source_path, edit surface, verifier, backend)
  v
freeze -----------------------------------------------------------+
  |                                                               |
  | Git outer                                                     | ThinkThread outer
  | origin verifier                                               | exact Profile/ABI preflight
  | pin clean Git HEAD                                            | capture Root-owned S0
  |                                                               | verify S0 with fs.run(discard)
  +---------------- source ArtifactRef ----------------------------+
                                  |
                                  | new
                                  v
+-------------------- Goal Plus Search framework ---------------------+
| Git: private candidate worktrees                                   |
| TT:  independent Pi Child/Session/private branch from S0            |
| Inner AutoResearch-Git: attempt/restore commits + physical results.tsv |
| Inner common: no inner-agent Git ledger mechanics                   |
|                                  |                                  |
| search                           v                                  |
|                         writable candidate                          |
|                                  | quiesce/checkpoint                |
|                                  v                                  |
|                   ArtifactRef {git_commit | fs_snapshot}            |
|                                  | exact verifier                    |
|                                  v                                  |
|                    settled verifier-backed Evidence                 |
|                                  |                                  |
|                         retained Annotation Child                   |
|                       target/peer Artifact Views                    |
|                                  |                                  |
| apply                            v                                  |
|        selected artifact -> promotion verify -> filtered publish   |
|                                  |                                  |
|        Git: squash commit        | TT: optimistic fs.apply(S0, P)   |
+----------------------------------|----------------------------------+
                                   v
                            source / live Root
```

---

## 设计理念

### 固定生命周期，可替换后端

Search 保持同一个 `new -> search -> apply` 控制语义：建立 baseline、创建 candidate、
checkpoint、验证、结算、选择和发布。Git candidate 的文件操作由 `WorkspaceProvider`
实现。ThinkThread candidate 的 Child、Session、message、snapshot checkpoint 和 continuation
由 Pi host pool 实现；runtime 仍拥有 spec、Evidence、selection、publication 和 `.gp/`
状态。两条路径不能互相 fallback。

`AgentSessionRecord` 只是 context/provenance handle。Git worktree、Pi RPC 进程或 ThinkThread
Child 的实际生命周期始终属于对应 host，不进入 Search runtime 的 supervisor 边界。

### 可变工作区，不可变决策

worker 只修改自己的 current workspace 或 private branch。Git verifier 启动前，runtime
提交 candidate change 并固定 commit。ThinkThread verifier 启动前，controller 必须确认
Child Agent 已 ready、没有 pending wake，再用 `fs.branch.snapshot` 固定 private branch。

verifier result 必须绑定该 immutable artifact。ThinkThread process verifier 直接对同一个
snapshot 执行 `fs.run(writes="discard")`；回复文本、live branch 或自然语言完成声明都不能
替代 snapshot Evidence。后续 Evidence、Annotation、selection 和 promotion 继续引用相同
artifact，因此 worker 的后续修改不会改变既有分数。

### 外层隔离与内层行为

Git 路径使用 run-local repository、private worktree 和 candidate branch。ThinkThread 路径由 Root
捕获 baseline snapshot `S0`，再为每个 initial candidate 派生独立 Pi Child、native Session 和
private filesystem branch。它们只决定 outer isolation 和 public artifact identity。

AutoResearch-Git 决定 candidate-local 尝试历史：Git outer 复用 worktree repository；TT outer 在
Child 中初始化独立 repository。trusted adapter 在 verifier request 前提交 attempt，在 verifier
结果确定后保留或恢复产品 tree、追加并提交物理 `results.tsv`。TT 的 inner commit 永远不能替代
outer branch snapshot；continue 仍唤醒同一个 retained Child/Session/branch。`common` 只依赖
outer checkpoint 与 canonical `.gp` ledger，不创建这套 inner history。

### 构造发布产物，而非回写目录

选中 candidate 并不授权把 live workspace 覆盖到 source。runtime 从 selected artifact
重新执行 promotion verifier，只保留 allowed product paths，并构造 publication artifact。

Git 以 squash publication commit 写回 clean source。ThinkThread 从 `S0` 和 selected
snapshot 构造 filtered publication snapshot `P`，再使用稳定 RequestId 执行乐观
`fs.apply(base=S0, target=P)`。`fs.apply` 只采纳 publication delta，不是整树替换。

---

## 编程模型

### Freeze 与 New：验证 source 并固定身份

`source_path`、verifier 命令、编辑范围和 workspace backend 由 MainAgent 写入 SearchSpec。
runtime 不从目录结构猜测 candidate root，也不会在 backend 不匹配或 preflight 失败时降级。

Git + Git 在 freeze 时要求 clean Git `HEAD`，hash-pin verifier artifact，并在 origin
执行一次完整 process-verifier suite。`search_create` 再次确认 source commit 未改变，随后
只建立 `c001`，在真实 worktree 运行 relocation verifier。通过后才创建其余 candidate；
失败会 abort run，并保留诊断 Evidence。

ThinkThread 在 freeze 时校验 `source_path` 可表示为当前 Root 的逻辑 `source_subdir`，并固定
SearchSpec 与 verifier artifact。`search_create` 校验受支持的 Runtime release、Agent Control
protocol、Agent POSIX contract fingerprint、Root direct filesystem 和 delegated Child
capability，然后在短暂写入边界捕获 Root-owned `S0`。origin verifier 对 `S0` 执行 exact
snapshot `fs.run(writes="discard")`；失败会 abort run，且不会启动 candidate。

ThinkThread Child 只接收逻辑 `source_subdir`，不会获得 Root 的本地绝对路径。每个 candidate
从同一个 `S0` 创建，但拥有不同的 ThinkThread ID、Pi Session 和 private branch。Pi 支持
ThinkThread Search；Codex 的 ThinkThread worker host 仍保持关闭。

### Search：checkpoint、验证与结算

Git worker iteration 的事务为：

1. 校验 runtime ledger 和 allowed edit surface。
2. 将 candidate-controlled changes 提交为 attempt commit。
3. 对精确 commit 运行 verifier。
4. 将结果结算为 `keep`、`retain`、`discard` 或 `failure`。
5. 从 settled state 继续，或恢复 candidate-local best artifact。
6. 向 authoritative `.gp` ledger 追加 record；仅当 inner binding 声明
   `workspace_ledger_projection=results_tsv` 时同步该兼容文件。

ThinkThread worker iteration 的事务为：

1. Child 调用 `search_run_verifier`；AutoResearch-Git binding 先由 trusted extension 提交 private
   attempt，`common` 不执行 inner Git 操作。
2. 等待 Child Agent ready，并确认 accepted request、dispatch 和 registration 身份。
3. 用 `fs.branch.snapshot` 固定 current private branch，比较 `S0` 与该 attempt snapshot，并校验允许
   的 edit surface。
4. 对同一个 attempt snapshot 执行 exact process verifier，verifier 写入使用 discard 语义。
5. 计算 `keep`、`retain`、`discard` 或 `failure`。AutoResearch-Git 在 trusted
   `fs.run(writes="snapshot")` 中恢复 best product tree（如需要）、追加一行 `results.tsv`、提交
   ledger，并把 Child branch reset 到 post-settlement snapshot；`common` 直接使用 outer snapshot
   settlement。
6. 持久化 public `ArtifactRef(kind="fs_snapshot", provider="thinkthread")`、verifier result 和
   disposition，再发布 `candidate_ready`。需要继续时唤醒同一个 Child/Session/branch；全局停止条件
   成立后关闭 pool。

只有已结算的 worker iteration 才能成为 Evidence。stale dispatch acknowledgement、未知 nonce、
未 ready Child、live branch 或不匹配的 snapshot 都不能发布 candidate completion。

### Annotation：完整 Artifact View

每个新 settled Evidence 幂等进入 durable Annotation queue。ThinkThread 使用一个 run-scoped、
retained Annotation Child，而不是让 candidate 打开 sibling branch。该 Child 只激活 annotation
RPC 和授权的 `list`、`stat`、`pread`、`diff` Artifact View 工具；candidate tools 与
annotation tools 双向隔离。

target 与 comparison basis 中的 peer view 都绑定 immutable snapshot。bounded diff 只是索引和
fallback；超过字节上限时，内联 `actual_diff`、`candidate_diff` 为空并保留 omitted reason，
annotator 必须读取完整 Artifact View。Annotation 不运行命令、代码、测试或 verifier，也不为读取
派生、patch 或删除 snapshot；private paths 保留在原 snapshot 中，由 Artifact View 读取授权拒绝。

`search_select` 先关闭 queue 并推进 generation，再 interrupt retained Child。已经 claim 的旧
task 即使迟到提交，也不能越过 generation fence 发布 Annotation Evidence。

### Apply：乐观发布与恢复

promotion 从 selected snapshot 开始，而不是从 candidate live branch 开始。runtime 重新运行
promotion verifier，比较 `S0 -> selected`，过滤 `.git`、`.gp`、`.search`、`.tmp`、
`results.tsv` 等 private state，并使用 typed snapshot mutations 生成 publication snapshot `P`。

`workspace.dependencies[].path` 相对于 `source_path`；provider 在 publication/apply 时加上
Root 内的 `source_subdir`。调用方不能预先传 Root-relative path，否则会重复添加前缀。

乐观 apply 有三类结果：

- `applied`：Root 上 publication delta 之外的并发修改保留，publication 原子应用。
- `awaiting_main_integration`：changed path 或声明 dependency 已变化，Root 不被覆盖；保存
  base/publication snapshot pair 和 durable pending artifact。
- `recovering`：响应丢失或 provider 返回 `needs_recovery`，不发送成功或普通失败通知，也不
  生成 pending artifact。重启后继续查询同一 RequestId，不重复 apply delta。

ThinkThread 不提供自动 rebase 或三方合并。冲突后的人工整合或重新搜索必须重新建立可验证的
artifact；不得使用 `fs.replace` 覆盖 live Root。

---

## Outer Provider 对照

| 行为 | Git outer | ThinkThread outer |
| --- | --- | --- |
| baseline | clean Git commit | Root-owned `S0` snapshot |
| candidate isolation | private worktree/branch | private Child/Session/branch |
| attempt identity | Git commit | branch `fs_snapshot` |
| verifier input | exact clean commit | exact snapshot `fs.run(discard)` |
| continuation | same native worker/worktree | wake same Child/Session/branch |
| peer reading | detached Git Artifact View | retained Annotation Child tools |
| publication | filtered squash commit | filtered publication snapshot `P` |
| source apply | fetch + cherry-pick | optimistic `fs.apply(S0, P)` |
| conflict state | source apply rejects | `awaiting_main_integration` |
| uncertain result | ordinary operation failure | same RequestId recovery fence |
| execution boundary | path isolation, not sandbox | provider-enforced private Child |
| supported host | Codex and Pi RPC | installed Pi ThinkThread Profile |

## Inner Agent 对照

| 行为 | AutoResearch-Git (`inner_agent=autoresearch`) | `common` |
| --- | --- | --- |
| loop | 自主多轮；TT 每个 wake 一个 accepted attempt | 每个 dispatch 一个有界 attempt |
| private attempt history | Git commits；TT repository 位于 Child 内 | none |
| workspace ledger | physical `results.tsv` + ledger commits | none |
| canonical ledger | `.gp` | `.gp` |
| public Evidence | 只使用 outer provider ArtifactRef | 只使用 outer provider ArtifactRef |
| worker authority | 可只读检查 history，不得改写 refs/ledger | 不依赖 Git 命令 |

四种组合全部由以上两张表直接组合。尤其 `thinkthread + autoresearch` 不是“只有 prompt 的
autoresearch-like loop”，而是完整 AutoResearch-Git inner mechanics 加 outer snapshot Evidence；
`thinkthread + common` 才是无 private Git/`results.tsv` 的基线。

Git worktree 不是 security sandbox。它隔离 branch 和 filesystem tree，但不限制 absolute path、
process、network、system call 或 credential。ThinkThread 只有在 exact Profile/ABI preflight
证明 protected execution 时才开放；缺少能力必须 fail closed。

---

## 资源所有权与 cleanup

ThinkThread Child、branch、snapshot 和 durable request 都记录 Root/Session/run ownership。
pool close 只 interrupt 或 drain 自己创建的 candidate 与 Annotation Child；不会删除 Root、
其他 run 或用户管理的 ThinkThread 资源。

报告持久化后，cleanup 才回收不再被引用的资源。以下 snapshot 必须继续 pinned：

- active verifier settlement、Annotation task 或 selection 仍需读取的 Evidence；
- prepared publication；
- `awaiting_main_integration` 的 base/publication pair；
- `recovering`/`needs_recovery` 的 durable request anchors；
- active pool、Annotation Child、其他 active run 或外部 owner 仍依赖的 snapshot。

cleanup 的 provider response 不确定时记录 `needs_recovery`，不能假设资源已删除。已完成且无 pin
的 snapshot 可以回收，包括 verifier-backed Evidence snapshot；pending integration 和 recovery
artifact 必须保留到显式收敛。终态回收后 `.gp` 保留 Evidence/Annotation 结果与 artifact identity，
但完整 Artifact View 内容不可重开，也不会自动重进 Annotation Child。

---

## 框架保证

### 可复现验证

每个 verifier result、Evidence record 和 selection 都引用 immutable provider-native artifact。
ThinkThread 的 verifier snapshot ID 必须与 Evidence artifact ID 完全相同。

### Candidate 独立演化

candidate 可以修改、checkpoint、失败和继续，而不共享 writable tree。Git 使用独立 worktree；
ThinkThread 使用独立 private Child/Session/branch，并从同一个 `S0` 开始。

### 受控阅读

candidate 不获得 peer active workspace、Root live tree 或 Annotation tools。Annotation Child
只能读取 task 授权的 immutable target/peer snapshots，所有命令写入均被丢弃。

### 可审计发布

promotion 从 selected artifact 和 original baseline 派生，过滤到 allowed product surface，
重新验证后再使用 provider-native operation 应用。冲突、未知结果、通知和 cleanup 都有 durable
状态，Search history 与 runtime private state 不会进入 source。

---

## 相关文档

- [Shared Plane](shared-plane.md) 说明 Evidence、settlement、selection 和端到端 Search flow。
- [API](api.md) 列出 host 可用的 pool、Artifact View 和 promotion tools。
- [Agent Host Adapters](agent-host-adapters.md) 说明 Codex、Pi RPC 与 Pi + ThinkThread 的
  launch/continue/budget mapping。
