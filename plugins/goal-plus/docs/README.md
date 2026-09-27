# Goal Plus 文档地图

本页按用途组织当前文档，列出详细合同的维护位置。
用户快速入口见 [README](../README.md) / [中文 README](../README_zh.md)，
开发约束见 [AGENTS](../AGENTS.md)，稳定系统合同见 [SPEC](../SPEC.md)。

## 按任务阅读

| 任务 | 入口 |
| --- | --- |
| 安装、启动和控制 Goal | [README](../README.md)、[Codex](codex.md)、[Pi](pi.md) |
| 更新安装、开发刷新、卸载与迁移 | [开发指南](development.md) |
| 运行示例 | [Examples](../examples/README.md) |
| 查询工具、字段、命令配置和返回结果 | [API](api.md) |
| 理解 Goal 状态、授权和恢复 | [运行逻辑](runtime-logic.md)、[Search 恢复](search-recovery.md) |
| 理解 Search 与 Evidence | [共享平面](shared-plane.md)、[Adaptive Search](adaptive-search.md) |
| 修改 native session、scope 或宿主接线 | [Agent Host Adapters](agent-host-adapters.md) |
| 修改 workspace、artifact、reference 或 publication | [Workspace 架构](workspace-sandbox-arch.md)、[多目录 Submodule 方案](submodule-workspace-design.md) |
| 理解 sandbox 设计、安装、代码架构与支持范围 | [Agent sandbox](sandbox/README.md) |
| 修改 Annotation 或 Artifact View | [Evidence Annotator](evidence-annotator.md) |
| 修改普通任务委派 | [Ultra](ultra.md) |
| 理解分层、状态 owner 与角色能力 | [架构图集](architecture.md)、[图集页面](architecture.html) |
| 排障、查看日志、检查上下文概要 | [Runtime 调试](debugging-runtime.md) |
| 使用可选监控功能 | [Feature Plugins](feature-plugins.md) |
| 了解 Pi 进度展示与终态卡 | [Pi](pi.md#progress-ui-实时展示) |
| 选择测试和真实宿主验收 | [Tests](../tests/README.md) |

## 代码评审阅读路径

先读 [SPEC](../SPEC.md) 的授权与状态不变量，再看[架构图集](architecture.md)的分层。
以下入口帮助沿业务链路检查实现；完整 owner 与依赖约束仍由 [AGENTS](../AGENTS.md) 维护。

| 评审关注点 | 代码入口 | 对应合同 |
| --- | --- | --- |
| Goal 身份、控制与 Search 归属 | [GoalAuthorization](../src/goal_plus/application/goal_authorization.py)、[Goal facade](../src/goal_plus/goal_plus.py) | [运行逻辑](runtime-logic.md) |
| Search 冻结、候选与硬结算 | [Runtime facade](../src/goal_plus/runtime.py)、[Search bootstrap](../src/goal_plus/application/search_bootstrap.py)、[Settlement](../src/goal_plus/application/candidate_settlement.py) | [共享平面](shared-plane.md) |
| 可选评分与最终选择 | [Verifier](../src/goal_plus/application/verifier.py)、[Selection](../src/goal_plus/application/selection.py) | [LLM Verifier](llm-verifier.md) |
| 自适应分配与引导 | [Adaptive 入口](../src/goal_plus/adaptive/search.py)、[版本策略](../src/goal_plus/domain/adaptive/policy.py) | [Adaptive Search](adaptive-search.md) |
| 宿主会话与执行身份 | [Session host](../src/goal_plus/session_host.py)、[Session control](../src/goal_plus/execution/session_control.py) | [宿主适配](agent-host-adapters.md) |
| 精确产物发布与应用 | [Promotion](../src/goal_plus/application/promotion.py)、[Publication apply](../src/goal_plus/application/publication_apply.py) | [Workspace 架构](workspace-sandbox-arch.md) |

验证时先按[开发指南](development.md#测试环境)准备 checkout 本地环境，再选择
[测试层次](../tests/README.md#test-layers)。默认回归、真实宿主验收和 benchmark 效果报告
是不同证据：测试数量不能替代宿主 lifecycle 验证，功能可用也不代表已证明性能收益。

## 内容归属

以下归属不额外定义产品状态或执行权限。每个详细合同有明确维护位置，
其他文档保留适合其受众的摘要、例子和链接；不要求同一事实只能出现一次。

| 内容 | 详细维护位置 | 其他位置保留什么 |
| --- | --- | --- |
| 系统不变量、授权边界、对象与责任 | [SPEC](../SPEC.md) | 操作说明和相关合同的简要解释 |
| 文件 owner、依赖规则、开发与验收约定 | [AGENTS](../AGENTS.md) | 专项文档指向 owner，避免复制整张表 |
| 安装、更新和卸载的完整步骤 | [开发指南](development.md) | README 最短可用命令；宿主页保留信任、Profile 等特有步骤 |
| Goal 控制、状态与恢复语义 | [运行逻辑](runtime-logic.md) | 宿主页保留具体命令和原生 session 身份差异 |
| Search 业务流程、Evidence、selection 主线 | [共享平面](shared-plane.md) | README 心智模型；API 的调用顺序和返回合同 |
| 公共 session 操作、scope 与生命周期机制 | [Agent Host Adapters](agent-host-adapters.md) | API 保留工具合同；Codex/Pi 页保留协议及接线差异 |
| Provider、immutable artifact、reference、publication | [Workspace 架构](workspace-sandbox-arch.md) | Shared Plane 保留流程摘要；API 保留参数与产物身份 |
| Settlement、LLM 评分、task/record 与确定性 Selection | [LLM Verifier](llm-verifier.md) | Shared Plane 保留搜索收尾主线；Adaptive Search 只说明 Record 消费接口 |
| Snapshot、Allocation、Steering 与 Host 接线 | [Adaptive Search](adaptive-search.md) | LLM Verifier 定义评分生产；API 列工具和返回状态 |
| Annotation queue、View、supplemental evaluation | [Evidence Annotator](evidence-annotator.md) | API 保留字段及可见性；Shared Plane 保留它对搜索的作用 |
| Candidate checkpoint recovery | [Search 恢复](search-recovery.md) | Runtime Logic 链接；与 TT controller 接管明确区分 |
| 普通 delegation 的 attempt/rework/验收 | [Ultra](ultra.md) | API 保留工具合同；宿主页保留实际执行器行为 |
| 参数、schema、生成的命令配置表 | [代码与生成器](../scripts/generate_command_config_docs.py)、[API](api.md) | 用户页给例子，不手抄整份 schema；生成区不手改 |
| 日志位置、运行时布局、诊断操作 | [Runtime 调试](debugging-runtime.md) | 专项文档保留必要状态语义并链接排障入口 |
| Monitor 开关与视图 | [Feature Plugins](feature-plugins.md)、[API](api.md) | 前者描述使用，后者描述字段；调试文档说明如何排查 |
| 测试 marker、执行命令和证据边界 | [Tests](../tests/README.md) | 场景说明靠近 fixture；通过记录绑定实际 build |

## 模型可见资产

模型可见资产属于产品界面，不等同于用户手册。当前不发布 Goal Plus 导航 skill。

| 入口 | 内容 |
| --- | --- |
| [公共 MCP 概要](../assets/goal-plus-instructions.txt) | Main 的共同工作导航 |
| [MCP server](../src/goal_plus/server.py) | 工具 schema、description 和宿主可信角色接线 |
| [Candidate context](../src/goal_plus/application/candidate_context.py) | 已授权 session 的运行时 context |
| [Codex 角色资产](../assets/codex/agents/) | Codex candidate 原生配置 |
| [Pi 角色资产](../assets/pi/prompts/) | Pi candidate prompt |
| [Pi extension](../assets/pi/extensions/goal-plus.ts) | 宿主事件、命令和共同 MCP 加载 |

任务仓库自带的 `.agents/skills/` 与示例中的 task-local Skill 仍可能是合法冻结输入，
不因产品导航 skill 删除而一并删除。修改模型可见合同须遵守现有行为测试及宿主验收约定。

## 测试输入与语言

[ST prompt 合同](../tests/st/prompts/README.md)说明当前持久状态验收；
[Signal fixture](../tests/fixtures/signal_processing/README.md)说明固定输入、评分与无模型运行方法。
`examples/*/PROVENANCE.md` 等来源与许可信息跟随对应资产维护。

建议沿用现有语言：README 中英对齐，开发文档主要中文，测试文档和原生 prompt 保持原语言。
对齐支持范围、命令和默认行为，不要求翻译后逐字或逐段对应。
