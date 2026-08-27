# Goal Plus × ThinkThread 统一 Workspace 架构

> 状态：设计目标；Git/Copy 基础设施已落地，ThinkThread provider 尚未实现。
>
> 代码基线：`master@d5cd419`（2026-08-26）。
>
> 相关文档：[Shared Plane](../shared-plane.md) ·
> [Host Adapters](../agent-host-adapters.md) ·
> [实施与服务器验证计划](plan.md)

---

## 1. 结论

Goal Plus 不应把 Git worktree 当成唯一执行模型，也不应为 ThinkThread
另写一套 Search。统一后的核心是一个 **Workspace Provider 协议**：Goal Plus
只依赖“创建私有 workspace、生成不可变 checkpoint、恢复、打开只读参考副本”
这些能力，Git 和 ThinkThread 分别实现它。

两条后端的规范产物不同：

- Git：规范产物是 commit；私有 workspace 是 git worktree。
- ThinkThread：规范产物是 FsSnapshot；私有 workspace 是从共同 baseline
  派生的 COW Child。

Shared Plane 共享的是不可变产物引用、窄 Evidence 和 lineage，不共享其他
worker 的可写 workspace。需要理解较大修改时，worker 打开一个完整、只读、
每个 worker 唯一的 `ref-workspace` 中自己浏览，不再把巨型 diff 塞进上下文。

本设计同时服务 Codex 和 Pi；Host 选择与 Workspace Provider 选择相互独立。

---

## 2. 统一架构图

```text
                         Goal Plus Control Plane
                 Goal / FrozenSpec / Search / Selection
                                   |
                  +----------------+----------------+
                  |                                 |
             Host Adapter                    Workspace Provider
         (谁启动和看护 worker)             (worker 在哪里修改文件)
                  |                                 |
          +-------+-------+                 +-------+--------+
          |               |                 |                |
       Codex Host       Pi Host          Git Provider    TT Provider
          |               |                 |                |
          +-------+-------+                 |       baseline FsSnapshot
                  |                         |                |
            Candidate Worker          private worktree   private COW Child
                  |                         |                |
                  +-------------------------+----------------+
                                            |
                                  verifier 只验证私有状态
                                            |
                                  immutable ArtifactRef
                                  commit / FsSnapshot
                                            |
                         +------------------+------------------+
                         |                                     |
                   Shared Plane                       Reference Workspace
             Evidence + lineage + score        单 worker ref-workspace 完整参考视图
                         |                                     |
                         +---------------其他 worker-----------+
```

四种组合必须遵守同一上层语义：

| Host | Provider | 私有执行空间 | 规范 checkpoint |
| --- | --- | --- | --- |
| Codex | Git | git worktree | commit |
| Pi | Git | git worktree | commit |
| Codex | ThinkThread | private COW Child | FsSnapshot |
| Pi | ThinkThread | private COW Child | FsSnapshot |

当前只有 Git/Copy 路径可用。`thinkthread` 已作为显式 backend 注册，但会返回
unsupported，而不是悄悄降级到 copy 或 Git。

---

## 3. 必须分开的四个对象

### 3.1 CurrentWorkspace

worker 当前可以读写的私有工作目录。它是“正在进行的修改”，不是可分享产物。

### 3.2 ArtifactRef

某次 checkpoint 的稳定标识，统一表示为 `kind + id + provider`：

```text
Git:         { kind: "git_commit", id: "<sha>", provider: "git_worktree" }
ThinkThread:
{ kind: "fs_snapshot", id: "<snapshot_id>", provider: "thinkthread" }
```

ArtifactRef 是 verifier、候选结果、Shared Plane 和最终选择之间传递的规范引用。
业务层不能假设 `id` 一定是 Git SHA。

### 3.3 ReferenceWorkspace

每个 loop worker 拥有一个完整参考目录。例如：

```text
current/             # candidate 自己的可写 workspace
ref-workspace/       # 当前选中的 ArtifactRef checkout
```

它解决的是“大改动无法靠一个 diff 理解”的问题。diff 仍可作为导航和窄证据，
但不再承担完整代码传递。worker 选择另一条 Evidence 时，provider 把同一个
`ref-workspace` checkout 到新的 ArtifactRef，不创建第二个参考目录。

ReferenceWorkspace 必须满足：

- 精确：内容对应指定 ArtifactRef，而不是 peer 当前状态。
- 隔离：修改 reference 不影响原产物、candidate 或其他 reference。
- 有界：每个 worker 最多一个 reference，生命周期由 Runtime 管理，不要求 Agent close。
- 有授权：worker 只能打开 Shared Plane 明确暴露的产物。

### 3.4 LocalVcs

workspace 内部可选的本地版本控制。它用于 worker 自己回滚、查看历史或记录
多次尝试，不等同于 sandbox，也不必成为跨 worker 交换协议。

因此 ThinkThread workspace 内可以保留现有 `.git` 和 `results.tsv`：

- FsSnapshot 管整个 workspace 的隔离、checkpoint 和恢复。
- 内嵌 Git 继续做项目自己的版本历史。
- `results.tsv` 继续做 Goal Plus 的本地实验账本。

三者职责不同，可以同时存在。

---

## 4. COW、lower 与真正的树

COW 是 Copy-on-Write。多个 Child 初始读取同一个只读 baseline；只有发生修改的
文件或数据块才写入各自的私有层。

```text
                         root baseline snapshot
                         (shared, immutable lower)
                          /          |          \
                         /           |           \
                  child-A         child-B       child-C
                private upper    private upper private upper
                    |                 |             |
                 snapshot A       snapshot B    snapshot C
                    |
                 child-A1
```

这里的 `lower` 不是 Goal Plus 的一个业务目录，也不是可以让 worker 修改的
branch。它是 COW 文件系统共同读取的只读底层内容；每个 Child 的私有变化写在
自己的 upper/private layer 中。运行时看到的是 lower 与自己 private layer
合成后的完整目录。

ThinkThread 已有 fork/derive 能力时，应从 root baseline 派生真正的 Child，
后续 checkpoint 也保持这条 lineage。不能只创建一个 branch id，却让 agent
继续在外部 cwd 修改真实仓库；那只是“记录了分支”，没有得到执行隔离。

---

## 5. 控制平面职责

### 5.1 Goal Plus Runtime

Runtime 继续拥有：

- Goal、FrozenSpec 和 Search 生命周期；
- candidate/iteration 状态机；
- verifier 调度、结果判定和选择；
- `.gp` 状态、`results.tsv`、报告和 promotion；
- Shared Plane 中 Evidence、ArtifactRef 和 lineage 的发布规则。

Runtime 不直接写死 Git 命令或 ThinkThread MCP 调用；这些进入 provider。

### 5.2 Host Adapter

Host Adapter 继续拥有：

- Codex/Pi worker 的启动、停止和并发控制；
- 把 Goal Plus 生成的 worker contract 注入对应 host；
- 收集 host 日志、退出状态和运行时指标；
- 确保 worker 的真实 cwd 指向 provider 分配的 CurrentWorkspace。

Host 不决定 checkpoint 的格式，也不直接拼接 peer diff。

### 5.3 Workspace Provider

Provider 至少提供这些能力：

```text
prepare_source(source) -> SourceArtifact
create_workspace(baseline, owner) -> WorkspaceAccess
checkpoint(workspace, label) -> ArtifactRef
restore(workspace, artifact) -> None
checkout_reference(requester, artifact) -> ReferenceWorkspace
dispose_workspace(workspace) -> None
capabilities() -> WorkspaceCapabilities
```

当前代码中的 `WorkspaceProvider`、`WorkspaceAccess`、`ArtifactRef` 和 capability
检查是这层抽象的起点。不同 provider 可以有不同内部字段，但 Runtime 只能依赖
统一语义。

---

## 6. SourceImporter：启动 workspace

“源代码在哪里”与“candidate 使用哪种隔离后端”也要解耦。

```text
source input
    |
    +-- clean Git repo ------> 复用提交对象或高效导入
    |
    +-- dirty Git repo ------> 捕获 tracked + untracked 的精确输入状态
    |
    +-- plain directory -----> 文件系统导入
    |
    +-- multi-repo tree -----> 作为一个受管 workspace 整体捕获
                                   |
                              baseline ArtifactRef
```

Git provider 可对 clean Git repo 使用原生快路径。ThinkThread provider 则为整个
受管 workspace 创建 baseline FsSnapshot；嵌套仓库的 `.git`、配置和多个源码树
只是这个快照里的文件。这样多仓任务不需要强行合成一个 Git 仓库。

源导入必须明确忽略项、符号链接、超大文件和密钥策略。它不能用一次不透明的
手工 copy 作为长期启动协议。

---

## 7. 单个 candidate 的运行流程

```text
1. SourceImporter 生成共同 baseline
2. Provider 从 baseline 创建 candidate 私有 CurrentWorkspace
3. Host 在该 workspace 中启动 worker
4. worker 修改文件，并可使用自己的 LocalVcs / results.tsv
5. worker 请求 verify
6. Runtime 检查 edit surface
7. Provider 生成 pre-ver-N ArtifactRef
8. verifier 针对该精确 ArtifactRef 运行
9. Runtime 记录 score、Evidence、lineage 和 verifier 结果
10. 结果合格时发布到 Shared Plane；失败时保留诊断或恢复上一 checkpoint
11. worker 可把 peer ArtifactRef checkout 到自己的唯一 ref-workspace 阅读并继续自己的实现
12. 最终选择一个 ArtifactRef，再执行 promotion/export
```

### 7.1 Git provider 对应行为

```text
private worktree
    -> checkpoint: commit
    -> verify: commit 对应的精确树
    -> reference: detached temporary worktree
    -> restore: reset/checkout provider 管理的私有 worktree
```

worker 不应 checkout peer commit 到自己的 CurrentWorkspace。那会把对话上下文中
的“我正在实现 A”与磁盘上的“已经切成 B”混在一起。正确方式是保留 current，
把每个 worker 唯一的 `ref-workspace` 切到目标 ArtifactRef。

### 7.2 ThinkThread provider 对应行为

```text
private COW Child
    -> checkpoint: fs.branch.snapshot(child_id)
    -> verify: 在 snapshot 派生的受保护执行环境中运行
    -> reference: 从指定 FsSnapshot 派生只读/可丢弃 reference Child
    -> restore: 将 candidate Child 恢复到指定 FsSnapshot
```

`fs.snapshot.create`/`fs.branch.snapshot` 在架构上是 provider 使用的 ThinkThread
能力，不是“main agent 派生 subagent 就自动触发”的 Goal Plus 隐式规则。
Runtime 在 baseline、pre-ver-N、保留候选等明确生命周期点调用 provider；
Pi extension 或 Codex adapter 不应各自写死一份快照逻辑。

---

## 8. Shared Plane 与 peer 反馈

Shared Plane 发布的是一个可验证的候选事实：

```text
CandidateEvidence
├── candidate_id / iteration_id
├── artifact_ref
├── parent_artifact_ref
├── verifier summary / score / status
├── changed paths and narrow evidence
└── reference_available
```

subagent 先通过 Goal Plus MCP 得到 Evidence；当窄 Evidence 不足时，用一个工具把完整
参考树 checkout 到当前 worker 的稳定 reference 路径：

```text
search_reference_open
```

Git provider 把调用映射到 detached worktree，ThinkThread provider 映射到每个 worker
唯一的 FsSnapshot reference Child。worker 直接用普通文件和命令工具查看返回路径；再次
调用时同一 reference 被切到新的 ArtifactRef。worker 不需要 fetch peer workspace，也不
拥有 sibling 的可写 branch authority。

“无 sibling branch authority”具体是：A 可以读取 Shared Plane 授权的 B 的不可变
产物，但不能继续写 B 的 Child、移动 B 的 head、替 B checkpoint，或读取 B 尚未
发布的工作目录。读产物与控制别人的执行分支必须分开。

---

## 9. verifier、账本与回滚

一次 verify 必须绑定一个不可变 checkpoint，而不是 verifier 启动瞬间碰巧看到的
可变目录。建议顺序是：

```text
edit-surface check
    -> provider.checkpoint(pre-ver-N)
    -> verifier(checkpoint)
    -> record result in .gp / results.tsv
    -> publish Evidence when eligible
    -> retain, restore, or discard according to policy
```

`results.tsv` 是实验历史，不是跨 provider 的 canonical artifact。它通常与源码一起
被 checkpoint，因此回滚到旧 ArtifactRef 时也会看到当时的账本版本；如果策略需要
保留“回滚后仍不断增长的全局历史”，应由 `.gp` 控制面另外保存，不能靠在 candidate
目录中事后重新拼账本。

---

## 10. 安全边界

Git worktree 解决的是并发版本树和高效共享对象，不是 sandbox：

- worker 仍可能通过绝对路径访问父仓库或其他本地目录；
- 进程、网络、凭据和系统调用没有因此被限制；
- Git 元数据和 sibling commit 也不等于细粒度授权。

ThinkThread 的价值之一是把“看到一个 Child id”升级为真正的文件系统执行边界。
但只有以下条件同时成立时，Goal Plus 才能声明 filesystem confinement：

1. worker 进程本身在 private COW Child 内启动；
2. cwd 和文件访问都由该执行环境约束；
3. verifier 也针对不可变 snapshot 或其派生环境运行；
4. host 不能通过未受控外部路径绕开边界；
5. provider preflight 已验证当前系统支持这些能力。

因此，“Codex 在普通宿主 cwd 运行，然后远程调用几个 ThinkThread MCP tool”不能
算 sandbox。当前 ThinkThread provider 尚未实现，上述 Codex confinement 也是明确
TODO，而不是现有能力。

---

## 11. 当前实现与目标差距

| 能力 | 当前状态 | 目标 |
| --- | --- | --- |
| Provider-neutral `ArtifactRef` | 已实现 | 继续保持业务层不假设 Git SHA |
| Git/Copy checkpoint、restore | 已实现 | 作为统一语义基线 |
| Git/Copy 单 worker `ref-workspace` | 已实现 | 保持精确、隔离、数量有界 |
| 统一 reference checkout MCP | 已实现 | Git/TT 同名同语义 |
| ThinkThread backend 选择 | 已注册，显式 unsupported | 实现真实 provider，不静默 fallback |
| ThinkThread baseline/Child/checkpoint | 未实现 | 完成 COW lineage 与 FsSnapshot |
| ThinkThread verifier execution | 未实现 | verifier 绑定不可变 snapshot |
| Codex/Pi × ThinkThread host 接入 | 未实现 | worker 真正在 private Child 中运行 |
| TT reference workspace | 未实现 | 从已发布 snapshot 切换每个 worker 唯一的 reference Child |
| 多仓 SourceImporter | 未实现 | 精确、可复现地导入完整 workspace |
| 四组合规模化验证 | 未完成 | 用同一指标矩阵验收 |

具体实施顺序和服务器批量实验见 [plan.md](plan.md)。

---

## 12. 非目标与待决策项

本轮不做：

- 用 ThinkThread 重写 Goal Plus 状态机、Search 或 verifier 协议；
- 废弃 Git；
- 强迫 TT workspace 内每次 verify 都执行 `git add && git commit`；
- 用巨型 diff 代替完整 reference workspace；
- 把 Shared Plane 变成 peer workspace 的远程写接口；
- 在 TT provider 完成前宣称 Codex 已获得 sandbox 隔离。

实现前仍需确认：

- ThinkThread 服务端的 snapshot/derive/只读 reference/销毁 API 及错误语义；
- Linux 执行环境、进程和网络边界的实际保证；
- baseline 与 reference snapshot 的保留、配额和垃圾回收策略；
- multi-repo、符号链接、超大文件和敏感文件的导入规则；
- provider 能力协商与版本兼容策略。
