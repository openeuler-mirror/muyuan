/goal-plus max_parallel=2 max_duration_seconds=480 workspace_provider=git_worktree promotion_mode=artifact_only strategy=agent_guided 优化只读 VLIW 模板，并把结果保存在 `.gp/`。
<!-- markdownlint-disable-file MD013 MD041 -->

问题：当前 `solution.py` 为自定义 VLIW/SIMD 机器生成指令程序。必须保持所有公开案例
正确，并尽可能降低 verifier 输出的 `cycles`。

修改：只修改候选工作区中的 `solution.py`。不要修改模拟器、runner、verifier、测试案例、
score adapter 或其他冻结输入，不要硬编码案例 seed，不要访问网络。

工作区合同：`workspace-template/` 是受版本控制的只读 baseline。把当前目录中的
`search-spec.json` 作为权威 SearchSpec，保持
`source_path="workspace-template"` 和 `promotion_mode="artifact_only"`。
不要创建临时 `workspace/`，不要把 publication 回写到
`workspace-template/`；候选、Evidence、publication 和报告只保存在 `.gp/`。

验收：以 `workspace-template/` 为工作目录运行
`python .goal-plus-verifiers/vliw_score.py`。所有公开案例必须正确，并最小化 `cycles`。
最终报告需要给出选中结果的周期数和相对 `147734` 周期 baseline 的改进量；结束前确认
`workspace-template/` 保持不变。

时间约束：整个任务最多 8 分钟。候选返回后，由 Main 根据已有 Evidence 判断是否继续。
时间预算只控制探索与收尾，不改变正确结果的验收标准。

模型选择：prompt 不为 main 或任何 subagent 指定模型；所有 subagent（candidate
worker、Evidence annotator 等）默认不指定模型，直接继承 main agent 当前使用的模型。

执行节奏：有方案后及时提交 verifier 验证，切勿长时间内部迭代而不提交方案。
