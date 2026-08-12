# <Benchmark 名称> 实验报告

## 1. 实验概览

| 项目 | 内容 |
|---|---|
| 实验名称 | `<experiment_name>` |
| Benchmark | `<benchmark_name>` |
| Case 集合 | `<case_set>` |
| Case 数量 | `<case_count>` |
| 被测对象 | `<agent_or_plugin>` |
| 模型 | `<model>` |
| 代码版本 | `<commit_or_version>` |
| 运行时间 | `<started_at>` |

## 2. 实验配置

- 运行环境：`<environment>`
- 并发数：`<concurrency>`
- 超时：`<timeout>`
- 预算：`<budget>`
- 其他配置：`<configuration>`

## 3. 结果摘要

| 配置 | 有效 Case | 主指标 | 总耗时 | Token/成本 | 备注 |
|---|---:|---:|---:|---:|---|
| Baseline |  |  |  |  |  |
| Candidate |  |  |  |  |  |

## 4. 分项结果

按任务类型、难度、仓库、算子类型或漏洞类型展示分组结果。

## 5. 失败分析

区分：

- 任务失败；
- 模型或 Agent 失败；
- 工具或执行环境失败；
- 判分失败；
- 超时或预算耗尽。

## 6. 成本与效率

记录墙钟时间、模型调用、Token、工具调用和可获得的资源使用数据。未采集的数据保持为空，不填写为 `0`。

## 7. 结论

说明结果是否优于基线、主要收益、主要代价和证据边界。

## 8. 产物

- 实验配置：`<config_path>`
- 原始结果：`<result_path>`
- 日志：`<log_path>`
- 其他产物：`<artifact_path>`
