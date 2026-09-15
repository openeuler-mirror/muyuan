/goal-plus mode=probe max_parallel=2 max_duration_seconds=1200 workspace_provider=thinkthread promotion_mode=apply strategy=agent_guided inner_agent=autoresearch 优化并应用当前工作区的 26 圆 Circle Packing 结果。

完整采用当前目录的 `search-spec.json` 作为权威 SearchSpec，包括 `strategy.config`、
`strategy.evidence_annotator` 和 `constraints`，并冻结
`.goal-plus-verifiers/circle_packing_score.py`。只修改 `initial_program.py`；保持 spec 中的
ThinkThread 工作区、AutoResearch inner agent 与 apply 模式，不得回退到其他工作区实现或
inner agent。

只运行一个 K=2 Search，并把两个确定路线作为各 lane 的起点：c001 从
`candidate-hints/rectangular.py` 开始；c002 从 `candidate-hints/staggered.py` 开始。每个 lane 的
首轮只完整复制对应实现，立即调用一次 `goal_plus_search_run_verifier` 并结束当前 turn；先取得起点的
正式 Evidence，不在首轮展开参数扫描或数值优化。后续再由同一个 retained candidate 根据
verifier feedback 自主继续；只要全局
停止条件未满足就 continuation 同一个 candidate。不要新增 plan、candidate 或 run。

等待冻结 SearchSpec 要求的 Evidence 就绪后，选择最高 `sum_radii` 的有效结果，完成 promotion
并应用回当前 Root。回写后重跑 verifier，必须 `valid=true` 且 `sum_radii >= 2.4`。记录 Search
result，完成原始目标审计并把 Goal Plus 标记 complete，最后生成规范报告。正常完成流程只依据
monitor 与完成卡，不检查 ThinkThread 内部状态文件。
