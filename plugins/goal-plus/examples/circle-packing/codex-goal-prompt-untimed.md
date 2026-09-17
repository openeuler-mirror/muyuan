$goal-plus max_parallel=2 workspace_provider=git_worktree promotion_mode=artifact_only strategy=agent_guided inner_agent=autoresearch annotator=gpt-5.6-terra workers=gpt-5.6-terra,gpt-5.6-luna 优化只读 Circle Packing 模板，并把结果保存在 `.gp/`。
<!-- markdownlint-disable-file MD013 MD041 -->

问题：当前程序需要在单位正方形内放置恰好 26 个互不重叠的圆，并尽可能增大
所有圆的半径之和。

修改：只修改候选工作区中的 `initial_program.py`。不要修改 verifier 或其他冻结输入，
不要访问网络。

工作区合同：`workspace-template/` 是受版本控制的只读 baseline。把当前目录中的
`search-spec.json` 作为权威 SearchSpec，保持
`source_path="workspace-template"`、`promotion_mode="artifact_only"` 和
`inner_agent="autoresearch"`。不要创建临时 `workspace/`，不要把
publication 回写到 `workspace-template/`；候选、Evidence、publication 和报告只保存在
`.gp/`。

验收：运行
`python workspace-template/.goal-plus-verifiers/circle_packing_score.py`。结果必须满足
`valid=true`，圆的数量必须为 26，并最大化 `sum_radii`。两个候选通道都至少产出一个
verifier 已结算结果后结束探索，并确认 `workspace-template/` 保持不变。

执行节奏：有方案后及时提交 verifier 验证，切勿长时间内部迭代而不提交方案。
