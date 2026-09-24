# Goal Plus Submodule Workspace 支持方案

状态：**实施中；以下标为当前实现的部分已接入，递归发布与恢复仍按后续阶段实现**
审计基线：`muyuan@270111af4b64`，2026-09-23

### 当前实现切片

当前版本已经接入以下准入和物化行为：

- Git freeze 前检查 parent 与递归 formal submodule 的 checkout、HEAD、gitlink、
  `.gitmodules`、对象可用性、浅仓库、merge/rebase 状态和 dirty 状态；失败使用
  `Submodule*` 类型，包含 parent-relative 路径、阶段、原因以及 expected/actual。
- Git candidate worktree 执行 `submodule update --init --recursive`，随后重复递归检查；
  未初始化 child、缺对象或拓扑不一致不会继续到 verifier。
- ThinkThread 只在 source 对应 Git root 的 committed tree 含 `.gitmodules`/gitlink，或
  工作树含嵌套 `.git` 标记时拒绝，并返回 `ThinkThreadSubmoduleUnsupported` 及路径；root
  自身 `.git` 不触发拒绝。普通无 Git 目录和没有子模块的 ThinkThread Git root 保持原路径。
- 离线 fixture 位于 `examples/submodule-e2e/`；真实 Pi + ThinkThread 双 module apply
  fixture 位于 `examples/two-modules-thinkthread-apply/`。

当前切片尚未声称递归 child-first publication、跨仓库 apply 原子性或保留对象闭包恢复；
这些仍由本文后续生命周期方案约束，并需要独立行为测试后再标记为已实现。

本文固定 Goal Plus 的最小支持合同：workspace 根由 Goal Plus workspace provider 管理一套
parent Git；workspace 中参与组合的其他 Git root 必须沿 parent 链路登记为标准 submodule。
submodule 可以有任意有限层级。目标是把同一份 parent + child 版本组合正确地递归物化、
验证、恢复和发布。

## 1. 支持合同

### 1.1 Git workspace

Goal Plus 只保证以下拓扑：

```text
workspace/                         # Goal Plus parent Git
|-- .git/
|-- .gitmodules
`-- modules/
    |-- repo-a/                     # formal submodule
    |   `-- .git                  # gitfile or managed git directory
    `-- repo-b/                     # formal submodule
        `-- .git
```

约束：

- parent 是 workspace 的 Git root；
- parent commit 中的 `160000` gitlink 与 `.gitmodules` 一一对应；
- 每个 child 都有独立 repository identity、HEAD 和 object store；
- parent gitlink 指向的 child commit 必须可读取、可 checkout、可重建；
- 支持任意有限层级的标准 submodule；每一层的 parent 都必须用 `.gitmodules` 和 gitlink
  登记下一层；
- 不支持未登记嵌套 Git、循环拓扑、路径冲突、LFS 外部对象或 partial/shallow 缺对象；
- workspace 中其他形式的 `.git` 不在保证范围内，不自动当成普通目录，也不保证 checkout
  或 publication 的正确性；
- Goal Plus 不替用户创建、转换、修复或提交 submodule。submodule 是否已经准备好由
  workspace 的创建流程负责；Goal Plus 从实际 workspace 状态开始做准入检查。

如果 workspace 没有 child，按 parent-only workspace 处理。只要出现 child，就必须满足
正式 submodule 合同；子模块再包含子模块时，按同一规则递归检查。

### 1.2 ThinkThread workspace

ThinkThread 不走 parent/child Git 提交流程。workspace 的外层身份是 exact `fs_snapshot`：

- snapshot 是 verifier、selection、restore 和 publication 的输入身份；
- inner agent 的本地 Git history 不能代替 snapshot identity；
- `fs.apply` 后重新读取 Root 的实际文件结果；
- 不对 snapshot 中嵌套 Git 的 checkout、gitlink 或 Git object closure 单独承诺；
- snapshot provider 无法保留并复验完整工作区时，返回明确的
  `ThinkThreadSubmoduleUnsupported`。

## 2. 组合身份

Git artifact 的身份由 parent commit 和每个 child commit 共同构成：

```text
CombinationArtifact
  parent_root: workspace
  parent_commit: P1
  children:
    modules/repo-a -> repository_identity A -> commit A1
      modules/plugin -> repository_identity AP -> commit AP1
    modules/repo-b -> repository_identity B -> commit B1
  topology_digest: hash(.gitmodules + ordered gitlinks)
  object_closure: P1 + A1 + AP1 + B1 + verifier inputs
```

`.gitmodules` 和 parent gitlink 是拓扑真相。额外 manifest 只能作为索引，不能覆盖 commit
中记录的路径、来源或 commit。只有 parent、child 和必要对象全部可用，artifact 才可声明
可重建。

## 3. Git Runtime 生命周期

### 3.1 Admission / freeze

只读 topology inspector 检查：

```text
SubmoduleNode(
  path,
  url,
  repository_identity,
  gitlink,
  child_head,
  status,
  object_available,
)
SubmoduleTopology(parent_root, parent_head, nodes, depth, complete)
SubmoduleCheckResult(passed, failure_class, node, expected, actual, details)
```

准入条件：

1. 真实 Git root 是 Goal Plus workspace parent；
2. 每一层 parent tree 中的 gitlink 与该层 `.gitmodules` 一致；
3. 每个 child checkout 已初始化，且 child HEAD 等于对应 parent gitlink；
4. parent 和所有 child 状态可读取，必需对象可取得；
5. 没有 merge/rebase、dirty child、缺对象、未登记嵌套 Git、循环拓扑或路径冲突。

失败时报告 parent-relative child 路径、expected/actual 和失败阶段。不得把 formal submodule
扁平化成普通目录，也不得静默跳过缺失 child。

### 3.2 Candidate materialization

- parent 使用 run-owned mirror 和独立 candidate worktree；
- 每个 child 使用独立 clone 或经过证明隔离的 child worktree，并递归处理 child 自己的
  `.gitmodules` 和 gitlink；
- candidate、source、reference 的每层 parent/child index、HEAD、工作树和可写对象不共享；
- materialization 按每一层 parent gitlink 的精确 commit 递归执行，不跟随远端最新分支；
- 缺失 child 或对象不可得时准入失败，不能把空目录当作成功；
- capacity estimate 包含 parent、所有 child checkout、mirror、对象和并发 candidate；
- verifier 必须使用 candidate child，不能读取 source checkout 的 editable install、构建
  产物或共享可写缓存。

### 3.3 Child-first checkpoint / MCP verify

可信 checkpoint 顺序固定为：

1. 停止 worker 写入，读取 parent 和所有 child 的真实状态；
2. 任一 child dirty 时返回 `SubmoduleWorkspaceDirty`，不运行 verifier，不写 Evidence；
3. 从最深层 child 向 workspace root，worker 在每个发生变化的 repository 内完成允许的
   `add/commit`；
4. Runtime 从最深层向上逐层 stage 对应 gitlink 并提交各层 parent，直到 workspace parent
   得到 P1；
5. 递归检查每层 gitlink 等于 child HEAD，所有 parent/child 均 clean，object closure 完整；
6. 只有组合检查通过，才运行 frozen verifier 并生成 Evidence/iteration。

Runtime 不替 worker 自动 commit child，也不为 source 制造隐藏提交。child 已提交但某层
parent 仍指向旧 gitlink 时，Runtime 可以在 candidate checkpoint 中按深度完成各层 gitlink；
任一层的指针、checkout 或对象不一致时返回 typed error。

### 3.4 changed-files / edit surface

任一 parent diff 只看到下一层 gitlink 变化，不能作为完整产品 diff。对每个 child 递归展开
`base_commit..target_commit`，在每一层加上 parent-relative 前缀：

```text
modules/repo-a/src/value.py
modules/repo-a/modules/plugin/src/value.py
modules/repo-b/src/value.py
```

allow/deny、Annotation、reference view 和 protected input 检查都使用展开后的路径和固定
child commit 内容。child 中未授权文件、其他 child、`.gitmodules` 或拓扑变化不能通过
parent 的一行 gitlink 绕过检查。

### 3.5 Restore / selection / retention

- discard/failure 递归恢复整个 parent + child 组合，不只切回 workspace parent HEAD；
- Evidence、best、reference、pending apply 都 pin parent + child object closure；
- 删除 candidate、关闭原来源或执行受控 GC 后，保留 artifact 仍能在新目录离线重建；
- 不可重建时明确标记 `removed`/`unavailable`。

### 3.6 Publication / apply

publication 从最深层开始分别准备变化 child 的 product-only commit，再逐层构造引用这些
child commit 的 parent publication，直到 workspace parent。最终对实际 publication 组合重新
运行 promotion verifier。

apply 前检查 source parent/child 的版本、dirty 状态、merge/rebase 状态和 baseline。先准备
全部 child 对象，再从最深层向上更新 child 和 parent，最后递归读取每层 gitlink、每个 child
HEAD、产品文件并执行组合 verifier。workspace parent cherry-pick 或任一层 gitlink 更新
成功，不能单独报告 `applied`。

多个 Git 的写入不是原子操作。运行记录保存 operation identity、期望旧值、新值和逐仓阶段，
响应丢失时先查询实际 refs，再决定是否续做，不能重复副作用。

## 4. ThinkThread Runtime

ThinkThread adapter 的最小能力合同：

```text
thinkthread.submodule_git_snapshot = {
  exact_git_metadata: true,
  child_object_closure: true,
  snapshot_git_commands: true,
  apply_child_checkout: true,
}
```

如果不能证明这些能力，返回 `ThinkThreadSubmoduleUnsupported`。能力通过后，Root controller
在 Child quiescent 后捕获 exact snapshot，运行 parent/child 检查和 verifier，再将结果写回
同一 retained Child。`fs.apply` 后重新检查 Root 实际文件结果；snapshot 不得被 inner Git
commit 冒充。

## 5. E2E 示例：`examples/submodule-e2e/`

### 5.1 目标

示例完全离线、可重复生成，使用本地 child repos 和一个 Goal Plus parent workspace，
不触碰用户当前 Git workspace。它必须证明：

- parent `.git` + 递归正式 submodule 树的 materialization；
- child commit → parent gitlink commit 的顺序；
- MCP verify 在 dirty child、gitlink mismatch、缺 object 时 fail-closed；
- exact verifier 从固定组合得到 `2 + 20 = 22`；
- discard/restore、selection、publication/apply 和最终组合复验；
- 删除 candidate 后仍可用 run-owned object closure 重建。

### 5.2 目录约定

```text
plugins/goal-plus/examples/submodule-e2e/
|-- README.md
|-- prepare_workspace.py       # 创建 child remotes、parent P0、A0/B0/AP0
|-- run_e2e.py                 # 执行完整 provider/runtime 流程并断言结果
|-- search-spec.json
|-- verifier.py                # 读取两个 child，断言 2 + 20 == 22
`-- expected/
    |-- normal.json
    `-- failures.json
```

脚本使用临时目录和本地 bare remotes，结束时清理。parent fixture：

```text
parent/
|-- .gitmodules
`-- modules/
    |-- repo-a/                 # A0 -> A1
    |   `-- modules/plugin/     # AP0，递归 child
    `-- repo-b/                 # B0 -> B1
```

### 5.3 正常路径

1. 创建 child A0/B0、A 内的递归 child AP0 和 parent P0，初始化正式 submodule 树；
2. materialize candidate，递归核对 P0、A0、AP0、B0 和 object closure；
3. 修改 A 返回 2、B 返回 20，在 child 内分别提交 A1/B1，保持 AP0 不变；
4. 调用 MCP verify checkpoint，Runtime 从最深层向上检查并创建指向 A1/B1/AP0 的 parent P1；
5. 从 P1 + A1 + AP0 + B1 的 exact view 运行 verifier，生成 Evidence；
6. select 后生成 child publication 和 parent publication；
7. promotion verifier 重新得到 22；
8. apply 并重新读取 parent gitlinks、child HEAD 和 verifier 结果；
9. 删除 candidate，使用保留对象重新 materialize，重复组合检查。

### 5.4 必测失败路径

| 场景 | 期望 |
| --- | --- |
| A 只改文件，未 commit | `SubmoduleWorkspaceDirty`，无 Evidence |
| parent gitlink 仍为 A0，child HEAD 为 A1 | `SubmoduleHeadMismatch`，无 settled iteration |
| child 目录未初始化 | 准入失败；不得把空目录视为成功 |
| parent 指向 A1，但 A1 对象不在 run-owned closure | 不启动 verifier |
| verifier 后 child 再次 dirty | 不污染既有 Evidence，进入 recovery |
| restore 只恢复 parent，未同步 child | restore 失败，不报告成功 |
| apply 只更新 parent | `applied` 不成立 |
| 未登记嵌套 Git、循环拓扑或非标准 child | 明确 unsupported，不保证 checkout 正确性 |

### 5.5 验收命令

示例完成后只运行定向命令：

```bash
python plugins/goal-plus/examples/submodule-e2e/prepare_workspace.py
python plugins/goal-plus/examples/submodule-e2e/run_e2e.py
python -m pytest -q plugins/goal-plus/tests/test_workspace_backends.py -k submodule
python -m pytest -q plugins/goal-plus/tests/test_runtime_unit.py -k submodule
python -m pytest -q plugins/goal-plus/tests/test_tools.py -k verifier
python -m pytest -q plugins/goal-plus/tests/test_thinkthread_verification.py -k submodule
```

不运行全量 pytest。E2E 输出必须保存 parent/child SHA、topology digest、verifier result、
failure assertions 和重建结果。

## 6. 实施顺序与退出条件

1. D0：审计当前 provider、Runtime、MCP verify、publication、restore 和 ThinkThread adapter；
2. D1：实现只读 topology inspector、typed errors 和 child object closure；
3. D2：接入 child materialization、child-first checkpoint 和 expanded diff；
4. D3：接入 exact verifier、restore、selection、retention 和 publication/apply；
5. D4：完成 `examples/submodule-e2e/`，再做 ThinkThread capability gate 和 ST；
6. D5：只在 focused tests、E2E 和必要的 native ThinkThread ST 都有证据后宣布支持。

退出条件：

- parent-only workspace 和标准 direct-submodule workspace 都有明确行为；
- dirty child、gitlink mismatch、缺 object、非标准嵌套 Git 都 fail-closed；
- parent + child artifact 可离线重建；
- `examples/submodule-e2e/` 正常路径与失败路径实际跑通；
- ThinkThread 明确标记 `enabled` 或 `unsupported`，没有模糊的“应该支持”。
