# Goal Plus Workspace 架构：Git 与 ThinkThread

> 状态：`[CURRENT]` Git/Copy candidate runtime、Git provider-native promotion，
> 以及 ThinkThread filtered promotion adapter 已实现；`[TARGET]` ThinkThread
> candidate/Child、checkpoint、reference 和 host protected execution 尚未实现。
>
> 实现基线：`codex/goal-plus-with-tt-git-refactor` 当前实现（2026-08-31）。
>
> 相关文档：[Shared Plane](../shared-plane.md) ·
> [Host Adapters](../agent-host-adapters.md)

---

## 1. 结论

这次重构已经把 workspace 从 Search 状态机里拆成独立 provider 边界。Runtime
负责何时创建 candidate、checkpoint、验证、结算、选择和 promotion；provider
负责如何把这些动作映射到 Git tree 或未来的 ThinkThread FsSnapshot。

当前可运行的主路径是 `git_worktree`：

- `workspace.backend` 默认是 `git_worktree`，`promotion_mode` 默认是 `apply`；
- clean Git source 被完整 mirror 到 run-local bare repository；
- candidate 使用彼此独立、共享 object store 的 worktree 和 branch；
- 每次 verifier 绑定 runtime 创建的 immutable `git_commit`；
- 最终另建一个 squash publication commit，再显式 cherry-pick 回 source；
- candidate 的完整实验提交历史不会被 squash 或改写。

`copy` 是显式 filesystem-capture 路径。它为每个 candidate 复制 source 并建立独立
Git baseline，但固定为 `artifact_only`，不会由 runtime 写回 source。

ThinkThread 当前只完成了 provider 类型、`fs_snapshot` 产物类型和 filtered
`fs.apply` promotion adapter。Codex/Pi 的 host capability 仍会在 freeze 前拒绝
`thinkthread`，因此不能把 adapter 单测通过描述成 ThinkThread candidate 已可运行。

---

## 2. 当前运行架构

### 2.1 Runtime 主链路

下面的实线都是当前代码路径；ThinkThread target 不混入这张图。

```text
Codex $goal-plus / Pi /goal-plus
                |
                v
       Goal Plus state machine
                |
                v
      SearchSpec -> freeze_spec
                   |-- host/backend capability guard
                   |-- clean-source preflight
                   |-- workspace capacity estimate
                   |-- freeze verifier artifacts
                   `-- discover Git-tracked task Skills
                |
                v
      FrozenSpec -> create_run
                   |-- recheck host/backend + source cleanliness
                   `-- reject capacity fits=false before run materialization
                |
                v
         SearchPlan / fixed candidate lanes
                |
                v
           start_batch
                |
                v
       +----------------------+       CandidateTask
       | WorkspaceProvider    |------ workspace + WorkspaceAccess
       | materialize_candidate|       + base ArtifactRef
       +----------------------+                 |
                                                v
                                    Codex/Pi host launches worker
                                                |
                                      edits private workspace
                                                |
                                                v
                                      search_run_verifier
                                                |
                   +-- provider.checkpoint -> immutable ArtifactRef
                   +-- edit-surface + frozen verifier checks
                   +-- keep / retain / discard / failure settlement
                   +-- runtime-owned results.tsv ledger commit
                   `-- Evidence + lineage -> Shared Plane
                                                |
                                   repeated same-candidate loop
                                                |
                                                v
                                      select exact ArtifactRef
                                                |
                                                v
                                      prepare promotion artifact
                                                |
                     artifact_only <------------+------------> apply
                                                             |
                                                             v
                                                   provider.apply_promotion
                                                             |
                                                             v
                                                         source root
```

Host 与 provider 是两个正交维度：Host 拥有 worker 启动、继续、停止和预算；Runtime
调用 provider 并持久化 workspace/Evidence/promotion 状态。Host 不创建 checkpoint，
provider 也不启动 agent。

### 2.2 当前 Git 存储形态

clean Git source 会保留整个 repository，而不是只复制 `source_path` 子目录：

```text
source repository (clean HEAD = S)
        |
        | git clone --mirror --local
        v
.gp/runs/<run>/workspace-repository.git        # run-local bare mirror
        |
        +-- refs/heads/gp/<run>/baseline -> S
        |
        +-- gp/<run>/c001 -> workspace/c001/<source_subdir>
        |                      `-- attempt/restore/ledger commits
        |
        +-- gp/<run>/c002 -> workspace/c002/<source_subdir>
        |                      `-- attempt/restore/ledger commits
        |
        `-- refs/goal-plus/promotions/<run>/<candidate>
                               `-- squash publication commit

Evidence commit E
        |
        `-- detached worktree
            reference-workspaces/<agent_session>/ref-workspace
```

若 `source_path` 是 Git repository 的子目录，candidate 的 managed worktree 仍 checkout
整个 repository，worker 得到的 logical workspace 才指向该子目录。这样 root-level
历史、相对路径和 Git 对象都保持一致，同时 edit-surface 仍按 logical workspace 检查。

普通非 Git directory 仍可使用 `git_worktree`。provider 会先建立 synthetic baseline
repository，再创建 candidate worktree；它没有可写回的原 Git repository，因此最终仍是
legacy patch/export 路径，不具备 real-Git 的 provider-native cherry-pick 语义。

### 2.3 当前 candidate iteration

```text
settled ArtifactRef
        |
        +-- worker edits current workspace
        |
        +-- checkpoint(attempt-N) -> verifier reads exact commit
        |
        +-- keep    : attempt becomes strict-score best
        +-- retain  : equal-score attempt becomes latest local baseline
        +-- discard : restore previous hard-score best
        `-- failure : restore previous hard-score best
                         |
                         `-- append + commit results.tsv ledger
```

attempt commit、restore commit 和 ledger commit 都保持可达。`ArtifactRef` 表示 verifier
真正读取的完整 tree，不是临时 diff。Shared Plane 发布该 immutable identity 与窄
Evidence；worker 只有在窄 Evidence 不足时才打开完整 `ref-workspace`。

---

## 3. Provider-neutral 数据合同

### 3.1 WorkspaceSpec

```text
WorkspaceSpec
├── backend: git_worktree | copy | thinkthread
└── promotion_mode: apply | artifact_only
```

默认组合是 `git_worktree + apply`。`copy` 未显式填写 promotion mode 时自动规范化为
`artifact_only`，显式请求 `copy + apply` 会被模型校验拒绝。

### 3.2 WorkspaceMaterialization 与 WorkspaceAccess

provider 内部先返回 `WorkspaceMaterialization`：

```text
backend
workspace                 # worker 使用的 logical cwd
managed_root              # provider 管理的完整 worktree/root
source_subdir
branch / base_revision
provider_handle           # repository、source_repository 等 provider 坐标
```

Runtime 再把它持久化到 `CandidateTask`，并生成 host-facing `WorkspaceAccess`：

```text
logical_root = "workspace"
native_cwd
managed_root
source_subdir
confinement = soft_path               # 当前 Git/Copy
writable_surfaces = edit_surface.allow
provider_handle
```

当前 Codex/Pi 路径的 `confinement` 都是 `soft_path`。路径合同要求 worker 在 candidate
cwd 工作，但它不是 filesystem sandbox。未来 ThinkThread 真正接通后才应返回
`protected_execution`。

### 3.3 ArtifactRef

```text
Git / Copy:
{ kind: "git_commit", id: "<sha>", provider: "git_worktree|copy" }

ThinkThread:
{ kind: "fs_snapshot", id: "<snapshot-id>", provider: "thinkthread" }
```

`kind` 必须与 provider 匹配。Runtime 的 verifier、iteration、Evidence、reference、
selection 和 promotion 都传递 `ArtifactRef`；业务逻辑不能假设 `id` 一定是 Git SHA。

### 3.4 PromotionRecord

promotion 被拆成 prepare 与 apply 两个持久步骤：

```text
selected ArtifactRef
        |
        v
PreparedPromotion
├── strategy: legacy_patch | squash_commit | fs_apply
├── base / selected / publication ArtifactRef
├── artifact_path
├── changed_files
└── provider_handle
        |
        v
PromotionRecord.state
prepared -> applied
         `-> apply_failed -> retry apply
```

`promotion_mode=artifact_only` 在 prepared publication 停止。默认 `apply` 必须完成
provider apply，Goal Plus 才允许记录 Search result。

### 3.5 Operation timing 与 capacity

freeze 持久化 `WorkspaceCapacityEstimate`，区分：

- source scope 与实际 materialized source；
- 每个 node 的完整 checkout；
- `budget.max_parallel` 个 candidate checkout 合计；
- run-local shared object store、保守上界和目标文件系统可用空间。

Git 子目录任务按整个 repository checkout 估算。`fits_available_space=false` 时，
`create_run` 在创建 candidate 前失败。

每个成功创建的 candidate 在 `task.workspace_creation_timing` 保存 materialization
开始、完成和耗时。provider-native apply 另在 `PromotionRecord.apply_timing` 保存成功或
失败的调用耗时；重复读取已 applied promotion 不会再次写回。

---

## 4. 当前 WorkspaceProvider 协议

当前协议的真实方法是：

```text
materialize_candidate(...) -> WorkspaceMaterialization
checkpoint(workspace, message, pathspecs) -> ArtifactRef
restore(workspace, artifact_ref, preserved_files, message) -> ArtifactRef
current_artifact(workspace) -> ArtifactRef
is_descendant(workspace, base, target) -> bool
changed_files(workspace, base, target, pathspecs) -> list[path]
status(workspace, ignore_runtime_noise=...) -> list[status]
materialize_reference(source_workspace, artifact, destination) -> Path
remove_reference(source_workspace, artifact, destination) -> bool
prepare_promotion(...) -> PreparedPromotion
apply_promotion(prepared) -> PromotionApplyResult
```

旧设计中的 `prepare_source`、`create_workspace`、`checkout_reference` 和
`dispose_workspace` 不是当前 Python protocol。source preparation 目前内聚在 Git
materialization 中；reference 的创建/回收由一对显式方法完成；更通用的
SourceImporter 和 provider dispose/retention 仍是目标能力。

### 4.1 Capability 现状

| Provider | Candidate/Checkpoint | Reference | Protected exec | Prepare/Apply | 当前用途 |
| --- | --- | --- | --- | --- | --- |
| `git_worktree` | 已实现 | 已实现 | 否 | 已实现 | 默认可运行路径 |
| `copy` | 已实现 | 已实现 | 否 | 否 | filesystem capture + artifact-only |
| `thinkthread` | 未实现 | 未实现 | 否 | 已实现 adapter | 尚不可启动 candidate |

Codex 与 Pi host adapter 当前只声明 `copy`、`git_worktree`。`thinkthread` 会在
`freeze_spec` 的 host/backend guard 中失败，早于 verifier preflight 和 run 创建；Runtime
不会静默 fallback 到 Git 或 Copy。

---

## 5. Source preflight 与 baseline 语义

### 5.1 Git-backed source

当前 working tree 对 `git_worktree` 执行两次 clean-source 检查：

1. `freeze_spec` 在 verifier preflight 和 spec 落盘前检查；
2. `create_run` 在 run 落盘前复检，防止 freeze 后 source 漂移。

检查范围是包含 `source_path` 的整个 repository。staged、unstaged 和 non-ignored
untracked 任一种状态都会返回 `DirtySourceWorkspace`。Runtime 不会自动 commit、stash、
clean 或偷偷捕获 dirty tree；ignored untracked 以及 `.gp`、`.search`、`.tmp` 等既有
runtime-noise 排除项不阻塞。

`_validate_clean_source_repository` 的 policy 也包含未来 `thinkthread`，但正常 Codex/Pi
入口当前会先因 host 不支持 ThinkThread 而拒绝。

### 5.2 Copy 与 plain directory

需要有意把当前 filesystem 状态当输入时，使用：

```json
{
  "workspace": {
    "backend": "copy",
    "promotion_mode": "artifact_only"
  }
}
```

`copy` 不要求 source Git clean；它复制有效 source tree，并为每个 candidate 建立独立
Git baseline，以复用同一套 checkpoint/restore/Evidence 语义。

plain directory 配合 `git_worktree` 时使用 synthetic Git baseline。这保留既有 Search
兼容性，但不等于拥有原 repository write-back contract。

---

## 6. Reference Workspace 与共享边界

每个 agent session 最多拥有一个稳定 reference 路径：

```text
candidate workspace/                    # owner 可写实现
reference-workspaces/<session>/
└── ref-workspace/                      # 某条 settled Evidence 的完整 tree
```

`search_reference_open` 先验证 requested iteration 是已结算、带 worker identity 的
Evidence，再把它的 `ArtifactRef` 交给 source candidate 的 provider。再次 open 会先移除
旧 detached worktree，并在同一路径物化新 artifact；Runtime 记录旧 reference 是 clean、
contaminated 还是 unavailable。

Git reference 是隔离副本，但“只读”目前是 soft-detected contract：worker 写入 reference
不会改变原 commit，却可能把自己的 reference 标记为 contaminated。它不能获得 peer
活动 branch 的写 authority，也不能读取 peer 尚未发布的 current workspace。

---

## 7. 当前 Promotion 路径

### 7.1 Real Git source

```text
source baseline S
        |
selected verifier-backed commit C
        |
        | copy only allowed product paths into a temporary index
        v
publication tree P
        |
commit-tree -p S -> squash publication commit Q
        |
        +-- format-patch -> .gp/runs/<run>/promotion/<candidate>.patch
        `-- durable promotion ref + idempotency marker
                                |
                         search_apply_promotion
                                |
              require clean source -> fetch Q -> cherry-pick Q
```

Q 只包含 selected artifact 相对 run baseline 的产品变化。candidate 的
`results.tsv`、`.gp`、`.tmp` 和实验提交历史不会进入 publication commit。

apply 是显式、可重试动作：dirty source 会失败；cherry-pick 冲突会 abort；成功记录
source revision。相同 promotion marker 已存在时返回既有 apply 结果，不重复提交。

### 7.2 Copy 与 legacy export

`copy` 没有 provider-native prepare/apply capability。Runtime 生成 legacy patch，
`promotion_mode` 必须保持 `artifact_only`。synthetic `git_worktree` source 也没有原
`source_repository` 坐标，因此落到 legacy patch/export，而不是 cherry-pick。

### 7.3 ThinkThread adapter

ThinkThread adapter 已实现 promotion 的最后一段：

```text
BASE FsSnapshot + SELECTED FsSnapshot
                |
       fs.snapshot.diff
                |
  filter edit-surface product paths
  reject .git/.gp/.search/.tmp/results.tsv
  reject symlink and unsupported entry types
                |
       fs.snapshot.patch(BASE)
                |
       PUBLISH FsSnapshot
                |
     fs.apply(BASE, PUBLISH,
              policy=safe_content_v1,
              durable request-id)
```

这段 adapter 已有确定性 request id、分页 diff、snapshot read 和 apply result 处理；
但正常 Runtime 尚不能产生真实 ThinkThread `BASE`/`SELECTED` candidate snapshots，
所以它仍是独立能力，不是可用 E2E。

---

## 8. ThinkThread 目标架构

ThinkThread 接入不应新增一套 Search，也不应把 Git 命令散落进 host adapter。目标是把
同一 Runtime 生命周期映射到 private COW Child：

```text
[TARGET]

SourceImporter
    |
    v
root baseline FsSnapshot (immutable lower)
    |
    +-------------------+-------------------+
    |                   |                   |
    v                   v                   v
private Child A     private Child B     private Child C
private upper       private upper       private upper
    |                   |                   |
    | Host launches worker inside protected execution
    | Runtime checkpoint -> FsSnapshot
    | verifier runs against exact snapshot/derived environment
    | restore -> selected/retained FsSnapshot
    v
settled ArtifactRef(kind=fs_snapshot)
    |
    +--> Shared Plane Evidence
    |        `--> requester-unique snapshot-derived ref Child
    |
    `--> selection -> filtered publish snapshot -> fs.apply to Root
```

必须同时满足以下条件，才可以把 `WorkspaceAccess.confinement` 标成
`protected_execution`：

1. worker 进程真正运行在 private Child，而不是外部 cwd；
2. writable surfaces、路径逃逸、进程和凭据边界经过 capability preflight；
3. verifier 读取 immutable snapshot 或其受保护派生环境；
4. reference Child 只能读取已发布 ArtifactRef，不能获得 sibling authority；
5. snapshot/Child 的 retention、恢复和回收具有持久、可重试语义。

仅调用几个 ThinkThread CLI/MCP 命令、但仍让 worker 在普通宿主目录工作，不算
ThinkThread workspace，也不能宣称 sandbox。

---

## 9. Host × Provider 现状

| Host | Provider | Candidate cwd | Canonical artifact | 当前状态 |
| --- | --- | --- | --- | --- |
| Codex | `git_worktree` | private worktree | `git_commit` | 可用 |
| Pi | `git_worktree` | private worktree | `git_commit` | 可用 |
| Codex | `copy` | independent copy | `git_commit` | 可用，artifact-only |
| Pi | `copy` | independent copy | `git_commit` | 可用，artifact-only |
| Codex | `thinkthread` | private COW Child | `fs_snapshot` | fail closed，未实现 |
| Pi | `thinkthread` | private COW Child | `fs_snapshot` | fail closed，未实现 |

Git worktree 提供并发版本树、精确 commit、回滚和高效 object sharing，不提供强
sandbox。Copy 也只是路径级隔离。ThinkThread 的目标价值是 protected filesystem
execution；在真实 Child host E2E 通过前，三者不能被描述为相同安全等级。

---

## 10. 当前实现与剩余工作

| 能力 | 状态 | 代码语义 |
| --- | --- | --- |
| 默认 `git_worktree + apply` | `[CURRENT]` | WorkspaceSpec 默认值与 Goal Plus completion gate |
| clean Git freeze/create preflight | `[CURRENT]` | whole-repository dirty state fail-fast |
| capacity estimate/fail-fast | `[CURRENT]` | FrozenSpec、monitor、report 共享持久数据 |
| run-local mirror + candidate worktree | `[CURRENT]` | 完整历史、共享 objects、独立 branch |
| provider-neutral ArtifactRef | `[CURRENT]` | Git commit / TT FsSnapshot 类型约束 |
| verifier checkpoint/restore/settlement | `[CURRENT]` Git/Copy | immutable attempt 与可达 ledger history |
| requester-unique reference workspace | `[CURRENT]` Git/Copy | 同一路径切换 settled Evidence artifact |
| provider-native Git promotion/apply | `[CURRENT]` real Git source | squash publication + explicit cherry-pick |
| workspace/apply timing | `[CURRENT]` | provider-neutral durable timing |
| TT filtered publish snapshot/`fs.apply` | `[CURRENT]` adapter | 没有 candidate E2E 输入 |
| SourceImporter / multi-repo manifest | `[TARGET]` | source preparation 尚内聚于 Git provider |
| TT baseline/private Child/checkpoint | `[TARGET]` | materialize/checkpoint/restore 未实现 |
| TT verifier/reference | `[TARGET]` | snapshot-bound execution 与 ref Child 未实现 |
| Codex/Pi protected TT launch | `[TARGET]` | host capability 当前 fail closed |
| 四组合 contract/security campaign | `[TARGET]` | 需要真实 TT 环境验收 |

---

## 11. 非目标

- 不用 ThinkThread 重写 Goal Plus、Search 或 verifier 协议；
- 不把 Host lifecycle 状态塞进 WorkspaceProvider；
- 不废弃 Git，也不把 Git worktree 描述成 sandbox；
- 不让 worker checkout peer commit 到自己的 CurrentWorkspace；
- 不把 candidate 的 `.git`、`results.tsv`、`.gp` 或临时文件 apply 到 source root；
- 不在 TT Child/provider/host E2E 完成前静默 fallback 或宣称 protected execution；
- 不把 adapter mock/unit coverage 当成四组合运行证据。
