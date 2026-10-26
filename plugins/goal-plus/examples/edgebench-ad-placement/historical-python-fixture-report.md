# EdgeBench-Lite Pi Goal Plus 历史运行报告

> 仅作为历史证据：这次运行使用的是已经退役的 Python 形态 fixture。当前示例提交
> `solution.cpp`，并遵循 EdgeBench 文本和 CLI 合同，因此这里的分数与 artifact 文件名
> 不能作为当前验证结果。

本报告记录了旧版广告布局本地示例的一次持续 Pi Goal Plus/Search 运行。它证明的是
Goal Plus 工作流，不是 EdgeBench 官方隐藏评测成绩。这里的 `combined_score` 不能与
EdgeBench 排行榜直接比较：旧示例使用八个公开合成案例和未缩放的 0 到 100 平均分，
EdgeBench 则使用官方 work/judge container、隐藏案例、运行限制和分数缩放。

## 运行摘要

- 日期：2026-07-10
- Host：Pi RPC
- 模型：`openai-codex/gpt-5.6-sol`，thinking `low`
- Run：`run_20260710_053759_0c27d006`
- 用时：3656.88 秒，即 60.95 分钟
- Search 形态：8 个串行 batch，每个 batch 并发 2 个候选
- 候选数量：16
- 公开 verifier 调用：107 次
- 可编辑范围：仅 `initial_program.py`
- 公开 workload：8 个确定性案例，共 1488 个广告
- Worker 预算：480 秒，30 turn prompt hint，超限后 interrupt
- CPU 约束：通过标准 BLAS/OpenMP/Torch 环境设置限制为单线程

这次运行完成了当时定义的完整流程：

```text
/goal-plus -> frozen spec -> search -> candidate workspaces -> Pi workers
  -> verifier-backed Git iterations -> selection -> report -> promotion
  -> final Goal Plus audit
```

Goal Plus 状态到达 `complete`，Search run 状态到达 `promoted`。

## Search 结果

| Batch | 候选 | Batch 结束后的最佳分数 |
| ---: | --- | ---: |
| 1 | c001, c002 | 98.55661918035690 |
| 2 | c003, c004 | 98.60556683402933 |
| 3 | c005, c006 | 98.62881399236852 |
| 4 | c007, c008 | 98.69973782437563 |
| 5 | c009, c010 | 98.71807195497317 |
| 6 | c011, c012 | 98.87049624594145 |
| 7 | c013, c014 | 98.93116731402698 |
| 8 | c015, c016 | 99.07634533278639 |

主要改进 lineage 为：

```text
c002 -> c003 -> c005 -> c008 -> c010 -> c012 -> c013 -> c015
```

候选工作区只使用一个具体代码 parent。额外的 `parent_candidate_ids` 只提供历史信息或
灵感，runtime 不会递归合并候选工作区。

## 最终选择

Worker verifier 历史是 Search Evidence 和排序信号，不是最终权威。
`search_select` 会 checkout 已排序的 committed iteration，并由 main agent 再次运行
verifier；第一个通过最终 verifier 的 iteration 才会成为选中结果。

`c015` 的记录：

- 历史最佳：iteration 4，分数 `99.07634533278639`
- 历史最佳 Git head：`1fbc0d7242844b0e9bbbe0dddf9a45cbb29c1e52`
- iteration 4 最终复验：超过 30 秒 verifier 限制
- 选中的 fallback：iteration 7
- Main-agent verifier 选中分数：`99.0763453320608`
- 选中 Git head：`09634f84d2d28981b47bc0d97918045543c8a058`

两个分数相差约 `7.3e-10`。选中的 iteration 使用稍小的本地搜索范围，并在 28.9 秒内
完成最终验证。这符合 selection 语义：即使 worker 测量存在波动，main-agent 最终验证
仍是权威结果。

## Runtime Evidence

- 并发始终限制为两个 worker。
- 每个 batch 完成后才派发下一个 batch。
- Runner failure：0
- Redispatch：0
- Hard watchdog timeout：1 次，即 `c016`
- 发送 soft closeout：`c004`、`c008`、`c015`、`c016`
- `c016` 在超时前已经生成四个 verifier-backed iteration，因此不需要重新派发。
- 一个候选从多次无效 verifier 尝试中恢复，并最终产生该 batch 的最佳结果。
- 所有候选修改都位于 `initial_program.py`。
- Promotion patch 通过了针对原始 workspace 的 `git apply --check`。

Monitor 记录的 Pi 使用量：

- Input token：741,357
- Output token：76,233
- Cache-read token：9,638,912
- 估算成本：`$10.813231`
- 最大 worker context：55,519 token，即 14.92%

紧凑事件日志共 2,437,487 字节。没有保留原始文本日志或持久 Pi session transcript。
完整本地 run 目录约占 8.3 MB。

## 这份历史记录能证明什么

这次运行证明旧版 Pi GP 路径可以维持一小时、多 batch、有界并发的优化，并完成
Git-backed inheritance、反复公开验证、超时恢复、最终复验、报告生成和 promotion。

它不能衡量官方 EdgeBench 质量，没有使用等价 benchmark 数据，也不能建立可与排行榜
比较的分数。案例和 process verifier 对 worker 公开；正式隐藏 judge 必须位于 Goal Plus
之外，并由 benchmark harness 管理。本报告只能作为旧版 Goal Plus 工作流的参考证据。
