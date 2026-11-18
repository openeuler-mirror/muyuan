# 开源 Harness 模式

这些模式是设计参考，不是可以盲目照抄的代码。AHE evaluator 与任务合同
保持在可变候选面之外。

## SWE-agent

[SWE-agent 的 agent 循环](https://github.com/SWE-agent/SWE-agent/blob/main/sweagent/agent/agents.py)
把模型交互、动作解析、工具处理、历史处理、重试、超时与运行统计分开。
复用这种分离：把 AutoHarness 的效率变更放在一个稳定的挂钩上，让每次
重试/超时在 trace 中可见，而不是折叠进笼统的成功。

## OpenHands

OpenHands 描述了一种事件流架构：动作发给 runtime，观测返回。
[OpenHands 论文](https://arxiv.org/abs/2407.16741)是稳定的公开参考。
对 AHE 而言，把每次分发、工具动作、观测、校验与续跑都建模为带关联 ID
的事件，使排队时间与工具时间可以分开测量。

## Inspect AI

Inspect AI 把数据集、solver 与 scorer 组合成任务并记录评测日志。见
[Tasks](https://inspect.aisi.org.uk/tasks)、
[Scorers](https://inspect.aisi.org.uk/scorers.html) 与
[框架概览](https://inspect.aisi.org.uk/)。直接迁移这种分离：AHE 候选可以
改 harness/solver 策略，而任务集与 scorer 保持固定；日志应包含逐样本
证据与聚合指标。

Inspect 还提供显式的样本上限、提前停止、失败处理与日志导出。把这些思想
用于有界的 AHE 实验，但绝不用提前停止去掩盖 evaluator 或校验失败。

## Agentless 与分阶段工作流

[Agentless 论文](https://arxiv.org/abs/2407.01489)采用定位、生成、校验
的分阶段工作流，而不是一个无约束的大循环。对 AutoHarness 而言，把诊断、
提案、应用、校验、评测与晋级保持为独立阶段。这样可以在不改变正确性门禁
的前提下替换浪费的阶段。

## AHE 专属综合

- 保持只追加的事件/证据日志与稳定的候选 ID。
- 区分运行时完成与任务成功；退出码为零不是分数。
- 让排队、模型、工具、verifier、gate、compaction 与晋级计时成为一等
  字段。
- 任何打包或压缩视图都同时保存原始观测。
- 使用 best-so-far 保留、holdout 确认与显式的平台期/空提案策略。
- 当历史显示执行或记账异常时，优先选择窄而可测的机制变更，而不是大范围
  重写 prompt。
