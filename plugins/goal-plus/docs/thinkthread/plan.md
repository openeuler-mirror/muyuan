# Goal Plus × ThinkThread 实施与服务器验证计划

> 状态：执行计划；先稳定现有 Git 基线，再完成 ThinkThread 纵向切片。
>
> 计划基线：`master@d5cd419`（2026-08-26）。
>
> 架构定义见 [architecture.md](architecture.md)。

---

## 1. 目标与完成标准

目标不是把现有 `goal-plus + thinkthread` 支持代码原样搬进来，而是在当前 Goal
Plus 上实现一套统一 contract：

```text
Codex / Pi Host
       ×
Git worktree / ThinkThread Workspace Provider
       =
相同的 candidate、checkpoint、verify、reference、selection 语义
```

项目完成时需要同时满足：

- 四种 Host × Provider 组合使用相同 Runtime API；
- worker 的私有修改不会泄漏到 root 或 sibling；
- verifier 始终验证明确 ArtifactRef；
- Shared Plane 可以发布 Evidence，并 checkout 到每个 worker 唯一的完整 `ref-workspace`；
- 中断恢复、promotion 和本地账本行为可解释；
- ThinkThread 路径没有隐式 Git commit 依赖；
- ThinkThread 不可用时明确失败，不静默回退；
- 批量结果包含效率、正确性、隔离性和资源成本数据。

---

## 2. 当前基线

### 2.1 已完成

`db2b161 refactor(goal-plus): add provider-neutral reference workspaces` 已完成第一轮
去 Git 耦合：

- 引入 provider-neutral `ArtifactRef`、`WorkspaceAccess` 和 capability；
- Git/Copy provider 支持 checkpoint、restore 和完整 reference workspace；
- Runtime、持久化记录和 MCP 传递 ArtifactRef；
- Codex/Pi worker 都可使用 `search_reference_open` checkout 唯一 ref-workspace；
- `thinkthread` backend 已注册为显式 unsupported，不会假装运行成功；
- Git-backed source 在 freeze/create 边界要求整个 repository clean，不隐式捕获 dirty 状态；
- 已覆盖 reference 精确性、隔离性、回收、可见性和 Copy/Git parity 测试。

当前分支进一步完成了 Git source/promotion 重构：真实 Git source 使用保留完整历史的
run-local bare mirror 和 worktree，candidate running commits 原样保留；最终另外合成一个
squash publication commit 并通过显式 apply 写回。ThinkThread 已有独立的 filtered publish
snapshot/`fs.apply` adapter，但 baseline、private Child 和 host launch 尚未接通，因此仍不能
把普通 Codex/Pi cwd 当作 ThinkThread candidate。

`d5cd419 fix(goal-plus): separate completion timing from status` 已把“是否完成”与
“预设时间”分开：超过预设时间但实际完成的任务仍是 `complete`，报告描述预设时间
和实际完成收尾时间，不再误判为未完成或失败。

当前测试基线：

- 全量串行测试：`588 passed, 45 skipped, 5 subtests passed`；
- reference/timing 相关聚焦测试：`51 passed, 6 skipped`。

### 2.2 已完成的真实 Circle smoke

| 场景 | 终态 | 实际收尾 | 有效 verify |
| --- | --- | --- | --- |
| Pi，预设 5 分钟 | complete | 6m15s | 2 |
| Pi，不写时间 | complete | 9m33s | 4 |
| Codex，预设 5 分钟 | complete | 约 7m04s | worker 0，parent 1 |

对应运行记录：

- Pi 预设：`gp_0005` / `run_20260826_012805_611d8246`，选择值约
  2.5414213561；
- Pi 无预设：`gp_0006` / `run_20260826_013517_e9923ef3`，选择值约
  2.5414213562，后续收益很小；
- Codex 预设：`gp_0007` / `run_20260826_024254_71d66f5a`，worker 启动晚，
  parent 验证 baseline，promotion patch 为 0 字节。

这些 smoke 证明了新的终态语义和 source integrity，但不证明 Codex Search 已具备
批量生产效率。Codex 运行中 Search 在 Goal 创建约 3m06s 后才创建，两个 native
worker 在约 3m54s/4m03s 后才开始；这会吞掉 5 分钟任务的大部分预算。

### 2.3 当前未完成

- ThinkThread client/provider、baseline、private Child 和 FsSnapshot；
- ThinkThread verifier 与 reference workspace；
- Codex/Pi worker 真正在 ThinkThread private Child 中执行；
- multi-repo SourceImporter；
- 四种组合的大批量性能与隔离验证。

---

## 3. P0：服务器大批量运行前先修正观测基线

这些项不改变统一架构，但如果不先完成，大批量数据会失真。

### P0.1 固化 terminal 时间

问题：Goal 进入 terminal 后，重复 Stop gate 仍可能刷新 `updated_at`。当前某些展示
使用 `updated_at - created_at`，会把真实约 7m04s 的完成时间显示成更晚的 8m+。

实施：

- 新增不可变 `terminal_at` 或等价 terminal event timestamp；
- 第一次进入 `complete/stopped/failed` 时写入，后续幂等调用不得覆盖；
- 报告和卡片统一以 `terminal_at - created_at` 表示实际完成收尾时间；
- `updated_at` 仅表示记录最后一次被触碰，不能再承担完成时间语义。

验收：同一 Goal 在 terminal 后重复 Stop/查询，实际收尾时间保持不变。

### P0.2 结构化预设时间

不要长期依赖从自然语言 prompt 猜“5 分钟”。在 Goal/Search spec 中加入可选的
`preset_duration_seconds`（最终命名以模型为准），prompt 只负责展示任务意图。

验收：

- 有预设时报告“预设时间 + 实际完成收尾时间”；
- 无预设时只报告实际时间；
- 超过预设时间不会自动改变完成状态；
- 旧记录仍可读取。

### P0.3 Codex 冷启动与截止前调度

先定位 Goal intake、Search 创建、native worker 分配之间的分钟级空档，再做最小
修复：

- 在 dispatch 前完成 worker/promotion payload 的类型和必填字段校验；
- 记录 host queue、worker create、worker ready 的独立时间；
- 若剩余预算不足以完成一次最小 verify，不再新开 lane；
- 让短任务尽早进入第一个有效 candidate，而不是在收尾阶段才启动 worker。

验收：5 分钟 Circle 的首个 worker ready 和首个 verifier 不再落到预算末段；若
环境无法达到，则明确得出“该预设不适合当前 Codex 冷启动”的容量结论。

### P0.4 统一批量 telemetry

每个 run 至少记录：

```text
goal_created_at
search_created_at
worker_requested_at
worker_ready_at
first_edit_at
first_checkpoint_at
first_verifier_started_at
first_verifier_finished_at
terminal_at
promotion_finished_at
host / provider / model / runtime version
input tokens / cached tokens / output tokens / processed tokens / cost（可得时）
```

阶段耗时必须从事件计算，不能从日志文本猜。

### P0.5 可复现 preflight

在服务器开始 campaign 前记录并检查：

- Goal Plus commit、插件版本和 MCP server 版本一致；
- Codex/Pi 命令、模型路由和认证可用；
- verifier 依赖和 Circle 示例输入完整；
- Git 用户、磁盘、inode、进程数和并发上限；
- `.gp`/报告输出目录可写且不进入源码提交；
- 单个最小 run 通过后再扩大并发。

---

## 4. Phase 1：冻结 Git/Copy 统一语义

目的：先让已经可运行的路径成为 ThinkThread 的可比较 oracle。

### Phase 1 工作项

1. 为 provider contract 补充契约测试，而不是只测具体实现。
2. 固定 checkpoint 后修改 current，确认旧 ArtifactRef 内容不变。
3. 同一 worker 连续选择多个 reference，确认始终复用一个路径并精确切换 ArtifactRef。
4. 覆盖 worker 崩溃、verifier 失败和 restore 后继续；reference 数量不得随调用增长。
5. 覆盖 staged、unstaged、untracked source 的 fail-fast，以及 ignored/runtime-noise 文件和
   受限 edit surface。
6. 确认 Codex/Pi 生成相同的 Evidence 与 reference tool payload。

### Phase 1 退出条件

- Copy 和 Git provider 通过同一套 contract tests；
- reference 不需要 checkout/reset candidate current；
- Runtime 中新增逻辑不分支判断具体 host；
- Git SHA 只存在 provider/展示边界，不成为 Runtime 的隐式类型。

---

## 5. Phase 2：SourceImporter 与多仓 clean baseline

目的：解决“启动时手工复制 workspace”以及多 repo 输入问题。

### Phase 2 工作项

1. 定义 `SourceSpec -> SourceArtifact` 协议和 manifest。
2. 复用 clean Git fail-fast，并实现 Git provider 的 HEAD 快路径。
3. 实现显式 plain directory 导入；不得把 dirty Git 自动当作 filesystem source。
4. 用一个包含两个 clean 嵌套 Git repo 的 fixture 验证 multi-repo。
5. 明确 ignore、symlink、submodule、超大文件和敏感文件策略。
6. 记录输入 fingerprint，支持重试时确认 baseline 未漂移。

### Phase 2 退出条件

同一 SourceSpec 导入 Git 与 ThinkThread 后，在允许的元数据差异之外具有相同内容；
root source 在整个 run 中保持不变。

---

## 6. Phase 3：ThinkThread provider 最小纵向切片

目的：先跑通一个 candidate 的完整闭环，不先做并行 Search。

### 3.1 Client 与 capability preflight

- 封装 ThinkThread MCP/API client；
- 探测 baseline snapshot、derive/fork、protected exec、snapshot、restore、dispose；
- 记录服务端版本和能力；
- 任一硬依赖缺失时返回明确 unsupported，不 fallback。

### 3.2 Baseline 与 private Child

```text
SourceImporter
    -> root baseline FsSnapshot
    -> derive candidate private COW Child
    -> WorkspaceAccess(cwd, branch_id, provider=thinkthread)
```

Host 必须在这个 Child 的实际执行环境中启动 worker。仅把 `branch_id` 写进 metadata，
但让 worker 留在宿主源码 cwd，不算通过。

### 3.3 Checkpoint、verifier 与 restore

- `checkpoint` 调用 snapshot 能力并返回 `ArtifactRef(fs_snapshot, ...)`；
- verifier 在该 snapshot 或其只读派生环境运行；
- current 在 verify 期间继续变化也不能污染 verifier 输入；
- restore 精确恢复到指定 snapshot；
- dispose 回收 Child，但不破坏仍被 Evidence 引用的 snapshot。

### 最小 E2E

用单 worker Circle：

1. 创建 baseline；
2. 修改受限文件；
3. checkpoint；
4. verify；
5. 再修改并恢复；
6. 确认恢复内容和 score；
7. 导出被选 ArtifactRef；
8. 确认 root source SHA/文件 fingerprint 未变化。

### Phase 3 退出条件

一个 Pi worker 和一个 Codex worker 分别完成该闭环；所有文件访问均发生在
ThinkThread 管理的 private Child 内。

---

## 7. Phase 4：ThinkThread reference 与 Shared Plane

目的：让 peer 产物像 Git 单 worker `ref-workspace` 一样可直接阅读。

### Phase 4 工作项

1. Shared Plane 只发布 verifier 已绑定的 FsSnapshot ArtifactRef。
2. `search_reference_open` 校验 requester 权限，并把指定 snapshot checkout 到该 worker
   唯一的 reference Child。
3. worker 直接用普通文件和命令工具查看 reference 路径；实现修改仍留在 current Child。
4. 再次 open 时复用同一个 reference Child 并切换 snapshot，不累积 reference。
5. 对 reference 的任何写入不得影响 ArtifactRef、owner current 或其他 reference。
6. Evidence 中保留窄 changed paths/summary，作为是否 checkout 完整 reference 的导航。

### 对抗测试

- A 尝试使用 B 的可写 branch id；
- A 尝试打开未发布的 B checkpoint；
- reference 中写文件后 checkout 同一或另一 ArtifactRef；
- owner current 在 reference 打开后继续修改；
- snapshot 被 retention 标记后，已有引用仍可读取；
- 恶意相对路径、绝对路径和符号链接逃逸。

### Phase 4 退出条件

Git 与 TT 的 `search_reference_open` 对 worker 呈现相同的单路径 checkout 语义。

---

## 8. Phase 5：Codex/Pi host parity 与恢复

目的：provider 不再由某一个 host 的 extension 私有控制。

### Phase 5 工作项

- Codex adapter 与 Pi extension 都只接收 `WorkspaceAccess`，不自行创建 snapshot；
- 校验 worker 的真实 cwd/执行环境，拒绝伪造 workspace 字段；
- 统一 worker start、ready、exit、cancel、timeout 事件；
- parent/worker 重启后从 `.gp` 和 ArtifactRef 恢复；
- terminal 后重复 Stop 不改变 `terminal_at`；
- promotion 从选中的 ArtifactRef 导出，不依赖 worker 当前目录；
- 覆盖 host 崩溃、MCP 短暂断线和 orphan Child 清理。

### Phase 5 退出条件

同一 fixture 在 Codex/Pi 上产生等价的 checkpoint lineage、Evidence、selected result
和 promotion 内容；差异只来自模型行为与 host 性能。

---

## 9. Phase 6：四组合服务器 campaign

### 9.1 实验矩阵

先用低并发校准，再扩大规模：

| 维度 | 取值 |
| --- | --- |
| Host | Codex、Pi |
| Provider | git_worktree、thinkthread |
| Prompt | 预设 5 分钟、不写时间 |
| 任务 | Circle + 中等修改 + 多仓 fixture |
| 重复 | 每格至少 5 次校准；稳定后再按服务器容量扩展 |

不要一上来只跑一个大 campaign。先完成 2 × 2 最小单元，确认数据可解释，再增加
并发、任务规模和重复数。

### 9.2 主要指标

正确性：

- verifier pass rate；
- selected artifact 可重放率；
- promotion 内容一致率；
- root/sibling 泄漏次数；
- reference 精确性和授权失败次数；
- 恢复成功率。

效率：

- source import、workspace create、checkpoint、restore、reference checkout 延迟；
- Goal 到 Search、worker ready、首个 verifier、terminal 的时间；
- 有效 verify 次数与单位时间 score 改善；
- 磁盘增量、inode、峰值进程、CPU/内存和网络；
- token、processed token 和成本（host 可提供时）。

### 9.3 报告必须区分

- task `complete/failed/stopped`；
- 是否超过预设时间；
- 实际 terminal 时间；
- 是否产生有效 worker checkpoint/verifier；
- 是否只是 parent 验证 baseline；
- source integrity 是否通过；
- 功能正确但性能不达标，与功能失败不能混为一类。

### 9.4 Campaign gate

满足以下条件才扩大并发：

1. 连续最小 E2E 没有 root/sibling 写入；
2. terminal 时间不会被后续事件改写；
3. selected ArtifactRef 可独立重放；
4. orphan workspace/reference 可检测并回收；
5. 磁盘和 snapshot retention 有明确上限；
6. Codex/Pi 都能在目标 provider 上产生至少一次真实 worker verifier。

---

## 10. 测试层次

| 层次 | 范围 | 必须验证 |
| --- | --- | --- |
| Unit | ArtifactRef、capability、路径校验、terminal 时间 | 类型、幂等、拒绝非法状态 |
| Contract | Copy/Git/TT provider | checkpoint、restore、reference、dispose 同语义 |
| Integration | Runtime + provider + verifier | 精确快照、账本、Evidence、selection |
| Host E2E | Codex/Pi × provider | 真实 cwd、worker 生命周期、promotion |
| Security | TT protected exec/reference | 路径逃逸、sibling authority、root 泄漏 |
| Performance | 服务器 campaign | 冷启动、增量存储、并发扩展和成本 |

CI 中不具备 ThinkThread 服务时，TT 集成测试必须明确 skip 并给出原因；不能用 Copy
provider 冒充通过。服务器上的 protected E2E 结果应保留 runtime/provider 版本和
产物 fingerprint。

---

## 11. 推荐提交顺序

每个提交都保持可回滚、测试可解释：

1. `fix(goal-plus): persist immutable terminal timing`
2. `feat(goal-plus): add structured preset duration and batch telemetry`
3. `feat(goal-plus): add source importer manifests and multi-repo fixtures`
4. `feat(goal-plus): implement thinkthread workspace checkpoints`
5. `feat(goal-plus): add thinkthread reference workspaces`
6. `feat(goal-plus): run codex and pi inside thinkthread workspaces`
7. `test(goal-plus): add four-cell workspace provider campaign`

如果 ThinkThread 真实 API 与本架构假设不同，先只调整 provider contract 和本文件，
不要把差异泄漏到 Search 状态机或分别塞进 Codex/Pi adapter。

---

## 12. 风险与停止条件

遇到以下情况时停止扩大批量，先回到最小复现：

- worker 实际 cwd 不在声明的 private Child；
- verifier 读取的是可变 current，而非 checkpoint；
- root、sibling 或 source fingerprint 发生非预期变化；
- reference 能获得 sibling 的写 authority；
- terminal/阶段时间无法由稳定事件重建；
- ThinkThread 断线后 Runtime 静默切换 backend；
- snapshot 无法可靠回收或磁盘增量没有上界；
- Codex/Pi payload 错误在数分钟后才暴露，导致大批量空转。

功能与安全 gate 通过后，才比较 Git 与 ThinkThread 的运行效率。否则“更快”可能只是
少做了 checkpoint、verify 或隔离，并不是同一系统的公平比较。
