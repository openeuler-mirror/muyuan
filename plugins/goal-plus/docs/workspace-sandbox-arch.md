# Workspace 与 Sandbox 架构

Goal Plus 把 candidate 的代码环境分成两层：外层负责 workspace 或 sandbox，内层统一
使用 Git 管理代码状态和实验历史。因此核心模式不是“Git 或 ThinkThread”，而是：

- **Git + Git**：外层用 Git worktree 创建 workspace，内层 Git 管理 candidate branch、
  attempt commit、回滚和 `results.tsv` ledger；
- **ThinkThread + Git**：外层用 ThinkThread private Child 提供 COW workspace、snapshot
  和 protected execution，Child 内仍使用 Git 管理同一套 candidate 历史。

这里的加号表示两层职责，不表示 Git + Git 必须嵌套两个 repository。Git + Git 可以在
物理上复用同一个 run-local repository/worktree：第一个 Git 表示外层 workspace
materialization，第二个 Git 表示内层 candidate 状态机。ThinkThread + Git 则是实际的
TT Child 外层加 Child 内 Git。

Goal Plus Runtime 在两种模式下拥有相同的 `new -> search -> apply` 生命周期、verifier
合同和 selection 规则。Workspace/Sandbox Provider 只负责把这些动作映射到具体后端。

---

## 框架

```text
Source Git repository
(clean HEAD; no staged, unstaged, or non-ignored untracked product files)
                                |
                               NEW
                                |
                                v
                 +-------------------------------+
                 | Workspace / Sandbox Provider  |
                 +---------------+---------------+
                                 |
              +------------------+------------------+
              |                                     |
       Git outer workspace                    TT outer sandbox
       run-local mirror                       baseline FsSnapshot
       + private worktree                     + private COW Child
       confinement=soft_path                  confinement=protected_execution
              |                                     |
       +------+-------+                      +------+-------+
       |  inner Git   |                      |  inner Git   |
       | clean branch |                      | clean branch |
       +------+-------+                      +------+-------+
              |                                     |
              +------------------+------------------+
                                 |
                            SEARCH LOOP
                                 |
              worker edits candidate current workspace
                                 |
                    Runtime creates inner Git attempt commit
                                 |
              +------------------+------------------+
              |                                     |
       Git + Git checkpoint                   TT + Git checkpoint
       ArtifactRef=git_commit                 ArtifactRef=fs_snapshot
       verifier reads commit tree             snapshot contains committed Git tree
       Git restore + ledger commit            protected verify + snapshot restore
              |                                     |
              +------------------+------------------+
                                 |
                   select verifier-backed ArtifactRef
                                 |
                               APPLY
                                 |
              +------------------+------------------+
              |                                     |
       allowed product files                 allowed product files
       -> squash publication commit          -> filtered publish snapshot
       -> explicit cherry-pick               -> fs.apply(BASE, PUBLISH)
              |                                     |
              +---------------- source root --------+
```

`new` 创建隔离环境，`search` 在隔离环境内形成可验证、可回滚的 immutable artifact，
`apply` 只把最终选择的产品文件写回 source。Host 负责在 Provider 返回的
`WorkspaceAccess` 中启动 worker；Runtime 负责 checkpoint、verifier、settlement、selection
和 apply policy；Provider 不启动 Agent，也不决定哪个 candidate 胜出。

---

## 设计说明

### NEW：从干净的 inner Git 开始

两种模式都要求 source Git 在创建 workspace 前对应一个精确、干净的 `HEAD`。Runtime
在 freeze 和 create 边界检查整个 repository：

```text
git status --porcelain=v1 --untracked-files=all
```

在明确的 ignore 和 runtime-noise 排除项之外，结果必须为空。这意味着：

- 没有 staged 或 unstaged 产品修改；
- 没有会进入受管源码树的 untracked 文件；
- candidate 的 inner Git 从同一 baseline commit 开始；
- Runtime 不会自动 commit、stash、clean 或把 dirty source 偷偷当成 baseline。

`.gp`、`.tmp`、`.search` 等 Runtime 私有路径必须被隔离或显式忽略，不能作为 candidate
输入。确实要捕获当前 filesystem 状态时，应显式使用 `copy + artifact_only`；它是兼容
路径，不属于本文的两种 workspace/sandbox 主模式。

Git + Git 的 `new` 会建立 run-local bare mirror，并从 baseline ref 为每个 candidate
创建独立 worktree/branch。ThinkThread + Git 的 `new` 应从 source 建立 baseline
FsSnapshot，再派生 private COW Child；Child 内的 Git checkout 仍必须位于同一 baseline，
且 worker 的真实 cwd 必须在该 Child 内。

### SEARCH：inner Git 记录尝试，outer provider 固化执行状态

worker 只修改自己的 current workspace。每次 verifier 前，Runtime 先把 candidate-controlled
变化提交到 inner Git，形成可追溯的 attempt commit，然后由 outer provider 给出 verifier
绑定的 canonical artifact：

- Git + Git：attempt commit 本身就是 `ArtifactRef(kind=git_commit)`；
- ThinkThread + Git：在 inner Git attempt commit 后捕获 Child 的 immutable FsSnapshot，
  canonical artifact 是 `ArtifactRef(kind=fs_snapshot)`。

verifier 必须读取该 immutable artifact，而不是碰巧读取一个仍在变化的 cwd。硬 verifier
结果由 Runtime 结算：严格改善为 `keep`，同分为 `retain`，退化为 `discard`，执行失败为
`failure`。Git + Git 用 Git 恢复 candidate-local best；ThinkThread + Git 用 FsSnapshot
恢复 Child。随后 Runtime 追加并提交 `results.tsv` ledger，下一轮继续使用同一个 candidate。

Shared Plane 只发布已结算 ArtifactRef 与窄 Evidence。需要阅读 peer 完整实现时：

- Git + Git 打开 requester 唯一的 detached reference worktree；
- ThinkThread + Git 打开 requester 唯一的 snapshot-derived reference Child。

两者都不能把 peer artifact checkout 到自己的 current workspace，也不能获得 peer 活动
branch/Child 的写权限。

### APPLY：从 selected artifact 枸造发布产物

`apply` 不直接把 candidate current workspace 覆盖到 source。Runtime 先恢复精确的 selected
ArtifactRef，重新运行 promotion verifier，再让 Provider 构造只包含允许产品文件的
publication artifact：

- Git + Git：以 source baseline 为 parent，把 selected tree 中允许的文件组成一个 squash
  publication commit；source 仍干净时，显式 fetch 并 cherry-pick 该提交；
- ThinkThread + Git：以 baseline FsSnapshot 为底，只把 selected snapshot 的允许文件写入
  filtered publish snapshot，再执行 `fs.apply(BASE, PUBLISH)`。

两条路径都必须排除 candidate 内部状态，例如 `.git`、`.gp`、`.search`、`.tmp` 和
`results.tsv`。`artifact_only` 只生成 publication artifact，不写回 source；默认 `apply`
必须成功写回后，Goal Plus 才能记录 Search result。

---

## Git + Git 与 ThinkThread + Git 对照

| 行为 | Git + Git | ThinkThread + Git |
| --- | --- | --- |
| Source 前提 | clean Git `HEAD`；无 staged、unstaged、non-ignored untracked 产品文件 | 相同；inner Git 必须从同一 clean baseline 导入 Child |
| 外层环境 | run-local mirror + candidate private worktree | baseline FsSnapshot + candidate private COW Child |
| 内层版本管理 | candidate Git branch | Child 内 candidate Git branch |
| Worker cwd | worktree 的 logical source path | private Child 内的 logical source path |
| Confinement | `soft_path`；不是强 sandbox | `protected_execution`；必须由 Child 执行边界保证 |
| Search attempt | Runtime 创建 Git attempt commit | Runtime 创建 inner Git attempt commit，再捕获 Child snapshot |
| Canonical ArtifactRef | `git_commit` | `fs_snapshot` |
| Verifier 输入 | 精确 commit tree | 精确 snapshot 或其受保护派生环境 |
| Settlement/restore | Git commit/restore；ledger commit 保持历史可达 | FsSnapshot restore；Runtime 随后保留并追加 authoritative ledger |
| Peer reference | detached reference worktree | snapshot-derived reference Child |
| Publication | allowed tree -> squash publication commit | allowed paths -> filtered publish snapshot |
| Source apply | fetch + cherry-pick | `fs.apply(BASE, PUBLISH)` |
| Sandbox 强度 | 版本树隔离，不限制绝对路径、进程、网络或凭据 | 目标是文件系统 protected execution；实际保证取决于 TT capability |
| 当前可用性 | Codex/Pi 已支持 | promotion adapter 已有；Child new/search/verify/host execution 尚未接通 |

---

## 安全与实现边界

Git worktree 解决并发版本树、精确 commit、回滚和 object sharing，不等于 sandbox。
`confinement=soft_path` 只表示 Host 和 worker 应使用指定 cwd；它不能阻止绝对路径逃逸，
也不限制进程、网络、系统调用或凭据。

ThinkThread + Git 只有在以下条件都成立时才能标记为 `protected_execution`：

1. Host 真正在 private Child 内启动 worker，而不是只传一个 Child id；
2. verifier 针对 immutable FsSnapshot 或受保护派生环境运行；
3. writable surface、reference authority 和路径逃逸由 Provider 强制；
4. Child/snapshot 的恢复、保留、回收和失败重试具有持久语义；
5. capability 不满足时 fail closed，绝不静默 fallback 到 Git 或 Copy。

当前 Codex/Pi Host 支持 Git + Git 和兼容的 Copy 路径。ThinkThread Provider 已定义
`fs_snapshot` 和 filtered `fs.apply` publication，但 candidate materialization、checkpoint、
restore、reference 与 protected host execution 仍未完成，因此普通 `thinkthread` 请求会在
创建 Search run 前被拒绝。

---

## Ref-workspace 与 Diff 阅读策略

`ref-workspace` 用于在不替换 requester current workspace 的前提下，打开一个已经结算的
peer artifact。它是只读参考视图，不是新的 candidate，也不是实现修改的落点。

```text
requester current workspace                       settled Evidence
        |                                                |
        | own history / narrow Git evidence              | exact ArtifactRef
        |---------- git diff / show / log                 |
        |                                                v
        `------------------------ search_reference_open(...)
                                      |
                                      v
                       requester-unique ref-workspace
                       - fixed path; one open reference
                       - switching replaces old reference
                       - inspect only; never implement here
```

阅读方式按目标和范围选择：

| 阅读目标 | Git + Git | ThinkThread + Git |
| --- | --- | --- |
| 自己的当前状态或 attempt 历史 | 直接使用 inner Git 的 `diff`、`show`、`log` | 直接使用 Child 内 inner Git 的 `diff`、`show`、`log` |
| peer 的小范围、路径明确的修改 | 对已结算 `git_commit` 做 path-scoped `diff` 或 `show` | 只能打开 snapshot-derived `ref-workspace` |
| peer 的大范围、多文件或需要完整目录浏览的修改 | 打开 detached `ref-workspace` | 只能打开 snapshot-derived `ref-workspace` |
| 实际实现修改 | 始终留在 requester current workspace | 始终留在 requester current Child |

这里的“小范围”和“大范围”是阅读语义，不是固定的文件数或字节阈值，Runtime 不会自动
切换。一个明确文件或一段局部 Evidence 能回答问题时，Git + Git 可以直接比较；需要理解
跨文件关系、运行完整代码或浏览实现上下文时，应打开 `ref-workspace`。

Git + Git 可以直接比较 peer，是因为 candidate 共享 run-local Git object store，结算后的
canonical artifact 又是精确的 `git_commit`。ThinkThread + Git 的 peer canonical artifact
是 `fs_snapshot`，不保证存在于 requester 的 inner Git object graph，因此跨 candidate 的
Git diff 不能作为权威证据，必须从已结算 snapshot 派生 reference Child。复看自己的修改时，
当前 Child 内的 inner Git 仍可直接 diff；如果要复看一个已经保留、但不再由当前 inner Git
状态表示的精确 snapshot，则同样打开自己的 Evidence ref-workspace。

`search_reference_open` 只接受已经结算的 worker Evidence，并继续执行 Shared Plane 的可见性
约束。每个 requester 只有一个稳定的 `ref-workspace` 路径；再次打开其他 artifact 时，
Runtime 先关闭旧 reference、检测是否被污染，再在同一路径呈现新的精确 artifact。当前
Git Provider 使用 detached worktree，实现的是 `soft_detected` 只读约束：写入会被记录为
contaminated，但契约仍禁止任何修改或副作用。ThinkThread + Git 的目标实现应由 Provider
强制 reference Child 只读。

无论哪种模式，reference 都不能升级为实现 workspace，worker 也不能直接进入 peer 的活动
branch 或 Child。采用某段 peer 方案时，只能在自己的 current workspace 中重新实现，再由
正常的 checkpoint、verifier 和 settlement 流程生成新的 Evidence。
