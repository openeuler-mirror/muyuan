/goal-plus mode=probe max_parallel=2 max_duration_seconds=600 workspace_provider=thinkthread promotion_mode=apply strategy=agent_guided 完整跑通当前 ThinkThread Root 中的猜数 Search smoke test。

问题：公开目标数是 1。只修改 `guess.py`，让它输出恰好一个整数；运行
`python verify.py` 输出含 `guess_score` 的 JSON；分数为 `1-abs(1-输出值)`，越高越好。

工作区合同：读取当前目录的 `search-spec.json` 作为权威 SearchSpec，并冻结
`verify.py`。保持 spec 中的 ThinkThread 工作区与 apply 模式，不得回退到其他工作区实现。

为了只测编排而不测模型猜测，只运行一个 K=2 Search，并按顺序使用两个确定路线：c001 采用
zero 路线，把 `guess.py` 完整写成 `print(0)`；c002 采用 immediate-exact 路线，把它完整写成
`print(1)`。两个已知路线各评估一次即可；不要 continuation，也不要新增 plan、candidate 或 run。

验收：选择 `guess_score=1` 的 c002，执行 promotion 并应用回 Root。回写后重新运行
`python verify.py`，必须仍为 1，且 Root 的 `guess.py` 必须精确为 `print(1)` 加末尾换行。
记录 Search result，完成原始目标审计，把 Goal Plus 标记为 complete，最后生成规范报告。正常
完成流程只依据 monitor 与完成卡，不检查 ThinkThread 内部状态文件。

模型选择：prompt 不为 main 或任何 subagent 指定模型；所有 subagent（candidate
worker、Evidence annotator 等）默认不指定模型，直接继承 main agent 当前使用的模型。

执行节奏：有方案后及时提交 verifier 验证，切勿长时间内部迭代而不提交方案。
