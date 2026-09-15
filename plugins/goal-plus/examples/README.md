# Goal Plus 示例

本目录保存可直接运行、具有明确验收指标的用户示例，以及构造新任务的准备入口。
测试专用 SearchSpec 位于 `tests/fixtures/`，协议诊断脚本位于 `scripts/`。
从外部 PyPTO 算子生成实现可使用 [PyPTO Generate](pypto/generate/prompt.md)，
由 Main 在同一 Goal 中完成任务准备、实现和验收。

本目录维护展示 Goal Plus 能力的用户示例，以及各示例所需的冻结输入、来源和许可说明。
测试专用 SearchSpec 也可能位于 `tests/fixtures/` 或具体 ST 目录；它们不属于可粘贴的用户入口。

日常任务只需输入 Codex `$goal-plus <自然语言诉求>` 或 Pi `/goal-plus <自然语言诉求>`；
需要独立终审时使用 `$goal-plus-with-final-check` 或 `/goal-plus-with-final-check`。
下面的固定参数和 SearchSpec 用于复现示例，不是启动 Goal Plus 的前置配置。

## 从这里开始

第一次体验建议运行 [Circle Packing](circle-packing/README.md)。它只依赖 Python
标准库，可以通过 Pi `/goal-plus` 或 Codex `$goal-plus` 启动两个候选通道，并在十分钟
任务预算内完成公开 verifier、选择、artifact-only promotion 和最终报告。

如果只需要验证 Pi、Goal Plus 和 ThinkThread protected Root 的最小组合，使用
[ThinkThread Root 中的 Goal Plus](how-to-work-with-thinkthread/README.md)。这个场景只运行
一个由 main 负责的确定性 Goal Mode 任务，不创建 Search candidate 或 Git worktree。

只有需要验证源码回写时，才使用
[Circle Packing：Git 工作区回写](circle-packing-git-apply/README.md)。该示例会先生成
一个临时、独立、可写的 `workspace/` Git 仓库，然后把选中的结果回写到该仓库。

需要验证原生 Pi + ThinkThread 的 Search、评分、selection 和 Root 回写链路时，使用
[Guess Number：ThinkThread 回写](guess-number-thinkthread-apply/README.md)。该示例固定两个
公开候选，使模型质量不会掩盖编排或 publication 问题。

在 Guess Number 通过后，使用
[Circle Packing：ThinkThread Shared-Plane 回写](circle-packing-thinkthread-apply/README.md)
验证两个实际 packing 实现均产生有效分数，并把更好的结果回写到一次性 Root。内部
Child、Artifact View 和 cleanup 合同由真实 ThinkThread system test 覆盖，不写入可粘贴 prompt。

## 示例索引

| 示例 | 用途 | 入口 | 依赖 |
| --- | --- | --- | --- |
| [ThinkThread Root 中的 Goal Plus](how-to-work-with-thinkthread/README.md) | protected Pi Root 中的最小 Goal Mode，不启动 Search 或 Git | Pi，5 分钟 | ThinkThread Linux Runtime、Python 标准库 |
| [Circle Packing](circle-packing/README.md) | 最短的完整 Goal Plus 运行与报告演示 | Pi 或 Codex，10 分钟 | Python 标准库 |
| [Circle Packing：Git 工作区回写](circle-packing-git-apply/README.md) | 真实 Git 候选 worktree 和最佳结果回写 | Pi 或 Codex，5 分钟 | Python 标准库、Git |
| [Guess Number：ThinkThread 回写](guess-number-thinkthread-apply/README.md) | 两条固定路线、精确评分、结果回写和规范报告 | Pi，最多 10 分钟 | ThinkThread Linux Runtime、Python 标准库 |
| [Circle Packing：ThinkThread Shared-Plane 回写](circle-packing-thinkthread-apply/README.md) | 两个真实 packing candidate、公开 verifier 和 Root 回写 | Pi，最多 20 分钟 | ThinkThread Linux Runtime、Python 标准库 |
| [Final Check Skill Guard](final-check-skill-guard/README.md) | task-local Skill 打回、修复和重新验收 | Pi 或 Codex，最多 30 分钟 | Python 标准库、Git |
| [Model Optimize](model-optimize/README.md) | CPU/PyTorch 优化 | Pi 或 Codex，10 分钟 | PyTorch |
| [VLIW Kernel Optimization](vliw-kernel-optimization/README.md) | 带模拟器正确性门禁的程序与调度优化 | Pi，8 分钟 | Python 标准库 |
| [EdgeBench Ad Placement](edgebench-ad-placement/README.md) | 十个公开案例上的 C++ 编译与布局优化 | Pi，8 分钟 | C++17 编译器 |
| [AscendC Direct Search](ascendc-direct-search/README.md) | 动态生成 NPU 优化任务 | Pi，60 分钟 | 目标 Ascend NPU 和参考资料 |
| [PyPTO 生成与优化](pypto/README.md) | 通用任务准备，以及 CSA、MoE 两个性能优化 case | 准备 skill；优化使用 Pi、TT + Pi 或 Codex，配置见 prompt | PyPTO 工具链；性能优化另需 CANN 和两张可用卡 |

Circle、Guess Number、VLIW、EdgeBench 和 task-local Skill 示例具有中性 fixture/asset
验证入口。Model Optimize 与 AscendC 是专项参考示例，不维护 Torch、AscendC/CANNBench 的
自动测试或硬件验收；索引中的时长和依赖不表示当前机器已验证。来源、许可和历史报告仍跟随
其示例维护，不把它们当作当前产品合同。

## 工作区类型

Circle Packing、VLIW 和 Ad Placement 都采用只读模板模式：

```text
example/
|-- workspace-template/   # 受版本控制的只读输入
|-- search-spec.json      # 权威 SearchSpec
`-- .gp/                  # 运行时生成，保存候选、Evidence、publication 和报告
```

运行这些示例时，不要创建临时 `workspace/`，也不要把选中的 publication 回写到
`workspace-template/`。`workspace.promotion_mode` 必须是 `artifact_only`。Goal Plus
会在 `.gp/` 内部创建隔离的候选 worktree；这是运行时行为，不需要用户提前准备目录。

Git Apply 示例采用可写源码模式：

```text
circle-packing-git-apply/
|-- workspace/            # prepare_workspace.py 生成的独立 Git 源码仓库
`-- .gp/                  # 候选、Evidence 和报告
```

该模式使用 `promotion_mode=apply`，并在最终验收后把 provider publication 回写到
`workspace/`。不要把这套准备步骤复制到其他示例。

ThinkThread Apply 示例同样先生成一次性 `workspace/`，但它作为 protected Root 使用
`workspace.provider=thinkthread`。完成后把选中的结果应用回 Root；它不会创建 Git worktree，
也不能回退到其他工作区实现。

## 通用运行约定

1. 使用各示例 README 指定的启动目录。只读模板与 Git Apply 从示例目录启动；
   ThinkThread 的 `--fs` 指向生成的 workspace；Final Check Skill Guard 从准备脚本返回的目录启动。
2. 完整粘贴对应的 `pi-goal-prompt.md` 或 `codex-goal-prompt.md`。
3. 第一行的可选 `key=value` 参数约束并行度、工作区实现、发布模式、策略和
   inner-agent 工作流；Main 在准备 Search 时将这些约束写入冻结合同。
4. 固定 lane 场景中，Main 根据已验证结果判断是否继续同一候选和 native session。
5. 收尾时选择 verifier 验证过的最佳结果，完成 promotion 和 Goal Plus 审计，再生成一次
   最终报告。

## 调度与预算

可信 Main session 决定 agent 身份（Codex 或 Pi）；系统根据执行环境和工作区要求解析
`runtime_provider`，不需要用户指定。直接执行默认使用 AutoResearch，ThinkThread 默认使用
Common，显式 `inner_agent` 选择优先。`worker_budget` 可省略；显式设置时限制候选的一次执行，
不是整个 Goal 的时长。命令 `max_duration_seconds` 建立持久 Goal deadline；普通自然语言中的
分钟数是任务要求，不能替代该字段。运行时没有最低探索时长或强制 closeout 预留。
个别专项参考 prompt 中的额外时间分配只属于该示例，不是所有宿主的默认策略。
wait timeout 只限制一次等待，不取消候选，也不自动启动下一轮；verifier 超时由冻结 verifier 定义。

`parallel_loops` 的 `budget.max_parallel` 是初始固定 lane 数，workspace 随 lane 长期保留。
显式 `adaptive_search` 需要非空 `allocation` 和 Git worktree；此时该字段表示同时活跃的
candidate 上限，可选 `budget.max_candidates` 限制累计唯一候选数，`null` 不设置累计上限。
完整调度合同见 [Adaptive Search](../docs/adaptive-search.md)。

普通 prompt 使用当前 Host 已选择的模型和 Goal Plus 默认值，不固定 `main`、
`annotator` 或 `workers`。Circle Packing 的 Codex prompt 是唯一的多模型路由示例：两个
候选通道分别使用 Terra 和 Luna，并由 Terra 执行 Evidence annotation。

## 结果位置

只读模板示例的源码目录应保持不变。完成卡会给出 `run_id`、publication artifact、
`report.md` 和 `report.html` 的路径。报告位于当前 runtime 的 `runs/<run_id>/`；
本地默认 runtime 是示例目录的 `.gp/`，Pi TT 则位于 native session 私有目录。
使用返回的实际路径，不从 product workspace 猜测 TT runtime。HTML 报告可直接在浏览器中打开。

需要恢复或确认结果时，先使用只读 monitor：

```bash
goal-plus-pi-tool goal_plus_monitor_snapshot \
  --root .gp \
  --args-json '{"run_id":"run_..."}' \
  --pretty
```

如果最终报告缺失，并且关联的 Goal Plus 记录已经处于终态，可以再次调用
`goal_plus_search_report(run_id="run_...")`，不需要重新运行优化任务。

## 公开评测边界

这些示例中的 verifier 和案例都对 worker 可见，适合演示带反馈的优化流程，不代表隐藏
评测或官方排行榜成绩。隐藏答案不能通过 worker 可见的 verifier 暴露；需要隐藏评测时，
应在 Search 完成后由外层 benchmark harness 独立执行。
