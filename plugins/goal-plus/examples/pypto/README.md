# PyPTO 任务

生成任务与性能优化共用来源、环境和验收合同的准备约定，按目标选择入口：

| 入口 | 用途 |
| --- | --- |
| [Generate](generate/prompt.md) | Codex Goal Plus 生成任务，Main 准备后继续实现和 CPU 正确性验收；默认 MoE，不设总时限、3 并发 |
| [Optimize](optimize/README.md) | 在已有 PyPTO 实现上优化性能，提供 CSA、MoE 两个 case 及 Codex、Pi、TT prompt |

Main 使用的准备流程与脚本见 [Task Preparation Skill](task-preparation/SKILL.md)；
实现 worker 使用准备阶段生成的 task-local kernel skill。

CPU simulator 用于编译与正确性检查，真实设备用于性能验收。优化任务保留上游实现，
不执行空 kernel 提取步骤。任务源码、环境和运行产物放在仓库外，不写入本示例目录。