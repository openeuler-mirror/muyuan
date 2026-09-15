# Goal Plus (GP)

[English](README.md) | 简体中文

Goal Plus 是面向长时间 agent 任务的宿主中立运行时。在 Pi 中使用
`/goal-plus`，在 Codex 中使用 `$goal-plus`。它可以直接处理普通目标；遇到可度量的
优化任务时，会升级到 Search Mode：冻结评价合同、在隔离候选中探索，并提升经过
verifier 验证的最佳结果。

Pi 和 Codex 是当前支持的宿主路径。

## 安装

从 Goal Plus 源码目录运行安装器：

```bash
git clone git@gitcode.com:yiyanzhi_akane1/muyuan.git && cd muyuan/plugins/goal-plus
```

| 安装方式 | 命令 |
| --- | --- |
| 仅 Codex | `./install.sh --codex` |
| 仅 Pi | `./install.sh --pi` |
| Pi + ThinkThread Search | `./install.sh --pi --thinkthread` |
| Pi 开发态 | `./install.sh --pi-dev && source .goal-plus-dev/set_env.sh` |
| 查看 Pi 开发环境及空间占用 | `./install.sh --env-show` |

正常安装通过 uv 准备独立 Python 3.12 和锁定依赖，各宿主绑定不可变代码 build，
不受 Conda、`PATH` 或目标 workspace 影响。`--pi` 与 `--pi-dev` 互斥。
用 `./install.sh --runtime-info` 检查实际版本；更新及清理方式见[开发指南](docs/development.md)。
已有 Codex/Pi 注册时会询问是否卸载重装（`y/N`），自动化可加 `--yes`。
产品资产位于 `assets/codex/`、`assets/pi/`，编辑源码不会自动加载该插件的运行时资源。
`--dry-run` 不修改宿主注册或 release；shell bootstrap 可能先下载运行检查所需的 uv/Python。

`--thinkthread` 要求机器上已经独立安装受支持的 ThinkThread Runtime。它会验证内置 Pi Profile
并生成 `goal-plus-pi` Profile，但绝不安装、升级或删除 ThinkThread 本身。使用
`tt run --profile goal-plus-pi --fs <目标-workspace>` 启动 protected Root。
在 Root 内使用 `workspace_provider=thinkthread`，即可在独立 private Child 中运行 Pi Search、
验证精确 snapshot，并通过乐观 `fs.apply` 发布结果。Codex 尚不支持 ThinkThread worker host。

Codex 首次安装后，新建线程，在 `/hooks` 中检查并信任 Goal Plus，然后再新建一个
线程。Pi 开发态通过 `pi-goal-plus` 启动，不修改 Pi 认证或全局配置。

## 卸载

| 卸载内容 | 命令 |
| --- | --- |
| 可移除的全部 Goal Plus 组件 | `./uninstall.sh` |
| Codex 注册 | `./uninstall.sh --codex` |
| Pi 注册 | `./uninstall.sh --pi` |
| Pi 开发环境 | `./uninstall.sh --pi-dev` |
| Goal Plus ThinkThread Profile | `./uninstall.sh --thinkthread` |
| 未引用的受管版本 | `./uninstall.sh --runtime` |
| 检查完整卸载计划 | `./uninstall.sh --dry-run` |

指定选择器时，卸载器只移除对应组件。Codex 和普通 Pi 注册不要求来自当前 checkout；来源
不同时打印警告并直接卸载，不询问确认。`--dry-run` 只展示计划，本地开发目录仍校验归属。
正常宿主安装使用共享的 Python runtime，因此只卸载一个宿主时会保留 runtime；如果仍有
未选择的宿主注册依赖它，runtime 卸载会拒绝执行。Pi 开发态使用独立的受管环境。
卸载始终不会删除目标 workspace 中的
`.gp/` 记录，也不会回退已经提升的修改。`--thinkthread` 只删除当前 checkout 生成的
`goal-plus-pi` Profile，绝不删除 ThinkThread Runtime、Root 或 state。

## 启动目标

在 Pi 中启动目标：

```text
/goal-plus 修复这个 bug 并验证测试套件。
/goal-plus 在不改变正确性的前提下，用两小时优化 p95 延迟。
/goal-plus mode=probe 先确认向量化是否可行。
/goal-plus mode=autonomous 深度优化这个 kernel。
/goal-plus max_parallel=2 promotion_mode=artifact_only 优化这个只读模板。
```

Codex 使用精确宿主命令：

```text
$goal-plus 修复这个 bug 并验证测试套件。
$goal-plus 在不改变正确性的前提下，用两小时优化 p95 延迟。
$goal-plus mode=probe 先确认向量化是否可行。
$goal-plus mode=autonomous 深度优化这个 kernel。
$goal-plus max_parallel=2 promotion_mode=artifact_only 优化这个只读模板。
```

开头连续的 `key=value` 是强类型命令配置；第一个非配置 token 开始就是自然语言目标。
完整 key、取值范围和作用见 [Host Command Config](docs/api.md#host-command-config)。

编辑和独立最终检查使用各自宿主的相同前缀：

```text
Pi:    /goal-plus edit <完整的新目标>
Codex: $goal-plus edit <完整的新目标>
Pi:    /goal-plus-with-final-check <目标>
Codex: $goal-plus-with-final-check <目标>
```

原生会话恢复不启动任务。已中断 Main 使用
`codex exec resume <session-id> '$goal-plus resume'` 或
`pi -p --mode json --session <session-id> '/goal-plus resume'` 显式恢复。
运行中收到新思考会先处理再继续；中断后的普通聊天不解锁。两端支持摘要、pause/resume/clear，
clear 永久撤权但保留历史。完整合同与验证边界见[运行逻辑](docs/runtime-logic.md)。
原生恢复不会创建或修改目标；修改仍使用 `$goal-plus edit` 或 `/goal-plus edit`。
新的原生 session 使用精确 start 命令时会创建独立 Goal，即使同一 workspace 的其他 session
仍有未终态 Goal；edit、resume 和其他控制命令始终只作用于本 session 绑定的 Goal。

上述精确宿主命令是创建/编辑目标的唯一授权入口。自然语言请求、skill 自动加载、直接 MCP
调用和通用 Pi CLI facade 都不能创建或编辑 Goal。两端从共同 MCP instructions 加载流程概要，
不安装 Goal Plus 导航 skill。工具授权见 [API](docs/api.md)。

一次请求会启动自主运行。agent 自己判断 Goal Mode 是否足够，或冻结 verifier 后
并行 Search 是否更有价值；进入 Search 不需要额外确认。`mode=autonomous`（默认）
指导持续探索；`mode=probe` 优先安排有界可行性检查。候选返回后，Main 根据持久 Evidence
和用户明确给定的上限判断是否继续，继续时复用候选和 session。
没有最低 worker 时长或强制提前收尾窗口；wait 超时不代表 Goal 完成，也不自动启动下一轮。

## 宿主

| 宿主 | 集成资产 | 入口 | Search worker 路径 |
| --- | --- | --- | --- |
| Pi | 已安装 Pi package | `/goal-plus` 或 `pi -p "/goal-plus ..."` | Pi RPC 上的共同 session 工具；参阅 [Pi](docs/pi.md) |
| Codex | 已安装 plugin | `$goal-plus` 宿主命令 | Codex app-server 上的共同 session 工具；参阅 [Codex](docs/codex.md) |

安装 Codex plugin 后，从要修改的 workspace 启动：

```bash
cd <目标-workspace>
codex -m <main-model> -c model_reasoning_effort=max
```

从要修改的 workspace 启动 Pi：

```bash
cd <目标-workspace>
pi --model <provider/main-model> --thinking xhigh
```

然后在 Codex 中提交 `$goal-plus <目标>`，或在 Pi 中提交 `/goal-plus <目标>`。
Pi 保留已选 thinking；复杂任务可选 `xhigh`，它不是强制值。
两个宿主的 main agent 都是已经运行的会话，应在启动时或通过 `/model` 预先选择；
Goal Plus 不能切换它，`main=` 也不是受支持的命令配置。
[Circle Packing 示例](examples/circle-packing/README.md)提供了完整的 Codex/Pi 启动命令和
可直接粘贴的 prompt。

宿主差异和策略覆盖见 [Agent Host Adapters](docs/agent-host-adapters.md)。

下面按原生 Main Agent 类型列出当前支持的 Search 路径。ThinkThread 提供工作区和
Child 执行基础设施；当前受支持的 ThinkThread 路径内运行的 Agent 仍是 Pi。

| Main Agent | 工作区后端 | Candidate 驱动 | Annotation 驱动 | LLM Verifier / Adaptive Allocation |
| --- | --- | --- | --- | --- |
| Codex | `git_worktree` / `copy` | Codex app-server | Codex CLI | Codex CLI；仅 Git worktree 支持 |
| Pi | `git_worktree` / `copy` | Pi RPC | Pi CLI | Pi CLI；仅 Git worktree 支持 |
| Pi | `thinkthread` | TT Pi Child | TT Pi Child | 当前不支持 |
| Codex | `thinkthread` | 当前不支持 | 当前不支持 | 当前不支持 |

`workspace.provider` 表示工作区实现。Agent 身份来自已授权 Main 会话，记录为
`FrozenSpec.agent_harness`；装配层独立固定 `runtime_provider=direct|thinkthread`。
这些字段不增加用户配置：仍可直接使用 `$goal-plus <诉求>` 或 `/goal-plus <诉求>`，
Main 仅在需要时准备 Search 合同，信息不足时继续原有澄清流程。
LLM Verifier 同次生成质量及可选 allocation 评分；选择和分配由确定性策略执行。

此次协议不迁移旧记录。切换前使用旧 build 关闭活动资源并收尾旧记录，新 build 创建新记录；
安装和 `.gp` 目录布局保持不变。

## 心智模型

- 一条 **Goal Plus 记录**对应完整的用户任务。
- 一个 **search task** 是在一份 frozen spec 上运行的一个 `run_id`；一个目标可关联
  多个 search task。
- 初始 **SearchPlan** 只分配固定 candidate lane，不是逐轮提交 plan 的协议。
- 一个 **candidate** 是带 verifier 历史的持久 lane；`strategy.inner_agent` 选择多轮
  `autoresearch`，或每次派发完成一个 attempt 的 `common`。
- 一个 **worker session** 是宿主上下文/来源句柄。worker 生命周期属于宿主，
  不属于 Search 运行时。
- **共享平面**保存冻结合同、verifier 精确 commit、Global Evidence、异步客观 View
  和 selection 状态，不暴露其他 candidate 的推理或 workspace。
- 一个 **verifier concern** 只是 worker 的建议；只有 main agent 能确认。确认后先封锁
  当前 run，再停止全部宿主 worker，并创建新的 spec/run。

默认 `parallel_loops` 使用固定 candidate lane。Main 审查每次返回并决定是否继续同一候选，
后续技术方向由 candidate 选择，分数不触发替换。Git worktree 可显式选择
[Adaptive Search](docs/adaptive-search.md)，由 Scheduler 提出候选替换。
共同流程与 Evidence 合同见 [Shared Plane](docs/shared-plane.md)。

Git worktree 模式在 freeze 时先直接在 clean origin 中运行一次 process-verifier suite，
并固定该 commit。batch 启动时先在 `c001` 中重复同一验证，通过后才创建其余 lane 或启动
worker。这样 source root 与相对路径错误会在消耗候选 Search 预算前暴露；bootstrap 验证
不是 candidate iteration。

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
| 完整索引与内容归属 | [文档地图](docs/README.md) |
| 稳定的产品与 runtime 合同 | [系统规格](SPEC.md) |
| 宿主命令与配置 | [Codex](docs/codex.md)、[Pi](docs/pi.md)、[API](docs/api.md) |
| 安装、更新、卸载与 checkout 开发 | [开发指南](docs/development.md) |
| 运行时与宿主日志 | [调试指南](docs/debugging-runtime.md) |
| spec 与可运行示例 | [Examples](examples/README.md) |
| 测试与真实宿主证据 | [Tests](tests/README.md) |

Benchmark 专用任务、evaluator、campaign 和对比证据不属于这个隔离的 runtime
目录。

## Workspace 与 Sandbox 架构

Goal Plus 将工作区隔离与 inner-agent 行为分开。Git worktree 产生 commit Evidence，
Pi ThinkThread Child 产生 snapshot Evidence。`autoresearch` 维护候选本地 Git 历史，
`common` 依赖 outer artifact 与 canonical runtime ledger。Copy 是显式 artifact-only 路径。
Git worktree 不是安全沙箱；支持组合和 provider 保证由下方的工作区专题定义。

`new -> search -> apply` ASCII 主流程、设计不变量和 Git/TT 行为对照见
[ThinkThread 与 Git Workspace 架构](docs/workspace-sandbox-arch.md)。
