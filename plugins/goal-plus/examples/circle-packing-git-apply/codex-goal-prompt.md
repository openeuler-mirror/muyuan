$goal-plus max_parallel=2 workspace_backend=git_worktree promotion_mode=apply strategy=agent_guided 优化 `workspace/initial_program.py` 的 Circle Packing 解。

问题：当前程序需要在单位正方形内放置恰好 26 个互不重叠的圆，并尽可能增大
所有圆的半径之和。

修改：只修改 `workspace/initial_program.py`。不要修改 verifier 或其他任务输入，
不要访问网络。

验收：运行 `python workspace/.goal-plus-verifiers/circle_packing_score.py`。结果必须
满足 `valid=true`，圆的数量必须为 26，并最大化 `sum_radii`。

时间约束：任务预算为 5 分钟，第 4 分钟开始收尾；单个 worker 最长运行 3 分钟。
时间预算只控制探索与收尾，不改变有效结果的验收标准。
