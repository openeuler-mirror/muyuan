/goal-plus max_parallel=2 max_duration_seconds=300 workspace_provider=git_worktree promotion_mode=apply strategy=agent_guided inner_agent=autoresearch 优化 `workspace/initial_program.py` 的 Circle Packing 解。

问题：当前程序需要在单位正方形内放置恰好 26 个互不重叠的圆，并尽可能增大
所有圆的半径之和。

修改：只修改 `workspace/initial_program.py`。不要修改 verifier 或其他任务输入，
不要访问网络。

工作区合同：`workspace/` 是本任务可写的独立源码 Git 仓库。以当前目录中的
`search-spec.json` 为权威 SearchSpec 基础，保持 `source_path="workspace"`；
不要把当前示例目录或其外层仓库当作 source。

极简 LLM-Verifier 模式：保持 `strategy.orchestration_mode="parallel_loops"`，
并在冻结 SearchSpec 时仅增加以下 Selection 配置：

```yaml
strategy:
  selection:
    ranking_keys: [quality_score, hard_score]
    ranking_tolerance: [0.05, 0.0]
```

`quality_score` 的消费会由 runtime 自动启用 LLM-Verifier。不要添加独立的
verifier 开关、`allocation` 或 `adaptive_search`。

验收：运行 `python workspace/.goal-plus-verifiers/circle_packing_score.py`。结果必须
满足 `valid=true`，圆的数量必须为 26，并最大化 `sum_radii`。
选中结果必须通过 provider publication 回写到 `workspace/`；回写后从该源码仓库
重新运行 verifier，并确认 Git worktree 干净。

时间约束：任务预算为 5 分钟，第 4 分钟开始收尾；单个 worker 最长运行 3 分钟。
时间预算只控制探索与收尾，不改变有效结果的验收标准。
