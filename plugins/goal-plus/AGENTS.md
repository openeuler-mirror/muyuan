# Goal Plus 开发约定

本文件是 `plugins/goal-plus/` 下所有开发工作的入口，适用于整个目录，除非更深层的
`AGENTS.md` 明确覆盖。它说明应先读什么、代码和状态归谁负责、修改必须遵守哪些约束，
以及交付前需要什么证据；不重复完整产品规格或功能使用手册。

## 文档入口

先读 [SPEC.md](SPEC.md) 了解稳定系统合同，再按改动范围选择文档：

| 改动范围 | 文档 |
| --- | --- |
| 安装、快速开始和文档索引 | [README.md](README.md) |
| Search 架构、Evidence、selection 和 promotion | [共享平面](docs/shared-plane.md) |
| Adaptive Search 与 Search Scheduler | [Adaptive Search](docs/adaptive-search.md) |
| Main delegation 与轻量 dispatch 记录 | [Ultra 编排](docs/ultra.md) |
| MCP 与 Pi-local tool 合同 | [API](docs/api.md) |
| Codex/Pi 共同边界 | [Agent Host Adapters](docs/agent-host-adapters.md) |
| Codex 或 Pi 的安装与宿主行为 | [Codex](docs/codex.md)、[Pi](docs/pi.md) |
| Monitor feature plugin | [Feature Plugins](docs/feature-plugins.md) |
| 状态、日志和 report 排障 | [Runtime 调试](docs/debugging-runtime.md) |
| Checkout 开发与安装刷新 | [开发指南](docs/development.md) |
| SearchSpec 示例 | [Examples](examples/README.md) |
| 测试层次、marker 和真实 host ST | [Tests](tests/README.md) |

受版本控制的 model、行为测试和文档共同定义当前协议。旧日志、`.gp/` 产物和 transcript
只用于诊断，不作为协议来源。

## 架构边界

Goal Plus 包含 Goal Mode、内部 Search Mode 和 host 集成。Runtime 负责持久化领域状态；
Main Agent 负责理解目标、拆解工作、决定 delegation/Search 和审查结果；candidate 负责在
自己的合同和 workspace 内探索；host 负责真实进程或 native agent 的生命周期。

Search runtime 不是 worker supervisor。不要在 runtime 或 SearchTools 中增加 host wait loop、
heartbeat、进程 liveness、通用 abort API 或观察总线。`AgentSessionRecord` 是 context 与
provenance 绑定，不代表进程存活；launch payload 是 host 启动 worker 的权威输入。

Ultra 只记录实际发生的 dispatch 和 attempt identity，不要求预建 work DAG。Search 使用自己的
FrozenSpec、candidate、verifier 和 promotion 状态，不能映射成普通 subagent dispatch。

### 状态与代码 owner

规则应落在掌握可信输入或持久状态的最窄 owner 中：

| Owner | 职责 |
| --- | --- |
| `models.py` | 严格 schema、字段边界和跨字段结构不变量 |
| `goal_plus.py` | Goal 授权、revision、phase、dispatch attempt、Search link 和终态 |
| `runtime.py` | FrozenSpec、workspace、candidate、verifier、Evidence、selection、promotion 和 report |
| `adaptive_search/adaptive_search.py` | Adaptive settlement/barrier 的统一 facade 与模块协调 |
| `adaptive_search/scheduler.py` | ScheduleTask、scheduler context、异步调用和调用后端 |
| `adaptive_search/allocation.py` | AllocationDecision、retirement、派生 workspace/session 和执行校验 |
| `adaptive_search/policies.py` | 版本化 reward/allocation policy 与对应 context builder |
| `tools.py` | Codex/Pi 共用 facade 和 caller 前置条件 |
| `server.py`、`pi_tool.py` | MCP/Pi 参数转换、调用和序列化 |
| `agent_hosts.py`、Pi pool/worker | Host launch、budget、continuation、interrupt 和 host-local 状态 |
| Codex hook、Pi extension | 可信 command transport、session identity 和 host lifecycle 事实 |
| `monitor.py`、`reporting.py` | 持久状态的只读投影与最终静态报告 |
| Runtime 生成的 `candidate_task` | Candidate edit surface、环境和已启用能力的可见合同 |
| Skill/prompt | 用户意图、评价设计、技术方向等必须由模型判断的工作 |

`runtime.py` 仅根据 orchestration mode 调用 `AdaptiveSearch` facade，不重复实现其调度与分配
逻辑。Adaptive Search 的具体行为合同见 [Adaptive Search](docs/adaptive-search.md)。

## 实现原则

### 确定性规则由代码执行

先判断规则属于哪一类：

1. **可确定规则**：输入与持久状态足以判定。例如 caller role、Goal/run 归属、phase、edit
   surface、artifact、容量和 promotion 前态。必须由 model、facade 或状态 owner 执行。
2. **Host 事实**：只有宿主知道的 command transport、session identity、worker handle、deadline
   和进程状态。由 hook、extension、adapter 或 supervisor 采集和验证。
3. **模型判断**：需要理解用户目标、设计 evaluator、评价边际价值或选择探索方向。保留在
   skill/prompt，代码校验其结构和后续状态转换。

所有模型生成的结构化结果都是非可信输入。Pydantic model 负责结构边界，状态 owner 在持久化
或执行前校验 identity、归属、artifact、容量、当前状态和幂等前态。AGENTS、SPEC、README、
skill 和普通 prompt 可以解释规则，但不能成为确定性规则的唯一防线。

例如，skill 只需告诉 candidate 在实质修改后调用 `search_run_verifier`。Host adapter 负责提供
可信的 session identity；shared facade/runtime 校验该 session 的 run/candidate 归属、workspace
和 verifier 前态后才执行并记录 Evidence。不要把这些校验复述进 Codex、Pi 的 prompt，也不要
让 host adapter 各自实现一套。

### 保持状态边界

- Search bootstrap、Main 后续操作、candidate 操作和终态 report 分别使用其可验证的归属依据；
  run 完成链接后，Main 对该 run 的操作必须匹配当前 Goal 的持久化链接。
- 非法调用不得改变 Goal、run、candidate、Evidence、selection 或 promotion 等领域状态。
  审计事件、counter、lock 和临时目录属于操作性状态，是否允许变化由入口合同明确说明。
- 异步任务失败、重试或恢复只能更新它拥有的 task/worker/审计状态，不得撤销已经结算的
  verifier Evidence 或改写 hard score。
- 持久化状态是恢复依据，prompt 记忆不是。恢复逻辑必须幂等，不通过手工改写 `.gp/` 跨 gate。

### 保持隔离与最小可见性

- Candidate 只获得 runtime 投影的工作合同。不得泄露 Main context、peer workspace、credential、
  runtime-private constraints、hidden evaluation 或任意 host environment variable。
- Candidate 可以读取自己的 workspace 并本地诊断，但交付修改受 `allowed_files` 限制；冻结
  verifier 和 `denied_files` 保持只读。
- 可选能力关闭时，不生成对应工具说明或操作要求；启用时由 runtime 根据冻结配置动态投影。
- Codex 与 Pi 共用的业务规则在 shared facade 或领域 owner 中实现一次；host 层只处理自己的
  可信事实，不复制业务状态机。

### 保持实现精简

- 优先修改现有状态 owner。新生产抽象必须统一至少两处真实重复、封闭已有职责边界，或关闭
  测试复现的绕过；不要预建 policy DSL、通用快照框架、错误体系或测试 runner。
- 使用现有 Pydantic model 和结构化 API，不用 ad hoc JSON 或字符串解析代替已有合同。
- Skill/prompt 只保留模型判断与正常调用路径，不复制 schema、错误分支或已由代码拒绝的规则。
  删除仍承担真实功能入口的 task-specific workflow 前，必须先迁移并证明等价。
- 将已有 prompt 防御迁移到代码时，先用行为测试复现绕过并建立等价 gate，再删除对应文字
  约束；不保留旧路径兼容层。
- 不增加未要求的旧格式兼容、双写或 fallback。确需迁移时，明确旧数据边界、切换方式和删除
  条件。
- 不使用无来源的魔数。协议边界需要命名、依据和边界测试。
- Verifier 执行和 artifact 绑定必须确定、可复现，并由 runtime 掌握。

## 修改流程

1. 确认改动所属 owner，阅读对应规格、文档和现有测试。
2. 用最小行为测试明确预期，测试规模与改动风险匹配。
3. 在 owner 边界内完成最小实现，并同步受影响的 facade、host asset 或 projection。
4. 运行 focused test，再按影响范围运行 default、integration 或真实 host ST。
5. 系统不变量变化时更新 `SPEC.md`；用户可见合同变化时更新对应专项文档。
6. 检查最终 diff，排除无关修改、生成状态和 credential。

## Host 与资产同步

Host asset 是可执行产品界面，不是装饰性文档。

- Codex canonical skill 位于 `.codex/skills/`；发布副本位于 `skills/`。修改后运行：

  ```bash
  python scripts/sync_codex_plugin_assets.py
  python scripts/sync_codex_plugin_assets.py --check
  ```

- Codex 合同变化时检查 `.codex/skills/`、`.codex/agents/`、hook/config 模板及
  `tests/test_codex_assets.py`。
- Pi 合同变化时检查 `.pi/prompts/`、`.pi/skills/`、`.pi/extensions/`、worker/pool 及
  `tests/test_pi_assets.py`。
- 不要让任一 host asset 重新引用已删除的 runtime API。共同业务合同不得只在某一端修复。
- `install.sh` 是唯一受支持的源码安装入口。安装行为变化时同步 `scripts/install.py`、安装测试
  和 [开发指南](docs/development.md)；不要静默替换另一 checkout 或删除用户管理的数据。

Pi checkout-local 开发和 Codex 正常 plugin 刷新的具体步骤见
[开发指南](docs/development.md)。不要把账号、认证或全局 host 配置写进仓库。

## 测试与验收

测试应验证行为、返回结构和状态变化，不断言整段 prompt、中文措辞或内部调用顺序。可以断言
稳定 schema、窄的 reason token 和真正属于接口的文字。

- 共同业务规则在 shared facade/runtime 层测试一次。
- MCP、Pi 和 Codex 各保留薄的入口、序列化及 host-specific 测试。
- 状态 gate 同时覆盖合法前态、非法前态、领域状态不变和允许的操作性副作用。
- Worker context 或操作性 prompt 变化时验证结构化 projection，而不是复述文案。
- 修改 hook、Pi extension、worker context、skill/prompt 的真实操作合同时，运行对应真实 host ST；
  mock 不能替代 lifecycle 证据。

修改过程中运行相关 test node/file；每个可提交改动运行：

```bash
python -m pytest -q
git diff --check
```

阶段结束按影响范围运行：

```bash
python -m pytest -m integration -q
python -m pytest -m example -q
python -m pytest -m codex -q
python -m pytest -m pi -q
```

真实 host ST 的命令与前置条件见 [Tests](tests/README.md)。环境不具备时明确记录未验证行为，
不得把跳过描述成通过。默认测试使用 `pytest.ini` 的并行配置；覆盖并行度时不超过可用 CPU 的
一半和 16 workers，避免 Git lock contention。

Markdown 改动必须运行 `markdownlint-cli2` 并检查受影响的相对链接。

## 调试与本地状态

排查运行问题时：

1. 先用 `goal_plus_monitor_snapshot` 查看 Goal/Search 持久状态。
2. Monitor 未投影所需字段时，再读取 `.gp/runs/<run_id>/` 中的准确记录。
3. 只有分析 worker/host 行为时才查看 native transcript 或 raw log。
4. 使用 `goal_plus_id`、`run_id`、`candidate_id`、`agent_session_id` 和 host handle 关联记录。

Report 是终态持久数据的静态投影，不是 live UI。生成顺序和恢复方法见
[Runtime 调试](docs/debugging-runtime.md)。缺失的观测数据保持 unavailable，不从 transcript 猜测。

- 不提交 `.gp/`、`.tmp/`、`.goal-plus-dev/`、transcript、raw log、credential 或 cache。
- 不手工修改 FrozenSpec、candidate workspace 或既有运行状态来制造通过结果。
- 保留用户已有修改；并行实验使用不同目标目录或 Git worktree。
- Commit 保持单一目的，避免无关格式化和生成文件变动。
