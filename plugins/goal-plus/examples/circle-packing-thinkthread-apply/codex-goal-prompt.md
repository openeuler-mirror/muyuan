请先调用 `goal_plus_host_start`，将下面从 `mode=probe` 开始的完整任务原样作为
`raw_goal`；启动后按 `goal_plus_status` 返回的当前状态和 `next_action` 推进。

mode=probe max_parallel=2 max_duration_seconds=1200 workspace_provider=thinkthread promotion_mode=apply strategy=agent_guided inner_agent=common 优化并应用当前工作区的 26 圆 Circle Packing 结果。

完整采用当前目录的 `search-spec.json` 作为权威 SearchSpec，包括
`strategy.global_evidence_mode`、`strategy.evidence_annotator` 和 `constraints`；只把
`strategy.inner_agent` 从 `autoresearch` 改为当前 Codex ThinkThread binding 支持的
`common`，其余字段保持不变，并冻结
`.goal-plus-verifiers/circle_packing_score.py`。只修改 `initial_program.py`；不得回退到其他
工作区实现、inner agent 或 promotion mode。

只运行一个 K=2 Search，并把两个确定路线作为各 lane 的起点：c001 从
`candidate-hints/rectangular.py` 开始；c002 从 `candidate-hints/staggered.py` 开始。每个 lane
在首轮完整复制对应实现，立即调用一次 `goal_plus_search_run_verifier` 并结束当前 turn。
两次 candidate delivery 足够，不需要继续改进，也不要新增 plan、candidate 或 run。

等待冻结 SearchSpec 要求的 Annotation Evidence 就绪后，选择 `sum_radii` 最高的有效精确
snapshot，完成独立 publication 验证、promotion 并应用回当前 Root。回写后重跑 verifier，
必须 `valid=true` 且 `sum_radii >= 2.4`。记录 Search result，完成原始目标审计并把 Goal Plus
标记 complete，关闭 Goal 的 execution scope，最后生成规范 MD/HTML 报告并确认 cleanup
complete。

执行节奏：有方案后及时提交 verifier 验证，切勿长时间内部迭代而不提交方案。
