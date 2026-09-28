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
  |  SearchSpec(source_path, edit surface, verifier, workspace_provider)
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
checkpoint、验证、结算、选择和发布。Git candidate 文件操作经本地 workspace/artifact 窄接口
执行。ThinkThread candidate 的 Child、Session、message 和 continuation 经共同 session actor
与 TT adapter 执行，snapshot checkpoint 经 provider port 固定。runtime 仍拥有 spec、Evidence、
selection、publication 和 `.gp/` 状态。两条路径不能互相 fallback。

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

`source_path`、verifier 命令、编辑范围和 workspace provider 由 MainAgent 写入 SearchSpec。
runtime 不从目录结构猜测 candidate root，也不会在 workspace_provider 不匹配或 preflight 失败时降级。

Git + Git 在 freeze 时要求 clean Git `HEAD`，hash-pin verifier artifact，并在 origin
执行一次完整 process-verifier suite。`goal_plus_search_create` 再次确认 source commit 未改变，随后
只建立 `c001`，在真实 worktree 运行 relocation verifier。通过后才创建其余 candidate；
失败会 abort run，并保留诊断 Evidence。

当 Git root 的 committed tree 含 `.gitmodules` 或 gitlink 时，clean 检查会递归验证所有
formal child。它报告 `SubmoduleWorkspaceDirty`、`SubmoduleHeadMismatch`、
`SubmoduleUninitialized`、`SubmoduleObjectUnavailable` 等明确类型，并带 child 相对路径、
检查阶段、原因和 expected/actual；candidate worktree 会先递归初始化 child 再进入 verifier。

ThinkThread 在 freeze 时校验 `source_path` 可表示为当前 Root 的逻辑 `source_subdir`，并固定
SearchSpec 与 verifier artifact。`goal_plus_search_create` 校验 Root direct filesystem 和 delegated Child
capability；Runtime release、Agent Control protocol 与 Agent POSIX fingerprint 偏离已测基线时
只警告，实际接口调用仍须满足合同。通过后在短暂写入边界捕获 Root-owned `S0`。origin verifier 对 `S0` 执行 exact
snapshot `fs.run(writes="discard")`；失败会 abort run，且不会启动 candidate。

ThinkThread Child 只接收逻辑 `source_subdir`，不会获得 Root 的本地绝对路径。每个 candidate
从同一个 `S0` 创建，但拥有不同的 ThinkThread ID、native Session 和 private branch。
Pi/Codex 的 ThinkThread Profile 都支持 Search 及 Adaptive allocation。

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

1. Child 调用 `goal_plus_search_run_verifier`；AutoResearch-Git binding 先由 trusted extension 提交 private
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
   disposition，并将 typed feedback 写入准确 invocation 结果。Main 决定继续时显式 wake
   同一个 Child/Session/branch；收尾时通过共同 session 工具停止执行并释放资源。

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

`goal_plus_search_select` 先关闭 queue 并推进 generation，再 interrupt retained Child。已经 claim 的旧
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

## Git 与 Candidate-Local Best

新 `git_worktree` run 不再删除 source 的 `.git` 后重新初始化。Runtime 对真实 Git source
创建 run-local bare mirror，保留完整 refs、commit history 和对象，再从启动 HEAD 建立
baseline ref 与各 candidate worktree。`source_path` 位于 repository 子目录时，worktree 仍是
完整 repository，worker cwd 和 edit surface 则保持相对于该 source 子目录。

Git-backed source 的规范输入是 clean `HEAD`。`goal_plus_search_freeze_spec` 在 origin verifier 前检查
整个 repository，验证后再次检查并把 HEAD 固定为 `source_artifact_ref`；`goal_plus_search_create` 在写入
run 前复检 clean 状态和准确 commit。

Freeze、create 和 apply 共用 Git 状态检查，范围为整个 source repository，排除 `.gp`、
`.search`、`.tmp` 等 runtime-noise。其余 staged、unstaged 或 non-ignored untracked 变更，
以及 Git 状态读取错误，均阻止当前操作。过滤只作用于状态读取，源码和 Git ignore 配置保持原样。
Freeze/create 对源码变更返回 `DirtySourceWorkspace`；创建 run 时若 clean HEAD 与冻结 commit
不一致，则返回 `FrozenSourceChanged`。需要以当前目录状态作为输入时，使用 `copy + artifact_only`。

run-local mirror 以 frozen commit 建立 baseline。`goal_plus_search_start_batch` 先物化 `c001`，确认其 HEAD
与 frozen artifact 一致，并在该 logical workspace 重跑 process verifier；只有通过后才创建
剩余 candidate。该 relocation gate 专门阻止 origin 路径能运行、worktree 路径却失效的合同。

Evidence 中的 commit 表示 verifier 实际读取的完整 Git tree，不是相对于初始源码的
patch。Runtime 分别记录：

- `attempt_base_git_head`：尝试前的 settled HEAD；
- `git_head`：verifier 实际验证的 attempt commit；
- `attempt_changed_files`：用于 annotation 的 `base..attempt` 净变化；
- `changed_files`：相对于原始 source 的最终 artifact 变化，用于策略检查和 promotion。

Runtime 会在验证前提交所有 candidate-controlled 修改，并要求 artifact worktree
干净。candidate 可以包含多个手工 commit；annotation 使用完整
`settled-base..attempt` 范围，而不是只查看最后一个 commit。

每次 process-verifier 尝试都会永久记录在 canonical `.gp` ledger。严格改善和同分分别以
`keep`、`retain` 更新 candidate-local 最新最佳。只有 `discard` 或 `failure` 时，Runtime 才把
代码恢复到此前最佳版本。内置 `autoresearch` binding 还请求 trusted adapter 管理的
`results.tsv` 兼容投影，因此它的物理历史为：

```text
keep:     settled -> attempt -> ledger
retain:   settled -> equal-score attempt -> ledger
discard:  settled -> attempt -> restore-best -> ledger
```

attempt、恢复和 ledger commit 都保持在 candidate-private Git 历史中。因此下一轮始终从
candidate-local best
规划，而 Global Evidence 仍保留好、差和失败的全部尝试。`common` binding 不请求该投影：
keep/retain 后直接以 attempt commit 继续，discard/failure 后以 restore commit 继续，不创建
`results.tsv` 或额外 ledger commit。一个 candidate 的回滚不会改变 peer 工作区，也不会修改
peer 的判断。

在 ThinkThread 中，public `IterationRecord.artifact_ref` 仍是 verifier 实际读取的 quiescent
attempt snapshot，public Git compatibility fields 保持 `null`。private attempt commit 和
post-settlement ledger commit 只保存在 Child snapshot 与 `inner_agent_state` 中；settled Child
branch 指向 post-settlement snapshot。Annotation Artifact View、changed-file validation 和
publication 一律过滤 `.git`、`.gp`、`.search`、`.tmp` 与任意层级的 `results.tsv`。

最终 promotion 不改写或压缩 candidate branch。Runtime 以 run baseline 为 parent、以 selected
artifact 的允许产品文件为 tree，另行创建一个 squash publication commit；`results.tsv`、`.gp`、
`.tmp` 和 Git metadata 不进入该提交。`goal_plus_search_apply_promotion` 把这一个提交 cherry-pick
回原 repository，因此产品历史保持单提交，而完整 autoresearch running history 仍留在 run-local refs。
`workspace.promotion_mode` 默认是 `apply`，Goal Plus 在 apply 完成前拒绝记录 Search result；
只有受保护 source、只读 template 或测试 fixture 显式使用 `artifact_only`。

freeze 还会持久化 `workspace_capacity_estimate`。它同时区分当前 source scope、有效
materialized source、整个 repository 的每-node checkout、`max_parallel` 个 node 合计和共享
object store；因此 Git 子目录任务不会错误地只按子目录估算，也不会简单使用 `du cwd × K`。
保守上界和目标文件系统可用空间共同产生 `fits_available_space`，并进入 monitor/report。

### Task-local Skill 与 Reference

任务局部 Skill 仍属于 source baseline，而不是共享平面。runtime 自动发现 source Git
index 中的 `.agents/skills/<name>/SKILL.md`，并要求该 Skill 目录内所有文件都由 Git 跟踪；
freeze 持久化发现结果，launch/context 向每个 candidate 提供清单；candidate
通过 `task_skill_paths` 获得自己 worktree 的绝对路径。无需额外 spec 配置或逐文件 Skill
哈希；Git baseline 决定内容。worker 只加载明确列出的 task-local Skill，不扫描
parent/global/plugin 或 peer worktree。

`git_worktree` 后端下，各 candidate 共享一个 Git common directory。窄、单文件证据可以
从自己的工作区查看 peer Evidence commit：

```bash
git show <commit>:<allowed-file>
git diff HEAD <commit> -- <allowed-file>
```

改动较大或需要浏览完整实现时，candidate 调用 `goal_plus_search_reference_open`。runtime 把 root-owned
Evidence identity checkout 到当前 worker 唯一、稳定的 detached reference worktree：

```text
candidate workspace/       # 当前 worker 的实现工作区
ref-workspace/             # 当前 worker 的参考 checkout
```

worker 直接使用普通文件和命令工具查看返回路径。再次调用会丢弃 ref-workspace 的本地变化，
并把同一目录切到新的 Evidence commit；不会创建第二个 reference 目录，也不要求 worker
close。candidate 的实现修改始终留在自己的 workspace，不访问 peer 活动 workspace。
ThinkThread 的完整 target/peer 阅读由独立 retained Annotation Child 的 immutable Artifact View
工具提供；candidate 不获得 sibling live branch 或 Annotation 工具。

最终 promotion artifact 虽然是
相对于 source 生成的 patch，但这不会改变 Evidence commit 代表完整代码树的语义。

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
| execution boundary | worktree path isolation + direct host sandbox | provider-enforced private Child |
| supported host | Codex and Pi RPC | installed Pi or Codex ThinkThread Profile |

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

Direct Copy/Git 的 `WorkspaceAccess.confinement` 现在要求 `protected_execution`。Pi 的
child 默认经过 OS wrapper，Codex 使用原生策略；共享 runtime 写根等限制统一见
[Agent sandbox](sandbox.md)。Git worktree 不能单独承担该进程边界。

---

## 资源所有权与 cleanup

ThinkThread Child、branch、snapshot 和 durable request 都记录 Root/Session/run ownership。
session/scope close 只停止自己登记的 candidate 与 Annotation Child；不会删除 Root、
其他 run 或用户管理的 ThinkThread 资源。

报告持久化后，cleanup 才回收不再被引用的资源。以下 snapshot 必须继续 pinned：

- active verifier settlement、Annotation task 或 selection 仍需读取的 Evidence；
- prepared publication；
- `awaiting_main_integration` 的 base/publication pair；
- `recovering`/`needs_recovery` 的 durable request anchors；
- active session、Annotation Child、其他 active run 或外部 owner 仍依赖的 snapshot。

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
只能通过授权只读工具读取 task 的 immutable target/peer snapshots，不开放命令执行。

### 可审计发布

promotion 从 selected artifact 和 original baseline 派生，过滤到 allowed product surface，
重新验证后再使用 provider-native operation 应用。冲突、未知结果、通知和 cleanup 都有 durable
状态，Search history 与 runtime private state 不会进入 source。

---

## 相关文档

- [Shared Plane](shared-plane.md) 说明 Evidence、settlement、selection 和端到端 Search flow。
- [API](api.md) 列出 host 可用的 session、Artifact View 和 promotion tools。
- [Agent Host Adapters](agent-host-adapters.md) 说明 Codex、Pi RPC 与 Pi/Codex + ThinkThread 的
  launch/continue/budget mapping。
