/goal-plus max_parallel=2 workspace_provider=git_worktree promotion_mode=apply strategy=agent_guided inner_agent=autoresearch 优化 `workspace/initial_program.py` 的 Circle Packing 解。

问题：当前程序需要在单位正方形内放置恰好 26 个互不重叠的圆，并尽可能增大
所有圆的半径之和。

修改：只修改 `workspace/initial_program.py`。不要修改 verifier 或其他任务输入，
不要访问网络。实现只使用 Python 标准库，禁止 numpy、scipy 等第三方依赖
（执行环境未安装，verifier 会直接失败）。

工作区合同：`workspace/` 是本任务可写的独立源码 Git 仓库。把当前目录中的
`search-spec.json` 作为权威 SearchSpec，保持 `source_path="workspace"`；
不要把当前示例目录或其外层仓库当作 source。

Search 要求：这是强制进入 Search Mode 的任务。

验收：运行 `python workspace/.goal-plus-verifiers/circle_packing_score.py`。结果必须
满足 `valid=true`，圆的数量必须为 26，并最大化 `sum_radii`。
选中结果必须通过 provider publication 回写到 `workspace/`；回写后从该源码仓库
重新运行 verifier，并确认 Git worktree 干净。

执行节奏：有方案后及时提交 verifier 验证，切勿长时间内部迭代而不提交方案。
