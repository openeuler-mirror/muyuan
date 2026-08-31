# Goal Plus (GP)

[English](README.md) | 简体中文

Goal Plus 是面向长时间 agent 任务的宿主中立运行时。在 Pi 中使用
`/goal-plus`，在 Codex 中使用 `$goal-plus`。它可以直接处理普通目标；遇到可度量的
优化任务时，会升级到 Search Mode：冻结评价合同、在隔离候选中探索，并提升经过
verifier 验证的最佳结果。

Pi 和 Codex 是当前支持的宿主路径。

## 安装

### 用户安装

从当前 GitCode 仓库取得 Muyuan，并进入 Goal Plus 源码目录：

```bash
git clone git@gitcode.com:yiyanzhi_akane1/muyuan.git
cd muyuan/plugins/goal-plus
python -m pip install .
```

这会把 Goal Plus 命令安装到当前本地 Python 环境。如果这个环境已经能找到
`goal-plus`，就不需要重复安装 runtime。

使用 Pi 自己的命令安装或卸载 Pi package：

```bash
pi install .
pi remove .
```

使用 Codex 自己的命令安装或卸载 Codex plugin：

```bash
codex plugin marketplace add .
codex plugin add goal-plus@goal-plus

codex plugin remove goal-plus@goal-plus
codex plugin marketplace remove goal-plus
```

所有宿主都卸载后，如有需要再从该 Python 环境删除 Goal Plus：

```bash
python -m pip uninstall -y goal-plus
```

任一宿主仍在使用 Goal Plus 时都要保留这个源码目录：Pi 从这里加载本地 package，
Codex 也把它作为已配置的 marketplace 来源。

Codex 首次安装后，新建线程，在 `/hooks` 中检查并信任 Goal Plus，然后再新建一个
线程。验证命令：

```bash
codex plugin list
goal-plus-codex-doctor --project-root .
```

### 开发态 checkout

需要 editable 开发时，进入同一个 plugin 目录，并安装 checkout 和开发依赖：

```bash
cd muyuan/plugins/goal-plus
python -m pip install -e ".[dev]"
```

Pi 直接安装当前 checkout：

```bash
pi install .
pi remove .
```

编辑 Codex plugin 资产时使用本地刷新脚本：

```bash
python scripts/install_codex_plugin.py --skip-runtime-install
python scripts/install_codex_plugin.py --uninstall --keep-runtime
```

两个开发态宿主都不再使用 editable runtime 后，再运行
`python -m pip uninstall -y goal-plus`。详细开发和更新机制见
[Pi](docs/pi.md) 与 [Codex](docs/codex.md)。

在 Pi 中启动目标：

```text
/goal-plus 修复这个 bug 并验证测试套件。
/goal-plus 在不改变正确性的前提下，用两小时优化 p95 延迟。
/goal-plus mode=probe 先确认向量化是否可行。
/goal-plus mode=autonomous 深度优化这个 kernel。
```

Codex 使用已安装的 skill 入口：

```text
$goal-plus 修复这个 bug 并验证测试套件。
$goal-plus 在不改变正确性的前提下，用两小时优化 p95 延迟。
$goal-plus mode=probe 先确认向量化是否可行。
$goal-plus mode=autonomous 深度优化这个 kernel。
```

编辑、恢复和独立最终检查使用各自宿主的相同前缀：

```text
Pi:    /goal-plus edit <完整的新目标>
Codex: $goal-plus edit <完整的新目标>
Pi:    /goal-plus resume
Codex: $goal-plus resume
Pi:    /goal-plus-with-final-check <目标>
Codex: $goal-plus-with-final-check <目标>
```

一次请求会启动自主运行。agent 自己判断 Goal Mode 是否足够，或冻结 verifier 后
并行 Search 是否更有价值；进入 Search 不需要额外确认。`mode=autonomous`（默认）
允许给每个初始 candidate worker 较长且可续的探索 lease；`mode=probe` 先做短时可行性
探测。该探索模式只会作为规范化说明写入 `raw_goal` 最后一行，不是新的调度状态。

## 宿主

| 宿主 | 集成资产 | 入口 | Search worker 路径 |
| --- | --- | --- | --- |
| Pi | 已安装 Pi package | `/goal-plus` 或 `pi -p "/goal-plus ..."` | 持久化 Pi RPC pool；参阅 [Pi](docs/pi.md) |
| Codex | 已安装 plugin | `$goal-plus` skill 提示 | 固定 parallel loops 与原生同 worker continuation；参阅 [Codex](docs/codex.md) |

安装 Codex plugin 后，从要修改的 workspace 启动：

```bash
cd <目标-workspace>
codex -m <main-model> -c model_reasoning_effort=max
```

从要修改的 workspace 启动 Pi：

```bash
cd <目标-workspace>
pi --model <provider/main-model> --thinking max
```

然后在 Codex 中提交 `$goal-plus <目标>`，或在 Pi 中提交 `/goal-plus <目标>`。
两个宿主的 main agent 都是已经运行的会话，应在启动时或通过 `/model` 预先选择；
Goal Plus 不能切换它。正常 prompt 省略 `main=`；两个入口都只把 `main=` 保留为可选的
一致性断言，不匹配时拒绝命令。[Torch CPU
示例](examples/model-optimize/README.md)提供了完整的 Codex/Pi 启动命令和
可直接粘贴的 prompt。

宿主差异和策略覆盖见 [Agent Host Adapters](docs/agent-host-adapters.md)。

## 心智模型

- 一条 **Goal Plus 记录**对应完整的用户任务。
- 一个 **search task** 是在一份 frozen spec 上运行的一个 `run_id`；一个目标可关联
  多个 search task。
- 初始 **SearchPlan** 只分配固定 candidate lane，不是逐轮提交 plan 的协议。
- 一个 **candidate** 是带 verifier 历史的长期并行 loop 工作区。
- 一个 **worker session** 是宿主上下文/来源句柄。worker 生命周期属于宿主，
  不属于 Search 运行时。
- **共享平面**保存冻结合同、verifier 精确 commit、Global Evidence、异步客观 View
  和 selection 状态，不暴露其他 candidate 的推理或 workspace。
- 一个 **verifier concern** 只是 worker 的建议；只有 main agent 能确认。确认后先封锁
  当前 run，再停止全部宿主 worker，并创建新的 spec/run。

Search 使用固定 parallel loops：初始 candidate 只创建一次；任意 worker 完成后，
main 只校验结果、刷新 verifier-backed best，并在全局停止条件未满足时续跑同一个
candidate。main 不选择后续技术方向，也不会按分数替换 candidate。完整流程见
[Shared Plane](docs/shared-plane.md)。

同一份有效评价/编辑合同应保持一个 run。必须新建 successor 时，
`source_run_id` 只继承有界 frontier、feature 和带作用域 pitfalls 作为研究上下文；
旧分数不能复用，必须重新验证。

运行时状态保存在 `.gp/`。`search_tasks` 只追加；`linked_search` 只是当前任务的
兼容视图。

配置 `promotion_verifiers` 后，Promotion 会执行独立检查，而不是复用缓存结果。
Runtime 会检出选中的 verifier-backed revision，以
`GOAL_PLUS_VERIFIER_PHASE=promotion` 重新运行每个 Promotion Gate，将证据绑定到
Selected Git Head 和 Artifact Hash，之后才生成可被 Git 应用的 Patch。Promotion
失败时保持 `ready_to_promote` 以便重试，并且不会生成 Patch。

## 文档

| 需求 | 文档 |
| --- | --- |
| 架构、共享 Evidence、回滚和端到端流程 | [Shared Plane](docs/shared-plane.md) |
| 当前 MCP 与 Pi 本地工具 | [API](docs/api.md) |
| 宿主能力对比 | [Agent Host Adapters](docs/agent-host-adapters.md) |
| Ultra 工作编排 | [Ultra Orchestration](docs/ultra.md) |
| 可选监控投影 | [Monitor Feature Plugins](docs/feature-plugins.md) |
| 运行时与宿主日志 | [Debugging](docs/debugging-runtime.md) |
| Checkout 开发、plugin 刷新与多开发者隔离 | [Development](docs/development.md) |
| spec 与可运行示例 | [Examples](examples/README.md) |
| 测试与真实宿主证据 | [Tests](tests/README.md) |

Benchmark 专用任务、evaluator、campaign 和对比证据不属于这个隔离的 runtime
目录。

## Workspace 与 Sandbox 架构

Goal Plus 使用两层 workspace 模型。**Git + Git** 由外层 Git-worktree workspace 和
内层 Git candidate 历史组成；**ThinkThread + Git** 在 private ThinkThread COW Child
内保留同一套 inner Git 历史，并增加 snapshot artifact 与 protected execution。两种模式
都要求 inner Git 从干净 baseline 启动，受管源码树中没有 untracked 产品文件。

Git + Git 已可用于 Codex/Pi。ThinkThread + Git 当前已有 filtered promotion adapter，
Child 创建、Search checkpoint、snapshot-bound verifier、reference 与 host execution 仍会
fail closed。Copy 保留为显式 artifact-only 兼容路径。

`new -> search -> apply` ASCII 主流程、设计不变量和 Git/TT 行为对照见
[Workspace 与 Sandbox 架构](docs/workspace-sandbox-arch.md)。
