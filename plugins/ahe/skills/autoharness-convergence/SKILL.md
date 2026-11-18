---
name: autoharness-convergence
description: 分析 AutoHarness/AHE 运行历史，把 harness 优化导向有产出的收敛。当处理 AHE trace.jsonl、evidence.jsonl、report.json、debugger 产物、远端运行导出，或需要判断一次运行属于快速收敛、浪费、不稳定还是执行/记账异常时使用。
---

# AutoHarness 收敛分析

用本 skill 把历史 AHE 运行转化为有证据支撑的下一步动作。优化对象是
harness 循环（分发、工具、中间件、上下文、校验、debugger 与候选策略），
任务 evaluator 与任务相关的正确性检查保持受保护。

## 工作流

1. **定位运行数据。** 优先使用远端记录的本地副本。在记录主机（下文记为
   `<trace-host>`）上，常规来源是 trace 根目录（下文记为 `<trace-dir>`）；只复制需要的
   `trace.jsonl`、`evidence.jsonl`、`ahe-component-report.json`、
   debugger JSON 与运行元数据。除非需要检查源，不要复制凭证或可变
   workspace。

2. **先汇总再解读。** 运行：

   ```bash
   python scripts/summarize_ahe_runs.py --root ./traces --format text
   python scripts/summarize_ahe_runs.py --root ./traces --format json --output summary.json
   ```

   存在 `evidence.jsonl` 评测记录时以其为权威；否则回退到
   `trace.jsonl` 的评测事件。按候选与运行标识去重。

3. **对每次运行分类。** 用相对时间序列，不用绝对分数阈值：

   - **fast_converge（快速收敛）**：前 35% 的有效评测内（短运行至多三次）
     出现新 best，best 显著高于 baseline，零分/失败率低。
   - **productive_but_wasteful（有产出但浪费）**：出现新 best，但重复的
     baseline 分数、空提案候选或 best 之后的长尾消耗了大部分后续评测。
   - **unstable（不稳定）**：有改进，但失败、零分或大幅回退反复出现；
     保留 best 候选，先修方差再扩大搜索。
   - **no_progress（无进展）**：没有分数超过 baseline，或大多数评测重复
     baseline/零分。停止重复同一提案族，检查 evaluator 合同与搜索多样性。
   - **accounting_anomaly（记账异常）**：返回码成功但 `runner_ok` 为假、
     分数缺失/为零、或 runner、Pi、debugger 与 evaluator 的迭代计数
     不一致。优化时延前先插桩生命周期计数器。
   - **insufficient_data（数据不足）**：有效评测少于两次。不要称之为收敛。

4. **编辑前先定变更等级。** 阅读 `references/change-policy.md`，并在实验
   记录中登记 `A`、`B`、`C` 或 `F` 之一。绝不把 A/B 类变更悄悄拔高成对
   端到端 AHE 提升的结论。

5. **选择与诊断匹配的最小机制。**

   | 观测模式 | 首选干预 | 必须保持的不变量 |
   | --- | --- | --- |
   | 迭代成功但缓慢 | 插桩分发、队列等待、worker、verifier、gate、compaction 与下次分发的时间戳 | 每次校验仍然执行 |
   | 重复 baseline/空提案候选 | 增加候选新颖性、空提案检测、best-so-far 反馈与有界重试/分叉策略 | 不削弱 evaluator |
   | 重复的大型观测或 trace | Observation 打包或保证据压缩，附精确归档句柄 | 原始输出仍可召回且可哈希校验 |
   | 编辑后跟一次可预期的检查 | 把编辑与检查融合为一次工具操作 | 返回检查结果并保留 stderr/stdout |
   | 已完成历史占据上下文 | 只在安全边界压缩，并从持久进度自动续跑 | 未完成工作与证据在压缩后存活 |
   | 相同 prompt/工具前缀重复 | 稳定前缀，把动态状态追加在其后 | 缓存变更不得改变语义 |
   | 早期 best 良好随后回退 | 保留 best 候选、holdout 确认、测得平台期后停止 | 仅 dev 分数不能让候选晋级 |

   选择具体的 SoL-Pi 风格机制时参考 `agent-harness-efficiency` skill 的
   references。组合机制前先单独隔离验证。

6. **保持编辑面显式。** 用任务 adapter/manifest 决定可变文件。本 skill
   可以针对 harness 优化任务建议修改 prompt、中间件、memory、工具策略、
   分发或上下文处理；但不得扩大白名单。任何任务中只有 manifest 声明的
   可变组件可变。

7. **按顺序验证。** 遵循 `change-policy.md` 要求的等级：窄变更做静态/
   定向检查，行为变更做 dev 评测，晋级需 dev 加 holdout。只有主质量指标
   不降、独立正确性指标不回退、且目标资源指标改善时才接受。缺少证据或
   减少检查的更短运行不是效率收益。

8. **写紧凑的决策记录。** 包含运行 ID、baseline 与 best 序列、首次改进
   索引、零分/失败/重复率、疑似瓶颈、变更等级、所选机制、不变量、回退
   方案与确切的下一个实验。保留原始 stdout/stderr、trace、evidence、
   debugger 诊断与候选 patch 路径。

## 远端历史操作流程

在代理机上只使用只读命令。分析前先导出有界的一组产物：

```bash
ssh <trace-host> 'find <trace-dir> -name evidence.jsonl -o -name trace.jsonl'
scp <trace-host>:<trace-dir>/<run>/evidence.jsonl ./traces/<run>/evidence.jsonl
scp <trace-host>:<trace-dir>/<run>/trace.jsonl ./traces/<run>/trace.jsonl
```

debugger 文件只用于解释已分类的失败。debugger 诊断是证据，不是分数，
也不是编辑未声明文件的许可。在未检查输出的分数、`runner_ok`、通过率与
迭代/候选计数之前，绝不把进程退出码为零当作有产出的进展。

## 接受与安全规则

- 质量是门禁；仅 token、时延或模型调用节省永远不能获胜。
- dev 与 holdout 任务集保持分离。不要把分类器调到某个运行名或某个任务的
  分数尺度上。
- 保留原始产物与只追加的证据。打包、压缩、缓存、compaction 或动作融合
  失败时，回退到原始行为并记录失败。
- 要求唯一权威的迭代/候选计数器。宣称吞吐提升前先对齐 runner、Pi、
  evaluator 与 debugger 的数字。
- 计时噪声大时对比重复测量。汇报原始值与归一化值，例如每次有效评测的
  分数、每小时接受的候选数、每次接受改进的 token 数。
- 回放/分叉 trace 只用于诊断与开发；最终接受必须是真实运行并有 holdout
  支撑。
- 不接受靠删除校验、隐藏失败或修改任务 evaluator 换取速度的候选。

## 参考资源

- 用 `scripts/summarize_ahe_runs.py` 做确定性的运行分类。
- 阅读 `references/historical-findings.md` 了解历史运行主机上的观测及其
  局限。
- 提出变更前，把 `references/baseline-memory.json` 当作只读的历史记忆。
- 阅读 `references/change-policy.md` 判断变更需要静态检查、dev 运行还是
  dev 加 holdout。
- 阅读 `references/open-source-patterns.md` 了解从公开 harness/评测项目
  中提炼的可复用模式。
