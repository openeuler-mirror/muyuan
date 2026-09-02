# Workspace 与 Sandbox 框架

Goal Plus 的 Workspace/Sandbox 框架是 Search Mode 的执行底座。它从一个确定的
source baseline 创建彼此独立的 candidate workspace，把每次尝试固化成 immutable
artifact，并让 verifier、peer review 和最终发布都引用同一份精确状态。

框架把 Search 的控制语义与文件系统实现分开。Goal Plus Runtime 始终执行相同的
`new -> search -> apply` 生命周期；`WorkspaceProvider` 决定 candidate 如何创建、
checkpoint、恢复、只读引用和发布。当前模型包含两种组合：**Git + Git** 和
**ThinkThread + Git**。外层负责 workspace 或 sandbox，内层 Git 负责 candidate 的尝试历史。

```text
MainAgent
  |  SearchSpec(source_path, edit surface, verifier, backend)
  v
freeze ---------------------------------------------------------+
  |                                                             |
  | Git + Git                                                   | TT + Git
  | origin verifier x1                                          | origin verifier x1
  | pin clean Git HEAD                                          | pin FsSnapshot
  |                                                             |
  +---------------- source ArtifactRef --------------------------+
                                |
                                | create
                                v
+------------------- Goal Plus Workspace Framework -------------------+
| new                                                                 |
|   Git: c001 worktree -> relocation verifier x1 -> remaining lanes   |
|   TT:  private Child lanes; provider-equivalent layout, no relocate |
|                                |                                    |
| search                         v                                    |
|                     writable current candidate                     |
|                                | edit                               |
|                                v                                    |
|                       inner Git attempt commit                      |
|                                | checkpoint                         |
|                                v                                    |
|                     ArtifactRef {git_commit | fs_snapshot}          |
|                                | verify                             |
|                                v                                    |
|                     keep | retain | discard | failure               |
|                                |                                    |
|                 restore / continue -----> settled Evidence          |
|                                                   |                 |
|                                                   `-> ref-workspace |
|                                                                    |
| apply                                                              |
|   selected ArtifactRef -> promotion verify -> filtered publication |
+---------------------------------------------------|----------------+
                                                    v
                                             source repository
```

框架允许 candidate 持续修改 current workspace，但所有参与决策的状态都先固化为
immutable artifact。同一套生命周期可以映射到 Git commit 或 ThinkThread snapshot，
而不改变 Search policy。

---

## 设计理念

### 固定生命周期，可替换后端

Search 只依赖一组稳定动作：创建 candidate、checkpoint 一次尝试、验证 checkpoint、
恢复已结算状态、阅读 peer 结果，以及发布 winner。这些动作构成
`WorkspaceProvider` 的契约。Runtime 决定何时执行以及哪个结果胜出，Provider 只决定
workspace 与 artifact 如何表示。

这条边界使调度、verifier settlement、Evidence 和 selection 不依赖 Git 或 ThinkThread。
增加一种存储后端，不应同时产生第二套 Search 状态机。

### 可变工作区，不可变决策

worker 只修改自己的 **current workspace**。verifier 启动前，Runtime 把受管变化提交到
inner Git，再要求 Provider 返回 `ArtifactRef`。Git 用 commit 标识该状态，ThinkThread
用 filesystem snapshot 标识该状态。

verifier result 必须绑定这个 immutable artifact。当前 Git 路径只在 current workspace
干净且内容与 attempt commit 完全一致时启动 verifier；ThinkThread 路径则要求在 snapshot
或其受保护派生环境中执行。Evidence、restore、selection 和 promotion 继续引用同一个
artifact，因此后续修改不能悄悄改变产生既有分数的状态。

### 外层隔离，内层历史

Git 与 ThinkThread 不是对所有职责二选一。它们位于外层：Git 创建 private worktree，
ThinkThread 创建 private Child。两者内部都使用 Git，记录 attempt commit 和 Runtime
拥有的 `results.tsv` ledger。

**Git + Git** 表示两种职责，不一定是两个嵌套 repository。它的 worktree 和 candidate
branch 可以共享同一个 run-local Git repository。**ThinkThread + Git** 则是物理分层：
Git repository 位于 private ThinkThread Child 内。

### 构造发布产物，而非回写目录

选中 candidate 并不意味着可以把它的 current directory 覆盖到 source。Runtime 先恢复
精确的 selected artifact，重新执行 promotion verification，再要求 Provider 构造只包含
allowed product files 的 filtered publication artifact。应用该 artifact 是另一个显式操作。

---

## 编程模型

一次 Search run 包含三个 workspace 阶段：`new` 建立 baseline，`search` 通过 immutable
checkpoint 演化 candidate，`apply` 发布一个 selected artifact。

### Freeze：验证 source 并固定身份

`source_path`、verifier 命令和编辑范围由 MainAgent 写入 SearchSpec。Runtime 不从目录结构
反推另一个 candidate root，也不拼接 `workspace/` 等路径。`search_freeze_spec` 负责执行已给定
合同，并在创建 Search run 之前证明该合同至少能在 source 上运行。

对于 Git + Git，freeze 先检查 source repository 是 clean `HEAD`，hash-pin verifier
artifact，然后直接在 origin `source_path` 执行一次完整 process-verifier suite。成功后再次
检查 clean 状态和 HEAD，把该 commit 保存为 `FrozenSpec.source_artifact_ref`。verifier 可以按
自身构建逻辑生成被 Git 忽略的 cache；Runtime 不清理或猜测这些产物，但任何 staged、unstaged
或 non-ignored untracked 变化仍会阻止冻结。

`search_create` 会再次确认 origin 仍处于该 commit。HEAD 即使以 clean commit 的方式变化，
也会以 `FrozenSourceChanged` 拒绝使用旧 FrozenSpec。这样 run-local mirror 和 candidate branch
都从 verifier 实际验证过的 source artifact 建立，而不是从稍后的目录状态建立。

ThinkThread + Git 的 provider contract 只需要在 origin 执行一次 verifier，然后固定
`FsSnapshot`。它不需要 Git 的 relocation gate，因为 Child 由同一 provider-native snapshot
派生。当前 Codex/Pi host 还不支持 TT candidate materialization，因此该路径在 freeze 前
fail closed；这里定义的是启用该 backend 时必须满足的合同，不代表它已经可运行。

### New：建立精确 baseline

所有 candidate 从同一个 committed baseline 开始。执行开始时，inner Git 必须干净：
受管代码树中没有 staged change、unstaged change 或 non-ignored untracked file。

如果 source 已经是 Git repository，Runtime 会在冻结 SearchSpec 前检查整个 repository，
并在创建 run 前再次检查。它不会自动 commit、stash、clean，也不会重新解释 dirty source。
非 Git source 可以建立 synthetic committed baseline；如果确实要捕获 dirty filesystem，
应使用兼容的 `copy + artifact_only` 路径，而不是本文的两种主模式。

`new` 随后要求 Provider 为 candidate 物化 private current workspace。Git + Git 先只建立
首个真实 candidate `c001`，确认其 HEAD 等于 frozen source artifact，并在该 worktree 中执行
同一套 process verifier。这个 relocation gate 用于发现 verifier 的 `cwd`、相对路径或 source
root 在移动到 run-local worktree 后失效的问题。验证后 `c001` 必须仍处于同一 HEAD，且不能
留下 non-ignored Git change；Git-ignored cache 可以保留。

relocation 通过后才创建其余 candidate，并允许 Host 启动 worker。失败时 Runtime 自动把 run
置为 `aborted`，原因记录为 `verifier_target_mismatch`，保留 `c001` 供诊断，不创建 `c002`，
也不创建 worker session。Goal Plus record 保持 active，由 MainAgent 修复 SearchSpec 或 verifier
后重新 freeze。origin 与 relocation 结果是 control-plane bootstrap Evidence；它们不是 candidate
iteration，不写 `results.tsv`，不计入 worker budget，也不参与排名。

Provider 最终返回 `WorkspaceAccess`，其中包含 logical root、native cwd、writable surfaces 和
confinement。Host 在这个 access context 中启动 worker；Provider 不负责启动或监督 agent。

### Search：checkpoint、验证与结算

worker 在 current workspace 中修改代码。调用 verifier 时，Runtime 执行以下事务：

1. 校验 Runtime-owned ledger 和 allowed edit surface。
2. 把 candidate-controlled change 提交到 inner Git，形成一个 attempt。
3. 要求 Provider 把该 attempt checkpoint 为 immutable `ArtifactRef`。
4. 在与该 artifact 精确一致的 workspace 或派生环境中运行 verifier。
5. 将结果结算为 `keep`、`retain`、`discard` 或 `failure`。
6. 从 settled state 继续，或者恢复 candidate-local best artifact。
7. 向 `results.tsv` 追加一条 authoritative record 并提交。

只有已经结算的 worker iteration 才能成为 Evidence。selection 引用 Evidence artifact 本身，
而不是 Search loop 停止时碰巧留在 current workspace 中的内容。

### Apply：发布 selected artifact

`apply` 从 selected `ArtifactRef` 开始，而不是从 candidate 的 live workspace 开始。Runtime
恢复该 artifact，运行 promotion verifier，计算 allowed changed files，再要求 Provider 基于
original baseline 准备 publication artifact。

`.git`、`.gp`、`.search`、`.tmp` 和 `results.tsv` 等 candidate-private state 不是产品输出，
不能进入 publication。`artifact_only` 模式只返回 publication，不修改 source；`apply` 模式
则要求 source 仍然满足 Provider 的写回前提。

---

## 后端实现

两种后端实现同一套编程模型，差异只存在于 artifact identity、workspace isolation、restore
和 publication mechanics。

| 行为 | Git + Git | ThinkThread + Git |
| --- | --- | --- |
| 外层 workspace | run-local Git mirror 和 private candidate worktree | baseline FsSnapshot 和 private COW Child |
| 内层历史 | worktree 中的 candidate Git branch | Child 内的 candidate Git branch |
| origin bootstrap | process verifier x1，固定 clean source commit | process verifier x1，固定 source snapshot |
| `new` baseline | 从 frozen commit 建立 baseline ref 和 candidate branch | 包含同一 clean inner Git commit 的 baseline snapshot |
| relocation gate | `c001` 中 process verifier x1，通过后才创建其余 lane | 不需要；Child 直接派生自 provider-native snapshot |
| relocation 失败 | abort run，保留 `c001`，不创建 worker 或后续 lane | 不适用 |
| checkpoint identity | `ArtifactRef(kind=git_commit)` | inner Git attempt 后生成 `ArtifactRef(kind=fs_snapshot)` |
| verifier 输入 | 干净且确认与 attempt commit 一致的 current worktree | 精确 snapshot 或其受保护派生环境 |
| restore | 从 settled commit 恢复，再保留 Runtime ledger state | 恢复 settled snapshot，再保留 Runtime ledger state |
| peer reference | detached Git reference worktree | read-only snapshot-derived reference Child |
| publication | allowed changes 组成一个 squash publication commit | allowed changes 组成一个 filtered publication snapshot |
| source apply | fetch publication commit 后 cherry-pick | `fs.apply(BASE, PUBLISH)` |
| isolation | `soft_path` 下的 version-tree isolation | Host 真正在 Child 内执行时的 filesystem protected execution |
| 当前实现 | Codex 和 Pi 已支持 | 已有 promotion adapter；candidate、checkpoint、restore、reference 和 protected execution 仍会 fail closed |

Git worktree 不是 security sandbox。它隔离 branch 和 filesystem tree，但不限制 absolute path、
process、network、system call 或 credential。只有 Provider 能为 worker 和 verifier 强制执行
Child boundary 时，ThinkThread 才能声明 `protected_execution`；缺少能力时必须 fail closed，
不能静默 fallback。

---

## 阅读已结算结果

worker 的 current workspace 是唯一可写的实现空间。`ref-workspace` 是另一个 settled
Evidence artifact 的 read-only view。它让 worker 在不 checkout peer state、不获得 peer
active workspace 权限的前提下理解另一份实现。

| 阅读目标 | Git + Git | ThinkThread + Git |
| --- | --- | --- |
| 自己的 current state 或 attempt history | 使用 inner Git 的 `diff`、`show` 或 `log` | 在 Child 内使用 inner Git 的 `diff`、`show` 或 `log` |
| 小范围、path-scoped peer change | 直接比较 settled peer commit | 打开 snapshot-derived `ref-workspace` |
| 大范围或 cross-file peer change | 打开 detached `ref-workspace` | 打开 snapshot-derived `ref-workspace` |
| 采用 peer idea | 在 current workspace 中重新实现 | 在 current Child 中重新实现 |

“小范围”和“大范围”描述所需的阅读上下文，不对应自动的文件数或字节阈值。Git + Git
可以直接比较窄范围 peer change，因为 settled candidate 共享 run-local object store，且
Evidence 引用 `git_commit`。ThinkThread peer artifact 是 `fs_snapshot`，不保证存在于
requester 的 inner Git object graph，因此 cross-candidate review 必须打开 reference Child。
复看自己的 TT + Git 历史时，仍可直接使用 inner Git。

`search_reference_open` 只接受 settled worker Evidence。每个 requester 只有一个稳定的
`ref-workspace` 路径；打开另一个 artifact 会替换原 reference，并记录旧 view 是 clean 还是
contaminated。当前 Git 实现是在事后检测写入（`soft_detected`）；ThinkThread 设计要求
Provider 强制 read-only access。

---

## 框架保证

### 可复现验证

每个 verifier result、Evidence record 和 selection 都引用 immutable provider-native artifact。
被评价的状态不受 worker 后续修改影响。

freeze 保存 origin bootstrap Evidence；Git worktree 另外保存 `c001` relocation Evidence。
两次验证使用同一 frozen verifier hash 与 process-verifier suite，使“origin 能运行”和
“真实 candidate 路径能运行”在 worker 消耗预算前分别得到证明。

### Candidate 独立演化

candidate 可以修改、checkpoint、失败和恢复，而不共享 writable tree。inner Git 提供统一的
attempt history model，外层 Provider 决定隔离强度。

### 受控的 peer learning

settled Evidence 可以通过窄范围 commit comparison 或 read-only reference workspace 查看。
peer artifact 始终只是 reference，不会成为另一个 candidate 的 writable authority。

### 可审计发布

promotion 从 selected artifact 和 original baseline 派生，过滤到 allowed product surface，
重新验证后再由 provider-native operation 应用。Search history 和 Runtime state 不会进入 source。

---

## 后续文档

- [Shared Plane](shared-plane.md) 说明 Evidence、settlement、selection 和端到端 Search flow。
- [API](api.md) 列出 Host 可用的 workspace、reference 和 promotion tools。
- [Agent Host Adapters](agent-host-adapters.md) 说明 Codex 和 Pi 如何在
  `WorkspaceAccess` 中启动 worker。
