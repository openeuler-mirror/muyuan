# Prompt And Skill Rebase Audit, 2026-09-01

## 结论

这次 rebase 后的主要问题不是“prompt 变短”本身，而是部分缩减删除了无法从 typed
command config 或 runtime 自动恢复的任务事实。最严重的已确认问题有三类：

1. Codex Stop hook 会从任意 hook 文本中递归提取 `gp_*`，从而把一个未绑定的 Pi
   Goal Plus 记录错误绑定到当前 Codex Stop。
2. Circle Packing Git Apply prompt 删除了 `workspace/` 是源码 Git 根和
   `search-spec.json` 是权威合同等信息，恰好触发仓库已有测试所描述的错误外层 Git
   root 路径。
3. 示例中的自然语言 whole-goal 时间预算没有进入 Pi host 的强制 deadline 路径；目前
   extension 只把部分时间表述解析给完成卡展示。

当前 source checkout 已经包含 session ownership、relocation gate、typed config、固定候选
lane 和 provider-native promotion 等正确机制，但这些机制之间仍有以下断点。不能用现有
asset test 全绿来判定端到端行为正确。

## 审计范围

- rebase 基线：`origin/master` 的 `3e1af47`
- 当前 HEAD：`4f3557d`
- 分支相对基线：27 个提交
- 逻辑资产：3 个 Codex skill、1 个 Pi skill、1 个 Pi compatibility prompt、13 个示例
  prompt，以及相关 README、runtime 和测试
- `.codex/skills/` 与发布用 `skills/` 的三个 skill 当前逐字节一致
- `agents/openai.yaml` 均设置 `allow_implicit_invocation: false`
- 安装流程由另一工作项处理；本审计不修改 `install.sh`、`scripts/install.py`、安装测试或
  安装文档
- 当前 worktree 中 Pi thinking 行为已有未提交修订；本审计保留并单独标注，不把它误记为
  HEAD 已有行为

状态含义：

- **confirmed**：代码路径或可复现实验直接证明
- **coverage gap**：实现可能正确，但测试不能证明用户入口正确
- **design gap**：当前会 fail closed，但失败发生得太晚或错误模型过于宽松
- **environment**：来自已安装宿主状态，不归本轮 source 修复直接所有

## 缺陷登记

### GP-AUD-001: Stop hook 可被任意文本中的 Goal ID 劫持

- 严重级别：P0
- 状态：confirmed
- 所有者：Codex hook/runtime

[goal_plus_stop_hook.py](../src/goal_plus/goal_plus_stop_hook.py) 中 `_first_goal_id()` 会递归
扫描 hook input 的所有字符串，`_explicit_goal_ids()` 再把扫描结果视为显式 Goal ID。
`_select_hook_goal_id()` 又让这个值优先于 session ownership。

因此，一个没有绑定当前 Codex session 的 active Pi 记录，只要其 ID 出现在
`last_assistant_message` 等任意 Stop payload 文本中，就会被 Codex Stop gate 接管。本次审计
用一个未绑定记录和以下无关 Stop payload 复现了阻塞：

```json
{
  "hook_event_name": "Stop",
  "session_id": "unrelated-codex",
  "last_assistant_message": "Why is gp_0001 involved?"
}
```

这也解释了现场现象：`gp_0002` 是 Pi 在共享 `.gp` 中创建的 active 记录；讨论这个 ID 后，
Codex Stop hook 错误输出了该 Pi 目标的 continuation prompt。`gp_0002` 这个编号本身没有异常。

要求的修复：

1. 顶层 Stop 只能使用当前 attached Codex session 的 Goal。
2. 仅接受受信任环境传入的 `GOAL_PLUS_ID`，或明确列入 schema 的顶层结构化字段。
3. 不得从 assistant text、transcript、error、tool result 或任意递归字符串中推断 Goal ID。
4. 增加“未绑定 active Pi Goal + assistant 文本提及该 ID”的跨宿主回归测试。

### GP-AUD-002: Git Apply prompt 删除了不可由 typed config 恢复的 source 合同

- 严重级别：P0
- 状态：confirmed
- 引入提交：`5457426`
- 所有者：示例 prompt、Pi/Codex Main spec discovery、示例测试

`5457426` 把四个
[Circle Packing Git Apply prompt](../examples/circle-packing-git-apply/pi-goal-prompt.md)
从完整合同缩减成问题、修改、验收和简短时间说明。被删除的信息包括：

- `workspace/` 是可写源码 Git 仓库；
- `search-spec.json` 是权威任务合同；
- `source_path` 必须保持为 `workspace`；
- apply 成功后才记录 Search 结果；
- 最终应从源码仓库重新验证并确认 clean。

其中 workflow 顺序可以由 skill 负责，但 source identity 和权威 spec 是任务事实。
[GoalPlusCommandConfig](../src/goal_plus/models.py) 只有 backend、promotion、strategy、并行度和
模型路由字段，没有 `source_path`，所以 typed config 无法恢复这些事实。

当前 [test_examples.py](../tests/test_examples.py) 反而禁止 prompt 正文出现
`search_apply_promotion`、`search_promote` 和 canonical report 等词，却没有断言 source Git root
或权威 spec。直接 runtime 测试
[test_circle_packing_git_apply.py](../tests/test_circle_packing_git_apply.py) 又在模型之外把
`source_path` 改成正确的临时内层仓库，因此绕过了实际失败点。

要求的修复：恢复最小但完整的任务事实，不需要恢复逐工具微操；新增从真实 prompt 经过 Main
spec discovery 到 apply 的一命令 Pi smoke。

### GP-AUD-003: whole-goal 时间预算没有进入 Pi host 强制层

- 严重级别：P0
- 状态：confirmed
- 所有者：Pi extension、host-local deadline、example config

Pi pool 和 verifier 已支持 `GOAL_PLUS_OUTER_DEADLINE_AT`：pool 会按 outer deadline 和
closeout reserve 缩短 worker lease，并阻止错误时机的 close。问题在入口：普通
`/goal-plus` 没有把用户给出的整个任务时间转换为 host-local deadline。

[goal-plus.ts](../.pi/extensions/goal-plus.ts) 中 `wholeGoalPresetDurationSeconds()` 只在构造完成卡
时调用。它不会设置 `GOAL_PLUS_OUTER_DEADLINE_AT`，也不会建立等价的宿主状态。当前测试均由
harness 手工设置该环境变量，只证明“有 deadline 时能执行”，没有证明普通 slash command 会
创建 deadline。

因此 skill 中的“外层剩余时间”对普通示例只是模型语义，不是 host enforcement。现场 timed
Circle 运行在预算后仍启动 worker，与代码路径一致。

要求的修复：把 whole-goal 预算变成 host command 的结构化参数或 host-local deadline 状态，
以 Goal 创建时间为起点传给 pool、verifier 和 closeout；不要依赖模型自行看钟或完成卡 regex。

### GP-AUD-004: Pi 终态报告存在两位 owner，会重复 render

- 严重级别：P1
- 状态：confirmed
- 所有者：Pi skill、Pi extension finalizer

[Pi skill](../.pi/skills/goal-plus/SKILL.md) 要求 Main 在 Goal terminal 后对每个 run 调用且只调用
一次 `search_report`。随后 extension 的 `agent_end` 又调用
`pi_goal_plus_finalize_reports`。该 finalizer 先记录 Main usage，再对所有已预留
`report_path` 的 task 再次调用 `FileSearchRuntime.report()`。

这会产生至少两次 render，而且第一次报告尚未包含 finalizer 刚记录的 Main usage。当前测试只
断言两个路径都存在，没有断言单一 owner 或调用次数。

要求的修复：Pi 交互路径由 extension finalizer 在记录 Main usage 后唯一生成报告；skill 不再让
模型预先生成。Codex 和显式恢复场景仍可保留 runtime 的 on-demand `search_report`。

### GP-AUD-005: AscendC 新 workspace 与强制 Git backend 合同不完整

- 严重级别：P1
- 状态：confirmed contract gap
- 所有者：AscendC Spec Discovery 文档和 smoke

[AscendC prompt](../examples/ascendc-direct-search/pi-goal-prompt.md) 显式要求
`workspace_backend=git_worktree promotion_mode=apply`。
[SPEC_DISCOVERY.md](../examples/ascendc-direct-search/SPEC_DISCOVERY.md) 要求创建 fresh
source-owned workspace 并生成全部输入，但没有要求把该目录初始化成独立 Git 仓库、提交冻结
输入并确认 clean HEAD。

结果只能是 freeze 拒绝非 Git source，或 Main 把生成目录错误绑定到某个外层 Git 仓库。

要求的修复：在 Search freeze 前显式初始化 standalone Git source、提交生成的任务和 verifier、
确认 clean HEAD，并增加不需要真实 NPU 的 workspace-materialization contract test。

### GP-AUD-006: Git Apply 中文时间表述无法被完成卡 parser 识别

- 严重级别：P1
- 状态：confirmed
- 引入提交：`5457426`
- 所有者：Pi completion presentation

Git Apply prompt 使用“任务预算为 5 分钟”。当前 parser 只识别“预设时间为”或“5 分钟内”等
形式，所以这个示例即使正常完成，完成卡也不会显示预设时长。现有测试只检查 prompt 包含该
中文短语，没有执行 parser。

要求的修复：优先随 GP-AUD-003 使用结构化 host 预算；若仍保留兼容 parser，至少为所有受支持
示例逐一增加 table-driven 测试。

### GP-AUD-007: 测试对用户入口形成了错误的全绿信号

- 严重级别：P1
- 状态：coverage gap
- 所有者：example、Pi ST、asset tests

当前 focused gate 为 `49 passed, 8 skipped`，但它没有覆盖以下关键路径：

- prompt 到正确 `source_path` 的 Main 决策；
- 正常 `/goal-plus` 到 host deadline 的传播；
- Pi terminal 后报告只 render 一次；
- Git Apply 的 provider apply 和 source verifier 通过同一个真实 Pi command 完成；
- 缩减 prompt 与 checked-in `search-spec.json` 的任务事实等价性。

本次 rebase 后的 VLIW live run 也不能作为成功证据：两条 lane 都在零修改、零 candidate
verifier iteration 的情况下达到有效 lease 上限，Goal 最终是 `abandoned`。它证明入口和 pool
能启动，但没有证明优化、selection 或 promotion 正常。

要求的修复：保留快速 asset tests，但增加 prompt-driven spec contract tests 和至少一个受控的
Pi one-command Git Apply ST。真实运行必须分别报告 materialization、worker Evidence、selection、
apply、terminal 和 report，不能把“进程启动”当成通过。

### GP-AUD-008: 文档仍有三处执行合同歧义

- 严重级别：P2
- 状态：confirmed documentation drift
- 所有者：Pi skill、examples README

1. Pi skill 前文说新 process/session 只能显式 `/goal-plus resume` 重新绑定，后文又说“下一
   session/轮次从原生状态恢复”，容易被理解为新 session 自动授权。
2. [examples/README.md](../examples/README.md) 声称每个 paste-ready scenario 都有有限 whole-goal
   limit，但 Circle 两个 host 都提供明确的 untimed paste-ready prompt。
3. Host Flow 写成 `search_select` 后“record/promote”，与 runtime 要求的
   `select -> promote -> apply -> record result -> terminal -> report` 顺序不一致。

要求的修复：分别写清 same-session turn continuation 与 cross-session explicit resume；把 untimed
variant 标为例外；统一 promotion/result/report 顺序。

### GP-AUD-009: 当前 relocation gate 正确 fail closed，但可以更早拒绝错误 source

- 严重级别：P2
- 状态：design gap
- 所有者：freeze validation

现有 runtime 测试明确覆盖“外层仓库忽略内层独立 Git 仓库”的情况：origin verifier 可以在 live
filesystem 成功，第一次 materialized `c001` 缺少该文件，于是 run abort。这是正确的最终保护，
不能删除 relocation gate。

但 freeze 已宣称 verifier artifact 是 source-owned、materialized path，却没有在 Git artifact
commit 中检查该文件可达。因而错误 spec 会先持久化，再创建 run 和 `c001` 后才失败。

建议在 freeze 增加 commit-materializability 检查，先确认 verifier artifact 在
`source_repository + source_subdir` 对应的冻结 commit 中可读；relocation gate 继续负责验证 cwd、
环境和执行行为。

### GP-AUD-010: Pi thinking 修订已覆盖 start，但 durable resume provenance 仍需决定

- 严重级别：P2
- 状态：partially fixed in worktree
- 所有者：Pi Ultra binding

当前未提交修订已经移除 `pi.setThinkingLevel("xhigh")`，把 Main policy 记录为
`main_reasoning_effort="host_selected"`，并让普通 Goal work-item child 使用 Main 当前档位。
这符合“Main 是什么就是什么，只推荐 xhigh”的要求。

仍需明确两点：

1. 新 Pi session 用 `/goal-plus resume` 时，record 中的 execution policy 是否应刷新为新 session
   的实际 thinking；当前 edit/resume 不更新该字段。
2. `piHostInvocation.session_id` 当前是 `pi:<cwd>`，不是原生 session identity，无法仅凭持久证据
   区分同 cwd 的不同 Pi session。

`gpt-5.6-sol` 的默认 medium 属于 Pi host 配置/安装面，不应再次写死在 Goal Plus prompt、skill
或 Search runtime 中。

### GP-AUD-011: 已退役的 `goal-plus-local` 安装态仍出现在现场 hook 证据中

- 严重级别：P1 environment
- 状态：安装工作项处理中
- 所有者：安装/迁移

现场 Codex hook path 来自 `goal-plus@goal-plus-local`，而当前仓库合同已经明确退役该 staging
marketplace。当前 cache 目录已被清理为空，但 Codex config 中仍保留旧 hook trusted-state 条目。

这不是本审计要修改的安装文件，但修复验收必须确认：

- 只有 `goal-plus@goal-plus` 被注册和加载；
- 旧 checkout path 和 `goal-plus-local` 不再提供 active hook；
- 新会话的 hook path、console script 和 Python environment 指向同一安装；
- 旧 trusted-state 残留不会重新激活已删除插件。

## 资产逐项结论

| 资产 | 结论 | 说明 |
| --- | --- | --- |
| Codex `goal-plus` skill | conditional pass | 主流程、typed config、apply/report 顺序正确；需补 source materializability 指导 |
| Codex `search` skill | pass with design gap | fixed lanes、真实 spawn/bind、continuation 和 relocation 合同一致 |
| Codex final-check skill | pass | host-only startup、stateless checker 和 atomic complete 一致 |
| Pi compatibility prompt | conditional pass | startup authority 和 Search 合同正确；whole-goal deadline 仍非 host-enforced |
| Pi `goal-plus` skill | conditional pass | pool 生命周期和 apply 顺序正确；report owner、session wording、deadline 有缺陷 |
| Circle Packing read-only | pass at contract level | source path、authoritative spec、artifact-only 和 closeout 均明确 |
| Circle Packing Git Apply | fail | prompt 删除 source/spec 任务事实，测试绕过 Main 决策 |
| EdgeBench | pass at contract level | read-only source、metric、edit surface 和 artifact-only 明确 |
| Model Optimize | pass at contract level | source、edit/deny surface、CPU constraint 和 artifact-only 明确 |
| VLIW | contract pass, live inconclusive | prompt 完整；最新 live run 无 candidate Evidence，不能验收效果正常 |
| AscendC Direct Search | fail at materialization contract | fresh generated workspace 没有独立 Git 初始化/clean commit 步骤 |
| Codex published skill copies | pass | `.codex/skills` 与 `skills` 当前逐字节一致 |
| implicit invocation metadata | pass | 三个 skill 都禁止 implicit invocation |
| retired Search lifecycle APIs | pass | host assets未重新引入 submit/wait/abort/host-sync runtime API |

## 修复顺序

1. **P0-1**：修复 Stop hook 的任意文本 ID 绑定，并添加跨宿主回归测试。
2. **P0-2**：恢复 Git Apply 的 source/spec 事实，删除当前会强化错误缩减的测试断言，增加
   prompt-driven spec test。
3. **P0-3**：设计并实现 Pi host-owned whole-goal deadline；让 timed examples 使用可强制的结构化
   值。
4. **P1-1**：统一 Pi report owner，保证 usage 落盘后只 render 一次。
5. **P1-2**：补 AscendC standalone Git materialization 合同和非 NPU smoke。
6. **P1-3**：增加 Circle Git Apply one-command Pi ST，再重新运行 VLIW effect smoke。
7. **P2**：收紧 freeze materializability、session provenance 和文档歧义。

在前三项完成前，不应再用 Circle Git Apply 或 timed VLIW 作为“rebase 后行为正常”的发布证据。
