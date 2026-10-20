/goal-plus spawn_mode=fresh max_parallel=2 max_duration_seconds=480 workspace_provider=git_worktree promotion_mode=artifact_only strategy=agent_guided 优化只读 Ad Placement 模板，并把结果保存在 `.gp/`。
<!-- markdownlint-disable-file MD013 MD041 -->

问题：当前 `solution.cpp` 需要为十个公开确定性案例生成合法、互不重叠的广告矩形，并
尽可能提高 verifier 输出的 `local_score_sum`。

修改：只修改候选工作区中的 `solution.cpp`。不要修改 generator、tester、verifier、
任务输入或其他冻结资产，不要访问网络。

工作区合同：`workspace-template/` 是受版本控制的只读 baseline。把当前目录中的
`search-spec.json` 作为权威 SearchSpec，保持
`source_path="workspace-template"` 和 `promotion_mode="artifact_only"`。
不要创建临时 `workspace/`，不要把 publication 回写到
`workspace-template/`；候选、Evidence、publication 和报告只保存在 `.gp/`。

验收：以 `workspace-template/` 为工作目录运行
`python .goal-plus-verifiers/ad_local_score.py`。`solution.cpp` 必须通过 C++17 编译，
十个公开案例都必须输出合法矩形，并最大化 `local_score_sum`。结束前确认
`workspace-template/` 保持不变。

时间约束：整个任务最多 8 分钟。候选返回后，由 Main 根据已有 Evidence 判断是否继续。
时间预算只控制探索与收尾，不改变有效结果的验收标准。

模型选择：prompt 不为 main 或任何 subagent 指定模型；所有 subagent（candidate
worker、Evidence annotator 等）默认不指定模型，直接继承 main agent 当前使用的模型。

执行节奏：有方案后及时提交 verifier 验证，切勿长时间内部迭代而不提交方案。
